"""Restore a Binance snapshot against HTTP-intercepted official history."""

from contextlib import closing
from datetime import timedelta
from decimal import Decimal as D
import json

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading import demo_disaster_recovery as disaster
from factorlab.integrations.binance_futures_trading.demo_cloud_storage import atomic_json, backup_state, restore_state
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, journal_id, open_grid_session
from factorlab.integrations.binance_futures_trading.fake import MS, NOW
from factorlab.trading.storage import PaperStore
from test_binance_demo_grid_network import network_grid
from test_binance_demo_network import NetworkFixture, wired
from test_demo_execute_systemd import _start_continuous, execute_harness


def _trade(fake, client_id, *, quantity, at):
    fake.trade_update(client_id, quantity, "146")
    order = fake.orders[client_id]
    order["updateTime"] = at
    fake.trades[-1].update(time=at, side=order["side"], positionSide="BOTH", realizedPnl="0")


@pytest.fixture
def restored(execute_harness, tmp_path_factory, monkeypatch):
    h = execute_harness
    h.fake.income = []
    current_response = NetworkFixture.__call__

    def order_history(exchange, request, timeout):
        response = current_response(exchange, request, timeout)
        if request.path.endswith("/allOrders"):
            _, _, params = exchange.calls[-1]
            start, end = int(params["startTime"][0]), int(params["endTime"][0])
            rows = [row for row in json.loads(response.body)
                    if row["symbol"] == params["symbol"][0] and start <= row["time"] <= end]
            return Response(200, {}, json.dumps(rows).encode())
        return response

    monkeypatch.setattr(NetworkFixture, "__call__", order_history)
    monkeypatch.setattr(disaster, "DemoFileCredentialStore", lambda **_: h.store)

    def snapshot(*, lost_response, control):
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
