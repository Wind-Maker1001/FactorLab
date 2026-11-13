from dataclasses import replace, FrozenInstanceError
from datetime import timedelta
from decimal import Decimal as D

import pytest

from test_binance_demo_network import wired, make_demo, reauthorize
from test_binance_demo_execution import intent
from factorlab.integrations.binance_futures_trading.bounded_quote import (
    BoundedQuotePolicy, BoundedQuoteObservation, bounded_quote,
)
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked, Environment
from factorlab.integrations.binance_futures_trading.stream import UserDataEvents
from factorlab.trading.paper import Quote
from factorlab.trading import OrderStatus as S


@pytest.fixture(autouse=True)
def network_clock(monkeypatch):
    from datetime import datetime
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.network.datetime', Clock)


def setup(wired, **changes):
    a, g, c = make_demo(wired)
    values = dict(account_id=a.account_id, client_order_id='d03-bounded', quantity=D('1'),
        offset_bps=D('50'), min_price=D('98'), max_price=D('101'), max_ask=D('102'),
        max_notional=D('105'), max_fee=D('1'), max_funds=D('106'), fee_rate=D('.001'),
        expires_at=NOW+timedelta(seconds=60))
    values.update(changes)
    p = BoundedQuotePolicy(**values)
    reauthorize(a, order_types=frozenset({'LIMIT'}), quote_policy=p, max_orders=1, max_open_orders=1)
    g.clock = lambda: NOW
    o = BoundedQuoteObservation.capture(facts=a.facts, rules=g.rules, observed_at=NOW,
        mark=D('100'), bid=D('100'), ask=D('100.1'), mark_at=NOW, book_at=NOW)
    c.quote(Quote('NVDAUSDT', D('100.1'), NOW))
    return a, g, c, p, o


def candidate(g, a, o):
    return bounded_quote(a.client.transport.authorization.plan, o, facts=a.facts, rules=g.rules, now=NOW)


def writes(wired, method):
    return [r for r in wired[1] if r.get_method() == method]


def test_deterministic_versioned_hash_and_frozen_snapshot(wired):
    a,g,c,p,o = setup(wired)
    assert p.digest == replace(p, quantity=D('1.00')).digest
    assert hash(p) == hash(replace(p))
    assert candidate(g,a,o) == candidate(g,a,o)
    with pytest.raises(FrozenInstanceError):
        o.bid = D('101')
    with pytest.raises(ValueError):
        replace(p, version=2)


def test_tick_floor_exact(wired):
    a,g,c,p,o = setup(wired)
    order, receipt = candidate(g,a,replace(o,bid=D('100.013')))
    assert order.limit_price == D('99.51')
    assert order.limit_price % D('.01') == 0
    assert receipt['policy_digest'] == p.digest


def test_small_allowed_move_uses_formula_not_clamp(wired):
    a,g,c,p,o = setup(wired)
    assert candidate(g,a,replace(o,bid=D('100.2'),ask=D('100.3')))[0].limit_price == D('99.69')


def test_bid_lower_bound(wired):
    a,g,c,p,o = setup(wired)
    with pytest.raises(SafetyBlocked, match='PRICE_BOUNDS'):
        candidate(g,a,replace(o,bid=D('98')))


def test_ask_upper_bound(wired):
    a,g,c,p,o = setup(wired)
    with pytest.raises(SafetyBlocked, match='ASK_LIMIT'):
        candidate(g,a,replace(o,ask=D('102.01')))


def test_mark_band(wired):
    a,g,c,p,o = setup(wired)
    with pytest.raises(SafetyBlocked, match='MARK_BAND'):
        candidate(g,a,replace(o,mark=D('110')))


def test_fixed_quantity_min_notional(wired):
    a,g,c,p,o = setup(wired,quantity=D('.01'))
    with pytest.raises(SafetyBlocked, match='MIN_NOTIONAL'):
        candidate(g,a,o)


def test_occupied_funds_include_fee(wired):
    a,g,c,p,o = setup(wired,max_funds=D('100.15'))
    with pytest.raises(SafetyBlocked, match='BUDGET') as error:
        candidate(g,a,o)
    assert error.value.receipt['funds'] == '100.2001'
    assert not writes(wired,'POST')


def test_five_second_freshness_at_dispatch(wired):
    a,g,c,p,o = setup(wired)
    order = g.prepare_bounded_quote(o)
    g.clock = lambda: NOW+timedelta(seconds=5,microseconds=1)
    with pytest.raises(SafetyBlocked, match='STALE'):
        c.submit(order)
    assert not writes(wired,'POST')


def test_account_mismatch(wired):
    a,g,c,p,o = setup(wired)
    with pytest.raises(SafetyBlocked, match='ACCOUNT_MISMATCH'):
        candidate(g,a,replace(o,account_id='other'))


def test_expiry(wired):
    a,g,c,p,o = setup(wired,expires_at=NOW)
    with pytest.raises(SafetyBlocked, match='EXPIRED'):
        candidate(g,a,o)


