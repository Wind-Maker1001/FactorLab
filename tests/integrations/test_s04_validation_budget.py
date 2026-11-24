"""Bounded validation entry/exit admission and authoritative fill evidence."""
from decimal import Decimal as D
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from test_unified_cloud_strategy_s04 import harness, build_decision_package, EXAMPLES, promote_candidate
from test_binance_demo_grid_network import network_grid, writes
from test_binance_demo_network import wired
from factorlab.trading.contracts import OrderStatus, Side

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('s04_validation', ROOT / 'tools/validate_p8_s04_demo.py')
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


def planning_health(posts=2, notional='100'):
    return dict(global_budget=dict(state='RUNNING', remaining_posts=posts, remaining_notional=notional, opening_equity='100', max_loss='10'),
                campaign=dict(remaining_posts=4), account_recovered=True, unknown_count=0,
                open_orders=[], positions=[], equity='100')


RULES = dict(filters=[dict(filterType='PRICE_FILTER', tickSize='.01', minPrice='.01', maxPrice='1000'),
    dict(filterType='LOT_SIZE', stepSize='.001', minQty='.001', maxQty='1000'),
    dict(filterType='MIN_NOTIONAL', notional='5')])


@pytest.mark.parametrize('posts,mode', [(0, 'ZERO_POST_SWITCH_ONLY'), (1, 'ZERO_POST_SWITCH_ONLY'),
    (2, 'ROUNDTRIP_2_POST'), (3, 'ROUNDTRIP_2_POST'), (4, 'ROUNDTRIP_2_POST')])
def test_budget_profiles(posts, mode):
    assert validation.acceptance_plan(planning_health(posts), RULES, dict(askPrice='150'))['mode'] == mode


@pytest.mark.parametrize('change', [dict(global_budget=dict(state='RUNNING', remaining_posts=10, remaining_notional='7')),
    dict(positions=[dict(quantity='.01')]), dict(unknown_count=1), dict(account_recovered=False),
    dict(equity='1'), dict(equity='90'), dict(campaign=dict(remaining_posts=1))])
