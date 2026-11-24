"""Small official Paper round trips with durable identities and public evidence."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import uuid

from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side, OrderStatus as S, TERMINAL
from factorlab.trading.control import RiskLimits

from .adapter import AlpacaAdapter, order_status
from .http import AlpacaConfig, AlpacaError, PaperHTTP

D = Decimal
LIMITS = RiskLimits(D("5"), D("10"), D("20"), 20)


def positions(snapshot):
    return {p.instrument_id: str(p.quantity) for p in snapshot.positions if p.quantity != 0}


def account_evidence(adapter):
    facts = adapter.read_account_details()
    snapshot = adapter.read_account()
    return {"observed_at": facts.observed_at.isoformat(), "equity": str(facts.equity),
            "cash": str(facts.cash), "buying_power": str(facts.buying_power),
            "positions": positions(snapshot), "reconciled": snapshot.reconciled}


class PaperAcceptance:
    """One persisted campaign of two tiny round trips, without an approval state machine."""
    def __init__(self, adapter):
        self.adapter = adapter

    def buy_failure(self, run, symbol):
        key = f"{run['prefix']}-{symbol}-buy"
        buy = self.adapter.store.order(key)
        if buy is not None and buy[1] is None and self.adapter.store.get("rejection:" + key) is not None:
            return {**run, "status": "test_buy_not_filled", "symbol": symbol,
                    "failed_leg": key, "order_status": "rejected"}
        return None

    def sell_failure(self, run, symbol):
        key = f"{run['prefix']}-{symbol}-sell"
        sell = self.adapter.store.order(key)
        if sell is None:
            return None
        row = sell[1]
        rejected = row is None and self.adapter.store.get("rejection:" + key) is not None
        if rejected or (row is not None and order_status(row) in TERMINAL and order_status(row) != S.FILLED):
            bought = D(self.adapter.store.order(f"{run['prefix']}-{symbol}-buy")[1]["filled_qty"])
            return {**run, "status": "test_sell_not_filled", "symbol": symbol,
                    "failed_leg": key, "order_status": "rejected" if rejected else row["status"],
                    "remaining_quantity": str(bought - (D(row["filled_qty"]) if row else D("0")))}
        return None

    def step(self):
        a = self.adapter
        snapshot = a.recover()
        run = a.store.get("acceptance")
        if run is None:
            run = {"prefix": "alpe01-" + uuid.uuid4().hex[:16], "symbols": ["QQQ", "AAPL"],
                   "quantity": "0.003", "reservation_price": "1000", "initial": account_evidence(a),
                   "status": "running"}
            with a.store.transaction():
                a.store.put("acceptance", run)
        if run["status"] == "complete":
            return run
        if run["status"] == "awaiting_process_restart":
            if a.store.get("acceptance-process") == os.getpid():
                return run
            after = account_evidence(a)
            restored = (snapshot.reconciled and after["positions"] == run["final"]["positions"]
                        and after["cash"] == run["final"]["cash"]
                        and len(a.store.activities()) == a.store.get("acceptance-activity-count"))
            run = {**run, "status": "complete" if restored else "reconciliation_mismatch",
                   "restart_recovered": restored, "restart": after}
            with a.store.transaction():
                a.store.put("acceptance", run)
            return run
        for symbol in run["symbols"]:
            failure = self.buy_failure(run, symbol) or self.sell_failure(run, symbol)
            if failure is not None:
                return failure
        clock = a.http.request("GET", "/v2/clock")
        if not clock["is_open"]:
            return {**run, "status": "waiting_market_session", "next_open": clock["next_open"]}
        if not snapshot.reconciled:
            return {**run, "status": "waiting_official_reconciliation"}
        complete = True
        for symbol in run["symbols"]:
            buy_id = f"{run['prefix']}-{symbol}-buy"
            sell_id = f"{run['prefix']}-{symbol}-sell"
            buy = a.store.order(buy_id)
            if buy is None:
                try:
                    a.submit_order(OrderIntent(Market.ALPACA_US_EQUITY, symbol, Side.BUY, OrderType.MARKET,
                                               D(run["quantity"]), None, buy_id, "DAY"),
                                   reservation_price=D(run["reservation_price"]))
                except AlpacaError:
                    failure = self.buy_failure(run, symbol)
                    if failure is None:
                        raise
                    return failure
                complete = False
                continue
            if buy[1] is None or order_status(buy[1]) not in TERMINAL:
                complete = False
                continue
            filled = D(buy[1]["filled_qty"])
            if filled == 0:
                return {**run, "status": "test_buy_not_filled", "symbol": symbol}
            confirmations = run.setdefault("buy_confirmations", {})
            if symbol not in confirmations:
                confirmations[symbol] = account_evidence(a)
                with a.store.transaction():
                    a.store.put("acceptance", run)
            sell = a.store.order(sell_id)
            if sell is None:
                try:
                    a.submit_order(OrderIntent(Market.ALPACA_US_EQUITY, symbol, Side.SELL, OrderType.MARKET,
                                               filled, None, sell_id, "DAY"),
                                   reservation_price=D(run["reservation_price"]))
                except AlpacaError:
                    failure = self.sell_failure(run, symbol)
                    if failure is None:
                        raise
                    return failure
                failure = self.sell_failure(run, symbol)
                if failure is not None:
                    return failure
                complete = False
            elif sell[1] is None or order_status(sell[1]) != S.FILLED:
                complete = False
        if not complete:
            return {**run, "status": "waiting_official_orders"}
        final = a.recover()
        after = account_evidence(a)
        if not final.reconciled or after["positions"] != run["initial"]["positions"]:
            return {**run, "status": "reconciliation_mismatch", "final": after}
        test_orders = []
        ids = set()
        for key, (_, row, _, _) in a.store.orders():
            if key.startswith(run["prefix"]):
                ids.add(row["id"])
                test_orders.append({k: row[k] for k in ("id", "client_order_id", "symbol", "side", "status",
                                                        "qty", "filled_qty", "filled_avg_price", "filled_at")})
        activities = [{k: row[k] for k in ("id", "order_id", "symbol", "side", "qty", "price", "transaction_time")}
                      for row in a.store.activities() if row["order_id"] in ids]
        cash_flow = sum((D(r["qty"]) * D(r["price"]) * (-1 if r["side"] == "buy" else 1)
                         for r in activities), D("0"))
        cash_change = D(after["cash"]) - D(run["initial"]["cash"])
        count = len(a.store.activities())
        a.recover()
        repeated_count = len(a.store.activities())
        result = {**run, "status": "awaiting_process_restart" if repeated_count == count
                  and abs(cash_change - cash_flow) <= D("0.01") else "reconciliation_mismatch",
                  "final": after, "orders": test_orders, "activities": activities,
                  "cash_change": str(cash_change), "fill_cash_flow": str(cash_flow),
                  "cash_rounding_or_other_activity_difference": str(cash_change - cash_flow),
                  "restart_recovered": False, "duplicate_activity_count": repeated_count - count,
                  "observed_partial_fills": [a.store.get("partial:" + key) for key, _ in a.store.orders()
                                             if key.startswith(run["prefix"]) and a.store.get("partial:" + key) is not None],
                  "online_multiple_execution_activities_observed": any(
                      sum(1 for r in activities if r["order_id"] == order_id) > 1 for order_id in ids)}
        with a.store.transaction():
            a.store.put("acceptance-process", os.getpid())
            a.store.put("acceptance-activity-count", count)
            a.store.put("acceptance", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--watch", action="store_true", help="advance on official trade_updates without polling")
    args = parser.parse_args(argv)
    config = AlpacaConfig.from_env() if args.credentials is None else AlpacaConfig.from_file(args.credentials)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    adapter = AlpacaAdapter(PaperHTTP(config), args.state, limits=LIMITS)
    try:
        runner = PaperAcceptance(adapter)
        result = None

        def advance():
            nonlocal result
            result = runner.step()
            args.evidence.parent.mkdir(parents=True, exist_ok=True)
            args.evidence.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"status": result["status"], "evidence": str(args.evidence)}), flush=True)
            return result["status"] not in {"waiting_official_orders", "waiting_official_reconciliation"}

        done = advance()
        if args.watch and not done:
            from .stream import trade_updates
            trade_updates(adapter, on_update=advance)
    finally:
        adapter.close()
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
