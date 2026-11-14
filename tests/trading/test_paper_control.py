from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from factorlab.trading import (
    Market, OrderEvent, OrderIntent, OrderStatus as S, OrderType, Side,
)
from factorlab.trading.control import RiskLimits, RiskRejected, TradingController
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
LIMITS = RiskLimits(D("1000"), D("2000"), D("3000"), 10, 5)


def intent(key="one", quantity="2", side=Side.BUY, market=Market.BINANCE_EQUITY, limit=None, reduce=False):
    return OrderIntent(market, "TEST", side, OrderType.MARKET if limit is None else OrderType.LIMIT,
                       D(quantity), None if limit is None else D(limit), key, reduce_only=reduce)


def controller(path=":memory:", *, market=Market.BINANCE_EQUITY, cash="10000", limits=LIMITS,
               broker_type=PaperBroker, clock=lambda: NOW, max_events=10000, max_orders=1000):
    currency = "USDT" if market == Market.BINANCE_USDM else "USD"
    broker = broker_type(market, "test", (Instrument("TEST", currency),), {currency: D(cash)})
    store = PaperStore(path, max_events=max_events, max_orders=max_orders)
    control = TradingController(broker, store, limits=limits, clock=clock)
    control.quote(Quote("TEST", D("100"), clock()))
    return control


def test_continuous_store_preserves_history_and_identity_across_reopen(tmp_path):
    path = tmp_path / 'continuous.sqlite'
    store = PaperStore(path, continuous_capacity=True)
    broker = PaperBroker(Market.BINANCE_EQUITY, 'test', (Instrument('TEST', 'USD'),),
                         {'USD': D(10000)})
    broker.state.orders.update({str(i): str(i) for i in range(1001)})
    broker.state.runtime['large'] = 'x' * (32 * 1024 * 1024)
    with store.transaction():
        store.save(broker.state)
        for i in range(10001):
            store.record_metadata(str(i), {'sequence': i})
    store.close()
    reopened = PaperStore(path, continuous_capacity=True)
    restored = reopened.load()
    assert len(restored.orders) == 1001
    assert len(restored.runtime['large']) == 32 * 1024 * 1024
    assert reopened.event_cursor() == 10001
    assert reopened.record_metadata('0', {'sequence': 0}) is False
    with pytest.raises(ValueError, match='different payload'):
        reopened.record_metadata('0', {'sequence': -1})
    with pytest.raises(ValueError, match='metadata too large'):
        reopened.record_metadata('oversize', 'x' * 8193)
    assert reopened.record_metadata('10001', {'sequence': 10001})
    reopened.close()
    bounded = PaperStore(path)
    with pytest.raises(ValueError, match='order storage budget'):
        bounded.save(restored)
    restored.orders.clear()
    with pytest.raises(ValueError, match='snapshot storage budget'):
        bounded.save(restored)
    with pytest.raises(ValueError, match='event storage budget'):
        bounded.record_metadata('new', {})
    bounded.close()


@pytest.mark.parametrize("market", list(Market))
def test_partial_fill_cancel_and_late_full_fill(market):
    c = controller(market=market)
    order = intent(market=market)
    accepted = c.submit(order)
    assert c.submit(order) == accepted
    assert len(c.broker.state.orders) == 1
    assert c.account().assets[0].reserved == D("200.200")
    partial = c.fill("one", D("0.5"))
    account = c.account()
    assert account.positions[0].quantity == D("0.5")
    assert c.replay(partial) is False
    assert c.account() == account
    canceled = c.cancel("one")
    assert canceled.status == S.CANCELED
    assert canceled.cumulative_filled_quantity == D("0.5")
    # A full execution already in flight can arrive after cancel confirmation.
    late = replace(canceled, status=S.FILLED, cumulative_filled_quantity=D("2"),
                   fill_price=D("100"), cumulative_fee=D("0.2"), event_id="late-fill")
    assert c.replay(late)
    account = c.reconcile()
    assert account.reconciled and not account.open_orders
    assert account.positions[0].quantity == D("2")
    assert c.broker.state.orders["one"].snapshot.cumulative_fee == D("0.2")
    expected_cash = D("9799.8") if market == Market.BINANCE_EQUITY else D("9999.8")
    assert account.assets[0].total == expected_cash
    assert account.assets[0].reserved == (D("0") if market == Market.BINANCE_EQUITY else D("200"))


