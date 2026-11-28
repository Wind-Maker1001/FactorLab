"""Shared-policy execution through the offline Alpaca HTTP adapter."""
from contextlib import ExitStack, closing
from copy import deepcopy
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.alpaca import OrderNotFound, OutcomeUnknown
from factorlab.integrations.alpaca.portfolio import AlpacaPortfolioRunner
from factorlab.trading.capital import CapitalPolicyMode
from factorlab.trading.contracts import Market
from factorlab.trading.portfolio_execution import ExecutionQuote, InstrumentRules, PortfolioStore
from test_alpaca_execution import Official, adapter
from test_alpaca_portfolio import NOW

ACCOUNT = 'account-one'


class SharedOfficial(Official):
    def __init__(self):
        super().__init__()
        self.post_ids = []
        self.nontrade = []

    def __call__(self, method, path, params, body):
        if path == '/v2/account/activities':
            return 200, deepcopy(self.nontrade)
        if path == '/v2/orders' and method == 'GET':
            rows = list(self.orders.values())
            if params.get('status') == 'open':
                rows = [row for row in rows if row['status'] not in {'filled', 'canceled'}]
            return 200, deepcopy(rows)
        if path == '/v2/orders' and method == 'POST':
            self.post_ids.append(body['client_order_id'])
        return super().__call__(method, path, params, body)

    # TODO: Cover charged execution fees when the Alpaca portfolio bridge exposes
    # activity fees in OrderEvent; it currently forwards execution notional only.
    def fill(self, client_id, cumulative=None):
        row = self.orders[client_id]
        cumulative = D(row['qty']) if cumulative is None else cumulative
        quantity, price = cumulative - D(row['filled_qty']), D(row['limit_price'])
        assert quantity > 0
        row.update(filled_qty=str(cumulative), filled_avg_price=str(price),
                   status='filled' if cumulative == D(row['qty']) else 'partially_filled',
                   filled_at=NOW.isoformat(), updated_at=NOW.isoformat())
        self.activities.append(dict(id='fill-' + client_id + '-' + str(cumulative), order_id=row['id'],
            qty=str(quantity), price=str(price), side=row['side'], symbol=row['symbol'],
            transaction_time=NOW.isoformat()))
        sign = D(1) if row['side'] == 'buy' else D(-1)
        self.cash = str(D(self.cash) - sign * quantity * price)
        positions = {item['symbol']: item for item in self.positions}
        previous = D(positions.get(row['symbol'], {}).get('qty', '0'))
        remaining = previous + sign * quantity
        if remaining:
            positions[row['symbol']] = dict(symbol=row['symbol'], qty=str(remaining),
                avg_entry_price=str(price), current_price=str(price))
        else:
            positions.pop(row['symbol'], None)
        self.positions = list(positions.values())


def open_runner(stack, path, official):
    actual, _ = adapter(path, official)
    stack.callback(actual.close)
    store = stack.enter_context(closing(PortfolioStore(path / 'portfolio.sqlite')))
    runner = AlpacaPortfolioRunner(actual, store, order_not_found=OrderNotFound,
                                   clock=lambda: NOW)
    return runner, store


def qualify(runner, store, owner, symbols):
    runner.executor.allocate(account_id=ACCOUNT, strategy_id=owner,
        allocation_id=owner + '-usd', capital=D(400), initial_cash=D(400),
        official_equity=D(1000))
    binding = dict(account_id=ACCOUNT, market=Market.ALPACA_US_EQUITY,
        strategy_id='shared-strategy', instance_id=owner, allocation_id=owner + '-usd')
    spec = SimpleNamespace(account_id=ACCOUNT, market=Market.ALPACA_US_EQUITY,
        strategy_id='shared-strategy', symbols=tuple(symbols),
        capital={'currency': 'USD', 'requested': D(400)})
    store.capital_policy.qualify(spec, binding)


def setup_pair(runner, store):
    store.capital_policy.configure(account_id=ACCOUNT, currency='USD', budget=D(1000),
        fee_rate=D('.01'), mode=CapitalPolicyMode.SHARED_SAME_DIRECTION)
    qualify(runner, store, 'A', ('AAPL',))
    qualify(runner, store, 'B', ('AAPL', 'MSFT'))


def create(runner, owner, weights, event):
    return runner.executor.create(account_id=ACCOUNT, strategy_id=owner,
        strategy_version='1', trading_session='test-session', weights=weights,
        owned_symbols=set(weights), now=NOW, rebalance_id=event)


def advance(runner, identity):
    now = runner.clock()
    return runner.run_once(identity,
        quotes={symbol: ExecutionQuote(symbol, D(100), D(100), now, now,
            'test-session', 'offline-http') for symbol in ('AAPL', 'MSFT')},
        rules={symbol: InstrumentRules(D('.001'), D(1), time_in_force='DAY') for symbol in ('AAPL', 'MSFT')})


def fill_plan(runner, store, official, identity):
    result = advance(runner, identity)
    for order in store.orders(identity):
        client_id = order['record'].snapshot.intent.client_order_id
        assert client_id in official.orders, result
        if official.orders[client_id]['status'] != 'filled':
            official.fill(client_id)
    assert advance(runner, identity).phase == 'COMPLETE'


