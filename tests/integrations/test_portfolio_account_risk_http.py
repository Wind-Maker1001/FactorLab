"""Portfolio risk exercised through both brokers' deterministic HTTP transports."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from threading import Barrier

import pytest

from factorlab.integrations.alpaca.corporate_actions import CorporateReceiptJournal
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
        self.cumulative_external_cash_flow = D(0)

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

    def read_account(self):
        # Supply the public capital snapshot field alongside the HTTP account facts.
        account = super().read_account()
        return replace(account, cumulative_external_cash_flow=self.client.transport.cumulative_external_cash_flow)

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
            max_gross_exposure=D(150), max_daily_loss=D(20)))
        executor.adapter.sync_funding(start_time=http.now, end_time=http.now)
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
        http.cash -= D(100)
        http.cumulative_external_cash_flow -= D(100)
        withdrawal = executor.portfolio_risk_snapshot('offline', official_equity=http.cash)
        assert withdrawal.official_equity == rolling.official_equity - D(100)
        assert withdrawal.rolling_loss == rolling.rolling_loss
        assert withdrawal.drawdown == rolling.drawdown
        exit_id = binance_direct(executor, 'A', 'binance-risk-exit', side=Side.SELL)
        binance_advance(executor, exit_id, http)
        posts = [parameters for method, path, parameters in http.calls if method == 'POST' and path == '/fapi/v1/order']
        assert [parameters['newClientOrderId'][0] for parameters in posts] == ['binance-open', 'binance-risk-exit']
        assert posts[-1]['reduceOnly'] == ['true']
