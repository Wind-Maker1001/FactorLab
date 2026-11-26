from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json

import pytest

from factorlab.experiments.grid_f11_f08_offline import default_demo_rules, _demo_grid_config
from factorlab.experiments.grid_funding_adaptive import (
    FundingSensitiveGrid, FundingPolicy, VolatilityAdaptiveGrid, VolatilityPolicy,
)
from factorlab.integrations.binance_futures_trading.demo_runtime import restore_grid
from factorlab.trading import Market
from factorlab.trading.storage import dumps, decode


@pytest.mark.parametrize('kind', ['funding', 'volatility'])
def test_restore_preserves_adaptive_constructor_inputs_after_history_changes(kind):
    rules = default_demo_rules()
    config = _demo_grid_config(D('132.89'), rules, max_cash_committed=D(7),
                              fee_rate=D('.0003'), slippage=D(0))
    arguments = dict(deployment_id='adaptive-restore', account_id='account',
        market=Market.BINANCE_USDM, instrument_id='NVDAUSDT', initial_equity=D(5000),
        fee_rate=D('.0003'), continuous_capacity=True)
    events = [(datetime(2026, 11, 1, tzinfo=timezone.utc), D('.0001'), D(133), 'original')]
    if kind == 'funding':
        original = FundingSensitiveGrid(config, rules, **arguments,
            funding_policy=FundingPolicy(), funding_events=events, use_f11_limit_exit=True)
    else:
        original = VolatilityAdaptiveGrid(config, rules, **arguments,
            vol_policy=VolatilityPolicy(), warmup_closes=[130, 131, 132, 133])
        original.close_history.append(200)
    deployment = decode(json.loads(dumps(original.deployment())))
    restored = restore_grid(deployment)
    assert type(restored) is type(original)
    if kind == 'funding':
        assert restored._all_funding_events == events
        assert restored.funding_policy == original.funding_policy
        assert restored.use_f11_limit_exit is True
    else:
        assert restored.close_history == [130, 131, 132, 133]
        assert restored.vol_policy == original.vol_policy


def test_demo_closed_bar_does_not_observe_future_funding(monkeypatch):
    from factorlab.strategies.grid_execution import GridExecution
    rules = default_demo_rules()
    config = _demo_grid_config(D('132.89'), rules, max_cash_committed=D(7),
                              fee_rate=D('.0003'), slippage=D(0))
    close = datetime(2026, 11, 1, 1, tzinfo=timezone.utc)
    grid = FundingSensitiveGrid(config, rules, deployment_id='causal-funding',
        account_id='account', market=Market.BINANCE_USDM, instrument_id='NVDAUSDT',
        initial_equity=D(5000), funding_policy=FundingPolicy(),
        funding_events=[(close + timedelta(minutes=30), D(133), D('.01'), 'future')])
    # Isolate the funding gate from the account/order transition it precedes.
    monkeypatch.setattr(GridExecution, '_observe', lambda *args: None)
    grid.state = 'STOPPED'
    from factorlab.strategies.grid_execution import _observation
    event = dict(id='causal-bar', time=close.replace(tzinfo=None).isoformat(),
        price=D(133), high=D(133), low=D(133), close=D(133), volume=0, completed_bar=True)
    grid._observe(_observation(dict(event, source_bar=dict(
        opened_at=(close-timedelta(hours=1)).isoformat()))), None)
    assert grid.last_funding_bps_h == 0
    grid._observe(_observation(event), None)
    assert grid.last_funding_bps_h > 0