def test_notional_risk_and_account_prevent_entry(change):
    health = planning_health(10)
    health.update(change)
    assert validation.acceptance_plan(health, RULES, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'


def test_each_phase_replans_instead_of_spending_last_post():
    assert validation.acceptance_plan(planning_health(3), RULES, dict(askPrice='150'))['mode'] == 'ROUNDTRIP_2_POST'
    assert validation.acceptance_plan(planning_health(1), RULES, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'


def install_cycle(tmp_path, harness, *, mode='buy_then_cancel'):
    archive, manifest = build_decision_package(tmp_path, 'budget-cycle', EXAMPLES,
        strategy_id='limit_cycle', strategy_version='1.0.2', entrypoint='limit_cycle:LimitCycle',
        files=['limit_cycle.py'], configuration=dict(symbol='NVDAUSDT', interval_seconds=60,
        quantity='.04', tick='.01', buy_offset='-2', sell_offset='-.30', mode=mode,
        cancel_after_quotes=2, version='1.0.2', strategy_id='limit_cycle',
        max_entry_price='175', max_exit_price='175', exit_budget_notional='7'), execution_mode='orders')
    harness.install(archive)
    return manifest


def seed_posts(runtime, count):
    import json
    campaign = runtime.active_campaign()
    with runtime.db:
        runtime.db.executemany('INSERT INTO dispatches VALUES (?, ?, ?, ?, ?)',
            [(campaign['id'], 'POST', f'old-{i}', json.dumps(dict(quantity='.04', price='150')), None)
             for i in range(count)])


def test_single_remaining_post_is_refused_atomically(tmp_path, harness):
    manifest = install_cycle(tmp_path, harness)
    with harness.open() as router:
        seed_posts(router.bridge.runtime, 99)
        router.store.request(manifest, promote_candidate(manifest, 'budget-one'))
        router.before_poll()
        harness.set_time(1, 1)
        from factorlab.integrations.binance_futures_trading.demo_market_feed import FeedPaused
        with pytest.raises(FeedPaused, match='reserved for exit'):
            harness.quote(router, '150')
        assert writes(harness.requests) == []
        assert router.bridge.runtime.consumption(router.bridge.runtime.continuous_authorization())['posts'] == 99


@pytest.mark.parametrize('filled', ['.04', '.02'])
def test_resting_fill_and_canceled_partial_have_reserved_reduce_only_exit(tmp_path, harness, filled):
    manifest = install_cycle(tmp_path, harness)
    with harness.open() as router:
        seed_posts(router.bridge.runtime, 98)
        router.store.request(manifest, promote_candidate(manifest, 'budget-two'))
        router.before_poll()
        harness.set_time(1, 1)
        harness.quote(router, '150')
        entry_id = writes(harness.requests)[0]['newClientOrderId'][0]
        assert router.bridge.runtime.db.execute('SELECT exit_id FROM exit_budget_reservations').fetchone()[0].endswith('-exit')
        # Another admitted strategy cannot consume the one remaining reserved POST.
        from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
        params = {key: value[0] for key, value in writes(harness.requests)[0].items()}
        campaign = router.bridge.runtime.active_campaign()
        params['newClientOrderId'] = campaign['prefix'] + '-competitor'
        with pytest.raises(SafetyBlocked, match='reserved for exit'):
            router.bridge.runtime.reserve(campaign, 'POST', params,
                router.bridge.session.client.transport.authorization.plan,
                expected=(params['newClientOrderId'], 'BUY', D('.04'), 'false'),
                facts=router.bridge.session.adapter.facts)
        harness.fake.trade_update(entry_id, filled, '148')
        harness.set_time(1, 2)
        router.before_poll()
        harness.quote(router, '148')
        if filled == '.02':
            harness.set_time(1, 3)
            router.before_poll()
            harness.quote(router, '148')
            harness.set_time(1, 4)
            router.before_poll()
            harness.quote(router, '148')
        posts = writes(harness.requests)
        assert len(posts) == 2
        assert posts[1]['side'] == ['SELL']
        assert posts[1]['reduceOnly'] == ['true']
        assert D(posts[1]['quantity'][0]) == D(filled)
        assert router.bridge.runtime.db.execute('SELECT count(*) FROM exit_budget_reservations').fetchone()[0] == 0
        # A still-open exit is not a flat account or a completed rollback.
        assert router.bridge.session.status()['positions'][0]['quantity'] == D(filled)
        exit_id = posts[1]['newClientOrderId'][0]
        harness.fake.trade_update(exit_id, filled, '147.70')
        harness.set_time(1, 5)
        router.before_poll()
        harness.quote(router, '148')
        assert not router.bridge.session.status()['positions']
        assert len(writes(harness.requests)) == 2
        import json
        db = router.bridge.session.adapter.db
        receipts = [json.loads(raw) for raw, in db.execute('SELECT payload FROM futures_receipts ORDER BY rowid')]
        result = validation.real_fill_evidence(router.bridge.gateway.state.orders, receipts,
                                              entry_id, exit_id, inventory='0')
        assert result['verified']


def evidence_fixture(*, filled='.04', exit_status=OrderStatus.FILLED, reduce_only=True):
    orders = {}
    receipts = []
    for key, side, status, reduce in [('v-entry', Side.BUY, OrderStatus.FILLED, False),
                                       ('v-exit', Side.SELL, exit_status, reduce_only)]:
        orders[key] = NS(snapshot=NS(status=status, reconcile_required=False,
            intent=NS(side=side, reduce_only=reduce, instrument_id='NVDAUSDT'), cumulative_filled_quantity=D(filled),
            cumulative_fee=D('.001') if D(filled) else D(0), exchange_order_id=key))
        receipts.append(dict(order=dict(clientOrderId=key, orderId=key, status=status.value, executedQty=filled, side=side.value, symbol='NVDAUSDT'),
            trades=[] if D(filled) == 0 else [dict(id=key, orderId=key, side=side.value, qty=filled, symbol='NVDAUSDT',
                                                 commission='.001', commissionAsset='USDT')]))
    return orders, receipts


@pytest.mark.parametrize('filled,unknown,positions,expected', [('0', 0, [], True), ('.01', 0, [], False),
    ('0', 1, [], False), ('0', 0, [dict(quantity='.01')], False)])
def test_budget_race_downgrades_only_reconciled_zero_fill(filled, unknown, positions, expected):
    health = planning_health(1)
    health.update(unknown_count=unknown, positions=positions)
    assert validation.can_downgrade_after_timeout(health, dict(mode='ZERO_POST_SWITCH_ONLY'),
        dict(actual_filled_quantity=filled)) is expected


def test_two_unfilled_orders_never_verify_real_fill():
    orders, receipts = evidence_fixture(filled='0', exit_status=OrderStatus.CANCELED)
    assert not validation.real_fill_evidence(orders, receipts, 'v-entry', 'v-exit', inventory='0')['verified']


def test_authoritative_buy_and_reduce_only_sell_with_fees_verify():
    orders, receipts = evidence_fixture()
    result = validation.real_fill_evidence(orders, receipts, 'v-entry', 'v-exit', inventory='0')
    assert result['verified']
    assert result['fills']['v-entry']['commissions'] == [dict(amount='.001', asset='USDT')]


@pytest.mark.parametrize('mutation', ['wrong_order', 'no_fee', 'wrong_quantity', 'not_reduce_only', 'residual'])
def test_evidence_rejects_unconfirmed_or_mismatched_roundtrip(mutation):
    orders, receipts = evidence_fixture()
    inventory = '0'
    if mutation == 'wrong_order': receipts[1]['trades'][0]['orderId'] = 'other'
    elif mutation == 'no_fee': del receipts[1]['trades'][0]['commission']
    elif mutation == 'wrong_quantity': receipts[1]['trades'][0]['qty'] = '.03'
    elif mutation == 'not_reduce_only': orders['v-exit'].snapshot.intent.reduce_only = False
    else: inventory = '.001'
    assert not validation.real_fill_evidence(orders, receipts, 'v-entry', 'v-exit', inventory=inventory)['verified']
