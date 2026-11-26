"""Capital operations on the real Alpaca Paper adapter with an offline HTTP transport."""
from contextlib import ExitStack
from decimal import Decimal as D

from factorlab.integrations.alpaca.corporate_actions import CorporateReceiptJournal
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
from test_shared_portfolio_execution import (
    ACCOUNT, NOW, SharedOfficial, advance, create, fill_plan, open_runner, setup_pair,
)


class CapitalOfficial(SharedOfficial):
    def __init__(self):
        super().__init__()
        self.cancel_requests = []

    def __call__(self, method, path, params, body):
        if method == 'DELETE':
            self.cancel_requests.append(path)
            # Acknowledging the request leaves the order live until REST reports a terminal state.
            return 204, None
        status, payload = super().__call__(method, path, params, body)
        if path == '/v2/account':
            equity = D(self.cash) + sum(D(p['qty']) * D(p['current_price']) for p in self.positions)
            payload['equity'] = str(equity)
        return status, payload

    def confirm_cancel(self, client_id):
        self.orders[client_id].update(status='canceled', updated_at='2026-11-24T15:01:00Z')


def sleeve(capital, owner):
    return next(row for row in capital.snapshot(ACCOUNT).strategies if row.strategy_id == owner)


def test_http_partial_buy_cancel_then_staged_transfer_after_own_sell(tmp_path):
    official = CapitalOfficial()
    with ExitStack() as stack:
        runner, store = open_runner(stack, tmp_path, official)
        setup_pair(runner, store)
        capital = runner.executor.capital
        fill_plan(runner, store, official, create(runner, 'B', {'AAPL': D('.25')}, 'b-buy'))
        b_inventory = store.holdings(ACCOUNT, 'B')
        buy = create(runner, 'A', {'AAPL': D('.25')}, 'a-buy')
        advance(runner, buy)
        buy_id = official.post_ids[-1]
        assert store.cash(ACCOUNT, 'A', reserve=False) == D(400)
        assert sleeve(capital, 'A').withdrawable == D(299)

        rejected = capital.change(account_id=ACCOUNT, event_id='live-order',
            source='A', destination=None, amount=D(310))
        assert rejected.status == 'INSUFFICIENT_CAPITAL'
        assert rejected.applied == 0 and rejected.remaining == D(310)
        official.fill(buy_id, D('.5'))
        advance(runner, buy)
        assert store.holdings(ACCOUNT, 'A') == {'AAPL': D('.5')}
        assert store.cash(ACCOUNT, 'A', reserve=False) == D(350)
        assert sleeve(capital, 'A').withdrawable < D(310)

        runner.executor.cancel(buy, buy_id)
        assert len(official.cancel_requests) == 1
        assert official.orders[buy_id]['status'] == 'partially_filled'
        assert sleeve(capital, 'A').withdrawable < D(310)
        official.confirm_cancel(buy_id)
        assert advance(runner, buy).phase == 'COMPLETE'
        cash_before = official.cash
        released = capital.change(account_id=ACCOUNT, event_id='idle-cash',
            source='A', destination=None, amount=D(325))
        assert released.status == 'COMPLETE' and released.applied == D(325)
        assert official.cash == cash_before
        assert store.holdings(ACCOUNT, 'A') == {'AAPL': D('.5')}

        pending = capital.change(account_id=ACCOUNT, event_id='a-to-b',
            source='A', destination='B', amount=D(65), mode='STAGED')
        assert pending.status == 'WAITING' and 0 < pending.applied <= D(25)
        assert pending.remaining == D(65) - pending.applied
        assert capital.advance(account_id=ACCOUNT, event_id='a-to-b',
            step_id='before-sell').remaining == pending.remaining

        exit_id = 'sell-a-only'
        intent = OrderIntent(Market.ALPACA_US_EQUITY, 'AAPL', Side.SELL, OrderType.LIMIT,
            D('.5'), D(100), exit_id, time_in_force='DAY')
        exit_plan = runner.executor.create(account_id=ACCOUNT, strategy_id='A', strategy_version='1',
            trading_session='test-session', rebalance_id=exit_id, weights={}, owned_symbols=['AAPL'],
            now=NOW, direct_orders=[(intent, D(100))], concurrent_direct_orders=True)
        advance(runner, exit_plan)
        official.fill(exit_id, D('.25'))
        advance(runner, exit_plan)
        partial = capital.advance(account_id=ACCOUNT, event_id='a-to-b', step_id='partial-sell', reduction_plan=exit_plan)
        assert partial.status == 'WAITING' and 0 < partial.remaining < pending.remaining
        assert store.holdings(ACCOUNT, 'A') == {'AAPL': D('.25')}
        assert store.holdings(ACCOUNT, 'B') == b_inventory
        posts_before_restart = tuple(official.post_ids)

    with ExitStack() as stack:
        runner, store = open_runner(stack, tmp_path, official)
        capital = runner.executor.capital
        assert capital.advance(account_id=ACCOUNT, event_id='a-to-b', step_id='partial-sell', reduction_plan=exit_plan) == partial
        official.fill(exit_id)
        assert advance(runner, exit_plan).phase == 'COMPLETE'
        cash_after_sell = official.cash
        complete = capital.advance(account_id=ACCOUNT, event_id='a-to-b', step_id='final-sell', reduction_plan=exit_plan)
        assert complete.status == 'COMPLETE' and complete.remaining == 0
        assert store.holdings(ACCOUNT, 'A') == {'AAPL': D(0)}
        assert store.holdings(ACCOUNT, 'B') == b_inventory
        assert store.cash(ACCOUNT, 'A', reserve=False) == D(10)
        assert store.cash(ACCOUNT, 'B', reserve=False) == D(365)
        assert store.allocation(ACCOUNT, 'A')['allocation_id'] == 'A-usd'
        assert store.allocation(ACCOUNT, 'B')['allocation_id'] == 'B-usd'
        assert official.cash == cash_after_sell
        assert D(official.cash) == D(900)
        assert tuple(official.post_ids) == posts_before_restart


