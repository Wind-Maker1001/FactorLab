"""Optional pre-posting policy preserves the non-strategy Paper contract."""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
from factorlab.trading.control import TradingController, RiskLimits
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def account(policy=None):
    store = PaperStore(':memory:')
    broker = PaperBroker(Market.BINANCE_EQUITY, 'paper', (Instrument('TEST', 'USD'),), {'USD': D(1000)})
    controller = TradingController(broker, store, clock=lambda: NOW, fill_admission=policy,
        limits=RiskLimits(D(1000), D(1000), D(1000), 10))
    controller.quote(Quote('TEST', D(100), NOW))
    controller.submit(OrderIntent(Market.BINANCE_EQUITY, 'TEST', Side.BUY,
        OrderType.MARKET, D(2), None, 'one'))
    return controller


def test_optional_policy_runs_after_authority_reload_before_sequence_or_fees():
    calls = []

    def admit(broker, order, quantity, price):
        calls.append((broker.state.cash['USD'], broker.state.sequence, order.intent.client_order_id, quantity, price))
        return min(quantity, D('.5'))

    controller = account(admit)
    # Local presentation is stale; policy must see the stored account.
    controller.broker.state.cash['USD'] = D(1)
    event = controller.fill('one', D(2), D(99))
    assert calls == [(D(1000), 1, 'one', D(2), D(99))]
    assert event.cumulative_filled_quantity == D('.5') and event.cumulative_fee == D('.0495')
    assert controller.broker.state.cash['USD'] == D('950.4505')
    # Historical confirmations are authoritative, not new simulated candidates.
    assert controller.replay(event) is False and len(calls) == 1
    controller.store.close()


def test_zero_admission_does_not_create_event_and_default_remains_full_fill():
    controller = account(lambda *_: D(0))
    before = controller.store.load()
    assert controller.fill('one', D(2)) is None
    assert controller.store.load() == before and controller.broker.state == before
    controller.store.close()
    controller = account()
    event = controller.fill('one', D(2))
    assert event.cumulative_filled_quantity == 2 and event.cumulative_fee == D('.2')
    controller.store.close()


def test_policy_cannot_enlarge_candidate_and_account_failure_rolls_back():
    controller = account(lambda *_: D(3))
    before = controller.store.load()
    with pytest.raises(ValueError, match='cannot increase'):
        controller.fill('one', D(1))
    assert controller.store.load() == before
    controller.broker.fill_admission = lambda _, __, quantity, ___: quantity
    with pytest.raises(ValueError, match='insufficient collateral'):
        controller.fill('one', D(2), D(1000))
    assert controller.store.load() == before and controller.broker.state == before
    controller.store.close()
