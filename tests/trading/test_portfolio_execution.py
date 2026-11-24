from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from factorlab.trading.contracts import (
    AccountSnapshot, AssetBalance, Market, OrderEvent, OrderSnapshot, OrderStatus as S,
    Position, Side,
)
from factorlab.trading.portfolio_execution import (
    ExecutionQuote, InstrumentRules, OrderAbsent, PortfolioExecutor, PortfolioStore,
)

NOW = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)
SYMBOLS = ('AAPL', 'SNDK', 'BA', 'C', 'JPM', 'GE', 'TQQQ', 'QQQ')


class Broker:
    def __init__(self, cash=D(100), positions=None):
        self.cash = cash
        self.positions = dict(positions or {})
        self.orders = {}
        self.events = {}
        self.posts = []
        self.queries = []
        self.mode = 'fill'
        self.reserved = D(0)
        self.foreign = ()
        self.crash_after_post = False
        self.absent_after_post = False

    def read_account(self):
        return AccountSnapshot(Market.BINANCE_EQUITY, 'paper',
            (AssetBalance('USD', self.cash, self.reserved),),
            tuple(Position(s, q, D(10), D(10), 'USD') for s, q in self.positions.items() if q),
            tuple(OrderSnapshot(i, self.events[k].status,
                self.events[k].cumulative_filled_quantity) for k, i in self.orders.items()
                if self.events[k].status not in {S.FILLED, S.REJECTED, S.CANCELED}) + self.foreign,
            NOW, True)

    def query_order(self, identity):
        self.queries.append(identity)
        if identity not in self.events or self.absent_after_post:
            raise OrderAbsent(identity)
        return self.events[identity]

    def submit_order(self, intent):
        assert intent.client_order_id not in self.orders
        self.posts.append(intent)
        self.orders[intent.client_order_id] = intent
        if self.mode == 'reject':
            event = self.confirm(intent.client_order_id, D(0), S.REJECTED)
        elif self.mode == 'partial':
            event = self.confirm(intent.client_order_id, intent.quantity / 2, S.PARTIALLY_FILLED)
        elif self.mode == 'pending':
            event = self.confirm(intent.client_order_id, D(0), S.SUBMITTED)
        else:
            event = self.confirm(intent.client_order_id, intent.quantity, S.FILLED)
        if self.crash_after_post:
            raise SystemExit('process died after POST')
        if self.mode == 'timeout':
            raise TimeoutError('response lost')
        return event

    def cancel_order(self, identity):
        return self.confirm(identity, self.events[identity].cumulative_filled_quantity, S.CANCELED)

    def confirm(self, identity, quantity, status):
        intent = self.orders[identity]
        previous = self.events.get(identity)
        delta = quantity - (previous.cumulative_filled_quantity if previous else D(0))
        signed = delta if intent.side == Side.BUY else -delta
        self.positions[intent.instrument_id] = self.positions.get(intent.instrument_id, D(0)) + signed
        self.cash -= signed * intent.limit_price
        assert self.positions[intent.instrument_id] >= 0
        assert self.cash >= 0
        event = OrderEvent(identity, 'broker-' + identity, status, quantity,
            intent.limit_price if delta else None, NOW, 'test broker',
            f'{identity}:{status}:{quantity}', D(0))
        self.events[identity] = event
        return event


@pytest.fixture
def setup(tmp_path):
    broker = Broker()
    store = PortfolioStore(tmp_path / 'portfolio.sqlite')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    yield broker, store, executor
    store.close()


def create(executor, weights=None, symbols=SYMBOLS, initial=None, **kwargs):
    return executor.create(account_id='paper', strategy_id='test', strategy_version='1',
        trading_session='2026-11-24', weights=weights or {'AAPL': D('.5'), 'QQQ': D('.5')},
        owned_symbols=symbols, now=NOW, initial_inventory=initial, **kwargs)


def run(executor, identity, prices=None, rules=None, **kwargs):
    prices = {s: D(10) for s in SYMBOLS} if prices is None else prices
    quotes = {s: ExecutionQuote(s, p, p, NOW, NOW, '2026-11-24', 'test') for s, p in prices.items()}
    return executor.run_once(identity, equity=D(100), quotes=quotes,
        rules=rules or {s: InstrumentRules(D('.001'), D(1)) for s in SYMBOLS}, now=NOW, **kwargs)


