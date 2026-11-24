"""Deterministic official-response lifecycle and fault recovery tests."""
from copy import deepcopy
from decimal import Decimal as D

import pytest

from factorlab.integrations.alpaca import (
    AlpacaAdapter, AlpacaConfig, AlpacaStore, PaperHTTP, AuthenticationError,
    PermissionDenied, RateLimited, ServiceError, OutcomeUnknown, OrderNotFound,
)
from factorlab.integrations.alpaca.models import order_parameters
from factorlab.trading.contracts import Market, OrderIntent, OrderStatus as S, OrderType, Side
from factorlab.trading.control import RiskLimits, RiskRejected

ASSET = {"symbol": "AAPL", "class": "us_equity", "status": "active", "tradable": True, "fractionable": True}
LIMITS = RiskLimits(D("100"), D("1000"), D("1000"), 20)


def intent(key="alp-test", *, side=Side.BUY, quantity="0.1", market=Market.ALPACA_US_EQUITY, tif="DAY"):
    return OrderIntent(market, "AAPL", side, OrderType.LIMIT, D(quantity), D("100"), key, tif)


def order(key="alp-test", *, qty="0.1", filled="0", average=None, status="new", time="2026-11-24T15:00:00Z"):
    return {"id": "exchange-" + key, "client_order_id": key, "symbol": "AAPL", "side": "buy", "type": "limit",
            "qty": qty, "limit_price": "100", "filled_qty": filled, "filled_avg_price": average,
            "time_in_force": "day", "status": status, "created_at": "2026-11-24T15:00:00Z",
            "submitted_at": "2026-11-24T15:00:00Z", "updated_at": time, "filled_at": None}


class Official:
    def __init__(self):
        self.orders, self.activities, self.positions, self.posts = {}, [], [], 0
        self.cash = "1000"
        self.submit_timeout = False

    def __call__(self, method, path, params, body):
        if path == "/v2/account":
            return 200, {"id": "account-one", "currency": "USD", "cash": self.cash, "equity": "1000",
                         "buying_power": "2000", "status": "ACTIVE", "trading_blocked": False, "account_blocked": False}
        if path == "/v2/clock":
            return 200, {"is_open": True, "next_open": "2026-11-25T14:30:00Z"}
        if path.startswith("/v2/assets/"):
            return 200, {**ASSET, "symbol": path.rsplit("/", 1)[1]}
        if path == "/v2/positions":
            return 200, deepcopy(self.positions)
        if path == "/v2/account/activities/FILL":
            return 200, deepcopy(self.activities)
        if path == "/v2/orders:by_client_order_id":
            row = self.orders.get(params["client_order_id"])
            return (200, deepcopy(row)) if row else (404, None)
        if path == "/v2/orders" and method == "GET":
            return 200, deepcopy(list(self.orders.values()))
        if path == "/v2/orders" and method == "POST":
            self.posts += 1
            row = order(body["client_order_id"], qty=body["qty"])
            row.update(side=body["side"], symbol=body["symbol"], type=body["type"],
                       limit_price=body.get("limit_price"))
            self.orders[body["client_order_id"]] = row
            if self.submit_timeout:
                raise TimeoutError()
            return 200, deepcopy(row)
        if method == "DELETE":
            row = next(r for r in self.orders.values() if path.endswith(r["id"]))
            row.update(status="canceled", updated_at="2026-11-24T15:01:00Z")
            return 204, None
        raise AssertionError((method, path))


def adapter(tmp_path, official=None):
    official = official or Official()
    def transport(method, path, params, body):
        assert not instance.store.db.in_transaction, "HTTP request inside SQLite transaction"
        return official(method, path, params, body)

    http = PaperHTTP(AlpacaConfig("not-real", "not-real"), transport=transport)
    instance = AlpacaAdapter(http, tmp_path / "alpaca.sqlite", limits=LIMITS)
    return instance, official


@pytest.mark.parametrize(("status", "error"), [(401, AuthenticationError), (403, PermissionDenied),
                                               (429, RateLimited), (500, ServiceError), (503, ServiceError)])
