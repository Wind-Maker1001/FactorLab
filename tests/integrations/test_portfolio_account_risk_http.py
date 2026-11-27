"""Portfolio risk exercised through both brokers' deterministic HTTP transports."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from threading import Barrier
from types import SimpleNamespace

import pytest

from factorlab.integrations.alpaca.corporate_actions import CorporateReceiptJournal
from factorlab.integrations.alpaca.strategy_instances import StrategyInstances
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.adapter import FuturesAdapter
from factorlab.integrations.binance_futures_trading.fake import FakeTransport, MS, NOW as BINANCE_NOW
from factorlab.integrations.binance_futures_trading.protocol import Credentials, ExchangeError, FuturesClient
from factorlab.trading.capital import CapitalPolicyMode
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side, ZERO
from factorlab.trading.portfolio_execution import (
    ExecutionQuote, InstrumentRules, PortfolioExecutor, PortfolioStore, SubmissionNotAttempted,
)
from factorlab.trading.portfolio_risk import PortfolioRiskLimits
from factorlab.trading.storage import PaperStore
from test_alpaca_execution import adapter as alpaca_http_adapter
from test_shared_portfolio_execution import ACCOUNT, SharedOfficial, open_runner, qualify, setup_pair


SESSION = 'http-risk-session'


class AlpacaRiskHTTP(SharedOfficial):
    pending_cancel = None
    lose_cancel_reply = False

    @property
    def equity(self):
        return D(self.cash) + sum(D(row['qty']) * D(row['current_price']) for row in self.positions)

    def __call__(self, method, path, params, body):
        if method == 'DELETE' and self.lose_cancel_reply:
            row = next(row for row in self.orders.values() if path.endswith(row['id']))
            self.pending_cancel = row['client_order_id']
            raise TimeoutError('offline cancellation receipt lost')
        if path == '/v2/orders:by_client_order_id' and params['client_order_id'] == self.pending_cancel:
            return 404, None
        status, payload = super().__call__(method, path, params, body)
        if path == '/v2/account':
            payload.update(equity=str(self.equity), initial_margin='0')
        elif path == '/v2/orders' and method == 'POST':
            at = wall_clock().isoformat()
            timestamps = dict(created_at=at, submitted_at=at, updated_at=at)
            self.orders[body['client_order_id']].update(timestamps)
            payload.update(timestamps)
        elif method == 'DELETE':
            row = next(row for row in self.orders.values() if path.endswith(row['id']))
            row['updated_at'] = wall_clock().isoformat()
        return status, payload

    def fill(self, client_id, cumulative=None):
        super().fill(client_id, cumulative)
        at = wall_clock().isoformat()
        self.orders[client_id].update(filled_at=at, updated_at=at)
        self.activities[-1]['transaction_time'] = at

    def confirm_cancel(self):
        self.orders[self.pending_cancel].update(status='canceled', updated_at=wall_clock().isoformat())
        self.pending_cancel = None


def wall_clock():
    return datetime.now(timezone.utc)


def alpaca_runner(stack, path, http):
    runner, store = open_runner(stack, path, http)
    runner.clock = runner.executor.clock = wall_clock
    return runner, store


def direct(executor, *, account, owner, market, symbol, key, quantity, side=Side.BUY, price=D(100),
           emergency_only=False):
    tif = 'DAY' if market == Market.ALPACA_US_EQUITY else 'GTC'
    intent = OrderIntent(market, symbol, side, OrderType.LIMIT, quantity, price, key,
                         time_in_force=tif, reduce_only=market == Market.BINANCE_USDM and side == Side.SELL)
    return executor.create(account_id=account, strategy_id=owner, strategy_version='1',
        trading_session=SESSION, rebalance_id=key, weights={}, owned_symbols=[symbol],
        now=executor.clock(), direct_orders=[(intent, price)], concurrent_direct_orders=True,
        emergency_only=emergency_only)


def alpaca_direct(runner, owner, symbol, key, *, quantity=D(1), side=Side.BUY, price=D(100),
                  emergency_only=False):
    return direct(runner.executor, account=ACCOUNT, owner=owner, market=Market.ALPACA_US_EQUITY,
                  symbol=symbol, key=key, quantity=quantity, side=side, price=price,
                  emergency_only=emergency_only)


def order_inputs(symbol, now, *, tif, price=D(100), age=timedelta()):
    timestamp = now - age
    return dict(quotes={symbol: ExecutionQuote(symbol, price, price, timestamp, timestamp, SESSION, 'offline-http')},
                rules={symbol: InstrumentRules(D('.01'), D(1), time_in_force=tif)})


def alpaca_advance(runner, identity, symbol='AAPL', *, price=D(100), age=timedelta()):
    return runner.run_once(identity, **order_inputs(symbol, runner.clock(), tif='DAY', price=price, age=age))


def alpaca_snapshot(runner, http):
    return runner.executor.portfolio_risk_snapshot(ACCOUNT, official_equity=http.equity)


def test_alpaca_official_cash_activities_adjust_nav_across_restart(tmp_path):
    class CashHTTP(AlpacaRiskHTTP):
        unavailable = False

        def __call__(self, method, path, params, body):
            if self.unavailable and path == '/v2/account/activities':
                return 503, {'message': 'activity history unavailable'}
            return super().__call__(method, path, params, body)

    http = CashHTTP()
    date = wall_clock().date().isoformat()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_daily_loss=D(20)))
        assert alpaca_snapshot(runner, http).daily_loss == 0
        http.cash = str(D(http.cash) + D(200))
        http.nontrade = [dict(id='external-in', activity_type='CSD', date=date, net_amount='200')]
        assert alpaca_snapshot(runner, http).daily_loss == 0
        http.cash = str(D(http.cash) - D(300))
        http.nontrade.append(dict(id='external-out', activity_type='CSW', date=date, net_amount='-300'))
        assert alpaca_snapshot(runner, http).drawdown == 0
        http.unavailable = True
        assert alpaca_snapshot(runner, http).daily_loss is None
        http.unavailable = False

    with ExitStack() as stack:
        runner, _ = alpaca_runner(stack, tmp_path, http)
        assert alpaca_snapshot(runner, http).daily_loss == 0
        http.cash = str(D(http.cash) - D(25))
        loss = alpaca_snapshot(runner, http)
        assert loss.daily_loss == D(25)
        assert loss.drawdown == D('.025')
        http.nontrade.append(dict(id='ambiguous-journal', activity_type='JNLC', date=date, net_amount='5'))
        assert alpaca_snapshot(runner, http).daily_loss is None


def test_alpaca_concurrent_orders_share_the_account_gross_limit(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, _ = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, runner.store)
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_gross_exposure=D(150)))

    starts = Barrier(2)

    def reserve(owner, symbol):
        with ExitStack() as stack:
            runner, _ = alpaca_runner(stack, tmp_path, http)
            starts.wait(timeout=10)
            try:
                return alpaca_direct(runner, owner, symbol, 'open-' + owner)
            except ValueError as error:
                return error

    with ThreadPoolExecutor(max_workers=2) as workers:
        submissions = [workers.submit(reserve, 'A', 'AAPL'), workers.submit(reserve, 'B', 'MSFT')]
    results = [submission.result() for submission in submissions]
    accepted, = [result for result in results if not isinstance(result, ValueError)]
    rejected, = [result for result in results if isinstance(result, ValueError)]
    assert str(rejected) == 'PORTFOLIO_GROSS_EXPOSURE_LIMIT'
    assert http.post_ids == []

    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        symbol, = store.plan(accepted)['request']['owned']
        alpaca_advance(runner, accepted, symbol)
        assert len(http.post_ids) == 1
        snapshot = alpaca_snapshot(runner, http)
        assert snapshot.gross_exposure == 0
        assert snapshot.pending_open_exposure == snapshot.projected_gross_exposure == D(100)


def test_alpaca_same_symbol_limit_and_owned_exit(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_symbol_exposure=D(150)))
        opened = alpaca_direct(runner, 'A', 'AAPL', 'a-open')
        alpaca_advance(runner, opened)
        http.fill('a-open')
        alpaca_advance(runner, opened)
        with pytest.raises(ValueError, match='PORTFOLIO_SYMBOL_EXPOSURE_LIMIT'):
            alpaca_direct(runner, 'B', 'AAPL', 'b-open')
        assert http.post_ids == ['a-open']
        with pytest.raises(ValueError, match='owned inventory'):
            alpaca_direct(runner, 'B', 'AAPL', 'b-exit', side=Side.SELL)
        http.positions[0]['current_price'] = '180'
        assert alpaca_snapshot(runner, http).gross_exposure == D(180)
        exit_id = alpaca_direct(runner, 'A', 'AAPL', 'a-exit', side=Side.SELL, price=D(180))
        alpaca_advance(runner, exit_id, price=D(180))
        http.fill('a-exit')
        alpaca_advance(runner, exit_id, price=D(180))
        assert http.post_ids == ['a-open', 'a-exit']
        assert alpaca_snapshot(runner, http).gross_exposure == 0


def test_alpaca_concentration_uses_confirmed_diversification(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        for owner, symbol in (('A', 'AAPL'), ('B', 'MSFT')):
            key = 'owned-' + symbol
            opened = alpaca_direct(runner, owner, symbol, key)
            alpaca_advance(runner, opened, symbol)
            http.fill(key)
            alpaca_advance(runner, opened, symbol)
        runner.executor.configure_portfolio_risk(ACCOUNT,
            PortfolioRiskLimits(max_symbol_concentration=D('.6')))
        assert alpaca_snapshot(runner, http).symbol_concentration == {'AAPL': D('.5'), 'MSFT': D('.5')}
        with pytest.raises(ValueError, match='PORTFOLIO_SYMBOL_CONCENTRATION_LIMIT'):
            alpaca_direct(runner, 'B', 'AAPL', 'add-to-concentration')
        assert http.post_ids == ['owned-AAPL', 'owned-MSFT']


@pytest.mark.parametrize(('limits', 'reason'), [
    (PortfolioRiskLimits(max_strategy_exposure=D(99)), 'PORTFOLIO_STRATEGY_EXPOSURE_LIMIT'),
    (PortfolioRiskLimits(paused_strategies=('A',)), 'PORTFOLIO_STRATEGY_PAUSED'),
    (PortfolioRiskLimits(paused_symbols=('AAPL',)), 'PORTFOLIO_SYMBOL_PAUSED'),
])
def test_alpaca_scoped_rules_only_block_affected_openings(tmp_path, limits, reason):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        runner.executor.configure_portfolio_risk(ACCOUNT, limits)
        with pytest.raises(ValueError, match=reason):
            alpaca_direct(runner, 'A', 'AAPL', 'blocked-a')
        allowed = alpaca_direct(runner, 'B', 'MSFT', 'allowed-b', quantity=D('.5'))
        alpaca_advance(runner, allowed, 'MSFT')
        assert http.post_ids == ['allowed-b']


def test_alpaca_partial_fill_retains_uncertain_remainder_through_restart(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_gross_exposure=D(160)))
        opened = alpaca_direct(runner, 'A', 'AAPL', 'partial-open')
        alpaca_advance(runner, opened)
        http.fill('partial-open', D('.4'))
        alpaca_advance(runner, opened)
        partial = alpaca_snapshot(runner, http)
        assert partial.gross_exposure == partial.net_exposure == D(40)
        assert partial.pending_open_exposure == D(60)
        assert partial.projected_gross_exposure == D(100)
        http.lose_cancel_reply = True
        runner.executor.cancel(opened, 'partial-open')
        unresolved = alpaca_snapshot(runner, http)
        assert unresolved.pending_cancel_exposure == D(60)
        assert unresolved.projected_gross_exposure == D(100)

    with ExitStack() as stack:
        runner, _ = alpaca_runner(stack, tmp_path, http)
        with pytest.raises(ValueError, match='PORTFOLIO_GROSS_EXPOSURE_LIMIT'):
            alpaca_direct(runner, 'B', 'MSFT', 'over-limit-until-canceled')
        assert alpaca_snapshot(runner, http).pending_cancel_exposure == D(60)
        http.confirm_cancel()
        alpaca_advance(runner, opened)
        resolved = alpaca_snapshot(runner, http)
        assert resolved.pending_cancel_exposure == resolved.pending_open_exposure == 0
        assert resolved.projected_gross_exposure == D(40)
        allowed = alpaca_direct(runner, 'B', 'MSFT', 'allowed-after-cancel')
        alpaca_advance(runner, allowed, 'MSFT')
        assert http.post_ids == ['partial-open', 'allowed-after-cancel']


def test_alpaca_owner_tick_reconciles_partial_fill_before_risk_cancellation(tmp_path):
    class RecordingHTTP(AlpacaRiskHTTP):
        def __init__(self):
            super().__init__()
            self.requests = []

        def __call__(self, method, path, params, body):
            self.requests.append((method, path))
            if path == '/v2/corporate_actions/announcements':
                return 200, []
            return super().__call__(method, path, params, body)

    http = RecordingHTTP()
    configuration = dict(instances=[], releases=tmp_path / 'releases', trusted_python=True)

    def open_host(stack):
        actual, _ = alpaca_http_adapter(tmp_path, http)
        stack.callback(actual.close)
        host = StrategyInstances(tmp_path, actual, configuration, clock=wall_clock)
        stack.callback(host.close)
        return host

    def tick(host):
        facts = host.adapter.read_account_details()
        host.tick(SimpleNamespace(), {}, wall_clock(), facts)

    with ExitStack() as stack:
        host = open_host(stack)
        runner, store = host.runner, host.store
        setup_pair(runner, store)
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(
            max_gross_exposure=D(100), cancel_open_on_breach=True))
        original = alpaca_direct(runner, 'A', 'AAPL', 'tick-partial', quantity=D('.2'))
        alpaca_advance(runner, original)
        http.fill('tick-partial', D('.1'))

        http.requests.clear()
        tick(host)
        assert http.requests.count(('GET', '/v2/positions')) == 1
        assert http.requests.count(('GET', '/v2/orders:by_client_order_id')) == 1
        assert all(method != 'DELETE' for method, _ in http.requests)
        assert store.holdings(ACCOUNT, 'A')['AAPL'] == D('.1')
        partial = alpaca_snapshot(runner, http)
        assert partial.gross_exposure == partial.pending_open_exposure == D(10)
        assert partial.projected_gross_exposure == D(20)
        assert 'ownership' not in partial.missing

        # A mark move puts the portfolio over 100 USD despite the same residual.
        http.positions[0]['current_price'] = '1000'
        http.lose_cancel_reply = True
        tick(host)
        assert http.pending_cancel == 'tick-partial'
        assert sum(method == 'DELETE' for method, _ in http.requests) == 1
        uncertain = alpaca_snapshot(runner, http)
        assert uncertain.pending_cancel_exposure == D(10)
        assert uncertain.projected_gross_exposure == D(110)

    with ExitStack() as stack:
        host = open_host(stack)
        runner, store = host.runner, host.store
        http.requests.clear()
        tick(host)
        assert all(method != 'DELETE' for method, _ in http.requests)
        assert alpaca_snapshot(runner, http).pending_cancel_exposure == D(10)
        http.confirm_cancel()
        http.lose_cancel_reply = False
        http.positions[0]['current_price'] = '100'
        tick(host)
        assert alpaca_snapshot(runner, http).pending_open_exposure == ZERO

        pending = alpaca_direct(runner, 'B', 'MSFT', 'tick-unknown-owner', quantity=D('.2'))
        alpaca_advance(runner, pending, 'MSFT')
        http.positions.append(dict(symbol='NVDA', qty='.1', avg_entry_price='100', current_price='100'))
        tick(host)
        assert http.orders['tick-unknown-owner']['status'] == 'canceled'
        assert 'ownership' in alpaca_snapshot(runner, http).missing
        assert 'NVDA' not in store.holdings(ACCOUNT)
        with pytest.raises(ValueError, match='PORTFOLIO_RISK_OWNERSHIP'):
            alpaca_direct(runner, 'B', 'MSFT', 'tick-opening-with-unknown-owner', quantity=D('.1'))


def test_alpaca_tqqq_dividend_and_split_keep_nominal_cash_separate(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        store.capital_policy.configure(account_id=ACCOUNT, currency='USD', budget=D(1000), fee_rate=D(0),
                                       mode=CapitalPolicyMode.SHARED_SAME_DIRECTION)
        qualify(runner, store, 'A', ('TQQQ',))
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_gross_exposure=D(300),
            min_available_cash=D(900)), economic_multipliers={'TQQQ': D(3)})
        opened = alpaca_direct(runner, 'A', 'TQQQ', 'tqqq-open')
        alpaca_advance(runner, opened, 'TQQQ')
        http.fill('tqqq-open')
        alpaca_advance(runner, opened, 'TQQQ')
        before = alpaca_snapshot(runner, http)
        assert before.gross_exposure == D(300)
        assert before.cash == D(900)
        assert before.official_equity == D(1000)

        today = wall_clock().date()
        dividend_date = (today + timedelta(days=1)).isoformat()
        split_date = (today + timedelta(days=2)).isoformat()
        http.nontrade = [dict(id='dividend', activity_type='DIV', symbol='TQQQ', date=dividend_date,
                             net_amount='1.25', qty='1'),
                         dict(id='split', activity_type='SSP', symbol='TQQQ', date=split_date,
                             net_amount='0', qty='1')]
        http.cash = '901.25'
        http.positions[0].update(qty='2', avg_entry_price='50', current_price='50')
        announcements = [dict(id='split', ca_type='split', initiating_symbol='TQQQ',
                              ex_date=split_date, old_rate='1', new_rate='2')]
        receipts = runner.adapter.read_corporate_activities(after=today.isoformat() + 'T00:00:00Z')
        journal = CorporateReceiptJournal(store, ACCOUNT)
        assert journal.reconcile_account(runner.executor, announcements=announcements, receipts=receipts) == {}
        after = alpaca_snapshot(runner, http)
        assert after.gross_exposure == D(300)
        assert after.cash == D('901.25')
        assert after.official_equity == D('1001.25')
        assert store.holdings(ACCOUNT, 'A') == {'TQQQ': D(2)}


def test_alpaca_unprepared_buy_is_unknown_risk_but_owned_reduction_remains_legal(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        owned = alpaca_direct(runner, 'A', 'AAPL', 'inventory-before-pending', quantity=D('.5'))
        alpaca_advance(runner, owned)
        http.fill('inventory-before-pending')
        alpaca_advance(runner, owned)
        unprepared = runner.executor.create(account_id=ACCOUNT, strategy_id='B', strategy_version='1',
            trading_session=SESSION, rebalance_id='pending-msft', weights={}, owned_symbols=['MSFT'],
            quantity_deltas={'MSFT': D(1)}, now=runner.clock())
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_gross_exposure=D(100)))
        snapshot = alpaca_snapshot(runner, http)
        assert 'pending_capital' in snapshot.missing
        assert snapshot.projected_gross_exposure is None
        with pytest.raises(ValueError, match='PORTFOLIO_RISK_PENDING_CAPITAL'):
            alpaca_direct(runner, 'A', 'AAPL', 'opening-before-buy-sizing', quantity=D('.1'))
        exit_id = alpaca_direct(runner, 'A', 'AAPL', 'exit-before-buy-sizing',
                                quantity=D('.5'), side=Side.SELL)
        alpaca_advance(runner, exit_id)
        http.fill('exit-before-buy-sizing')
        assert alpaca_advance(runner, exit_id).phase == 'COMPLETE'
        assert http.post_ids == ['inventory-before-pending', 'exit-before-buy-sizing']
        runner.executor.abandon(unprepared)
        assert alpaca_snapshot(runner, http).projected_gross_exposure == ZERO


@pytest.mark.parametrize('market', ['alpaca', 'binance'])
@pytest.mark.parametrize('gross_limit', [D(150), D(500)])
def test_pending_capital_defers_original_order_until_preparation_recovers(tmp_path, market, gross_limit):
    with ExitStack() as stack:
        if market == 'alpaca':
            http = AlpacaRiskHTTP()
            runner, store = alpaca_runner(stack, tmp_path, http)
            setup_pair(runner, store)
            executor, account, symbol = runner.executor, ACCOUNT, 'MSFT'
            prepared = alpaca_direct(runner, 'A', 'AAPL', 'prepared-before-missing')
            advance_prepared = lambda: alpaca_advance(runner, prepared)
            advance_pending = lambda identity: alpaca_advance(runner, identity, symbol)
        else:
            http = BinanceRiskHTTP()
            executor = binance_runner(stack, tmp_path, http, lambda: http.now)
            store, account, symbol = executor.store, 'offline', 'NVDAUSDT'
            executor.capital_policy.configure(account_id=account, currency='USDT', budget=D(1000),
                fee_rate=ZERO, mode=CapitalPolicyMode.SHARED_SAME_DIRECTION)
            for owner in ('A', 'B'):
                executor.allocate(account_id=account, strategy_id=owner, allocation_id=owner + '-usdt',
                    capital=D(400), initial_cash=D(400), official_equity=D(1000))
                executor.capital_policy.bind(account_id=account, strategy_id=owner, instance_id=owner,
                    allocation_id=owner + '-usdt')
            executor.adapter.sync_funding(start_time=http.now, end_time=http.now)
            prepared = binance_direct(executor, 'A', 'prepared-before-missing')
            advance_prepared = lambda: binance_advance(executor, prepared, http)
            advance_pending = lambda identity: binance_advance(executor, identity, http)
        pending = executor.create(account_id=account, strategy_id='B', strategy_version='1',
            trading_session=SESSION, rebalance_id='missing-quote', weights={}, owned_symbols=[symbol],
            quantity_deltas={symbol: D(1)}, now=executor.clock())
        executor.configure_portfolio_risk(account, PortfolioRiskLimits(max_gross_exposure=D(500)))
        advance_prepared()
        order, = store.orders(prepared)
        original = order['record'].snapshot.intent.client_order_id
        assert order['record'].snapshot.status.value == 'NEW'
        assert not order['attempted'] and not order['uncertain']
        assert store.plan(prepared)['reasons'][order['record'].snapshot.intent.instrument_id] == (
            'PORTFOLIO_RISK_PENDING_CAPITAL')
        advance_pending(pending)
        if market == 'binance':
            pending_order, = store.orders(pending)
            http.trade_update(pending_order['record'].snapshot.intent.client_order_id)
            advance_pending(pending)
        assert 'pending_capital' not in executor.portfolio_risk_snapshot(account).missing
        executor.configure_portfolio_risk(account, PortfolioRiskLimits(max_gross_exposure=gross_limit))
        advance_prepared()
        order, = store.orders(prepared)
        assert order['record'].snapshot.intent.client_order_id == original
        if gross_limit == D(150):
            assert order['record'].snapshot.status.value == 'REJECTED'
            assert store.plan(prepared)['reasons'][order['record'].snapshot.intent.instrument_id] == (
                'PORTFOLIO_GROSS_EXPOSURE_LIMIT')
            assert executor.portfolio_risk_snapshot(account).projected_gross_exposure == D(100)
        else:
            assert order['record'].snapshot.status.value == 'SUBMITTED'
            assert executor.portfolio_risk_snapshot(account).projected_gross_exposure == D(200)


def test_alpaca_recorded_direct_admission_keeps_pending_capital_retryable(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        executor = runner.executor
        pending = executor.create(account_id=ACCOUNT, strategy_id='B', strategy_version='1',
            trading_session=SESSION, rebalance_id='unprepared-direct-peer', weights={},
            owned_symbols=['MSFT'], quantity_deltas={'MSFT': D(1)}, now=executor.clock())
        executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_gross_exposure=D(500)))
        intent = OrderIntent(Market.ALPACA_US_EQUITY, 'AAPL', Side.BUY, OrderType.LIMIT,
            D(1), D(100), 'recorded-pending-admission', time_in_force='DAY')
        identity = executor.create(account_id=ACCOUNT, strategy_id='A', strategy_version='1',
            trading_session=SESSION, rebalance_id='recorded-pending-admission', weights={},
            owned_symbols=['AAPL'], now=executor.clock(), direct_orders=[(intent, D(100))],
            concurrent_direct_orders=True, record_risk_rejection=True)
        assert store.plan(identity)['phase'] == 'WAITING'
        order, = store.orders(identity)
        assert order['record'].snapshot.status.value == 'NEW'
        alpaca_advance(runner, pending, 'MSFT')
        alpaca_advance(runner, identity)
        assert http.post_ids.count(intent.client_order_id) == 1


def test_alpaca_partial_preparation_keeps_original_child_retryable(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        executor = runner.executor
        identity = executor.create(account_id=ACCOUNT, strategy_id='A', strategy_version='1',
            trading_session=SESSION, rebalance_id='partial-prepare', weights={},
            owned_symbols=['AAPL', 'MSFT'], quantity_deltas={'AAPL': D(1), 'MSFT': D(1)},
            now=executor.clock())
        executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_gross_exposure=D(500)))
        alpaca_advance(runner, identity, 'AAPL')
        first, = store.orders(identity)
        original = first['record'].snapshot.intent.client_order_id
        assert first['record'].snapshot.status.value == 'NEW'
        assert http.post_ids == []
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        inputs = order_inputs('AAPL', runner.clock(), tif='DAY')
        other = order_inputs('MSFT', runner.clock(), tif='DAY')
        for key in inputs:
            inputs[key].update(other[key])
        runner.run_once(identity, **inputs)
        assert original in http.post_ids
        assert len(http.post_ids) == len(set(http.post_ids)) == 2
        assert all(order['record'].snapshot.status.value == 'SUBMITTED'
                   for order in store.orders(identity))


def test_alpaca_stale_quote_cannot_dispatch_reserved_order(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_order_notional=D(100)))
        opened = alpaca_direct(runner, 'A', 'AAPL', 'stale-quote')
        result = alpaca_advance(runner, opened, age=timedelta(seconds=6))
        assert result.reasons['AAPL'] == 'STALE_QUOTE'
        assert http.post_ids == []
        alpaca_advance(runner, opened)
        assert http.post_ids == ['stale-quote']


def test_alpaca_account_fact_age_recovers_after_fresh_observation(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_account_age_seconds=5))
        runner.executor.clock = lambda: wall_clock() + timedelta(seconds=10)
        with pytest.raises(ValueError, match='PORTFOLIO_RISK_ACCOUNT_AGE'):
            alpaca_direct(runner, 'A', 'AAPL', 'old-account')
        assert http.post_ids == []
        runner.executor.clock = wall_clock
        opened = alpaca_direct(runner, 'A', 'AAPL', 'fresh-account')
        alpaca_advance(runner, opened)
        assert http.post_ids == ['fresh-account']


def test_alpaca_account_stop_cancels_opening_and_retains_own_exit_after_restart(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        owned = alpaca_direct(runner, 'A', 'AAPL', 'already-owned')
        alpaca_advance(runner, owned)
        http.fill('already-owned')
        alpaca_advance(runner, owned)
        pending = alpaca_direct(runner, 'B', 'MSFT', 'pending-opening')
        alpaca_advance(runner, pending, 'MSFT')
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(
            stop_new_risk=True, cancel_open_on_breach=True))
        runner.executor.enforce_portfolio_risk(ACCOUNT, official_equity=http.equity)
        assert http.orders['pending-opening']['status'] == 'canceled'

    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        with pytest.raises(ValueError, match='PORTFOLIO_ACCOUNT_STOP'):
            alpaca_direct(runner, 'B', 'MSFT', 'opening-after-stop')
        exit_id = alpaca_direct(runner, 'A', 'AAPL', 'exit-after-stop', side=Side.SELL)
        alpaca_advance(runner, exit_id)
        http.fill('exit-after-stop')
        assert alpaca_advance(runner, exit_id).phase == 'COMPLETE'
        assert http.post_ids == ['already-owned', 'pending-opening', 'exit-after-stop']
        assert alpaca_snapshot(runner, http).gross_exposure == 0


def test_alpaca_risk_snapshot_consumes_updated_public_capital_view(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        store.capital_policy.configure(account_id=ACCOUNT, currency='USD', budget=D(1000), fee_rate=D(0),
                                       mode=CapitalPolicyMode.SHARED_SAME_DIRECTION)
        qualify(runner, store, 'A', ('AAPL',))
        before = alpaca_snapshot(runner, http)
        qualify(runner, store, 'B', ('MSFT',))
        after = alpaca_snapshot(runner, http)
        assert before.capital.allocated == D(400)
        assert after.capital.allocated == D(800)
        assert after.official_equity == before.official_equity == D(1000)
        assert after.projected_gross_exposure == 0


def test_alpaca_rechecks_new_daily_loss_at_the_http_dispatch_boundary(tmp_path):
    class LateLoss(AlpacaRiskHTTP):
        def __call__(self, method, path, params, body):
            if method == 'GET' and path == '/v2/assets/MSFT':
                self.cash = D(970)
            return super().__call__(method, path, params, body)

    http = LateLoss()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        runner.executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(max_daily_loss=D(20)))
        identity = alpaca_direct(runner, 'B', 'MSFT', 'fresh-loss')
        result = alpaca_advance(runner, identity, 'MSFT')
        assert result.reasons['MSFT'] == 'PORTFOLIO_DAILY_LOSS_LIMIT'
        assert http.post_ids == []
        assert alpaca_snapshot(runner, http).pending_open_exposure == ZERO


def test_alpaca_drawdown_arms_configured_owner_reduction(tmp_path):
    http = AlpacaRiskHTTP()
    with ExitStack() as stack:
        runner, store = alpaca_runner(stack, tmp_path, http)
        setup_pair(runner, store)
        executor = runner.executor
        executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(
            max_drawdown=D('.02'), emergency_reduction_reasons=('PORTFOLIO_DRAWDOWN_LIMIT',)))
        opened = alpaca_direct(runner, 'A', 'AAPL', 'before-drawdown')
        alpaca_advance(runner, opened)
        http.fill('before-drawdown')
        alpaca_advance(runner, opened)
        exit_id = alpaca_direct(runner, 'A', 'AAPL', 'configured-exit', side=Side.SELL,
                                price=D(70), emergency_only=True)

        def evaluate_emergency():
            quote = order_inputs('AAPL', runner.clock(), tif='DAY', price=D(70))
            return executor.run_emergency_reduction(exit_id, equity=http.equity, now=runner.clock(), **quote)

        assert evaluate_emergency() is None
        assert http.post_ids == ['before-drawdown']
        http.positions[0]['current_price'] = '70'
        assert alpaca_snapshot(runner, http).drawdown == D('.03')
        with pytest.raises(ValueError, match='PORTFOLIO_DRAWDOWN_LIMIT'):
            alpaca_direct(runner, 'B', 'MSFT', 'opening-in-drawdown')
        assert evaluate_emergency() is not None
        assert http.post_ids == ['before-drawdown', 'configured-exit']


class BinanceRiskHTTP(FakeTransport):
    initial_margin = D(0)

    def __init__(self):
        super().__init__()
        self.income = []
        self.now = BINANCE_NOW

    def __call__(self, request, timeout):
        response = super().__call__(request, timeout)
        if request.path == '/fapi/v1/order' and request.method == 'POST' and response.status == 200:
            row = json.loads(response.body)
            row['updateTime'] = int(self.now.timestamp() * 1000)
            self.orders[row['clientOrderId']] = row
            return Response(200, {}, json.dumps(row).encode())
        if request.path != '/fapi/v3/account':
            return response
        account = json.loads(response.body)
        asset, = account['assets']
        if self.initial_margin is None:
            del asset['initialMargin']
        else:
            asset.update(initialMargin=str(self.initial_margin),
                         availableBalance=str(self.cash - self.initial_margin))
        return Response(200, {}, json.dumps(account).encode())


class BinancePortfolioFixture(FuturesAdapter):
    """Expose the offline protocol journal through the executor's adapter contract."""

    def margin_facts(self):
        return self.facts

    observed_margin_facts = margin_facts

    def query_order(self, client_order_id):
        try:
            self.order(client_order_id)
        except KeyError:
            try:
                self.client.request('GET', '/fapi/v1/order',
                                    dict(symbol='NVDAUSDT', origClientOrderId=client_order_id))
            except ExchangeError as error:
                if error.code != -2013:
                    raise
                raise SubmissionNotAttempted(client_order_id) from error
            raise AssertionError('unexpected broker order without an offline protocol intent')
        return super().query_order(client_order_id)