def test_eight_symbols_share_one_cash_and_nav(setup):
    broker, store, executor = setup
    identity = create(executor, {s: D('.125') for s in SYMBOLS})
    run(executor, identity)
    result = run(executor, identity)
    assert result.phase == 'COMPLETE'
    assert len(broker.posts) == 8
    assert broker.cash == 0
    assert set(broker.positions.values()) == {D('1.25')}
    assert sum(i.quantity * i.limit_price for i in broker.posts) == 100
    assert store.plan(identity)['request']['weights'] == {s: D('.125') for s in SYMBOLS}


def test_sell_then_realized_cash_funds_buys(setup):
    broker, store, executor = setup
    broker.cash, broker.positions = D(0), {'AAPL': D(10)}
    identity = create(executor, initial={'AAPL': (D(10), D(10))})
    run(executor, identity)
    assert [(i.instrument_id, i.side, i.quantity) for i in broker.posts] == [('AAPL', Side.SELL, D(5))]
    assert broker.cash == 50
    run(executor, identity)
    assert broker.posts[-1].side == Side.BUY and broker.posts[-1].instrument_id == 'QQQ'
    assert broker.cash == 0
    assert run(executor, identity).phase == 'COMPLETE'


def test_partial_sells_and_restart_never_spend_anticipated_cash(setup):
    broker, store, executor = setup
    broker.cash, broker.positions, broker.mode = D(0), {'AAPL': D(10)}, 'partial'
    identity = create(executor, initial={'AAPL': (D(10), D(10))})
    run(executor, identity)
    restored = PortfolioExecutor(broker, store, clock=lambda: NOW)
    assert run(restored, identity).phase == 'WAITING'
    assert len(broker.posts) == 1
    order = broker.posts[0]
    broker.confirm(order.client_order_id, order.quantity, S.FILLED)
    broker.mode = 'fill'
    run(restored, identity)
    assert len(broker.posts) == 2 and broker.cash == 0
    assert broker.positions == {'AAPL': D(5), 'QQQ': D(5)}


def test_lost_post_response_queries_identity_without_duplicate(setup):
    broker, store, executor = setup
    broker.mode = 'timeout'
    identity = create(executor)
    run(executor, identity)
    assert len(broker.posts) == 1
    # Unknown execution retains its cash reservation until identity recovery.
    run(PortfolioExecutor(broker, store, clock=lambda: NOW), identity)
    assert run(executor, identity).phase == 'COMPLETE'
    assert len(broker.posts) == 2
    assert set(broker.queries) == {i.client_order_id for i in broker.posts}


def test_actual_process_crash_preserves_submission_marker_and_recovers(tmp_path):
    path = tmp_path / 'restart.sqlite'
    broker = Broker()
    store = PortfolioStore(path)
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    identity = create(executor)
    broker.crash_after_post = True
    with pytest.raises(SystemExit):
        run(executor, identity)
    store.close()
    broker.crash_after_post = False
    recovered = PortfolioStore(path)
    restored = PortfolioExecutor(broker, recovered, clock=lambda: NOW)
    run(restored, identity)
    assert len(broker.posts) == 2
    assert broker.cash == 0
    assert run(restored, identity).phase == 'COMPLETE'
    recovered.close()


def test_eventually_consistent_404_never_reposts_attempted_order(setup):
    broker, store, executor = setup
    broker.mode = 'timeout'
    identity = create(executor)
    run(executor, identity)
    broker.absent_after_post = True
    result = run(PortfolioExecutor(broker, store, clock=lambda: NOW), identity)
    assert result.phase == 'WAITING'
    assert len(broker.posts) == 1
    assert 'ORDER_ABSENCE_AFTER_SUBMIT' in result.reasons.values()
    assert set(result.reasons.values()) <= {'ORDER_ABSENCE_AFTER_SUBMIT', 'INSUFFICIENT_CASH'}


def test_restart_before_any_post_and_duplicate_week(setup):
    broker, store, executor = setup
    identity = create(executor)
    assert create(executor) == identity
    run(PortfolioExecutor(broker, store, clock=lambda: NOW), identity)
    run(executor, identity)
    assert len(broker.posts) == 2
    with pytest.raises(ValueError, match='different targets'):
        create(executor, {'AAPL': D('.25')})


