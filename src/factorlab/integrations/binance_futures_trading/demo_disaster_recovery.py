"""GET-only reconstruction of Binance Demo evidence on a quarantined restore."""

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from itertools import groupby
import json
from pathlib import Path
from time import perf_counter

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.market import _decimal
from factorlab.trading.contracts import ZERO
from .account import AccountPositionConflict
from .adapter import _to_ms, funding_event_id
from .demo_cli import open_session
from .demo_cloud_runtime import durable_state
from .demo_cloud_storage import _QUARANTINE
from .demo_file_credentials import DemoFileCredentialStore
from .demo_runtime import DemoRuntime, operator_lock
from .observability import RequestCounts
from .protocol import SafetyBlocked
from .recovery import (
    MAX_QUERY_WINDOW_MS, UNFILLED_CANCEL_RETENTION_SECONDS, account_baseline,
    baseline_deltas, canonical_trade, canonical_tran_id, fetch_user_trades_paginated,
    latest_receipt_for_order, load_state, save_state,
)


_ANCHOR = "disaster_recovery_anchor"
_EVIDENCE = "disaster_recovery_evidence"
_ORDER_LIMIT = 1000
_HISTORY_PAGE_BUDGET = 64
_MAX_ACCOUNT_ATTEMPTS = 3
# The documented three calendar months can be shorter than 90 days.
_TRADE_RETENTION_MS = 84 * 24 * 3600 * 1000


def _anchor(adapter, since, *, restore_id):
    anchor = load_state(adapter.db, _ANCHOR)
    if anchor is not None and anchor.get("restore_id") == restore_id:
        if anchor["snapshot_since"] != since.isoformat():
            raise SafetyBlocked("cannot change the recovery watermark of an existing restore")
        return anchor
    row = adapter.db.execute(
        "SELECT payload,updated_at FROM futures_recovery_state WHERE scope='account_baseline'"
    ).fetchone()
    if row is None:
        raise SafetyBlocked("the backup has no reconciled Binance wallet or position baseline")
    payload, updated = row
    baseline = json.loads(payload)
    observed_at = baseline.get("observed_at", updated)
    anchor = dict(snapshot_since=since.isoformat(), restore_id=restore_id, baseline=baseline,
                  observed_at=observed_at)
    with adapter.store.transaction():
        save_state(adapter.db, _ANCHOR, anchor, adapter.clock().isoformat())
    return anchor


def _orders(adapter, symbol, start_ms, end_ms, *, pages):
    """Bisect a full page rather than treating the latest page as complete history."""
    windows = [(start_ms, end_ms)]
    result = {}
    while windows:
        start, end = windows.pop()
        if end - start >= MAX_QUERY_WINDOW_MS:
            split = start + MAX_QUERY_WINDOW_MS - 1
            windows.extend(((split + 1, end), (start, split)))
            continue
        if pages[0] >= _HISTORY_PAGE_BUDGET:
            raise DataQualityError("Binance disaster recovery order history exceeds the page budget")
        rows = adapter.client.request("GET", "/fapi/v1/allOrders", dict(
            symbol=symbol, startTime=start, endTime=end, limit=_ORDER_LIMIT))
        pages[0] += 1
        if not isinstance(rows, list) or len(rows) > _ORDER_LIMIT:
            raise DataQualityError("invalid Binance allOrders page")
        if len(rows) == _ORDER_LIMIT:
            if start == end:
                raise DataQualityError("Binance allOrders saturated a single millisecond")
            middle = (start + end) // 2
            windows.extend(((middle + 1, end), (start, middle)))
            continue
        for row in rows:
            key = row["orderId"]
            if (row["symbol"] != symbol or not start <= row["time"] <= end
                    or key in result or not row["clientOrderId"]):
                raise DataQualityError("invalid or duplicate Binance order history identity")
            result[key] = row
    return result.values()


