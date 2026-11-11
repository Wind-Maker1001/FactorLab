"""Integration with the shared P7-T01 trading contract."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side, OrderStatus, AssetBalance
from factorlab.trading.control import TradingController, RiskLimits, RiskRejected
from factorlab.trading.paper import Instrument, Quote
from factorlab.trading.storage import PaperStore
from factorlab.integrations.binance_equity.market import stream_rows, rest_quote
from factorlab.integrations.binance_equity_trading.paper import StockAdapter, StockPaperBroker, Observations
from factorlab.integrations.binance_equity_trading.protocol import Rules, Session, Unavailable
from factorlab.integrations.binance_equity_trading.reports import Report, contract_event

NOW = datetime(2026, 11, 10, 15, tzinfo=timezone.utc)
MS = int(NOW.timestamp()*1000)
CID = "nvda-order-00000000000000000000001"


@pytest.fixture
def setup():
    rules = Rules.from_exchange_info(dict(symbol="NVDA", tradability="BUY_SELL",
        stepSize="0.001", minQty="0.001", maxQty="1000", minNotional="1", maxNotional="100000",
        fractionable=True, fractionableEh=True, extendedSession=True, overnightSupported=True,
        maxNumOrders=100, multiplierDown="0.5", multiplierUp="1.5"))
    obs = Observations(rules, Session.REGULAR, D("150"), D("150"), MS, NOW, MS, NOW, MS, NOW, True)
    broker = StockPaperBroker("synthetic", (Instrument("NVDA", "USD"),), {"USD": D("1000")}, fee_rate=D("0.001"))
    store = PaperStore(":memory:")
    controller = TradingController(broker, store, clock=lambda: NOW,
        limits=RiskLimits(D("500"), D("1000"), D("1000"), 10))
    controller.quote(Quote("NVDA", D("150"), NOW))
    adapter = StockAdapter(controller)
    intent = OrderIntent(Market.BINANCE_EQUITY, "NVDA", Side.BUY, OrderType.MARKET, D("0.5"), None, CID)
    broker.bind(intent, obs, notional=D("75"))
    yield adapter, broker, controller, intent, obs
    store.db.close()


def test_end_to_end_demo():
    from factorlab.integrations.binance_equity_trading.demo import run
    result = run()
    assert result["risk"] == "APPROVED" and result["fill"] == "FILLED"
    assert D(result["cash"]) == D("924.925") and D(result["shares"]) == D("0.5")
    assert D(result["fee"]) == D("0.075") and result["reconciled"]
    assert result["settlement"] is None and result["request"]["tokenize"] == "false"


def test_partial_cancel_duplicate_and_cash_reconciliation(setup):
    adapter, broker, c, intent, obs = setup
    submitted = adapter.submit_order(intent)
    assert adapter.submit_order(intent) == submitted
    assert adapter.read_account().assets[0].reserved == D("75.075")
    partial = c.fill(CID, D("0.2"))
    before = adapter.read_account()
    assert c.replay(partial) is False
    assert adapter.read_account() == before
    assert adapter.query_order(CID).status == OrderStatus.PARTIALLY_FILLED
    assert adapter.cancel_order(CID).status == OrderStatus.CANCELED
    account = adapter.read_account()
    assert account.assets[0].reserved == 0 and account.assets[0].total == D("969.97")
    assert broker.synthetic_holdings() == ({"symbol": "NVDA", "quantity": D("0.2"), "sellable": D("0.2"), "settlement": None},)
    assert adapter.reconcile(account).reconciled


def test_inventory_reservation_and_sell(setup):
    adapter, broker, c, intent, obs = setup
    adapter.submit_order(intent)
    c.fill(CID, D("0.5"))
    sell = replace(intent, side=Side.SELL, quantity=D("0.2"), client_order_id="nvda-sell-000000000000000000000001")
    broker.bind(sell, obs)
    adapter.submit_order(sell)
    assert broker.synthetic_holdings()[0]["sellable"] == D("0.3")
    c.fill(sell.client_order_id, D("0.2"))
    assert adapter.read_account().assets[0].total == D("954.895")
    assert broker.synthetic_holdings()[0]["quantity"] == D("0.3")
    too_many = replace(sell, quantity=D("0.4"), client_order_id="nvda-sell-000000000000000000000002")
    broker.bind(too_many, obs)
    with pytest.raises(RiskRejected, match="INVENTORY"):
        adapter.submit_order(too_many)


def test_notional_context_idempotency_and_price_policy(setup):
    adapter, broker, c, intent, obs = setup
    with pytest.raises(ValueError, match="notional"):
        broker.bind(intent, obs, notional=D("76"))
    adapter.submit_order(intent)
    with pytest.raises(Unavailable, match="FIXED_PRICE"):
        c.fill(CID, D("0.2"), D("151"))
    assert adapter.read_account().assets[0].total == D("1000")
    with pytest.raises(ValueError):
        broker.bind(replace(intent, quantity=D("1")), obs, notional=D("150"))


def test_timeout_query_only_and_recovery(setup, monkeypatch):
    adapter, broker, c, intent, obs = setup
    original = broker.submit_order
    def lost_reply(value):
        original(value)
        raise TimeoutError("simulated lost acknowledgement")
    monkeypatch.setattr(broker, "submit_order", lost_reply)
    assert adapter.submit_order(intent) is None
    assert adapter.read_account().open_orders[0].status == OrderStatus.UNKNOWN
    with pytest.raises(ValueError, match="RECONCILE"):
        adapter.submit_order(intent)
    assert adapter.query_order(CID).status == OrderStatus.SUBMITTED
    assert c.reconcile().reconciled
    assert adapter.submit_order(intent).status == OrderStatus.SUBMITTED
    assert len(broker.state.orders) == 1


def test_disconnect_external_snapshot_and_cash_conflict(setup):
    adapter, broker, c, intent, obs = setup
    adapter.submit_order(intent)
    authority = adapter.read_account()
    adapter.disconnect()
    assert not adapter.read_account().reconciled
    assert adapter.reconcile(authority).reconciled
    assert broker.state.kill_switch
    wrong = replace(authority, assets=(AssetBalance("USD", D("999"), D("75.075")),))
    with pytest.raises(Unavailable, match="CONFLICT"):
        adapter.reconcile(wrong)
    assert not adapter.read_account().reconciled


def test_restart_uses_existing_shared_store_and_never_rebinds(setup):
    adapter, broker, c, intent, obs = setup
    adapter.submit_order(intent)
    restored_broker = StockPaperBroker("synthetic", (Instrument("NVDA", "USD"),), {"USD": D("1000")}, fee_rate=D("0.001"))
    restored = TradingController(restored_broker, c.store, clock=lambda: NOW, limits=c.limits)
    assert not restored.account().reconciled
    with pytest.raises(Unavailable, match="QUERY_ONLY"):
        restored_broker.bind(intent, obs, notional=D("75"))
    assert restored.reconcile().reconciled
    assert restored.submit(intent).status == OrderStatus.SUBMITTED
    with pytest.raises(Unavailable, match="QUERY_ONLY"):
        restored.fill(CID, D("0.1"))


def test_rule_and_quote_gates_before_simulation(setup):
    adapter, broker, c, intent, obs = setup
    for change in (dict(permission=False), dict(phase=Session.UNKNOWN)):
        broker.bind(intent, replace(obs, **change), notional=D("75"))
        with pytest.raises(Unavailable):
            adapter.submit_order(intent)
        assert not adapter.read_account().open_orders
    with pytest.raises(Unavailable):
        broker.bind(intent, replace(obs, quote_time=None), notional=D("75"))
    with pytest.raises(Unavailable):
        broker.bind(intent, replace(obs, rules_time=None), notional=D("75"))


def test_existing_quote_normalizers_and_missing_rest_time(setup):
    _, _, _, _, obs = setup
    _, q = stream_rows(dict(e="quote", s="NVDA", E=MS, T=MS, bp="149.99", ap="150", bs="1", **{"as":"1"}), NOW.isoformat(), "synthetic:q")
    _, s = stream_rows(dict(e="calendar", ts=MS, **{"from":"PRE", "to":"O"}), NOW.isoformat(), "synthetic:s")
    args = dict(rules_time=MS, rules_received_at=NOW, reference_price=D("150"), permission=True)
    result = Observations.from_equity(q[0], s[0], obs.rules, **args)
    result.validate(NOW)
    assert result.price == D("150")
    assert Observations.from_equity(q[0], s[0], obs.rules, side="SELL", **args).price == D("149.99")
    rest = rest_quote(dict(symbol="NVDA", bidPrice="149.99", askPrice="150", bidSize="1", askSize="1"), "NVDA", NOW.isoformat(), "synthetic:rest")[0]
    with pytest.raises(Unavailable, match="SOURCE_TIME"):
        Observations.from_equity(rest, s[0], obs.rules, **args).validate(NOW)
    with pytest.raises(ValueError, match="identity"):
        Observations.from_equity(dict(q[0], asset_class="TRADIFI_PERPETUAL"), s[0], obs.rules, **args)


def test_contract_report_export_notional_mismatch_and_missing_fee(setup):
    _, _, _, intent, _ = setup
    report = Report("e1", "FILLED", D("0.5"), D("75"), D("0.075"), MS, "NVDA", "BUY", "USD", CID)
    event = contract_event(intent, report)
    assert event.fill_price == D("150") and event.cumulative_fee == D("0.075")
    with pytest.raises(Unavailable, match="NOTIONAL_FILL"):
        contract_event(intent, replace(report, filled=D("0.6")))
    with pytest.raises(Unavailable, match="FEE_REQUIRED"):
        contract_event(intent, replace(report, fee=None))
