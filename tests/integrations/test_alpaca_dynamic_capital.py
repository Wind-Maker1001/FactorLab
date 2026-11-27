"""Capital operations on the real Alpaca Paper adapter with an offline HTTP transport."""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from decimal import Decimal as D

from factorlab.integrations.alpaca.corporate_actions import CorporateReceiptJournal
from factorlab.integrations.alpaca.strategy_instances import StrategyInstances
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
from factorlab.trading.portfolio_execution import ExecutionQuote, InstrumentRules
from factorlab.trading.portfolio_risk import PortfolioRiskLimits
from test_alpaca_execution import adapter as alpaca_http_adapter
from test_shared_portfolio_execution import (
    ACCOUNT, NOW, SharedOfficial, advance, create, fill_plan, open_runner, setup_pair,
)


class CapitalOfficial(SharedOfficial):
    def __init__(self, *, clock=lambda: NOW):
        super().__init__()
        self.clock = clock
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
        elif path == '/v2/orders' and method == 'POST':
            at = self.clock().isoformat()
            timestamps = dict(created_at=at, submitted_at=at, updated_at=at)
            self.orders[body['client_order_id']].update(timestamps)
            payload.update(timestamps)
        return status, payload

    def fill(self, client_id, cumulative=None):
        super().fill(client_id, cumulative)
        at = self.clock().isoformat()
        self.orders[client_id].update(filled_at=at, updated_at=at)
        self.activities[-1]['transaction_time'] = at

    def confirm_cancel(self, client_id):
        self.orders[client_id].update(status='canceled', updated_at=self.clock().isoformat())


def sleeve(capital, owner):
    return next(row for row in capital.snapshot(ACCOUNT).strategies if row.strategy_id == owner)


def test_http_partial_buy_cancel_then_staged_transfer_after_own_sell(tmp_path):
    official = CapitalOfficial(clock=lambda: datetime.now(timezone.utc))
    with ExitStack() as stack:
        runner, store = open_runner(stack, tmp_path, official)
        runner.clock = runner.executor.clock = lambda: datetime.now(timezone.utc)
        setup_pair(runner, store)
        capital = runner.executor.capital
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_gross_exposure=D(200)))
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
        risk = runner.executor.portfolio_risk_snapshot(ACCOUNT)
        assert risk.gross_exposure == D(150)
        assert risk.pending_open_exposure == D(50)
        assert risk.projected_gross_exposure == D(200)

        runner.executor.cancel(buy, buy_id)
        assert len(official.cancel_requests) == 1
        assert official.orders[buy_id]['status'] == 'partially_filled'
        assert runner.executor.portfolio_risk_snapshot(ACCOUNT).projected_gross_exposure == D(200)
        assert sleeve(capital, 'A').withdrawable < D(310)
        official.confirm_cancel(buy_id)
        assert advance(runner, buy).phase == 'COMPLETE'
        cash_before = official.cash
        before_transfer = runner.executor.portfolio_risk_snapshot(ACCOUNT)
        released = capital.change(account_id=ACCOUNT, event_id='idle-cash',
            source='A', destination=None, amount=D(325))
        assert released.status == 'COMPLETE' and released.applied == D(325)
        assert official.cash == cash_before
        assert store.holdings(ACCOUNT, 'A') == {'AAPL': D('.5')}

        pending = capital.change(account_id=ACCOUNT, event_id='a-to-b',
            source='A', destination='B', amount=D(65), mode='STAGED')
        assert pending.status == 'WAITING' and 0 < pending.applied <= D(25)
        assert pending.remaining == D(65) - pending.applied
        after_transfer = runner.executor.portfolio_risk_snapshot(ACCOUNT)
        assert after_transfer.official_equity == before_transfer.official_equity
        assert after_transfer.daily_loss == before_transfer.daily_loss == 0
        assert after_transfer.drawdown == before_transfer.drawdown == 0
        assert after_transfer.realized_pnl == before_transfer.realized_pnl == 0
        performance = capital.performance(ACCOUNT, 'A')['returns']
        assert performance.absolute_pnl == performance.twr == 0
        assert performance.capital_out == D(325) + pending.applied
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(
            max_gross_exposure=D(100), min_available_cash=D(1000)))
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
        runner.clock = runner.executor.clock = lambda: datetime.now(timezone.utc)
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