def test_weight_only_journal_replays_after_quantity_interface_upgrade(setup):
    from factorlab.trading.storage import dumps

    broker, store, executor = setup
    identity = create(executor)
    old = store.plan(identity)
    del old['request']['quantity_deltas']
    del old['request']['signal_inputs']
    with store.transaction():
        store.db.execute('UPDATE portfolio_plans SET payload=? WHERE id=?', (dumps(old), identity))
    assert create(executor) == identity
    run(executor, identity)
    assert len(broker.posts) == 2


def test_cash_shortfall_does_not_normalize_targets(setup):
    broker, store, executor = setup
    broker.cash = D(60)
    identity = create(executor)
    run(executor, identity)
    result = run(executor, identity)
    assert result.targets == {'AAPL': D(5), 'QQQ': D(5), **{s: D(0) for s in SYMBOLS if s not in {'AAPL', 'QQQ'}}}
    assert broker.positions == {'AAPL': D(5), 'QQQ': D(1)}
    assert broker.cash == 0
    assert 'QQQ' in result.reasons


def test_missing_quote_preserves_allocation_and_other_symbol_executes(setup):
    broker, store, executor = setup
    identity = create(executor)
    result = run(executor, identity, prices={'QQQ': D(10)})
    assert result.reasons['AAPL'] == 'MISSING_QUOTE'
    assert [i.instrument_id for i in broker.posts] == ['QQQ']
    assert broker.cash == 50
    run(executor, identity)
    assert len(broker.posts) == 2


def test_unowned_inventory_not_implicitly_adopted_or_sold(setup):
    broker, store, executor = setup
    broker.positions = {'AAPL': D(7), 'GE': D(3)}
    identity = create(executor)
    result = run(executor, identity)
    assert result.reasons['AAPL'] == 'POSITION_OWNERSHIP_MISMATCH'
    assert result.reasons['GE'] == 'POSITION_OWNERSHIP_MISMATCH'
    assert [i.instrument_id for i in broker.posts] == ['QQQ']
    assert broker.positions['AAPL'] == 7 and broker.positions['GE'] == 3


def test_different_strategy_cannot_claim_same_symbol(setup):
    broker, store, executor = setup
    create(executor)
    with pytest.raises(ValueError, match='ownership conflict'):
        executor.create(account_id='paper', strategy_id='other', strategy_version='1',
            trading_session='2026-11-24', weights={'AAPL': D('.1')}, owned_symbols={'AAPL'}, now=NOW)


def test_rejection_records_residual_without_reallocation(setup):
    broker, store, executor = setup
    broker.mode = 'reject'
    identity = create(executor)
    run(executor, identity)
    result = run(executor, identity)
    assert set(result.reasons.values()) == {'ORDER_REJECTED_RESIDUAL'}
    assert len(broker.posts) == 2 and broker.cash == 100


def test_stale_and_cross_symbol_quotes(setup):
    broker, store, executor = setup
    identity = create(executor)
    stale = ExecutionQuote('AAPL', D(10), D(10), NOW - timedelta(seconds=6), NOW,
                           '2026-11-24', 'test')
    result = executor.run_once(identity, equity=D(100), quotes={'AAPL': stale},
        rules={'AAPL': InstrumentRules(D('.001'), D(1))}, now=NOW)
    assert result.reasons['AAPL'] == 'STALE_QUOTE'
    with pytest.raises(ValueError, match='cross-instrument'):
        executor.run_once(identity, equity=D(100), quotes={'AAPL': replace(stale, instrument_id='QQQ')},
            rules={}, now=NOW)
    assert not broker.posts


def test_minimum_notional_and_fractional_step(setup):
    broker, store, executor = setup
    identity = create(executor, {'AAPL': D('.003'), 'QQQ': D('.33333')})
    result = run(executor, identity)
    assert result.reasons['AAPL'] == 'BELOW_MINIMUM_NOTIONAL'
    assert broker.posts[0].quantity == D('3.333')
    assert broker.cash == D('66.670')