def test_http_dividend_stays_with_eligible_instances_after_capital_transfer(tmp_path):
    official = CapitalOfficial()
    with ExitStack() as stack:
        runner, store = open_runner(stack, tmp_path, official)
        setup_pair(runner, store)
        for owner in ('A', 'B'):
            fill_plan(runner, store, official, create(runner, owner, {'AAPL': D('.25')}, owner + '-buy'))
        capital = runner.executor.capital
        receipt = dict(id='dividend', activity_type='DIV', symbol='AAPL', date='2026-11-25',
                       net_amount='1.01', qty='2')
        official.nontrade = [receipt]
        official.cash = str(D(official.cash) + D(receipt['net_amount']))
        receipts = runner.adapter.read_corporate_activities(after='2026-11-24T00:00:00Z')
        journal = CorporateReceiptJournal(store, ACCOUNT)
        assert journal.reconcile_account(runner.executor, announcements=(), receipts=receipts) == {}
        cash_by_owner = {owner: store.cash(ACCOUNT, owner) for owner in ('A', 'B')}
        assert sum(cash_by_owner.values(), D(0)) == D('601.01')

        transferred = capital.change(account_id=ACCOUNT, event_id='rebalance-capital',
            source='A', destination='B', amount=D(100))
        assert transferred.status == 'COMPLETE' and transferred.applied == D(100)
        assert journal.reconcile_account(runner.executor, announcements=(), receipts=receipts) == {}
        assert store.cash(ACCOUNT, 'A') == cash_by_owner['A'] - D(100)
        assert store.cash(ACCOUNT, 'B') == cash_by_owner['B'] + D(100)
        assert D(official.cash) - sum((store.cash(ACCOUNT, owner) for owner in ('A', 'B')), D(0)) == D(200)
        assert store.holdings(ACCOUNT) == {'AAPL': D(2)}
        assert len(official.post_ids) == 2

        report = capital.performance(ACCOUNT, 'B')
        assert report['corporate_cash_flow'] == cash_by_owner['B'] - D(300)
        assert report['returns'].absolute_pnl == report['corporate_cash_flow']
        assert report['returns'].capital_in == D(100)
