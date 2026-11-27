"""Restore a Binance snapshot against HTTP-intercepted official history."""

from contextlib import closing
from datetime import timedelta
from decimal import Decimal as D
import json
from threading import Barrier
from urllib.parse import parse_qs

import pytest

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading import demo_disaster_recovery as disaster
from factorlab.integrations.binance_futures_trading.demo_cloud_storage import atomic_json, backup_state, restore_state
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, journal_id, open_grid_session
from factorlab.integrations.binance_futures_trading.execution import DemoFuturesAdapter
from factorlab.integrations.binance_futures_trading.fake import MS, NOW
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.integrations.binance_futures_trading.recovery import load_state, save_state
from factorlab.trading.storage import PaperStore
from test_binance_demo_grid_network import network_grid
from test_binance_demo_network import NetworkFixture, wired
from test_demo_execute_systemd import _start_continuous, execute_harness


ACCOUNT = "/fapi/v3/account"
RISK = "/fapi/v3/positionRisk"


def _trade(fake, client_id, *, quantity, at, price="146"):
    cash_before = fake.cash
    fake.trade_update(client_id, quantity, price)
    order = fake.orders[client_id]
    order["updateTime"] = at
    trade = fake.trades[-1]
    realized = fake.cash - cash_before + D(trade["commission"])
    trade.update(time=at, side=order["side"], positionSide="BOTH", realizedPnl=str(realized))
    return trade


def _during_account_reads(h, *, account, risk, event):
    # Before means the still-in-flight request will include the event. After
    # means its response body has already been constructed on the venue side.
    gate = Barrier(2, action=event)
    h.account_hooks[account, ACCOUNT] = lambda: gate.wait(timeout=3)
    h.account_hooks[risk, RISK] = lambda: gate.wait(timeout=3)


def _journal_state(h, destination, scope):
    protocol = destination / journal_id(h.store.config["account_id"]) / "protocol.sqlite3"
    with closing(PaperStore(protocol)) as store:
        return load_state(store.db, scope)


@pytest.fixture
def restored(execute_harness, tmp_path_factory, monkeypatch):
    h = execute_harness
    h.fake.income = []
    h.account_hooks = {}
    current_response = NetworkFixture.__call__

    def order_history(exchange, request, timeout):
        before = h.account_hooks.pop(("before", request.path), None)
        if before is not None:
            before()
        response = current_response(exchange, request, timeout)
        if request.path.endswith("/allOrders"):
            params = parse_qs(request.query)
            start, end = int(params["startTime"][0]), int(params["endTime"][0])
            rows = [row for row in json.loads(response.body)
                    if row["symbol"] == params["symbol"][0] and start <= row["time"] <= end]
            response = Response(200, {}, json.dumps(rows).encode())
        after = h.account_hooks.pop(("after", request.path), None)
        if after is not None:
            after()
        return response

    monkeypatch.setattr(NetworkFixture, "__call__", order_history)
    monkeypatch.setattr(disaster, "DemoFileCredentialStore", lambda **_: h.store)

    def snapshot(*, lost_response, control, before_snapshot=None):
        _start_continuous(h.root, h.store, total_orders=10, total_notional=D("100"))
        with open_grid_session(h.root, h.store, h.factory, continuous=True) as bridge:
            bridge.recover()
            h.fake.timeout_next = lost_response
            bridge.observe(dict(id="initial", time=NOW.replace(tzinfo=None).isoformat(), price="150", volume=0))
            client_id, = h.fake.orders
            h.fake.orders[client_id]["time"] = MS + 200
            if not lost_response:
                _trade(h.fake, client_id, quantity=".01", at=MS + 350)
                h.state.seconds = 0.5
                bridge.recover()
            if before_snapshot is not None:
                client_id = before_snapshot(h, bridge, client_id)
        with closing(DemoRuntime(h.root)) as runtime:
            if control == "STOPPED":
                runtime.stop()
            else:
                runtime.halt("isolated disaster recovery drill")
        atomic_json(h.root / "binding.json", h.store.config)
        for path in (h.root, *h.root.rglob("*")):
            path.chmod(0o700 if path.is_dir() else 0o600)
        directory = tmp_path_factory.mktemp("binance-dr")
        directory.chmod(0o700)
        backup = directory / "backup"
        destination = directory / "restored"
        backup_state(h.root, backup)
        restore_state(backup, destination)
        captured = NOW + timedelta(seconds=h.state.seconds)
        h.state.seconds = 2
        return h, destination, captured, client_id

    return snapshot