def test_constraints_and_signal_time_dependency(setup):
    broker, store, executor = setup
    with pytest.raises(ValueError):
        create(executor, {'TQQQ': D('1.1')})
    identity = create(executor, {'TQQQ': D('.8')}, economic_multipliers={'TQQQ': D(3)})
    result = run(executor, identity, not_before=NOW)
    assert result.reasons['TQQQ'] == 'QUOTE_PRECEDES_DECISION_INPUT'
    assert not broker.posts
    run(executor, identity)
    assert broker.posts[0].quantity == 8
    assert store.plan(identity)['request']['weights']['TQQQ'] == D('.8')


def test_external_order_is_not_canceled_or_traded_over(setup):
    broker, store, executor = setup
    identity = create(executor)
    from factorlab.trading.contracts import OrderIntent, OrderType
    foreign = OrderIntent(Market.BINANCE_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
        D(8), D(10), 'foreign')
    broker.foreign = (OrderSnapshot(foreign, S.SUBMITTED),)
    result = run(executor, identity)
    assert result.reasons['AAPL'] == 'FOREIGN_ORDER_CONFLICT'
    assert [(i.instrument_id, i.quantity) for i in broker.posts] == [('QQQ', D(2))]
    assert broker.foreign[0].intent.client_order_id == 'foreign'


def test_local_pending_outbox_reserves_cash_from_other_strategy(setup):
    broker, store, executor = setup
    identity = create(executor, {'AAPL': D(1)}, symbols={'AAPL'})
    broker.mode = 'pending'
    run(executor, identity)
    other = executor.create(account_id='paper', strategy_id='other', strategy_version='1',
        trading_session='2026-11-24', weights={'QQQ': D(1)}, owned_symbols={'QQQ'}, now=NOW)
    result = run(executor, other)
    assert result.reasons['QQQ'] == 'INSUFFICIENT_CASH'
    assert len(broker.posts) == 1


def test_cancel_race_late_fill_recovered_by_successor_plan(setup):
    broker, store, executor = setup
    broker.cash, broker.positions, broker.mode = D(0), {'AAPL': D(10)}, 'partial'
    identity = create(executor, {'AAPL': D('.5')}, initial={'AAPL': (D(10), D(10))})
    run(executor, identity)
    sell = broker.posts[0]
    executor.cancel(identity, sell.client_order_id)
    assert run(executor, identity).phase == 'COMPLETE'
    successor = executor.create(account_id='paper', strategy_id='test', strategy_version='1',
        trading_session='next-session', weights={'AAPL': D('.5'), 'QQQ': D('.5')},
        owned_symbols=SYMBOLS, now=NOW)
    broker.confirm(sell.client_order_id, sell.quantity, S.FILLED)
    broker.mode = 'fill'
    quotes = {s: ExecutionQuote(s, D(10), D(10), NOW, NOW, 'next-session', 'test') for s in SYMBOLS}
    executor.run_once(successor, equity=D(100), quotes=quotes,
        rules={s: InstrumentRules(D('.001'), D(1)) for s in SYMBOLS}, now=NOW)
    assert [(i.side, i.instrument_id) for i in broker.posts] == [(Side.SELL, 'AAPL'), (Side.BUY, 'QQQ')]
    assert broker.positions == {'AAPL': D(5), 'QQQ': D(5)}
    assert broker.cash == 0


def test_cash_commitment_appearing_before_post_is_readmitted(setup):
    broker, store, executor = setup
    from factorlab.trading.contracts import OrderIntent, OrderType
    foreign = OrderIntent(Market.BINANCE_EQUITY, 'QQQ', Side.BUY, OrderType.LIMIT,
                          D(8), D(10), 'late-foreign-order')
    original = broker.read_account
    reads = 0

    def changed_account():
        nonlocal reads
        reads += 1
        if reads == 3:
            broker.foreign = (OrderSnapshot(foreign, S.SUBMITTED),)
        return original()

    broker.read_account = changed_account
    identity = create(executor, {'AAPL': D(1)}, symbols={'AAPL'})
    result = run(executor, identity)
    assert not broker.posts
    assert result.reasons['AAPL'] == 'INSUFFICIENT_CASH'
    assert not store.orders(identity)[0]['attempted']