def test_conflicting_order_and_event_ids_are_rejected():
    c = controller()
    accepted = c.submit(intent())
    with pytest.raises(ValueError, match="different intent"):
        c.submit(intent(quantity="3"))
    with pytest.raises(ValueError, match="different payload"):
        c.replay(replace(accepted, source="conflicting"))
    assert c.broker.state.orders["one"].snapshot.status == S.SUBMITTED


@pytest.mark.parametrize("change", [
    {"status": S.PARTIALLY_FILLED, "cumulative_filled_quantity": D("3"), "fill_price": D("100")},
    {"status": S.FILLED, "cumulative_filled_quantity": D("1"), "fill_price": D("100")},
    {"status": S.NEW},
    {"status": S.PARTIALLY_FILLED, "cumulative_filled_quantity": D("1")},
    {"exchange_order_id": "wrong"},
    {"event_time": NOW - timedelta(seconds=1)},
])
def test_invalid_events_rollback_journal_and_account(change):
    c = controller()
    accepted = c.submit(intent())
    before = c.account()
    with pytest.raises(ValueError):
        c.replay(replace(accepted, event_id="bad", **change))
    assert c.account() == before
    assert c.store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_fill_regression_fee_and_rejection_rules():
    c = controller()
    c.submit(intent())
    fill = c.fill("one", D("1"))
    with pytest.raises(ValueError, match="regression"):
        c.replay(replace(fill, event_id="regress", cumulative_filled_quantity=D("0.5")))
    with pytest.raises(ValueError, match="illegal"):
        c.reject("one")
    c.submit(intent("two"))
    assert c.reject("two").status == S.REJECTED
    with pytest.raises(ValueError):
        c.fill("two", D("1"))


def test_limit_execution_and_failed_fill_are_atomic():
    c = controller()
    c.submit(intent(limit="90"))
    with pytest.raises(ValueError, match="limit"):
        c.fill("one", D("1"))
    assert not c.account().positions
    assert c.fill("one", D("1"), D("90")).status == S.PARTIALLY_FILLED
    assert c.account().assets[0].total == D("9909.910")


class SubmitTimeout(PaperBroker):
    def submit_order(self, order):
        super().submit_order(order)
        raise TimeoutError("reply lost")


class UnconfirmedTimeout(PaperBroker):
    def submit_order(self, order):
        raise TimeoutError("no authoritative confirmation")


class CancelTimeout(PaperBroker):
    def cancel_order(self, order_id):
        self.fill(order_id, D("2"))
        raise TimeoutError("fill won cancel race; reply lost")


@pytest.mark.parametrize("broker_type", [SubmitTimeout, UnconfirmedTimeout])
def test_submit_timeout_restart_never_resubmits(tmp_path, broker_type):
    path = tmp_path / "paper.sqlite"
    c = controller(path, broker_type=broker_type)
    assert c.submit(intent()) is None
    assert c.account().open_orders[0].status == S.UNKNOWN
    assert not c.account().reconciled
    with pytest.raises(ValueError, match="RECONCILE_REQUIRED"):
        c.submit(intent())
    c.store.close()
    recovered = controller(path)
    with pytest.raises(ValueError, match="RECONCILE_REQUIRED"):
        recovered.submit(intent())
    with pytest.raises(RiskRejected, match="RECONCILE_REQUIRED"):
        recovered.submit(intent("replacement"))
    account = recovered.reconcile()
    if broker_type == SubmitTimeout:
        assert account.reconciled
        assert recovered.submit(intent()).status == S.SUBMITTED
    else:
        assert not account.reconciled
        assert account.open_orders[0].reconcile_required
    assert len(recovered.broker.state.orders) == 1


def test_cancel_timeout_fill_race_does_not_assume_cancellation():
    c = controller(broker_type=CancelTimeout)
    c.submit(intent())
    assert c.cancel("one") is None
    assert c.account().open_orders[0].status == S.UNKNOWN
    assert c.account().positions[0].quantity == D("2")
    account = c.reconcile()
    assert not account.open_orders and account.reconciled
    assert c.cancel("one").status == S.FILLED
    assert account.assets[0].total == D("9799.8")