def test_error_classification(status, error):
    http = PaperHTTP(AlpacaConfig("fake", "fake"), transport=lambda *args: (status, {}))
    with pytest.raises(error):
        http.request("GET", "/v2/account")


def test_details_keep_accepted_observation(tmp_path):
    a, official = adapter(tmp_path)
    a.submit_order(intent())
    stale = deepcopy(official.orders["alp-test"])
    official.orders["alp-test"].update(
        filled_qty="0.1", filled_avg_price="99", status="filled",
        updated_at="2026-11-24T15:01:00Z")
    a.query_order("alp-test")
    official.orders["alp-test"] = stale
    details = a.read_order_details("alp-test")
    assert details["status"] == S.FILLED
    assert details["filled_quantity"] == D("0.1")
    assert details["cumulative_filled_notional"] == D("9.9")
    a.close()


def test_external_partial_market_buy_remains_unpriced(tmp_path):
    a, official = adapter(tmp_path)
    row = order("external", filled="0.04", average="99", status="partially_filled")
    row.update(type="market", limit_price=None)
    official.orders["external"] = row
    official.activities = [{"id": "external-fill", "order_id": row["id"], "qty": "0.04",
                            "price": "99", "side": "buy", "symbol": "AAPL"}]
    with pytest.raises(RiskRejected, match="UNPRICED_EXTERNAL_ORDER"):
        a.submit_order(intent())
    assert D(a.store.order("external")[3]) == 0
    assert official.posts == 0
    a.close()


def test_pending_sell_does_not_increase_buy_exposure(tmp_path):
    a, official = adapter(tmp_path)
    a.limits = RiskLimits(D("100"), D("1000"), D("1000"), 20)
    official.positions = [{"symbol": "AAPL", "qty": "5", "avg_entry_price": "100",
                           "current_price": "100"}]
    row = order("external-sell", qty="5")
    row["side"] = "sell"
    official.orders["external-sell"] = row
    assert a.submit_order(intent(quantity="1")).status == S.SUBMITTED
    a.close()


@pytest.mark.parametrize(("status", "message", "unknown"), [
    (422, "insufficient buying power", False),
    (422, "qty must be greater than 0", False),
    (422, "client_order_id must be unique", True),
    (422, None, True),
    (408, "request timeout", True),
    (409, "conflict", True),
])
def test_submission_rejection_certainty(tmp_path, status, message, unknown):
    from factorlab.integrations.alpaca import AlpacaError
    a, official = adapter(tmp_path)
    real_transport = a.http.transport

    def reject(method, path, params, body):
        if method == "POST":
            return status, {"code": 42210000, "message": message}
        return real_transport(method, path, params, body)

    a.http.transport = reject
    with pytest.raises(OutcomeUnknown if unknown else AlpacaError):
        a.submit_order(intent())
    assert a.recover().reconciled is not unknown
    if unknown:
        with pytest.raises(RiskRejected, match="RECONCILE_REQUIRED"):
            a.submit_order(intent("second"))
    else:
        assert a.query_order("alp-test").status == S.REJECTED
        assert not a.read_account().open_orders
        a.http.transport = real_transport
        assert a.submit_order(intent("second")).status == S.SUBMITTED
    a.close()


@pytest.mark.parametrize("url", ["https://api.alpaca.markets", "http://paper-api.alpaca.markets",
                                 "https://paper-api.alpaca.markets.evil.test", "https://paper-api.alpaca.markets/"])
def test_only_exact_paper_address(url):
    with pytest.raises(ValueError):
        AlpacaConfig("fake", "fake", url)


def test_unknown_submit_restart_does_not_repeat(tmp_path):
    a, official = adapter(tmp_path)
    official.submit_timeout = True
    with pytest.raises(OutcomeUnknown):
        a.submit_order(intent())
    assert a.store.order("alp-test")[2]
    a.close()
    a, _ = adapter(tmp_path, official)
    assert not a.reconciled
    assert a.recover().reconciled
    assert a.submit_order(intent()).status == S.SUBMITTED
    assert official.posts == 1
    a.close()