def test_quote_expiring_during_account_requests_is_not_submitted(setup):
    broker, store, executor = setup
    executor.clock = lambda: NOW + timedelta(seconds=6)
    identity = create(executor)
    result = run(executor, identity)
    assert not broker.posts
    assert set(result.reasons.values()) == {'STALE_QUOTE'}


def test_refresh_after_account_read_keeps_original_limit_and_quantity(setup):
    broker, store, executor = setup
    identity = create(executor, {'AAPL': D('.5')}, symbols={'AAPL'})
    later = NOW + timedelta(seconds=6)
    executor.clock = lambda: later
    refreshes = []

    def refresh():
        refreshes.append(later)
        return {'AAPL': ExecutionQuote('AAPL', D(11), D(11), later, later,
                                      '2026-11-24', 'test')}

    initial = {'AAPL': ExecutionQuote('AAPL', D(10), D(10), NOW, NOW, '2026-11-24', 'test')}
    executor.run_once(identity, equity=D(100), quotes=initial,
        rules={'AAPL': InstrumentRules(D('.001'), D(1))}, now=NOW,
        refresh_quotes=refresh)
    assert refreshes == [later]
    assert broker.posts[0].quantity == 5
    assert broker.posts[0].limit_price == 10
    assert store.orders(identity)[0]['reference_price'] == 10


def test_cancel_cannot_touch_foreign_order(setup):
    broker, store, executor = setup
    identity = create(executor)
    with pytest.raises(StopIteration):
        executor.cancel(identity, 'foreign')
    assert not broker.posts


def test_legal_price_tick_and_cash_budget_use_same_rounded_price(setup):
    broker, store, executor = setup
    identity = create(executor, {'AAPL': D(1)}, symbols={'AAPL'})
    run(executor, identity, prices={'AAPL': D('10.001')},
        rules={'AAPL': InstrumentRules(D('.001'), D(1), price_tick=D('.01'))})
    assert broker.posts[0].limit_price == D('10.01')
    assert broker.posts[0].quantity * D('10.01') <= 100
    assert store.orders(identity)[0]['reference_price'] == D('10.001')


def test_two_distinct_intraday_decisions_and_repeat_identity(setup):
    broker, store, executor = setup
    first = create(executor)
    run(executor, first)
    run(executor, first)
    second = create(executor, {'AAPL': D('.25'), 'QQQ': D('.75')}, rebalance_id='intraday-2')
    assert first != second
    assert create(executor, {'AAPL': D('.25'), 'QQQ': D('.75')}, rebalance_id='intraday-2') == second
    run(executor, second)
    run(executor, second)
    run(executor, second)
    assert broker.positions == {'AAPL': D('2.5'), 'QQQ': D('7.5')}
    assert len(broker.posts) == 4


def test_multiple_partial_buys_restart_and_account_restore(setup):
    broker, store, executor = setup
    broker.mode = 'partial'
    identity = create(executor)
    assert run(executor, identity).phase == 'WAITING'
    assert len(broker.posts) == 2
    restored = PortfolioExecutor(broker, store, clock=lambda: NOW)
    assert run(restored, identity).phase == 'WAITING'
    for order in broker.posts:
        broker.confirm(order.client_order_id, order.quantity, S.FILLED)
    assert run(restored, identity).phase == 'COMPLETE'
    assert len(broker.posts) == 2 and broker.cash == 0
    report = restored.report(identity, official_equity=D(100))
    assert report.official_cash == 0 and report.traded_notional == 100
    assert report.tracking_error == 0
    assert {row.instrument_id: row.owned_quantity for row in report.positions if row.owned_quantity} == {'AAPL': D(5), 'QQQ': D(5)}


def test_disconnect_requires_rest_identity_recovery(setup):
    broker, store, executor = setup
    broker.mode = 'pending'
    identity = create(executor)
    run(executor, identity)
    original = broker.query_order

    def disconnected(_):
        raise ConnectionError('trade stream and transport disconnected')

    broker.query_order = disconnected
    assert run(executor, identity).phase == 'WAITING'
    assert len(broker.posts) == 2
    broker.query_order = original
    for order in broker.posts:
        broker.confirm(order.client_order_id, order.quantity, S.FILLED)
    assert run(executor, identity).phase == 'COMPLETE'
    assert len(broker.posts) == 2