def _trade_value(row):
    return (canonical_trade(row), row["time"], row["side"], row["positionSide"],
            _decimal(row["realizedPnl"]))


def _recorded_trades(adapter, client_id):
    receipt = latest_receipt_for_order(adapter.db, client_id)
    if receipt is None:
        return {}
    return {row["id"]: _trade_value(row) for row in receipt[1]["trades"]}


def _refresh_archived_orders(adapter, trades):
    by_order = defaultdict(list)
    for row in trades:
        by_order[row["symbol"], str(row["orderId"])].append(row)
    refreshed = False
    for (symbol, order_id), rows in by_order.items():
        archived = adapter.db.execute(
            "SELECT client_order_id FROM futures_order_recovery "
            "WHERE symbol=? AND exchange_order_id=? AND archived=1", (symbol, order_id),
        ).fetchone()
        if archived is not None:
            client_id, = archived
            booked = _recorded_trades(adapter, client_id)
            if any(booked.get(row["id"]) != _trade_value(row) for row in rows):
                adapter.query_order(client_id)
                refreshed = True
    return refreshed


def _window_residual(events, observed, window, update_ms=None):
    """Find an exact economic boundary using an independently sampled field."""
    start, end = window
    if update_ms is not None:
        if type(update_ms) is not int or not 0 <= update_ms <= end:
            return None, False
    after = sum((amount for at, amount in events if at > end), ZERO)
    candidates = [after]
    overlap = sorted(((at, amount) for at, amount in events if start <= at <= end), reverse=True)
    for _, group in groupby(overlap, key=lambda event: event[0]):
        change = sum((amount for _, amount in group), ZERO)
        if change:
            candidates.append(candidates[-1] + change)
    if candidates.count(observed) == 1:
        return ZERO, True
    return observed - after, False


def _economic_evidence(baseline, current, position_events, cash_events):
    windows = baseline.get("read_windows")
    if not windows:
        return dict(unproven_baseline=True, position_residual={}, cash_residual=None)
    position_deltas, wallet_delta = baseline_deltas(baseline, current)
    wallet_residual, wallet_proved = _window_residual(
        cash_events, wallet_delta, windows["account"], baseline["wallet_update_ms"])
    position_residual = {}
    unproved_positions = set()
    for endpoint, updates in zip(("account", "position_risk"), baseline["position_updates"], strict=True):
        last_updates = {symbol: at for symbol, side, at in updates if side == "BOTH"}
        for symbol in position_deltas.keys() | position_events.keys() | last_updates.keys():
            residual, proved = _window_residual(
                position_events.get(symbol, ()), position_deltas.get(symbol, ZERO),
                windows[endpoint], last_updates.get(symbol))
            if not proved:
                unproved_positions.add(symbol)
                if residual not in (ZERO, None):
                    position_residual[symbol] = str(residual)
    return dict(unproven_baseline=False, position_residual=position_residual,
                cash_residual=None if wallet_residual is None else str(wallet_residual),
                unproved_wallet=not wallet_proved, unproved_positions=sorted(unproved_positions))