def test_unknown_without_official_order_stays_blocked(tmp_path):
    a, _ = adapter(tmp_path)
    with a.store.transaction():
        a.store.admit(intent(), D("100"))
    assert not a.recover().reconciled
    with pytest.raises(OrderNotFound):
        a.submit_order(intent())
    with pytest.raises(RiskRejected, match="RECONCILE_REQUIRED"):
        a.submit_order(intent("other"))


def test_incremental_fill_amounts_and_activity_dedup(tmp_path):
    a, official = adapter(tmp_path)
    a.submit_order(intent())
    row = official.orders["alp-test"]
    row.update(filled_qty="0.04", filled_avg_price="99", status="partially_filled", updated_at="2026-11-24T15:01:00Z")
    official.activities = [{"id": "fill-one", "order_id": row["id"], "qty": "0.04", "price": "99", "side": "buy", "symbol": "AAPL"}]
    event = a.query_order("alp-test")
    assert event.status == S.PARTIALLY_FILLED and event.fill_price == D("99")
    row.update(filled_qty="0.1", filled_avg_price="99.6", status="filled", updated_at="2026-11-24T15:02:00Z")
    official.activities.append({"id": "fill-two", "order_id": row["id"], "qty": "0.06", "price": "100", "side": "buy", "symbol": "AAPL"})
    event = a.query_order("alp-test")
    assert event.fill_price == D("100")
    assert a.query_order("alp-test").fill_price is None
    official.activities = [
        {"id": "fill-one", "order_id": row["id"], "qty": "0.04", "price": "99", "side": "buy", "symbol": "AAPL"},
        {"id": "fill-two", "order_id": row["id"], "qty": "0.06", "price": "100", "side": "buy", "symbol": "AAPL"},
    ]
    assert a.recover().reconciled
    assert a.recover().reconciled
    assert len(a.store.activities()) == 2
    a.close()
    a, _ = adapter(tmp_path, official)
    assert a.recover().reconciled
    assert len(a.store.activities()) == 2


def test_exact_activity_price_survives_rounding_lag_and_restart(tmp_path):
    a, official = adapter(tmp_path)
    row = order(qty="0.500000001", filled="0.5", average="99.000001", status="partially_filled")
    official.orders["alp-test"] = row
    official.activities = [{"id": "first", "order_id": row["id"], "qty": "0.5", "price": "99.000001", "side": "buy", "symbol": "AAPL"}]
    assert a.query_order("alp-test").fill_price == D("99.000001")
    row.update(filled_qty="0.500000001", filled_avg_price="99", status="filled")
    delayed = a.query_order("alp-test")
    assert delayed.status == S.UNKNOWN
    assert delayed.cumulative_filled_quantity == D("0.5")
    assert a.read_order_details("alp-test")["filled_quantity"] == D("0.500000001")
    a.close()
    official.activities.append({"id": "second", "order_id": row["id"], "qty": "0.000000001", "price": "99", "side": "buy", "symbol": "AAPL"})
    a, _ = adapter(tmp_path, official)
    assert a.recover().reconciled
    priced = a.query_order("alp-test")
    assert priced.status == S.FILLED and priced.fill_price == D("99")
    assert priced.event_id != delayed.event_id
    assert a.query_order("alp-test").fill_price is None


@pytest.mark.parametrize("source", ["position", "order"])
def test_non_equity_exposure_blocks_recovery(tmp_path, source):
    a, official = adapter(tmp_path)
    if source == "position":
        official.positions = [{"symbol": "OPTION", "asset_class": "us_option", "qty": "1", "avg_entry_price": "5", "current_price": "5"}]
    else:
        official.orders["alp-test"] = {**order(), "asset_class": "us_option"}
    with pytest.raises(ValueError, match="asset_class"):
        a.recover()
    assert not a.reconciled


def test_stale_event_cannot_regress_fill(tmp_path):
    a, official = adapter(tmp_path)
    a.submit_order(intent())
    stale = deepcopy(official.orders["alp-test"])
    row = official.orders["alp-test"]
    row.update(filled_qty="0.04", filled_avg_price="99", status="partially_filled", updated_at="2026-11-24T15:01:00Z")
    official.activities = [{"id": "fill-one", "order_id": row["id"], "qty": "0.04", "price": "99", "side": "buy", "symbol": "AAPL"}]
    a.query_order("alp-test")
    assert a._apply(stale).cumulative_filled_quantity == D("0.04")