def test_uncertain_cancel_with_racing_full_fill_never_replaces_sell(setup):
    broker, store, executor = setup
    broker.cash, broker.positions, broker.mode = D(0), {'AAPL': D(10)}, 'partial'
    identity = create(executor, {'AAPL': D('.5')}, initial={'AAPL': (D(10), D(10))})
    run(executor, identity)
    order = broker.posts[0]

    def racing_cancel(client_id):
        broker.confirm(client_id, order.quantity, S.FILLED)
        raise TimeoutError('cancel response lost during fill')

    broker.cancel_order = racing_cancel
    executor.cancel(identity, order.client_order_id)
    assert run(executor, identity).phase == 'COMPLETE'
    assert broker.positions['AAPL'] == 5 and len(broker.posts) == 1


def test_explicit_strategy_quantities_are_not_resized_at_execution_prices(setup):
    broker, store, executor = setup
    identity = create(executor, {'AAPL': D('.5'), 'QQQ': D('.5')},
        quantity_deltas={'AAPL': D('1.23456')},
        signal_inputs={'session': '2026-11-23', 'adjust_ratio': D('.5')})
    run(executor, identity, prices={'AAPL': D(20), 'QQQ': D(10)})
    result = run(executor, identity)
    assert len(broker.posts) == 1
    assert broker.posts[0].quantity == D('1.234')
    assert result.targets['AAPL'] == D('1.23456')
    assert result.targets['QQQ'] == 0
    assert result.reasons['AAPL'] == 'ORDER_FILLED_RESIDUAL'
    assert store.plan(identity)['request']['signal_inputs']['adjust_ratio'] == D('.5')
    with pytest.raises(ValueError, match='different targets'):
        create(executor, quantity_deltas={'AAPL': D(2)})


def test_native_decision_checkpoint_and_capped_orders_are_one_immutable_plan(setup):
    from factorlab.integrations.alpaca.strategy_orders import create_strategy_plan

    broker, store, executor = setup
    decision = dict(weights={'AAPL': .04, 'QQQ': .04},
        orders=[dict(symbol='AAPL', side='buy', adjusted_quantity=2.5)],
        state={'last_week': [2026, 48]})
    args = dict(decision=decision, adjustment_ratios={'AAPL': D('.4'), 'QQQ': D(1)},
        signal_inputs={'previous_session': '2026-11-23', 'history_id': 'fixture'},
        account_id='paper', strategy_id='v69', strategy_version='69.1',
        trading_session='2026-11-24', owned_symbols={'AAPL', 'QQQ'}, now=NOW)
    identity = create_strategy_plan(executor, **args)
    assert create_strategy_plan(executor, **args) == identity
    run(executor, identity)
    assert [(i.instrument_id, i.quantity) for i in broker.posts] == [('AAPL', D(1))]
    request = store.plan(identity)['request']
    assert request['signal_inputs']['strategy_state'] == decision['state']
    assert request['signal_inputs']['original_orders'] == decision['orders']
    assert request['quantity_deltas'] == {'AAPL': D(1)}
    suppressed = dict(decision, weights=None, orders=[])
    assert create_strategy_plan(executor, **dict(args, decision=suppressed)) is None
    changed = dict(decision, state={'last_week': [2026, 49]})
    with pytest.raises(ValueError, match='different targets'):
        create_strategy_plan(executor, **dict(args, decision=changed))


def test_explicit_strategy_sell_quantity_cannot_adopt_foreign_inventory(setup):
    broker, store, executor = setup
    broker.positions = {'AAPL': D(3)}
    with pytest.raises(ValueError, match='exceeds owned'):
        create(executor, quantity_deltas={'AAPL': D(-1)})
    assert not broker.posts


def test_conflicting_broker_event_does_not_mutate_owned_ledger(setup):
    broker, store, executor = setup
    broker.mode = 'partial'
    identity = create(executor)
    run(executor, identity)
    order = broker.posts[0]
    original = broker.events[order.client_order_id]
    broker.events[order.client_order_id] = replace(original, cumulative_filled_quantity=D(1))
    with pytest.raises(ValueError, match='identity changed'):
        run(executor, identity)
    broker.events[order.client_order_id] = original
    assert store.orders(identity)[0]['record'].snapshot.cumulative_filled_quantity == order.quantity / 2