def _history_evidence(adapter, runtime, baseline, current, orders, trades, income, start_ms):
    """Compare official activity with pre-restore economic state and original intents."""
    journal = {(symbol, order_id): client_id for client_id, symbol, order_id in adapter.db.execute(
        "SELECT client_order_id,symbol,exchange_order_id FROM futures_order_recovery"
    ) if order_id is not None}
    unowned_orders = [row for row in orders if journal.get(
        (row["symbol"], str(row["orderId"]))) != row["clientOrderId"]]
    unowned_trades = []
    conflicting_trades = []
    missing_trade_receipts = []
    known_trades = {}
    position_events = defaultdict(list)
    trade_cash = {}
    for row in trades:
        value, at, side, position_side, realized = _trade_value(row)
        symbol, trade_id, order_id, qty, _, commission, currency = value
        client_id = journal.get((symbol, order_id))
        if client_id is None:
            unowned_trades.append(row)
            continue
        if client_id not in known_trades:
            known_trades[client_id] = _recorded_trades(adapter, client_id)
        order = adapter.order(client_id)
        if side != order.intent.side.value or position_side != "BOTH" or currency != "USDT":
            conflicting_trades.append(row)
            continue
        recorded = known_trades[client_id].get(trade_id)
        if recorded is None:
            missing_trade_receipts.append(row)
            continue
        if recorded != (value, at, side, position_side, realized):
            conflicting_trades.append(row)
            continue
        fee = Decimal(commission)
        trade_cash["COMMISSION", symbol, str(trade_id), currency] = (at, -fee)
        trade_cash["REALIZED_PNL", symbol, str(trade_id), currency] = (at, realized)
        position_events[symbol].append((at, Decimal(qty) if side == "BUY" else -Decimal(qty)))

    funding = {event.event_id: event for event in adapter.funding_events()}
    conflicting_funding = []
    missing_funding_receipts = []
    flows = defaultdict(list)
    cash_events = []
    for row in income.rows:
        kind, amount = row["incomeType"], _decimal(row["income"])
        if kind == "FUNDING_FEE":
            key = funding_event_id(kind, canonical_tran_id(row["tranId"]))
            event = funding.get(key)
            if event is None:
                missing_funding_receipts.append(row)
            elif (event.asset != "USDT" or event.amount != amount or event.symbol != row["symbol"]
                    or event.asset != row["asset"] or _to_ms(event.event_time) != row["time"]):
                conflicting_funding.append(row)
            else:
                cash_events.append((row["time"], amount))
        elif amount:
            flows[kind, row["symbol"], str(row.get("tradeId", "")), row["asset"]].append(row)
    unowned_income = []
    for key, rows in flows.items():
        amounts = [(row["time"], _decimal(row["income"])) for row in rows]
        expected = trade_cash.get(key)
        if expected is not None and sum((amount for _, amount in amounts), ZERO) == expected[1]:
            del trade_cash[key]
            cash_events.extend(amounts)
        else:
            unowned_income.extend(row for row in rows if row["time"] >= start_ms)
    cash_events.extend(trade_cash.values())
    unconfirmed_dispatches = [key for key, in runtime.db.execute(
        "SELECT client_id FROM dispatches WHERE method='POST' AND receipt IS NULL")
        if adapter.db.execute("SELECT 1 FROM futures_orders WHERE id=?", (key,)).fetchone() is None]
    return dict(unowned_orders=unowned_orders, unowned_trades=unowned_trades,
                conflicting_trades=conflicting_trades, conflicting_funding=conflicting_funding,
                missing_trade_receipts=missing_trade_receipts, missing_funding_receipts=missing_funding_receipts,
                unowned_income=unowned_income, unconfirmed_dispatches=unconfirmed_dispatches,
                **_economic_evidence(baseline, current, position_events, cash_events))


def _budget(runtime, balances):
    authorization = runtime.continuous_authorization()
    if authorization is None:
        return dict(verified=False, available=False, consumed=runtime.consumption(None))
    consumed = runtime.consumption(authorization)
    equity = balances["walletBalance"] + balances["unrealizedProfit"]
    loss = _decimal(authorization["opening_equity"]) - equity
    held = [Decimal(value) for value, in runtime.db.execute(
        "SELECT notional FROM exit_budget_reservations WHERE authorization=?", (authorization["id"],))]
    remaining_orders = authorization["total_orders"] - consumed["posts"] - len(held)
    remaining_notional = (_decimal(authorization["total_notional"])
                          - Decimal(consumed["submitted_notional"]) - sum(held, ZERO))
    remaining_loss = _decimal(authorization["max_loss"]) - loss
    return dict(verified=True, available=remaining_orders > 0 and remaining_notional > 0 and remaining_loss > 0,
                consumed=consumed, held_exit_count=len(held), remaining_orders=remaining_orders,
                remaining_notional=str(remaining_notional), remaining_loss=str(remaining_loss))