def test_duplicate_id_no_requote(wired):
    a,g,c,p,o = setup(wired)
    c.submit(g.prepare_bounded_quote(o))
    with pytest.raises(SafetyBlocked, match='consumed'):
        g.prepare_bounded_quote(replace(o,bid=D('100.2'),ask=D('100.3')))
    assert len(writes(wired,'POST')) == 1


def test_lost_post_no_retry_after_recovery(wired):
    a,g,c,p,o = setup(wired)
    order = g.prepare_bounded_quote(o)
    wired[0].timeout_next = True
    assert c.submit(order) is None
    assert a.order(p.client_order_id).reconcile_required
    g.recover(c)
    with pytest.raises(SafetyBlocked, match='consumed'):
        g.prepare_bounded_quote(o)
    assert len(writes(wired,'POST')) == 1


def test_cancel_restores_reserves_and_is_one_shot(wired):
    a,g,c,p,o = setup(wired)
    before = c.account().assets[0].available
    c.submit(g.prepare_bounded_quote(o))
    assert c.account().assets[0].available < before
    assert c.cancel(p.client_order_id).status == S.CANCELED
    assert c.account().assets[0].available == before
    assert len(writes(wired,'DELETE')) == 1
    assert a.db.execute('SELECT id FROM demo_cancel_attempts').fetchone()[0] == p.client_order_id


def test_lost_delete_reconciles_live_order_without_retry(wired, monkeypatch):
    a,g,c,p,o = setup(wired)
    c.submit(g.prepare_bounded_quote(o))
    fake = wired[0]
    original = type(fake).__call__

    def lose_delete(self, request, timeout):
        if request.method == 'DELETE':
            raise TimeoutError('synthetic cancellation outcome unknown')
        return original(self, request, timeout)

    monkeypatch.setattr(type(fake), '__call__', lose_delete)
    assert c.cancel(p.client_order_id) is None
    assert a.order(p.client_order_id).reconcile_required
    g.recover(c)
    assert a.order(p.client_order_id).status == S.SUBMITTED
    with pytest.raises(SafetyBlocked, match='DELETE opportunity already consumed'):
        c.cancel(p.client_order_id)
    assert len(writes(wired,'DELETE')) == 1
    assert len(writes(wired,'POST')) == 1


def test_rejection_receipt_identifies_prices_and_preserves_prepost_requote(wired):
    import json
    a,g,c,p,o = setup(wired)
    with pytest.raises(SafetyBlocked) as error:
        g.prepare_bounded_quote(replace(o, ask=D('102.01')))
    receipt = error.value.receipt
    assert receipt['failed_condition'] == 'ask <= max_ask'
    assert receipt['ask'] == '102.01'
    assert receipt['candidate'] == '99.50'
    assert receipt['mark_band'] == ['97.00', '103.00']
    assert receipt['actual_notional'] == '99.50'
    assert receipt['notional'] == '102.01'
    assert receipt['funds'] == '102.11201'
    assert receipt['dispatch'] == 'NOT_DISPATCHED'
    saved = a.db.execute('SELECT receipt FROM demo_quote_rejections WHERE id=?', (p.client_order_id,)).fetchone()[0]
    assert json.loads(saved) == receipt
    assert g.prepare_bounded_quote(o).limit_price == D('99.50')
    assert not writes(wired, 'POST')


def test_stale_source_cannot_be_refreshed_by_local_capture_time(wired):
    a,g,c,p,o = setup(wired)
    with pytest.raises(SafetyBlocked, match='STALE_QUOTE'):
        candidate(g,a,replace(o, book_at=NOW-timedelta(seconds=6)))


def test_crossed_book_is_not_a_valid_observation(wired):
    a,g,c,p,o = setup(wired)
    with pytest.raises(ValueError, match='uncrossed'):
        replace(o, bid=D('101'))


def test_partial_fill_inventory_retained(wired):
    a,g,c,p,o = setup(wired)
    c.submit(g.prepare_bounded_quote(o))
    c.replay(UserDataEvents(a).parse(wired[0].trade_update(p.client_order_id,'.5')))
    c.cancel(p.client_order_id)
    assert c.account().positions[0].quantity == D('.5')
    assert c.account().assets[0].available < D('1000')


def test_production_blocked(wired):
    a,g,c,p,o = setup(wired)
    with pytest.raises(ValueError):
        replace(a.client.transport.authorization.plan,environment=Environment.PRODUCTION)
    assert not writes(wired,'POST')


def test_fixed_limit_compatible(wired):
    a,g,c = make_demo(wired)
    assert c.submit(intent('d03-fixed',limit='99')).status == S.SUBMITTED
    assert c.cancel('d03-fixed').status == S.CANCELED


def test_unauthorized_network_post_delete_zero(wired):
    a,_,_ = make_demo(wired,authorized=False)
    for method in ('POST','DELETE'):
        with pytest.raises(SafetyBlocked):
            a.client.request(method,'/fapi/v1/order')
    assert not writes(wired,'POST') and not writes(wired,'DELETE')