def test_http_partial_sell_reservations_cancel_pause_and_restart_leave_b_unchanged(tmp_path):
    from factorlab.trading.contracts import OrderIntent, OrderType, Side
    official = SharedOfficial()
    with ExitStack() as stack:
        runner, store = open_runner(stack, tmp_path, official)
        setup_pair(runner, store)
        for owner in ['A', 'B']:
            fill_plan(runner, store, official, create(runner, owner, {'AAPL': D('.25')}, owner + '-buy'))
        other_cash, other_inventory = store.cash(ACCOUNT, 'B'), store.holdings(ACCOUNT, 'B')

        def sell(name, quantity):
            intent = OrderIntent(Market.ALPACA_US_EQUITY, 'AAPL', Side.SELL, OrderType.LIMIT,
                quantity, D(100), name, time_in_force='DAY')
            return runner.executor.create(account_id=ACCOUNT, strategy_id='A', strategy_version='1',
                trading_session='test-session', rebalance_id=name, weights={}, owned_symbols=['AAPL'],
                now=NOW, direct_orders=[(intent, D(100))], concurrent_direct_orders=True)

        first = sell('first-exit', D(1))
        with pytest.raises(ValueError, match='owned inventory'):
            sell('excess-exit', D('.1'))
        advance(runner, first)
        official.fill('first-exit', D('.5'))
        advance(runner, first)
        assert store.holdings(ACCOUNT, 'A')['AAPL'] == D('.5')
        assert store.available_inventory(ACCOUNT, 'A', 'AAPL') == 0
        runner.executor.cancel(first, 'first-exit')
        assert advance(runner, first).phase == 'COMPLETE'
        assert store.available_inventory(ACCOUNT, 'A', 'AAPL') == D('.5')
        second = sell('second-exit', D('.5'))
        store.capital_policy.pause(ACCOUNT, 'A')
        assert advance(runner, second).reasons['capital'] == 'STRATEGY_PAUSED'
        assert store.holdings(ACCOUNT, 'B') == other_inventory
        assert store.cash(ACCOUNT, 'B') == other_cash
        submitted_before_restart = tuple(official.post_ids)
    with ExitStack() as stack:
        runner, store = open_runner(stack, tmp_path, official)
        assert store.capital_policy.snapshot(ACCOUNT, 'A').paused
        assert advance(runner, second).reasons['capital'] == 'STRATEGY_PAUSED'
        assert tuple(official.post_ids) == submitted_before_restart
        store.capital_policy.pause(ACCOUNT, 'A', False)
        advance(runner, second)
        official.fill('second-exit')
        assert advance(runner, second).phase == 'COMPLETE'
        assert advance(runner, second).phase == 'COMPLETE'
        assert official.post_ids.count('second-exit') == 1
        runner.executor.transfer_flat_ownership(account_id=ACCOUNT, from_strategy='A', to_strategy='B',
                                                symbols=['AAPL'], request_id='handover')
        assert store.holdings(ACCOUNT, 'B') == other_inventory
        assert store.cash(ACCOUNT, 'B') == other_cash
        assert not store.capital_policy.snapshot(ACCOUNT, 'B').paused
        assert 'excess-exit' not in official.post_ids


def test_shared_buys_then_a_sells_only_its_inventory(tmp_path):
    official = SharedOfficial()
    with ExitStack() as stack:
        runner, store = open_runner(stack, tmp_path, official)
        setup_pair(runner, store)
        a = create(runner, 'A', {'AAPL': D('.25')}, 'a-buy')
        fill_plan(runner, store, official, a)
        b = create(runner, 'B', {'AAPL': D('.25')}, 'b-buy')
        fill_plan(runner, store, official, b)
        a_quantity = store.holdings(ACCOUNT, 'A')['AAPL']
        b_holdings = store.holdings(ACCOUNT, 'B')
        b_cash = store.cash(ACCOUNT, 'B')
        assert a_quantity > 0 and b_holdings['AAPL'] > 0
        sell = create(runner, 'A', {'AAPL': D(0)}, 'a-sell')
        fill_plan(runner, store, official, sell)
        assert store.holdings(ACCOUNT, 'A').get('AAPL', D(0)) == 0
        assert store.holdings(ACCOUNT, 'B') == b_holdings
        assert store.cash(ACCOUNT, 'B') == b_cash
        assert D(official.positions[0]['qty']) == b_holdings['AAPL']
        assert D(official.cash) == D(1000) - b_holdings['AAPL'] * D(100)
        assert runner.report(sell).official_cash == D(400)
        assert len(official.post_ids) == len(set(official.post_ids)) == 3