@pytest.mark.parametrize(("lost_response", "control"), [(True, "STOPPED"), (False, "HALTED")])
def test_catch_up_partial_fills_funding_and_lost_post(restored, lost_response, control):
    h, destination, captured, client_id = restored(lost_response=lost_response, control=control)
    _trade(h.fake, client_id, quantity=".02", at=MS + 1000)
    h.fake.funding_update("-.0002", time_ms=MS + 1200, tran_id=51)
    sent = len(h.requests)
    report = disaster.catch_up(destination, since=captured)
    repeated = disaster.catch_up(destination, since=captured)
    assert report["reconciled"] and repeated["reconciled"]
    assert report["desired"] == repeated["desired"] == control
    assert report["budget"]["consumed"]["posts"] == repeated["budget"]["consumed"]["posts"] == 1
    assert report["funding_event_count"] == repeated["funding_event_count"] == 1
    assert D(report["anomalies"]["cash_residual"]) == 0
    assert (destination / "restore-quarantine.json").exists()
    assert all(request.get_method() == "GET" for request in h.requests[sent:])
    parent = destination / journal_id(h.store.config["account_id"])
    with closing(PaperStore(parent / "controller.sqlite3")) as store:
        account = store.load()
        order = account.orders[client_id].snapshot
        quantity = D(".02") if lost_response else D(".03")
        assert order.exchange_order_id == str(h.fake.orders[client_id]["orderId"])
        assert order.cumulative_filled_quantity == quantity
        assert order.cumulative_fee == quantity * D("146") * D(".001")
        assert account.funding["USDT"] == D("-.0002")


def test_catch_up_cannot_absorb_unattributed_transfer_on_retry(restored):
    h, destination, captured, _ = restored(lost_response=True, control="STOPPED")
    h.fake.funding_update("4", time_ms=MS + 1000, income_type="TRANSFER", tran_id=52)
    first = disaster.catch_up(destination, since=captured)
    retry = disaster.catch_up(destination, since=captured)
    assert not first["reconciled"] and not retry["reconciled"]
    for evidence in (first, retry):
        assert D(evidence["anomalies"]["cash_residual"]) == D("4")
        assert [row["tranId"] for row in evidence["anomalies"]["unowned_income"]] == [52]
        assert evidence["requests"]["POST"] == evidence["requests"]["DELETE"] == 0
    assert (destination / "restore-quarantine.json").exists()


def test_catch_up_preserves_official_unowned_order_ids_without_inventing_intents(restored):
    h, destination, captured, client_id = restored(lost_response=True, control="STOPPED")
    h.fake.orders["unattributed"] = dict(h.fake.orders[client_id], clientOrderId="unattributed", orderId=9,
                                        time=MS + 500, origQty=".04")
    _trade(h.fake, "unattributed", quantity=".04", at=MS + 1000)
    evidence = disaster.catch_up(destination, since=captured)
    assert not evidence["reconciled"]
    assert [(row["clientOrderId"], row["orderId"]) for row in evidence["anomalies"]["unowned_orders"]] == [
        ("unattributed", 9)]
    assert [row["orderId"] for row in evidence["anomalies"]["unowned_trades"]] == [9]
    assert evidence["budget"]["consumed"]["posts"] == 1
    assert evidence["requests"]["POST"] == 0