def binance_runner(stack, path, http, clock):
    client = FuturesClient(Credentials('offline-key', 'offline-secret'), transport=http,
                           clock=lambda: clock().timestamp())
    protocol = stack.enter_context(closing(PaperStore(path / 'futures.sqlite')))
    adapter = BinancePortfolioFixture(client, protocol, clock=clock)
    adapter.reconcile()
    store = stack.enter_context(closing(PortfolioStore(path / 'portfolio.sqlite')))
    return PortfolioExecutor(adapter, store, quote_currency='USDT', clock=clock)


def binance_direct(executor, owner, key, *, quantity=D(1), side=Side.BUY):
    return direct(executor, account='offline', owner=owner, market=Market.BINANCE_USDM,
                  symbol='NVDAUSDT', key=key, quantity=quantity, side=side)


def binance_advance(executor, identity, http):
    now = executor.clock()
    return executor.run_once(identity, equity=http.cash, now=now,
                             **order_inputs('NVDAUSDT', now, tif='GTC'))


def test_binance_official_transfers_overlap_restart_and_retention_gap(tmp_path):
    http = BinanceRiskHTTP()
    with ExitStack() as stack:
        executor = binance_runner(stack, tmp_path, http, lambda: http.now)
        executor.capital_policy.configure(account_id='offline', currency='USDT',
                                          budget=D(1000), fee_rate=D(0))
        executor.configure_portfolio_risk('offline', PortfolioRiskLimits(max_daily_loss=D(20)))
        assert executor.portfolio_risk_snapshot('offline').daily_loss == 0
        http.now += timedelta(seconds=1)
        http.funding_update('200', symbol='', income_type='TRANSFER',
                            time_ms=int(http.now.timestamp() * 1000))
        assert executor.portfolio_risk_snapshot('offline').daily_loss == 0
        http.now += timedelta(seconds=1)
        http.funding_update('-300', symbol='', income_type='TRANSFER',
                            time_ms=int(http.now.timestamp() * 1000))
        assert executor.portfolio_risk_snapshot('offline').drawdown == 0

    with ExitStack() as stack:
        executor = binance_runner(stack, tmp_path, http, lambda: http.now)
        assert executor.portfolio_risk_snapshot('offline').daily_loss == 0
        http.now += timedelta(seconds=1)
        http.funding_update('-25', time_ms=int(http.now.timestamp() * 1000))
        loss = executor.portfolio_risk_snapshot('offline')
        assert loss.daily_loss == D(25)
        assert loss.drawdown == D('.025')
        http.now += timedelta(days=91)
        assert executor.portfolio_risk_snapshot('offline').daily_loss is None