def test_partial_cancel_and_fill_race(tmp_path):
    a, official = adapter(tmp_path)
    a.submit_order(intent())
    row = official.orders["alp-test"]
    row.update(filled_qty="0.04", filled_avg_price="99", status="partially_filled", updated_at="2026-11-24T15:00:30Z")
    official.activities = [{"id": "fill-one", "order_id": row["id"], "qty": "0.04", "price": "99", "side": "buy", "symbol": "AAPL"}]
    assert a.cancel_order("alp-test").status == S.CANCELED
    row.update(filled_qty="0.1", filled_avg_price="99", status="filled", updated_at="2026-11-24T15:02:00Z")
    official.activities.append({"id": "fill-two", "order_id": row["id"], "qty": "0.06", "price": "99", "side": "buy", "symbol": "AAPL"})
    assert a.query_order("alp-test").status == S.FILLED


@pytest.mark.parametrize("terminal", [False, True])
def test_cancel_returns_unconsumed_execution_price(tmp_path, terminal):
    a, official = adapter(tmp_path)
    a.submit_order(intent())
    row = official.orders["alp-test"]
    quantity = "0.1" if terminal else "0.04"
    row.update(filled_qty=quantity, filled_avg_price="99",
               status="filled" if terminal else "partially_filled")
    official.activities = [{"id": "cancel-fill", "order_id": row["id"], "qty": quantity,
                            "price": "99", "side": "buy", "symbol": "AAPL"}]
    event = a.cancel_order("alp-test")
    assert event.status == (S.FILLED if terminal else S.CANCELED)
    assert event.cumulative_filled_quantity == D(quantity)
    assert event.fill_price == D("99")
    a.close()
    a, _ = adapter(tmp_path, official)
    assert a.query_order("alp-test").fill_price is None


def test_cash_not_margin_and_inventory(tmp_path):
    a, official = adapter(tmp_path)
    official.cash = "1"
    with pytest.raises(RiskRejected, match="INSUFFICIENT_BALANCE"):
        a.submit_order(intent())
    with pytest.raises(RiskRejected, match="INSUFFICIENT_INVENTORY"):
        a.submit_order(intent(side=Side.SELL))
    assert official.posts == 0


def test_pending_reservations(tmp_path):
    a, official = adapter(tmp_path)
    official.cash = "15"
    a.submit_order(intent())
    with pytest.raises(RiskRejected, match="INSUFFICIENT_BALANCE"):
        a.submit_order(intent("second"))


@pytest.mark.parametrize("kwargs", [{"tif": "GTC"}, {"quantity": "0.0000000001"},
                                     {"market": Market.BINANCE_EQUITY, "tif": "GTC"}])
def test_invalid_and_cross_market_orders(kwargs):
    with pytest.raises(ValueError):
        order_parameters(intent(**kwargs), ASSET)


def test_binance_contract_still_rejects_day():
    with pytest.raises(ValueError):
        intent(market=Market.BINANCE_USDM, tif="DAY")


@pytest.mark.parametrize("tif", ["DAY", "GTC", "IOC", "FOK", "OPG", "CLS"])
@pytest.mark.parametrize("kind", [OrderType.MARKET, OrderType.LIMIT])
def test_whole_share_legal_tif_matrix(tif, kind):
    value = OrderIntent(Market.ALPACA_US_EQUITY, "AAPL", Side.BUY, kind, D("1"),
                        D("100") if kind == OrderType.LIMIT else None, "matrix", tif)
    assert order_parameters(value, ASSET)["time_in_force"] == tif.lower()


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_incomplete_mutation_reply_is_unknown(method):
    from http.client import IncompleteRead
    def broken(*args):
        raise IncompleteRead(b"partial")
    http = PaperHTTP(AlpacaConfig("fake", "fake"), transport=broken)
    with pytest.raises(OutcomeUnknown):
        http.request(method, "/v2/orders")