@pytest.mark.parametrize(("phase", "baseline_includes_fill"), [
    ("before", True),
    ("after", False),
])
def test_catch_up_attributes_fills_inside_the_saved_account_reads(restored, phase, baseline_includes_fill):
    def source(h, bridge, client_id):
        h.state.seconds = 0.7

        def fill():
            h.state.seconds = 0.8
            _trade(h.fake, client_id, quantity=".01", at=MS + 800)

        _during_account_reads(h, account=phase, risk=phase, event=fill)
        bridge.recover()
        return client_id

    h, destination, captured, client_id = restored(
        lost_response=False, control="STOPPED", before_snapshot=source,
    )
    original = _journal_state(h, destination, "account_baseline")
    expected = D(".02" if baseline_includes_fill else ".01")
    assert D(original["positions"][0][2]) == expected
    for source_key in ("account", "position_risk"):
        low, high = original["read_windows"][source_key]
        assert low <= MS + 800 <= high

    _trade(h.fake, client_id, quantity=".01", at=MS + 1200)
    first = disaster.catch_up(destination, since=captured)
    first_anchor = _journal_state(h, destination, disaster._ANCHOR)
    second = disaster.catch_up(destination, since=captured)
    assert first["reconciled"] and second["reconciled"]
    for result in (first, second):
        assert D(result["anomalies"]["cash_residual"]) == 0
        assert result["anomalies"]["position_residual"] == {}
        assert result["budget"]["consumed"]["posts"] == 1
        assert result["requests"]["POST"] == result["requests"]["DELETE"] == 0
    assert first_anchor["baseline"] == original
    assert _journal_state(h, destination, disaster._ANCHOR) == first_anchor


def test_catch_up_does_not_save_mixed_position_responses_as_a_source_baseline(restored):
    def source(h, bridge, client_id):
        h.state.seconds = 0.7

        def fill():
            h.state.seconds = 0.8
            _trade(h.fake, client_id, quantity=".01", at=MS + 800)

        _during_account_reads(h, account="after", risk="before", event=fill)
        with pytest.raises(DataQualityError, match="positions"):
            bridge.recover()
        return client_id

    h, destination, captured, _ = restored(
        lost_response=False, control="STOPPED", before_snapshot=source,
    )
    original = _journal_state(h, destination, "account_baseline")
    assert D(original["positions"][0][2]) == D(".01")
    evidence = disaster.catch_up(destination, since=captured)
    assert evidence["reconciled"]
    assert _journal_state(h, destination, disaster._ANCHOR)["baseline"] == original


@pytest.mark.parametrize("endpoint", ["/fapi/v1/allOrders", "/fapi/v1/userTrades"])
def test_catch_up_retries_when_a_partial_fill_during_recovery_changes_final_facts(restored, endpoint):
    h, destination, captured, client_id = restored(lost_response=False, control="STOPPED")

    def fill():
        h.state.seconds = 2.3
        _trade(h.fake, client_id, quantity=".01", at=MS + 2250)

    h.account_hooks["after", endpoint] = fill
    evidence = disaster.catch_up(destination, since=captured)
    assert evidence["reconciled"]
    assert evidence["attempts"] >= 2
    assert evidence["history"]["end_ms"] >= MS + 2250
    assert D(evidence["anomalies"]["cash_residual"]) == 0
    assert evidence["anomalies"]["position_residual"] == {}
    assert evidence["requests"]["POST"] == evidence["requests"]["DELETE"] == 0