def _read_history(session, facts, *, baseline, start_ms, end_ms, snapshot_ms):
    adapter = session.adapter
    if snapshot_ms > end_ms or start_ms > end_ms or end_ms - start_ms >= _TRADE_RETENTION_MS:
        raise SafetyBlocked("Binance recovery baseline outside authoritative trade and income retention")
    order_start = max(start_ms, end_ms - UNFILLED_CANCEL_RETENTION_SECONDS * 1000 + 1)
    orders = []
    trades = []
    order_pages = [0]
    trade_pages = 0
    with session.client.recover_get_clock():
        income = adapter.income_window(start_time=start_ms, end_time=end_ms, max_pages=_HISTORY_PAGE_BUDGET)
        symbols = (set(session.gateway.state.instruments)
                   | {row["symbol"] for row in facts.open_orders}
                   | {symbol for symbol, _ in facts.confirmed_position_amounts()}
                   | {symbol for symbol, _, _ in baseline["positions"]}
                   | {row["symbol"] for row in income.rows if row["symbol"]})
        for symbol in sorted(symbols):
            orders.extend(_orders(adapter, symbol, order_start, end_ms, pages=order_pages))
            if trade_pages >= _HISTORY_PAGE_BUDGET:
                raise DataQualityError("Binance disaster recovery trade history exceeds the page budget")
            rows, count = fetch_user_trades_paginated(
                adapter, symbol, start_time=start_ms, end_time=end_ms,
                max_pages=_HISTORY_PAGE_BUDGET - trade_pages)
            trades.extend(rows)
            trade_pages += count
    history = dict(start_ms=start_ms, end_ms=end_ms, order_start_ms=order_start,
                   complete_unfilled_order_history=order_start <= snapshot_ms,
                   symbols=sorted(symbols), order_pages=order_pages[0], trade_pages=trade_pages,
                   income_pages=income.pages, order_count=len(orders), trade_count=len(trades))
    return history, orders, trades, income


def _account_signature(facts, baseline):
    # A zero net quantity or cash change can still advance an official update watermark.
    orders = sorted((row["symbol"], row["clientOrderId"], row.get("orderId"),
                     row.get("status"), row.get("executedQty"), row.get("updateTime")) for row in facts.open_orders)
    return (baseline["wallet"], baseline["positions"], baseline["wallet_update_ms"],
            baseline["position_updates"], orders)