def test_post_5xx_unknown():
    http = PaperHTTP(AlpacaConfig("fake", "fake"), transport=lambda *args: (503, {}))
    with pytest.raises(OutcomeUnknown):
        http.request("POST", "/v2/orders", body={})


def test_http_error_details_redact_credentials():
    http = PaperHTTP(AlpacaConfig("secret-key-id", "secret-value"),
                     transport=lambda *args: (403, {"code": 40310000,
                                                   "message": "rejected secret-key-id secret-value"}))
    with pytest.raises(PermissionDenied) as caught:
        http.request("GET", "/v2/account")
    assert caught.value.code == 40310000
    assert "secret-key-id" not in str(caught.value)
    assert "secret-value" not in str(caught.value)
    assert caught.value.message == "rejected [redacted] [redacted]"


def test_changed_identity_rejected(tmp_path):
    a, official = adapter(tmp_path)
    a.submit_order(intent())
    with pytest.raises(ValueError, match="reused"):
        a.submit_order(intent(quantity="0.2"))
    official.orders["alp-test"]["id"] = "different"
    with pytest.raises(ValueError, match="identity changed"):
        a.query_order("alp-test")


def test_definite_rejection_does_not_lock_account(tmp_path):
    a, official = adapter(tmp_path)
    real_transport = a.http.transport

    def reject(method, path, params, body):
        if method == "POST":
            return 403, {}
        return real_transport(method, path, params, body)

    a.http.transport = reject
    with pytest.raises(PermissionDenied):
        a.submit_order(intent())
    assert a.recover().reconciled
    assert not a.read_account().open_orders
    assert official.posts == 0


def test_cumulative_details_survive_consumer_crash(tmp_path):
    a, official = adapter(tmp_path)
    a.submit_order(intent())
    official.orders["alp-test"].update(filled_qty="0.04", filled_avg_price="99", status="partially_filled",
                                      updated_at="2026-11-24T15:01:00Z")
    a.query_order("alp-test")
    a.close()
    a, _ = adapter(tmp_path, official)
    facts = a.read_order_details("alp-test")
    assert facts["cumulative_filled_notional"] == D("3.96")
    assert facts["filled_quantity"] == D("0.04")


def test_activity_pagination_and_duplicate_replay(tmp_path):
    a, official = adapter(tmp_path)
    rows = [{"id": f"fill-{i:03}", "order_id": "order", "qty": "1", "price": "1",
             "symbol": "AAPL", "side": "buy"} for i in range(101)]
    transport = a.http.transport

    def pages(method, path, params, body):
        if path.endswith("/FILL"):
            return 200, rows[100:] if params.get("page_token") else rows[:100]
        return transport(method, path, params, body)

    a.http.transport = pages
    assert a.read_activities() == tuple(rows)


def test_order_pagination_overlaps_tied_timestamp_group(tmp_path):
    a, _ = adapter(tmp_path)
    first = [order(str(i), time="2026-11-24T15:00:00Z") for i in range(500)]
    for i, row in enumerate(first):
        row["submitted_at"] = "2026-11-24T15:00:00Z" if i < 498 else "2026-11-24T14:59:00Z"
    tail = [first[-1], order("tail")]
    tail[-1]["submitted_at"] = "2026-11-24T14:58:00Z"
    calls = []

    def pages(method, path, params, body):
        calls.append(params)
        return 200, first if len(calls) == 1 else tail

    a.http.transport = pages
    result = a._all_orders()
    assert len(result) == 502
    assert calls[1]["until"] == "2026-11-24T15:00:00Z"


@pytest.mark.parametrize(("kind", "quantity"), [("stop", "1"), ("stop_limit", "1"),
                                                ("trailing_stop", "1"), ("market", None)])
def test_unsupported_external_history_is_diagnosed_not_omitted(tmp_path, kind, quantity):
    a, official = adapter(tmp_path)
    official.orders["external"] = {**order("external"), "type": kind, "qty": quantity}
    with pytest.raises(ValueError, match="id=exchange-external.*reconciliation incomplete"):
        a.recover()
    assert not a.reconciled
    assert official.posts == 0


