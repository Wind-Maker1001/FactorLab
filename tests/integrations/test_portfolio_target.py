from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from factorlab.trading.contracts import AccountSnapshot, AssetBalance, Market, Position
from factorlab.trading.portfolio_target import plan_targets


def plan(positions=(), available='100', **overrides):
    args = dict(account=AccountSnapshot(Market.BINANCE_USDM, 'a',
        (AssetBalance('USDT', D(available)),), positions, (),
        datetime.now(timezone.utc), True), equity=D(100),
        weights={'A': D('.5'), 'B': D('.5')},
        prices={'A': D(10), 'B': D(10)}, steps={'A': D(1), 'B': D(1)},
        owned_symbols={'A', 'B'})
    args.update(overrides)
    return plan_targets(**args)


def test_shared_cash():
    legs = plan(available='60')
    assert [leg.target for leg in legs] == [D(5), D(1)]
    assert sum(leg.notional for leg in legs) == 60


def test_reduce_before_increase():
    position = Position('A', D(8), D(10), D(10), 'USDT')
    legs = plan((position,))
    assert len(legs) == 1 and legs[0].delta == -3


def test_missing_symbol_keeps_cash():
    assert sum(leg.notional for leg in plan(prices={'A': D(10)})) == 50


def test_explicit_ownership():
    with pytest.raises(ValueError, match='ownership'):
        plan(owned_symbols={'A'})


def test_economic_exposure():
    leg = plan(economic_multipliers={'A': D(3)})[0]
    assert leg.economic_notional == leg.notional * 3


def test_dynamic_step():
    assert plan(steps={'A': D(3), 'B': D(1)})[0].target == 3


def test_invalid_weights():
    with pytest.raises(ValueError):
        plan(weights={'A': D('NaN')})