def _reconcile(session, runtime, *, since, restore_id):
    adapter = session.adapter
    anchor = _anchor(adapter, since, restore_id=restore_id)
    baseline = anchor["baseline"]
    source_windows = baseline.get("read_windows")
    discovery_ms = (min(window[0] for window in source_windows.values()) if source_windows
                    else _to_ms(datetime.fromisoformat(anchor["observed_at"])))
    snapshot_ms = _to_ms(since)
    start_ms = min(discovery_ms, snapshot_ms)
    for attempt in range(1, _MAX_ACCOUNT_ATTEMPTS + 1):
        try:
            outcome = session.recover()
        except AccountPositionConflict as error:
            evidence = dict(reconciled=False, error=str(error), attempts=attempt)
            continue
        except (DataQualityError, SafetyBlocked, TimeoutError) as error:
            evidence = dict(reconciled=False, error=str(error), attempts=attempt)
            break

        facts = adapter.facts
        current = account_baseline(facts)
        final_windows = current["read_windows"]
        quiet_from = min(window[0] for window in final_windows.values())
        end_ms = max(window[1] for window in final_windows.values())
        history, orders, trades, income = _read_history(
            session, facts, baseline=baseline, start_ms=start_ms, end_ms=end_ms, snapshot_ms=snapshot_ms)
        history.update(baseline_observed_at=anchor["observed_at"], baseline_read_windows=source_windows,
                       final_read_windows=final_windows, quiet_from_ms=quiet_from)
        # An active/terminal transition during the account reads does not
        # belong to a closed recovery round, even if the net economics agree.
        overlapping = (any(row["time"] >= quiet_from for row in trades)
                       or any(row["time"] >= quiet_from for row in income.rows)
                       or any(row["updateTime"] >= quiet_from for row in orders))
        newer_order_facts = adapter.db.execute(
            "SELECT count(*) FROM futures_order_recovery WHERE julianday(exchange_updated_at)>=julianday(?)",
            (datetime.fromtimestamp(quiet_from / 1000, timezone.utc).isoformat(),),
        ).fetchone()[0]
        with session.client.recover_get_clock():
            refreshed = _refresh_archived_orders(adapter, trades)
            try:
                readback = adapter.read_facts()
            except AccountPositionConflict:
                readback = None
        changed = (readback is None
                   or _account_signature(facts, current) != _account_signature(readback, account_baseline(readback)))
        anomalies = _history_evidence(
            adapter, runtime, baseline, current, orders, trades, income, discovery_ms)
        if anomalies["missing_funding_receipts"] and attempt < _MAX_ACCOUNT_ATTEMPTS:
            # The normal recovery fetch precedes the completed account/history read.
            with session.client.recover_get_clock():
                adapter.sync_funding(end_time=end_ms)
        unstable = (overlapping or newer_order_facts or refreshed or changed
                    or anomalies["missing_trade_receipts"] or anomalies["missing_funding_receipts"])
        residual = anomalies["cash_residual"]
        reconciled = (outcome["reconciled"] and not outcome["unknown"] and not unstable
                      and history["complete_unfilled_order_history"]
                      and residual is not None and Decimal(residual) == ZERO
                      and not any(value for key, value in anomalies.items() if key != "cash_residual"))
        budget = _budget(runtime, (facts if readback is None else readback).usdt())
        budget["verified"] = budget["verified"] and reconciled
        evidence = dict(reconciled=reconciled, history=history, anomalies=anomalies, attempts=attempt,
                        final_account_unstable=bool(unstable), unknown=outcome["unknown"],
                        newer_order_facts=newer_order_facts, budget=budget,
                        risk_budget_available=budget["available"],
                        open_orders=len(outcome["open_orders"]), positions=len(outcome["positions"]),
                        funding_event_count=outcome["funding"]["event_count"])
        if not unstable:
            break
    with adapter.store.transaction():
        save_state(adapter.db, _EVIDENCE, evidence, adapter.clock().isoformat())
    return evidence


def catch_up(root, *, since):
    """Recover original identities under a local lock, with no write capability."""
    started = perf_counter()
    root = Path(root)
    requests = RequestCounts()
    with operator_lock(root, allow_quarantined=True):
        marker = json.loads((root / _QUARANTINE).read_text(encoding="utf-8"))
        if marker["reason"] != "restored-state-requires-GET-only-recovery":
            raise SafetyBlocked("Binance catch-up requires a quarantined restored state")
        store = DemoFileCredentialStore(root=root)
        with closing(DemoRuntime(root)) as runtime:
            for attempt in range(_MAX_ACCOUNT_ATTEMPTS):
                try:
                    with open_session(root, store, runtime, requests, require_bound_configuration=True) as (session, _):
                        result = _reconcile(session, runtime, since=since, restore_id=marker.get("restore_id"))
                except AccountPositionConflict:
                    if attempt == _MAX_ACCOUNT_ATTEMPTS - 1:
                        raise
                else:
                    break
            default = "RUNNING" if runtime.continuous_authorization() is not None else "READ_ONLY"
            return dict(result, desired=durable_state(root, runtime, default=default), requests=dict(requests),
                        elapsed_seconds=perf_counter() - started)