def test_saturated_history_timestamp_blocks_recovery_with_diagnostic(tmp_path):
    a, official = adapter(tmp_path)
    assert a.recover().reconciled
    official.orders = {str(i): order(str(i)) for i in range(500)}
    with pytest.raises(ValueError, match="500 orders at submitted_at=2026-11-24T15:00:00Z"):
        a.recover()
    assert not a.reconciled
    assert official.posts == 0


def test_cash_ledger_deduplicates_exact_official_activities(tmp_path):
    a, _ = adapter(tmp_path)
    row = {"id": "execution-one", "order_id": "order", "qty": "0.1", "price": "99",
           "symbol": "AAPL", "side": "buy"}
    with a.store.transaction():
        a.store.activity(row)
        a.store.activity(row)
    assert a.ledger_summary() == {"cash_flow": D("-9.9"), "positions": {"AAPL": D("0.1")}, "activity_count": 1}
    with pytest.raises(ValueError, match="identity changed"):
        with a.store.transaction():
            a.store.activity({**row, "price": "98"})


@pytest.mark.parametrize("failure", ["canceled", "expired", "rejected", "http_rejected"])
def test_acceptance_reports_failed_sell_without_resubmission(tmp_path, failure):
    from factorlab.integrations.alpaca.acceptance import PaperAcceptance
    a, official = adapter(tmp_path)
    buy_id, sell_id = "failure-AAPL-buy", "failure-AAPL-sell"
    buy = order(buy_id, filled="0.1", average="100", status="filled")
    official.orders[buy_id] = buy
    official.activities = [{"id": "buy-fill", "order_id": buy["id"], "qty": "0.1", "price": "100", "side": "buy", "symbol": "AAPL"}]
    with a.store.transaction():
        a.store.put("acceptance", {"prefix": "failure", "symbols": ["AAPL"], "quantity": "0.1", "reservation_price": "100", "initial": {}, "status": "running", "buy_confirmations": {"AAPL": {}}})
        if failure == "http_rejected":
            a.store.admit(intent(sell_id, side=Side.SELL), D("100"))
            a.store.put("rejection:" + sell_id, {"status": 422})
        else:
            official.orders[sell_id] = {**order(sell_id, status=failure), "side": "sell"}
    result = PaperAcceptance(a).step()
    assert result["status"] == "test_sell_not_filled"
    assert result["failed_leg"] == sell_id
    assert D(result["remaining_quantity"]) == D("0.1")
    assert official.posts == 0


@pytest.mark.parametrize("mode", ["http", "terminal", "closed", "lag"])
def test_acceptance_failure_is_immediate_and_precedes_wait_gates(tmp_path, mode):
    from factorlab.integrations.alpaca.acceptance import PaperAcceptance
    a, official = adapter(tmp_path)
    buy_id, sell_id = "failure-AAPL-buy", "failure-AAPL-sell"
    buy = order(buy_id, filled="0.1", average="100", status="filled")
    official.orders[buy_id] = buy
    official.activities = [{"id": "buy-fill", "order_id": buy["id"], "qty": "0.1", "price": "100", "side": "buy", "symbol": "AAPL"}]
    official.positions = [{"symbol": "AAPL", "qty": "0.1", "avg_entry_price": "100", "current_price": "100"}]
    if mode in {"closed", "lag"}:
        official.orders[sell_id] = {**order(sell_id, filled="0.04", average="100", status="canceled"), "side": "sell"}
    with a.store.transaction():
        a.store.put("acceptance", {"prefix": "failure", "symbols": ["AAPL"], "quantity": "0.1", "reservation_price": "100", "initial": {}, "status": "running", "buy_confirmations": {"AAPL": {}}})
    def transport(method, path, params, body):
        assert not a.store.db.in_transaction
        if mode == "closed" and path == "/v2/clock":
            return 200, {"is_open": False, "next_open": "2026-11-25T14:30:00Z"}
        if method == "POST":
            if mode == "http":
                return 422, {"message": "order rejected"}
            row = {**order(sell_id, status="rejected"), "side": "sell", "type": "market", "limit_price": None}
            official.orders[sell_id] = row
            return 200, row
        return official(method, path, params, body)
    a.http = PaperHTTP(AlpacaConfig("fake", "fake"), transport=transport)
    result = PaperAcceptance(a).step()
    assert result["status"] == "test_sell_not_filled"
    assert result["failed_leg"] == sell_id
    assert D(result["remaining_quantity"]) == D("0.06" if mode in {"closed", "lag"} else "0.1")