def test_binance_official_margin_missing_margin_and_funding_through_http(tmp_path):
    http = BinanceRiskHTTP()
    with ExitStack() as stack:
        executor = binance_runner(stack, tmp_path, http, lambda: http.now)
        policy = executor.capital_policy
        policy.configure(account_id='offline', currency='USDT', budget=D(1000), fee_rate=D(0),
                         mode=CapitalPolicyMode.SHARED_SAME_DIRECTION)
        for owner in ('A', 'B'):
            executor.allocate(account_id='offline', strategy_id=owner, allocation_id=owner + '-usdt',
                              capital=D(400), initial_cash=D(400), official_equity=D(1000))
            policy.bind(account_id='offline', strategy_id=owner, instance_id=owner,
                        allocation_id=owner + '-usdt')
        executor.configure_portfolio_risk('offline', PortfolioRiskLimits(max_margin=D(150),
            max_gross_exposure=D(150), max_daily_loss=D(20), max_drawdown=D('.05')))
        executor.adapter.sync_funding(start_time=http.now, end_time=http.now)
        initial = executor.portfolio_risk_snapshot('offline', official_equity=http.cash)
        assert initial.drawdown == ZERO
        opened = binance_direct(executor, 'A', 'binance-open')
        with pytest.raises(ValueError, match='PORTFOLIO_GROSS_EXPOSURE_LIMIT'):
            binance_direct(executor, 'B', 'binance-portfolio-block')
        binance_advance(executor, opened, http)
        http.trade_update('binance-open')
        http.initial_margin = D(100)
        binance_advance(executor, opened, http)
        before = executor.portfolio_risk_snapshot('offline', official_equity=http.cash)
        assert before.gross_exposure == D(100)
        assert before.initial_margin == D(100)
        assert before.drawdown == (initial.official_equity - before.official_equity) / initial.official_equity

        http.initial_margin = D(151)
        with pytest.raises(ValueError, match='PORTFOLIO_MARGIN_LIMIT'):
            binance_direct(executor, 'B', 'binance-margin-block', quantity=D('.1'))
        http.initial_margin = None
        missing = executor.portfolio_risk_snapshot('offline', official_equity=http.cash)
        assert missing.initial_margin is None
        assert 'margin' in missing.missing
        with pytest.raises(ValueError, match='PORTFOLIO_UNKNOWN_MARGIN'):
            binance_direct(executor, 'B', 'binance-unknown-margin', quantity=D('.1'))

        http.initial_margin = D(100)
        http.now += timedelta(seconds=2)
        http.funding_update('-25', time_ms=MS + 1_000)
        executor.adapter.sync_funding(end_time=http.now)
        after = executor.portfolio_risk_snapshot('offline', official_equity=http.cash)
        assert after.official_equity == before.official_equity - D(25)
        assert after.daily_loss >= D(25)
        with pytest.raises(ValueError, match='PORTFOLIO_DAILY_LOSS_LIMIT'):
            binance_direct(executor, 'B', 'binance-funding-block', quantity=D('.1'))

        executor.configure_portfolio_risk('offline', PortfolioRiskLimits(
            max_rolling_loss=D(20), rolling_loss_hours=1))
        http.now += timedelta(hours=1)
        executor.adapter.sync_funding(end_time=http.now)
        rolling = executor.portfolio_risk_snapshot('offline', official_equity=http.cash)
        assert rolling.rolling_loss >= D(25)
        with pytest.raises(ValueError, match='PORTFOLIO_ROLLING_LOSS_LIMIT'):
            binance_direct(executor, 'B', 'binance-rolling-block', quantity=D('.1'))
        http.funding_update('-100', symbol='', income_type='TRANSFER',
                            time_ms=int(http.now.timestamp() * 1000))
        withdrawal = executor.portfolio_risk_snapshot('offline', official_equity=http.cash)
        assert withdrawal.official_equity == rolling.official_equity - D(100)
        assert withdrawal.rolling_loss == rolling.rolling_loss
        assert withdrawal.drawdown == rolling.drawdown
        exit_id = binance_direct(executor, 'A', 'binance-risk-exit', side=Side.SELL)
        binance_advance(executor, exit_id, http)
        posts = [parameters for method, path, parameters in http.calls if method == 'POST' and path == '/fapi/v1/order']
        assert [parameters['newClientOrderId'][0] for parameters in posts] == ['binance-open', 'binance-risk-exit']
        assert posts[-1]['reduceOnly'] == ['true']