def test_catch_up_reconciles_funding_and_realized_pnl_inside_the_wallet_read(restored):
    def source(h, bridge, buy_id):
        _trade(h.fake, buy_id, quantity=".03", at=MS + 550)
        h.state.seconds = 0.6
        bridge.recover()
        bridge.dispatch()
        sell_id, = set(h.fake.orders) - {buy_id}
        h.fake.orders[sell_id]["time"] = MS + 600
        h.state.seconds = 0.7

        def settle():
            h.state.seconds = 0.8
            sale = _trade(h.fake, sell_id, quantity=".01", price="154", at=MS + 750)
            assert D(sale["realizedPnl"]) == D(".08")
            for income_type, amount, tran_id in (
                ("COMMISSION", -D(sale["commission"]), 21),
                ("REALIZED_PNL", D(sale["realizedPnl"]), 22),
            ):
                h.fake.funding_update(amount, time_ms=MS + 750, tran_id=tran_id,
                                      income_type=income_type, apply_cash=False)
                h.fake.income[-1]["tradeId"] = str(sale["id"])
            h.fake.funding_update("-.0002", time_ms=MS + 780, tran_id=23)

        _during_account_reads(h, account="before", risk="before", event=settle)
        bridge.recover()
        return sell_id

    h, destination, captured, sell_id = restored(
        lost_response=False, control="STOPPED", before_snapshot=source,
    )
    _trade(h.fake, sell_id, quantity=".01", price="154", at=MS + 1200)
    first = disaster.catch_up(destination, since=captured)
    second = disaster.catch_up(destination, since=captured)
    for evidence in (first, second):
        assert evidence["reconciled"]
        assert evidence["funding_event_count"] == 1
        assert evidence["budget"]["consumed"]["posts"] == 2
        assert D(evidence["anomalies"]["cash_residual"]) == 0
        assert evidence["anomalies"]["position_residual"] == {}


def test_catch_up_replays_funding_receipt_visible_after_normal_funding_get(restored, monkeypatch):
    h, destination, captured, _ = restored(lost_response=False, control="STOPPED")
    sync_funding = DemoFuturesAdapter.sync_funding
    calls = []

    def funding_published(adapter, **kwargs):
        result = sync_funding(adapter, **kwargs)
        calls.append(kwargs.get("end_time"))
        if len(calls) == 1:
            h.fake.funding_update("-.0002", time_ms=MS + 1700, tran_id=31)
        return result

    monkeypatch.setattr(DemoFuturesAdapter, "sync_funding", funding_published)
    evidence = disaster.catch_up(destination, since=captured)
    assert evidence["reconciled"]
    assert evidence["attempts"] >= 2
    assert evidence["history"]["end_ms"] in calls
    assert evidence["funding_event_count"] == 1


@pytest.mark.parametrize(("field", "delta"), [("cash", D("3")), ("quantity", D(".01"))])
def test_catch_up_rejects_unexplained_final_cash_or_position_on_reopen(restored, field, delta):
    h, destination, captured, _ = restored(lost_response=False, control="STOPPED")
    setattr(h.fake, field, getattr(h.fake, field) + delta)
    for _ in range(2):
        evidence = disaster.catch_up(destination, since=captured)
        assert not evidence["reconciled"]
        if field == "cash":
            assert D(evidence["anomalies"]["cash_residual"]) == delta
        else:
            assert D(evidence["anomalies"]["position_residual"]["NVDAUSDT"]) == delta
    assert (destination / "restore-quarantine.json").exists()


def test_catch_up_keeps_the_unproven_source_anchor_across_sqlite_reopen(restored):
    h, destination, captured, _ = restored(lost_response=False, control="STOPPED")
    protocol = destination / journal_id(h.store.config["account_id"]) / "protocol.sqlite3"
    with closing(PaperStore(protocol)) as store, store.transaction():
        legacy = load_state(store.db, "account_baseline")
        del legacy["read_windows"]
        save_state(store.db, "account_baseline", legacy, captured.isoformat())

    for _ in range(2):
        evidence = disaster.catch_up(destination, since=captured)
        assert not evidence["reconciled"]
        assert evidence["anomalies"]["unproven_baseline"]
        assert _journal_state(h, destination, disaster._ANCHOR)["baseline"] == legacy
    assert (destination / "restore-quarantine.json").exists()
    backup = destination.with_name("quarantined-backup")
    copied = destination.with_name("quarantined-copy")
    backup_state(destination, backup)
    restore_state(backup, copied)
    with pytest.raises(SafetyBlocked, match="cannot change the recovery watermark"):
        disaster.catch_up(copied, since=captured + timedelta(milliseconds=1))
    assert not disaster.catch_up(copied, since=captured)["reconciled"]
    assert _journal_state(h, copied, disaster._ANCHOR)["baseline"] == legacy