def test_restart_retains_fills_fees_reservations_rate_and_kill_switch(tmp_path):
    path = tmp_path / "paper.sqlite"
    c = controller(path, limits=replace(LIMITS, orders_per_minute=1))
    c.submit(intent())
    c.fill("one", D("0.5"))
    before = c.account()
    c.set_kill_switch()
    c.store.close()
    recovered = controller(path, limits=replace(LIMITS, orders_per_minute=1))
    assert not recovered.account().reconciled
    after = recovered.reconcile()
    assert after.assets == before.assets and after.positions == before.positions
    assert after.open_orders[0].cumulative_fee == D("0.05")
    with pytest.raises(RiskRejected, match="KILL_SWITCH"):
        recovered.submit(intent("two"))
    recovered.set_kill_switch(False)
    with pytest.raises(RiskRejected, match="ORDER_RATE_LIMIT"):
        recovered.submit(intent("two"))
    assert recovered.cancel("one").status == S.CANCELED


@pytest.mark.parametrize("limits,cash,reason", [
    (LIMITS, "100", "INSUFFICIENT_BALANCE"),
    (replace(LIMITS, max_order_notional=D("100")), "10000", "ORDER_NOTIONAL_LIMIT"),
    (replace(LIMITS, max_instrument_exposure=D("100")), "10000", "INSTRUMENT_EXPOSURE_LIMIT"),
    (replace(LIMITS, account_budget=D("100")), "10000", "ACCOUNT_BUDGET_LIMIT"),
    (RiskLimits(), "10000", "ORDER_RATE_LIMIT"),
])
def test_fail_closed_risk(limits, cash, reason):
    c = controller(cash=cash, limits=limits)
    with pytest.raises(RiskRejected, match=reason):
        c.submit(intent())
    assert not c.broker.state.orders


def test_stale_quotes_and_pending_exposure():
    time = [NOW]
    c = controller(clock=lambda: time[0], limits=replace(LIMITS, max_instrument_exposure=D("300")))
    c.submit(intent())
    with pytest.raises(RiskRejected, match="INSTRUMENT_EXPOSURE_LIMIT"):
        c.submit(intent("two"))
    time[0] += timedelta(seconds=6)
    with pytest.raises(RiskRejected, match="STALE_QUOTE"):
        c.submit(intent("three"))
    c.quote(Quote("TEST", D("100"), time[0]))
    c.cancel("one")
    c.submit(intent("four"))


@pytest.mark.parametrize("market", list(Market))
def test_kill_switch_allows_cancel_and_owned_reduce_only(market):
    c = controller(market=market)
    c.submit(intent(market=market))
    c.fill("one", D("2"))
    c.submit(intent("pending", market=market))
    c.set_kill_switch()
    with pytest.raises(RiskRejected, match="KILL_SWITCH"):
        c.submit(intent("new", market=market))
    assert c.cancel("pending").status == S.CANCELED
    with pytest.raises(RiskRejected, match="INVALID_REDUCE_ONLY"):
        c.submit(intent("over", quantity="3", side=Side.SELL, market=market, reduce=True))
    c.submit(intent("exit", side=Side.SELL, market=market, reduce=True))
    with pytest.raises(RiskRejected, match="INVALID_REDUCE_ONLY"):
        c.submit(intent("double-exit", side=Side.SELL, market=market, reduce=True))
    c.fill("exit", D("2"), D("110"))
    account = c.reconcile()
    assert not account.positions and not account.open_orders
    assert account.assets[0].total == D("10019.58")
    assert account.assets[0].reserved == 0


def test_futures_short_realizes_pnl_without_crediting_sale_principal():
    c = controller(market=Market.BINANCE_USDM)
    c.submit(intent(side=Side.SELL, market=Market.BINANCE_USDM))
    c.fill("one", D("2"))
    assert c.account().assets[0].total == D("9999.8")
    assert c.account().assets[0].reserved == D("200")
    c.submit(intent("exit", side=Side.BUY, market=Market.BINANCE_USDM, reduce=True))
    c.fill("exit", D("2"), D("90"))
    assert c.account().assets[0].total == D("10019.62")
    assert not c.account().positions


def test_usd_and_usdt_never_net():
    broker = PaperBroker(Market.BINANCE_EQUITY, "isolated",
        (Instrument("TEST", "USD"),), {"USD": D("100"), "USDT": D("1000000")})
    c = TradingController(broker, PaperStore(":memory:"), limits=LIMITS, clock=lambda: NOW)
    c.quote(Quote("TEST", D("100"), NOW))
    with pytest.raises(RiskRejected, match="INSUFFICIENT_BALANCE"):
        c.submit(intent())
    with pytest.raises(RiskRejected, match="MISMATCH"):
        c.submit(intent(market=Market.BINANCE_USDM))


