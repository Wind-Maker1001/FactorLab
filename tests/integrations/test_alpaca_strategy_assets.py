from dataclasses import replace
from decimal import Decimal as D

import pytest

from examples.alpaca_balanced.strategy import BalancedPortfolio
from factorlab.integrations.alpaca.strategy_assets import StrategyAssets
from factorlab.trading.portfolio_execution import PortfolioStore
from test_alpaca_instance_runtime import Account
from test_alpaca_portfolio import NOW


def test_official_extended_assets_keep_stable_identity_and_cash_only_requirements(tmp_path):
    account = Account()
    store = PortfolioStore(tmp_path / 'portfolio.sqlite3')
    assets = StrategyAssets(account, store, clock=lambda: NOW)
    spec = BalancedPortfolio('paper').describe()
    binding = dict(market=spec.market, account_id='paper', strategy_id=spec.strategy_id,
                   instance_id='balanced', allocation_id='balanced-usd')
    allocation = dict(currency='USD', capital=D(100))
    assets.qualify(spec, binding, allocation)
    assert [(i.instrument_id, i.symbol) for i in assets.instruments(spec.symbols)] == [
        ('official-SPY', 'SPY'), ('official-IWM', 'IWM')]
    with pytest.raises(ValueError, match='cash-only'):
        assets.qualify(replace(spec, execution=dict(spec.execution, shorting=True)), binding, allocation)
    with pytest.raises(ValueError, match='exceeds'):
        assets.qualify(spec, binding, dict(allocation, capital=D(99)))
    original = account.read_asset
    account.read_asset = lambda symbol: dict(original(symbol), id='different-security')
    with pytest.raises(ValueError, match='different payload'):
        assets.resolve('SPY')
    store.close()


@pytest.mark.parametrize('change,reason', [
    ({'tradable': False}, 'active tradable'),
    ({'status': 'inactive'}, 'active tradable'),
    ({'class': 'crypto'}, 'active tradable'),
    ({'fractionable': False}, 'fractional'),
])
def test_official_asset_capability_refusals(tmp_path, change, reason):
    account = Account()
    original = account.read_asset
    account.read_asset = lambda symbol: dict(original(symbol), **change)
    store = PortfolioStore(tmp_path / 'portfolio.sqlite3')
    with pytest.raises(ValueError, match=reason):
        StrategyAssets(account, store, clock=lambda: NOW).resolve('SPY', fractional=True)
    store.close()