def test_acceptance_round_trips_use_only_added_positions(tmp_path):
    from factorlab.integrations.alpaca.acceptance import PaperAcceptance
    a, official = adapter(tmp_path)
    runner = PaperAcceptance(a)
    # Existing holdings are not sold by the acceptance runner.
    official.positions = [{"symbol": "BA", "qty": "2", "avg_entry_price": "100", "current_price": "100"}]
    assert runner.step()["status"] == "waiting_official_orders"
    assert official.posts == 2

    def fill_open_orders():
        for row in official.orders.values():
            if row["status"] == "filled":
                continue
            qty = D(row["qty"])
            row.update(status="filled", filled_qty=str(qty), filled_avg_price="100",
                       updated_at="2026-11-24T15:01:00Z", filled_at="2026-11-24T15:01:00Z")
            official.activities.append({"id": "fill-" + row["id"], "order_id": row["id"],
                                        "symbol": row["symbol"], "side": row["side"], "qty": str(qty),
                                        "price": "100", "transaction_time": "2026-11-24T15:01:00Z"})
            official.cash = str(D(official.cash) + qty * 100 * (-1 if row["side"] == "buy" else 1))
            if row["side"] == "buy":
                official.positions.append({"symbol": row["symbol"], "qty": str(qty),
                                           "avg_entry_price": "100", "current_price": "100"})
            else:
                official.positions = [p for p in official.positions if p["symbol"] != row["symbol"]]

    fill_open_orders()
    assert runner.step()["status"] == "waiting_official_orders"
    assert official.posts == 4
    fill_open_orders()
    result = runner.step()
    assert result["status"] == "awaiting_process_restart"
    assert result["final"]["positions"] == {"BA": "2"}
    assert result["cash_change"] == "0.000"
    assert not result["restart_recovered"]
    assert runner.step()["status"] == "awaiting_process_restart"
    assert official.posts == 4
    # The child has no POST route: restarting acceptance must only read/reconcile.
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    snapshot_file = tmp_path / "official.json"
    snapshot_file.write_text(json.dumps({
        "/v2/account": official("GET", "/v2/account", None, None)[1],
        "/v2/positions": official.positions, "/v2/orders": list(official.orders.values()),
        "/v2/account/activities/FILL": official.activities,
    }), encoding="utf-8")
    script = '''
import json, sys
from factorlab.integrations.alpaca import AlpacaAdapter, AlpacaConfig, PaperHTTP
from factorlab.integrations.alpaca.acceptance import PaperAcceptance
with open(sys.argv[2], encoding="utf-8") as stream:
    facts = json.load(stream)
def read(method, path, params, body):
    assert method == "GET", "restart attempted a mutation"
    return 200, facts[path]
a = AlpacaAdapter(PaperHTTP(AlpacaConfig("fake", "fake"), transport=read), sys.argv[1])
result = PaperAcceptance(a).step()
assert result["status"] == "complete", result
assert result["restart_recovered"]
a.close()
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path / "alpaca.sqlite"), str(snapshot_file)],
                           env=env, capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr


def test_stream_disconnect_reconciles_without_resubmitting(tmp_path):
    import json
    from factorlab.integrations.alpaca.stream import trade_updates
    a, official = adapter(tmp_path)
    a.submit_order(intent())

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, value):
            self.last = json.loads(value)

        def recv(self, **kwargs):
            self.received = getattr(self, "received", 0) + 1
            if self.received == 1:
                return json.dumps({"stream": "authorization", "data": {"status": "authorized"}})
            if self.received == 2:
                return json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}})
            if self.received == 3:
                return json.dumps({"stream": "trade_updates", "data": {"order": official.orders["alp-test"]}})
            raise ConnectionError("disconnected")

    with pytest.raises(ConnectionError, match="disconnected"):
        trade_updates(a, connect=lambda *args, **kwargs: Socket())
    assert a.reconciled
    assert official.posts == 1