def test_http_sell_then_partially_prepare_buy_legs_before_releasing_unused_capital(tmp_path):
    official = CapitalOfficial()
    configuration = dict(instances=[], releases=tmp_path / 'releases', trusted_python=True)

    def open_host(stack):
        adapter, _ = alpaca_http_adapter(tmp_path, official)
        stack.callback(adapter.close)
        return stack.enter_context(closing(StrategyInstances(
            tmp_path, adapter, configuration, clock=lambda: NOW)))

    def turn(runner, identity, *symbols):
        quotes = {symbol: ExecutionQuote(symbol, D(100), D(100), NOW, NOW,
                  'test-session', 'offline-http') for symbol in symbols}
        rules = {symbol: InstrumentRules(D('.001'), D(1), time_in_force='DAY') for symbol in symbols}
        return runner.run_once(identity, quotes=quotes, rules=rules)

    with ExitStack() as stack:
        host = open_host(stack)
        runner, store = host.runner, host.store
        setup_pair(runner, store)
        fill_plan(runner, store, official, create(runner, 'A', {'AAPL': D('.25')}, 'initial-inventory'))
        rotate = runner.executor.create(account_id=ACCOUNT, strategy_id='A', strategy_version='1',
            trading_session='test-session', rebalance_id='sell-then-two-buys', weights={},
            owned_symbols=('AAPL', 'MSFT', 'JPM'), now=NOW,
            quantity_deltas={'AAPL': D(-1), 'MSFT': D(1), 'JPM': D(1)})
        turn(runner, rotate, 'AAPL', 'MSFT', 'JPM')
        sale, = store.orders(rotate)
        sold = sale['record'].snapshot.intent
        assert sold.side is Side.SELL
        official.fill(sold.client_order_id)
        assert turn(runner, rotate).phase == 'BLOCKED'
        assert store.cash(ACCOUNT, 'A', reserve=False) == D(400)
        assert sleeve(host.capital, 'A').pending_decisions
        withdrawal = host.capital.change(account_id=ACCOUNT, event_id='before-buy-sizing',
            source='A', destination=None, amount=D(400))
        assert withdrawal.status == 'INSUFFICIENT_CAPITAL' and withdrawal.applied == 0

        independent = create(runner, 'B', {'IWM': D('.25')}, 'independent-decision')
        turn(runner, independent, 'IWM')
        official.fill(official.post_ids[-1])
        assert turn(runner, independent, 'IWM').phase == 'COMPLETE'
        assert store.holdings(ACCOUNT, 'B')['IWM'] == D(1)
        assert not sleeve(host.capital, 'B').pending_decisions

        turn(runner, rotate, 'MSFT')
        first_buy = sleeve(host.capital, 'A')
        assert first_buy.pending_decisions and first_buy.withdrawable == 0
        assert first_buy.reserved == D(101)

    with ExitStack() as stack:
        host = open_host(stack)
        runner, store = host.runner, host.store
        assert sleeve(host.capital, 'A').pending_decisions
        turn(runner, rotate, 'JPM')
        prepared = sleeve(host.capital, 'A')
        assert not prepared.pending_decisions
        assert prepared.reserved == D(202)
        assert prepared.withdrawable == D(198)
        wallet = D(official.cash)
        moved = host.capital.change(account_id=ACCOUNT, event_id='after-both-buy-reservations',
            source='A', destination='B', amount=D(198))
        assert moved.status == 'COMPLETE' and moved.applied == D(198)
        assert D(official.cash) == wallet
        assert host.store.cash(ACCOUNT, 'A') == 0
        assert host.store.cash(ACCOUNT, 'A', reserve=False) == D(202)
        assert host.store.cash(ACCOUNT, 'B', reserve=False) == D(498)
        assert host.store.allocated_capital(ACCOUNT) == D(800)