def test_registered_other_instance_waits_while_independent_symbol_proceeds(tmp_path):
    official = SharedOfficial()
    with ExitStack() as stack:
        runner, store = open_runner(stack, tmp_path, official)
        setup_pair(runner, store)
        a = create(runner, 'A', {'AAPL': D('.25')}, 'pending-a')
        advance(runner, a)
        a_id = official.post_ids[0]
        b = create(runner, 'B', {'AAPL': D('.25'), 'MSFT': D('.25')}, 'pending-b')
        result = advance(runner, b)
        assert result.reasons['AAPL'] == 'SYMBOL_ORDER_PENDING'
        assert 'FOREIGN_ORDER_CONFLICT' not in result.reasons.values()
        assert [official.orders[key]['symbol'] for key in official.post_ids] == ['AAPL', 'MSFT']
        assert official.orders[a_id]['status'] == 'new'
        official.fill(a_id)
        assert advance(runner, a).phase == 'COMPLETE'
        msft_id = official.post_ids[1]
        official.fill(msft_id)
        fill_plan(runner, store, official, b)
        assert store.holdings(ACCOUNT, 'A')['AAPL'] > 0
        assert set(store.holdings(ACCOUNT, 'B')) == {'AAPL', 'MSFT'}
        assert len(official.post_ids) == len(set(official.post_ids)) == 3


def test_shared_policy_official_corporate_receipts_and_child_allocations(tmp_path):
    from factorlab.integrations.alpaca.corporate_actions import CorporateReceiptJournal
    official = SharedOfficial()
    with ExitStack() as stack:
        runner, store = open_runner(stack, tmp_path, official)
        setup_pair(runner, store)
        for owner in ['A', 'B']:
            fill_plan(runner, store, official, create(runner, owner, {'AAPL': D('.25')}, owner + '-buy'))
        official.nontrade = [
            dict(id='dividend', activity_type='DIV', symbol='AAPL', date='2026-11-25',
                 net_amount='1.01', qty='2'),
            dict(id='split', activity_type='SSP', symbol='AAPL', date='2026-11-26',
                 net_amount='0', qty='2')]
        official.cash = str(D(official.cash) + D('1.01'))
        official.positions[0].update(qty='4', avg_entry_price='50', current_price='50')
        announcements = [dict(id='split', ca_type='split', initiating_symbol='AAPL',
            ex_date='2026-11-26', old_rate='1', new_rate='2')]
        receipts = runner.adapter.read_corporate_activities(after='2026-11-24T00:00:00Z')
        journal = CorporateReceiptJournal(store, ACCOUNT)
        for _ in range(2):
            assert journal.reconcile_account(runner.executor, announcements=announcements, receipts=receipts) == {}
        assert store.holdings(ACCOUNT) == {'AAPL': D(4)}
        assert store.cash(ACCOUNT, 'A') + store.cash(ACCOUNT, 'B') == D('601.01')
        assert store.db.execute('SELECT count(*) FROM portfolio_corporate_actions').fetchone()[0] == 4
        official.positions.append(dict(symbol='CHILD', qty='2', avg_entry_price='10', current_price='10'))
        actions = {owner: [dict(id='spinoff', symbol='AAPL', quantity_ratio=D(1),
            basis_ratio=D('.8'), cash_flow=D(0), evidence={'activity': 'official-spin'},
            allocations=[dict(symbol='CHILD', quantity=D(1), basis_fraction=D('.2'))])]
            for owner in ['A', 'B']}
        for _ in range(2):
            runner.executor.record_account_corporate_actions(account_id=ACCOUNT, by_strategy=actions)
        assert store.holdings(ACCOUNT) == {'AAPL': D(4), 'CHILD': D(2)}
        for owner in ['A', 'B']:
            assert store.holdings(ACCOUNT, owner) == {'AAPL': D(2), 'CHILD': D(1)}
        assert len(official.post_ids) == 2


def test_response_loss_restart_recovers_original_order_and_fee_reservations(tmp_path):
    official = SharedOfficial()
    with ExitStack() as stack:
        runner, store = open_runner(stack, tmp_path, official)
        setup_pair(runner, store)
        identity = create(runner, 'A', {'AAPL': D('.25')}, 'lost-response')
        official.submit_timeout = True
        try:
            advance(runner, identity)
        except OutcomeUnknown:
            pass
        original_ids = tuple(order['record'].snapshot.intent.client_order_id
                             for order in store.orders(identity))
        assert tuple(official.post_ids) == original_ids
        assert len(original_ids) == 1
        row = official.orders[original_ids[0]]
        notional = D(row['qty']) * D(row['limit_price'])
        assert store.cash(ACCOUNT, 'A', reserve=True) == D(400) - notional * D('1.01')
    official.submit_timeout = False
    official.fill(original_ids[0])
    with ExitStack() as stack:
        runner, store = open_runner(stack, tmp_path, official)
        assert advance(runner, identity).phase == 'COMPLETE'
        assert advance(runner, identity).phase == 'COMPLETE'
        assert tuple(official.post_ids) == original_ids
        assert tuple(order['record'].snapshot.intent.client_order_id
                     for order in store.orders(identity)) == original_ids
        assert store.cash(ACCOUNT, 'A', reserve=True) == D(400) - notional
        assert store.cash(ACCOUNT, 'B', reserve=True) == D(400)
        assert D(official.cash) == D(1000) - notional
        assert runner.report(identity).official_cash == D(400) - notional