def test_default_prohibits_live_adapter_and_live_flag():
    store = PaperStore(":memory:")
    with pytest.raises(ValueError, match="NO LIVE MONEY"):
        TradingController(object(), store)
    c = controller()
    with pytest.raises(ValueError, match="NO LIVE MONEY"):
        TradingController(c.broker, store, live=True)


def test_metadata_capacity_rolls_back_without_duplicate_fills():
    c = controller(max_events=2)
    c.submit(intent())
    c.fill("one", D("1"))
    before = c.account()
    with pytest.raises(ValueError, match="event storage budget"):
        c.fill("one", D("1"))
    assert c.account() == before
    c2 = controller(max_orders=1)
    c2.submit(intent())
    with pytest.raises(ValueError, match="order storage budget"):
        c2.submit(intent("two"))
    assert len(c2.broker.state.orders) == 1


def test_insufficient_execution_price_rolls_back_cash_and_journal():
    c = controller(cash="210")
    c.submit(intent())
    before = c.account()
    with pytest.raises(ValueError, match="insufficient collateral"):
        c.fill("one", D("2"), D("200"))
    assert c.account() == before
    assert c.store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_pending_futures_closes_cannot_reuse_collateral():
    c = controller(market=Market.BINANCE_USDM)
    c.submit(intent(market=Market.BINANCE_USDM))
    c.fill("one", D("2"))
    c.submit(intent("first-close", side=Side.SELL, market=Market.BINANCE_USDM))
    assert c.account().assets[0].reserved == D("200")
    c.submit(intent("second-close", side=Side.SELL, market=Market.BINANCE_USDM))
    assert c.account().assets[0].reserved == D("400.2")
    c.fill("first-close", D("1"))
    assert c.account().assets[0].reserved == D("300.2")
    c.fill("first-close", D("1"))
    c.fill("second-close", D("2"))
    assert c.account().positions[0].quantity == D("-2")
    assert c.account().assets[0].reserved == D("200")


def test_duplicate_quantity_with_new_event_id_does_not_double_book():
    c = controller()
    c.submit(intent())
    event = c.fill("one", D("1"))
    before = c.account()
    assert c.replay(replace(event, event_id="same-fill-another-report"))
    assert c.account() == before


def test_cancel_failure_preserves_open_order_and_reservation():
    class FailedCancel(PaperBroker):
        def cancel_order(self, order_id):
            raise ValueError("cancel rejected")

    c = controller(broker_type=FailedCancel)
    c.submit(intent())
    before = c.account()
    with pytest.raises(ValueError, match="cancel rejected"):
        c.cancel("one")
    assert c.account() == before
    assert c.fill("one", D("2")).status == S.FILLED


def test_unconfirmed_timeout_can_be_resolved_only_by_confirmation():
    c = controller(broker_type=UnconfirmedTimeout)
    c.submit(intent())
    event = OrderEvent("one", "paper:one", S.SUBMITTED, D("0"), None, NOW, "paper", "confirmed-later")
    c.replay(event)
    assert not c.account().reconciled
    assert c.reconcile().reconciled
    assert c.submit(intent()) == event
    assert len(c.broker.state.orders) == 1


def test_snapshot_roundtrip_preserves_arbitrary_order_identity(tmp_path):
    path = tmp_path / "paper.sqlite"
    c = controller(path)
    c.submit(intent("type"))
    c.fill("type", D("0.5"))
    before = c.account()
    c.store.close()
    recovered = controller(path)
    after = recovered.reconcile()
    assert after.assets == before.assets and after.positions == before.positions
    assert after.open_orders == before.open_orders


def test_decimal_and_utc_contract_validation():
    with pytest.raises(ValueError, match="Decimal"):
        replace(intent(), quantity=2.0)
    with pytest.raises(ValueError, match="UTC"):
        Quote("TEST", D("100"), NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="GTC"):
        replace(intent(), time_in_force="IOC")
    with pytest.raises(ValueError, match="LIMIT"):
        replace(intent(), limit_price=D("100"))
