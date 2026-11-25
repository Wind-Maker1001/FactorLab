from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from factorlab.integrations.binance_futures_trading.account import AccountFacts
from factorlab.trading.capital import CapitalPolicyMode
from factorlab.trading.contracts import Market
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from test_capital_funding import coverage
from test_portfolio_allocations import allocate, plan
from test_portfolio_execution import Broker, NOW


class MarginBroker(Broker):
    complete = True

    def read_account(self):
        account = super().read_account()
        return replace(account, market=Market.BINANCE_USDM,
            observed_at=NOW + timedelta(hours=1),
            assets=tuple(replace(asset, currency='USDT') for asset in account.assets),
            positions=tuple(replace(position, quote_currency='USDT') for position in account.positions))

    def margin_facts(self):
        positions = (dict(symbol='AAPL', positionSide='BOTH', positionAmt='3',
            entryPrice='10', markPrice='10', positionInitialMargin='6',
            initialMargin='6', unrealizedProfit='0'),)
        return AccountFacts(account_id='paper', observed_at=NOW + timedelta(hours=1),
            source_complete=self.complete,
            assets=(dict(asset='USDT', walletBalance='100', availableBalance='94'),),
            positions=positions, risk=positions, open_orders=(), trades=(), income=(), hedge_mode=False)

    def funding_events(self):
        return ()

    def funding_coverage(self):
        return coverage(NOW + timedelta(hours=1))


def test_shared_snapshot_uses_proportional_margin_and_propagates_incomplete_evidence(tmp_path):
    broker = MarginBroker(cash=D(70), positions={'AAPL': D(3)})
    store = PortfolioStore(tmp_path / 'shared-margin.sqlite3')
    executor = PortfolioExecutor(broker, store, quote_currency='USDT', clock=lambda: NOW)
    # Fixture represents a funded legacy journal before policy activation.
    for owner, quantity in [('one', D(1)), ('two', D(2))]:
        with store.transaction():
            store.db.execute('INSERT INTO portfolio_allocations VALUES (?,?,?,?,?)',
                ('paper', owner, owner + '-cash', '50', str(D(50) - quantity * D(10))))
        executor.create(account_id='paper', strategy_id=owner, strategy_version='1',
            trading_session='2026-11-24', weights={'AAPL': D(0)}, owned_symbols=['AAPL'],
            initial_inventory={'AAPL': (quantity, D(10))}, now=NOW)
    policy = executor.capital_policy
    policy.configure(account_id='paper', currency='USDT', budget=D(100), fee_rate=D(0),
                     mode=CapitalPolicyMode.SHARED_SAME_DIRECTION)
    for owner in ['one', 'two']:
        policy.bind(account_id='paper', strategy_id=owner, instance_id=owner, allocation_id=owner + '-cash')
    before = store.db.total_changes
    snapshot = policy.risk_snapshot(executor, 'paper', official_equity=D(100))
    assert snapshot.reconciled
    assert [s.occupied for s in snapshot.strategies] == [D(2), D(4)]
    assert all(s.complete for s in snapshot.strategies)
    assert snapshot.available == D(94)
    assert snapshot.margin_attribution.estimated
    assert snapshot.margin_attribution.basis == 'confirmed_quantity_proportion_of_official_initial_margin'
    assert store.db.total_changes == before
    broker.complete = False
    snapshot = policy.risk_snapshot(executor, 'paper', official_equity=D(100))
    assert all(not s.complete for s in snapshot.strategies)
    assert all(s.occupied is s.nav is s.remaining_budget is None for s in snapshot.strategies)
    store.close()
