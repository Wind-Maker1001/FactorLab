from datetime import timedelta
from decimal import Decimal as D

from factorlab.trading.capital_funding import attribute_funding
from factorlab.trading.contracts import FundingEvent
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from test_portfolio_allocations import allocate, plan
from test_portfolio_execution import Broker, NOW, run


def payment(when, symbol='AAPL'):
    return FundingEvent(account_id='paper', tran_id='1', symbol=symbol, asset='USDT',
        income_type='FUNDING_FEE', amount=D('-.1'), event_time=when,
        source='official', event_id='funding-1')


def coverage(end):
    return dict(complete=True, covered_from_ms=int((NOW-timedelta(days=1)).timestamp()*1000),
                covered_through_ms=int(end.timestamp()*1000))


def test_historical_funding_owner_is_not_current_symbol_owner(tmp_path):
    store = PortfolioStore(tmp_path / 'funding.sqlite3')
    executor = PortfolioExecutor(Broker(), store, clock=lambda: NOW)
    allocate(executor, 'one', '40')
    allocate(executor, 'two', '60')
    identity = plan(executor, 'one')
    run(executor, identity)
    run(executor, identity)
    when = NOW + timedelta(hours=1)
    event = payment(when)
    result = attribute_funding(store, 'paper', [event, event], coverage(when), when)
    assert dict(result.amounts) == {'one': D('-.1'), 'two': D(0)}
    assert result.complete
    ambiguous = attribute_funding(store, 'paper', [payment(NOW)], coverage(when), when)
    assert not ambiguous.complete
    assert ambiguous.unattributed_events == ('funding-1',)
    unknown = attribute_funding(store, 'paper', [payment(when, 'UNKNOWN')], coverage(when), when)
    assert not unknown.complete
    assert dict(unknown.amounts) == {'one': D(0), 'two': D(0)}
    store.close()


def test_margin_snapshot_uses_authoritative_margin_and_funding_not_cash_notional(tmp_path):
    from dataclasses import replace
    from factorlab.integrations.binance_futures_trading.account import AccountFacts
    from factorlab.trading.contracts import Market

    class MarginBroker(Broker):
        observed = NOW
        def read_account(self):
            account = super().read_account()
            return replace(account, market=Market.BINANCE_USDM, observed_at=self.observed,
                assets=tuple(replace(asset, currency='USDT') for asset in account.assets),
                positions=tuple(replace(position, quote_currency='USDT') for position in account.positions))
        def margin_facts(self):
            positions = tuple(dict(symbol=symbol, positionSide='BOTH', positionAmt=str(quantity),
                entryPrice='10', markPrice='10', initialMargin=str(quantity), unrealizedProfit='0')
                for symbol, quantity in self.positions.items() if quantity)
            return AccountFacts(account_id='paper', observed_at=self.observed, source_complete=True,
                assets=(dict(asset='USDT', walletBalance=str(self.cash), availableBalance=str(self.cash)),),
                positions=positions, risk=positions, open_orders=(), trades=(), income=(), hedge_mode=False)
        def funding_events(self):
            return () if self.observed == NOW else (payment(self.observed),)
        def funding_coverage(self):
            return coverage(self.observed)

    broker = MarginBroker()
    store = PortfolioStore(tmp_path / 'margin.sqlite3')
    executor = PortfolioExecutor(broker, store, quote_currency='USDT', clock=lambda: NOW)
    executor.capital_policy.configure(account_id='paper', currency='USDT', budget=D(100), fee_rate=D(0))
    allocate(executor, 'one', '40')
    executor.capital_policy.bind(account_id='paper', strategy_id='one', instance_id='one', allocation_id='one-cash')
    identity = plan(executor, 'one')
    run(executor, identity)
    run(executor, identity)
    broker.observed = NOW + timedelta(hours=1)
    snapshot = executor.capital_policy.risk_snapshot(executor, 'paper', official_equity=D(100))
    owner = snapshot.strategies[0]
    assert owner.complete
    assert owner.occupied == D(4)
    assert owner.cost_basis == D(40)
    assert owner.funding == D('-.1')
    assert owner.nav == D('39.9')
    broker.funding_coverage = lambda: None
    unknown = executor.capital_policy.risk_snapshot(executor, 'paper', official_equity=D(100)).strategies[0]
    assert not unknown.complete
    assert unknown.funding is unknown.nav is unknown.remaining_budget is None
    store.close()


def test_missing_funding_coverage_is_not_zero_income(tmp_path):
    store = PortfolioStore(tmp_path / 'funding.sqlite3')
    executor = PortfolioExecutor(Broker(), store, clock=lambda: NOW)
    allocate(executor, 'one', '40')
    result = attribute_funding(store, 'paper', [], None, NOW)
    assert not result.complete
    store.close()
