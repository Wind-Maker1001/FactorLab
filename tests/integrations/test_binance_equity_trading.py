"""Offline official-schema protocol tests; no credentials or remote dependencies."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import hashlib
import hmac
import json
from urllib.parse import parse_qs

import pytest

from factorlab.integrations.binance_equity_trading.protocol import (
    Rules, Session, StockOrder, Unavailable, admit, fresh,
)
from factorlab.integrations.binance_equity_trading.requests import (
    Credentials, ReadClient, PREFIX, live_submit, live_cancel,
)
from factorlab.integrations.binance_equity_trading.reports import Report, incremental, trades
from factorlab.integrations.binance_equity_trading.recovery import OrderRecovery

NOW = datetime(2026, 11, 10, 15, tzinfo=timezone.utc)
MS = int(NOW.timestamp() * 1000)
CID = "nvda-order-00000000000000000000001"


@pytest.fixture
def rules():
    return Rules.from_exchange_info(dict(symbol="NVDA", tradability="BUY_SELL",
        stepSize="0.001", minQty="0.001", maxQty="1000", minNotional="1", maxNotional="100000",
        fractionable=True, fractionableEh=True, extendedSession=True, overnightSupported=True,
        maxNumOrders=2, multiplierDown="0.5", multiplierUp="1.5"))


def order(side="BUY", kind="LIMIT", **kwargs):
    defaults = dict(quantity=D("0.5"), price=D("150"), trading_session="EXTENDED") if kind == "LIMIT" else (
        dict(notional=D("75")) if side == "BUY" else dict(quantity=D("0.5")))
    defaults.update(kwargs)
    return StockOrder("NVDA", side, kind, CID, **defaults)


@pytest.mark.parametrize("side,kind,expected,forbidden", [
    ("BUY", "MARKET", {"notional": "75"}, {"quantity", "price", "tradingSession"}),
    ("SELL", "MARKET", {"quantity": "0.5"}, {"notional", "price", "tradingSession"}),
    ("BUY", "LIMIT", {"quantity": "0.5", "price": "150", "tradingSession": "EXTENDED"}, {"notional"}),
    ("SELL", "LIMIT", {"quantity": "0.5", "price": "150", "tradingSession": "EXTENDED"}, {"notional"}),
])
def test_parameter_matrix(side, kind, expected, forbidden):
    params = order(side, kind).params()
    assert expected.items() <= params.items()
    assert not forbidden & params.keys()
    assert params["tokenize"] == "false" and params["quoteAsset"] == "USD"


@pytest.mark.parametrize("value", [
    order(kind="MARKET", quantity=D("1")), order("SELL", "MARKET", notional=D("1")),
    order(kind="MARKET", trading_session="RTH"), order(kind="MARKET", price=D("1")),
    order(kind="MARKET", time_in_force="GTC"), order(notional=D("1")),
    order(price=D("150.001")), order(quantity=0.5), order(quantity=D("NaN")),
    order(trading_session=None), order(trading_session="RTH", time_in_force="GTC"),
    order(quantity=D("0")), replace(order(), client_order_id="short"),
])
def test_invalid_matrix(value):
    with pytest.raises(ValueError):
        value.params()


def accept(value, rules, phase=Session.REGULAR, **kwargs):
    return admit(value, rules, phase, price=D("150"), reference_price=D("150"), permission=True, **kwargs)


@pytest.mark.parametrize("kwargs", [dict(quantity=D("0.5001")), dict(quantity=D("0.001")),
    dict(quantity=D("1001")), dict(price=D("10")), dict(price=D("300"))])
def test_quantity_notional_and_price_bounds(rules, kwargs):
    with pytest.raises(ValueError):
        accept(order(**kwargs), rules)


def test_precise_fractional_limits_and_minimum(rules):
    assert accept(order(quantity=D("0.007")), rules)["quantity"] == "0.007"
    assert accept(order(kind="MARKET", notional=D("1")), rules)["notional"] == "1"
    with pytest.raises(ValueError):
        accept(order(kind="MARKET", notional=D("0.99")), rules)


@pytest.mark.parametrize("phase", list(Session))
def test_market_session(rules, phase):
    if phase == Session.REGULAR:
        accept(order(kind="MARKET"), rules, phase)
    else:
        with pytest.raises(Unavailable):
            accept(order(kind="MARKET"), rules, phase)


@pytest.mark.parametrize("phase,session,allowed", [
    (Session.PRE_MARKET, "RTH", False), (Session.POST_MARKET, "EXTENDED", True),
    (Session.PRE_MARKET, "EXTENDED", True), (Session.OVERNIGHT, "24H", True),
    (Session.OVERNIGHT, "EXTENDED", False), (Session.CLOSED, "24H", False),
    (Session.UNKNOWN, "24H", False),
])
def test_limit_sessions(rules, phase, session, allowed):
    value = order(quantity=D("1"), trading_session=session)
    if allowed:
        accept(value, rules, phase)
    else:
        with pytest.raises(Unavailable):
            accept(value, rules, phase)


@pytest.mark.parametrize("change,phase", [
    (dict(tradability="NONE"), Session.REGULAR), (dict(fractionable=False), Session.REGULAR),
    (dict(fractionable_eh=False), Session.PRE_MARKET), (dict(extended=False), Session.POST_MARKET),
    (dict(overnight=False), Session.OVERNIGHT),
])
def test_exchange_capabilities(rules, change, phase):
    with pytest.raises(Unavailable):
        accept(order(trading_session="24H"), replace(rules, **change), phase)


def test_permission_and_order_limit(rules):
    with pytest.raises(Unavailable, match="PERMISSION"):
        admit(order(), rules, Session.REGULAR, price=D("150"), reference_price=D("150"))
    with pytest.raises(Unavailable, match="ORDER_LIMIT"):
        accept(order(), rules, open_orders=2)


@pytest.mark.parametrize("source,receive", [(None, NOW), (MS - 6000, NOW), (MS + 1, NOW),
    (MS, NOW - timedelta(seconds=1)), (MS, NOW + timedelta(seconds=1))])
def test_quote_time_policy(source, receive):
    with pytest.raises(Unavailable):
        fresh(source, receive, NOW, timedelta(seconds=5))


def test_both_times_checked():
    fresh(MS, NOW, NOW, timedelta(seconds=5))
    with pytest.raises(ValueError):
        fresh(MS, NOW.replace(tzinfo=None), NOW, timedelta(seconds=5))


def detail(**kwargs):
    row = dict(orderId="exchange-1", clientOrderId=CID, symbol="NVDA", quote="USD", side="BUY",
               status="PARTIALLY_FILLED", filledQty="0.2", filledTotal="30", fee="0.03", updatedAt=MS)
    row.update(kwargs)
    return Report.detail(row)


def test_partial_delta_is_not_cumulative_vwap():
    first = detail()
    second = detail(status="FILLED", filledQty="0.5", filledTotal="78", fee="0.078", updatedAt=MS+1)
    assert incremental(first, second) == (D("0.3"), D("160"), D("0.048"))
    assert incremental(second, second) == (0, None, 0)


@pytest.mark.parametrize("change", [dict(filledQty="0.1"), dict(filledTotal="29"), dict(fee="0.01"),
    dict(updatedAt=MS-1), dict(orderId="different"), dict(status="UNRECOGNIZED"),
    dict(status="REJECTED"), dict(symbol="TSLA")])
def test_query_conflicts(change):
    with pytest.raises(Unavailable):
        incremental(detail(), detail(**change))


def test_cancel_fill_race():
    canceled = detail(status="CANCELED")
    filled = detail(status="FILLED", filledQty="0.5", filledTotal="75", fee="0.075", updatedAt=MS+1)
    assert incremental(canceled, filled)[0] == D("0.3")
    with pytest.raises(Unavailable):
        incremental(filled, replace(canceled, updated_at=MS+2))


def test_stream_schema_unknown_fee_and_decimal_json():
    row = json.loads('{"e":"orderReport","i":"exchange-1","b":"EQ_NVDA","q":"USD",'
                     '"S":"buy","s":"partially_filled","fq":0.2,"FN":30,"tc":30.03,"U":1}', parse_float=D)
    report = Report.stream(row)
    assert report.filled == D("0.2") and report.symbol == "NVDA" and report.fee is None
    assert report.settlement is None
    row["b"] = "NVDAUSDT"
    with pytest.raises(ValueError):
        Report.stream(row)


def test_timeout_duplicates_and_reconnect():
    recovery = OrderRecovery()
    recovery.register(CID, "exchange-1")
    assert recovery.accept(CID, detail()) == (D("0.2"), D("150"), D("0.03"))
    assert recovery.accept(CID, detail()) is None
    recovery.disconnect()
    with pytest.raises(Unavailable):
        recovery.resubmit(CID)
    with pytest.raises(Unavailable):
        recovery.accept(CID, detail())
    recovery.accept(CID, detail(), query=True)
    assert not recovery.blocked
    with pytest.raises(Unavailable):
        recovery.accept(CID, detail(filledQty="0.1"), query=True)
    assert CID in recovery.blocked


def test_same_version_inconsistency_and_unknown_timeout():
    r = OrderRecovery()
    r.uncertain(CID)
    with pytest.raises(Unavailable):
        r.resubmit(CID)
    r.register(CID, "exchange-1")
    r.accept(CID, detail(), query=True)
    with pytest.raises(Unavailable):
        r.accept(CID, detail(filledQty="0.3", filledTotal="45", fee="0.045"), query=True)


def test_history_does_not_infer_fees_or_settlement():
    result = trades({"rows": [dict(executionId="fill-1", orderId="exchange-1", symbol="NVDA", quote="USD",
        side="BUY", qty="0.2", price="150", total="30", executionAt=MS)]})
    assert result[0]["quantity"] == D("0.2")
    assert result[0]["fee"] is None and result[0]["settlement"] is None


def test_offline_signature_and_redaction(monkeypatch):
    monkeypatch.setenv("BINANCE_EQUITY_API_KEY", "LocalExampleKey123")
    monkeypatch.setenv("BINANCE_EQUITY_API_SECRET", "LocalExampleSecret456")
    cred = Credentials.from_environment()
    request = cred.sign("POST", PREFIX + "order/place", order(kind="MARKET").params(), timestamp=MS)
    query, signature = request.query.rsplit("&signature=", 1)
    assert signature == hmac.new(cred.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    assert parse_qs(query)["tokenize"] == ["false"]
    assert cred.key not in repr(request) and cred.secret not in repr(cred)
    assert signature not in repr(request)


@pytest.mark.parametrize("operation,path", [("open", "order/open-orders"), ("detail", "order/detail"),
    ("orders", "order/history"), ("trades", "trade/history")])
def test_injected_signed_reads(operation, path):
    calls = []
    client = ReadClient(Credentials("examplekey", "examplesecret"), lambda r: calls.append(r) or {"rows": []})
    params = {"clientOrderId": CID} if operation == "detail" else (
        {"startTime": MS-1000, "endTime": MS, "current": 1, "size": 20} if operation in {"orders", "trades"} else {})
    client.read(operation, timestamp=MS, **params)
    assert calls[0].path == PREFIX + path and calls[0].method == "GET"


@pytest.mark.parametrize("failure", [PermissionError("secret"), TimeoutError("secret"), OSError("secret")])
def test_read_failures_do_not_leak_credentials(failure):
    def fail(_):
        raise failure
    with pytest.raises(Unavailable) as error:
        ReadClient(Credentials("examplekey", "examplesecret"), fail).read("open", timestamp=MS)
    assert "secret" not in str(error.value)


def test_echo_and_missing_account_endpoint():
    client = ReadClient(Credentials("examplekey", "examplesecret"), lambda _: {"msg": "examplekey"})
    with pytest.raises(Unavailable, match="DISCARDED"):
        client.read("open", timestamp=MS)
    with pytest.raises(Unavailable, match="ACCOUNT_READ_UNAVAILABLE"):
        client.read_account()


def test_cancel_template_and_history_field_names():
    creds = Credentials("examplekey", "examplesecret")
    request = creds.prepare_cancel("exchange-1", timestamp=MS)
    assert request.path == PREFIX + "order/cancel" and request.method == "POST"
    assert parse_qs(request.query)["orderId"] == ["exchange-1"]
    assert creds.prepare_order(order(kind="MARKET"), timestamp=MS).path == PREFIX + "order/place"
    client = ReadClient(creds, lambda _: {"rows": []})
    for params in ({}, {"startTime": MS, "endTime": MS, "size": 101},
                   {"startTime": MS, "endTime": MS, "page": 1}):
        with pytest.raises(ValueError):
            client.read("orders", timestamp=MS, **params)


@pytest.mark.parametrize("fn", [live_submit, live_cancel])
def test_live_mutations_unconditionally_disabled(fn):
    with pytest.raises(PermissionError, match="NO LIVE MONEY"):
        fn(network=True, live=True, tokenize=False)
