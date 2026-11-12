from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest

from factorlab.integrations.binance_equity_trading.protocol import Unavailable
from factorlab.integrations.binance_futures_trading.rules import RuleObservation, market_quantity_constraints
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side


def symbol(lot_step, market_step, *, lot_min='.01', market_min='.001',
           lot_max='100', market_max='50'):
    return dict(filters=[
        dict(filterType='LOT_SIZE', stepSize=lot_step, minQty=lot_min, maxQty=lot_max),
        dict(filterType='MARKET_LOT_SIZE', stepSize=market_step,
             minQty=market_min, maxQty=market_max),
    ])


@pytest.mark.parametrize('lot_step,market_step,expected', [
    ('.01', '.001', '.01'),
    ('.02', '.03', '.06'),
    ('.025', '.06', '.3'),
    ('.01', '0', '.01'),
    ('0', '.001', '.001'),
    ('0', '0', '0'),
    ('2E+1', '3E+1', '60'),
])
def test_market_quantity_common_grid(lot_step, market_step, expected):
    step, minimum, maximum = market_quantity_constraints(symbol(lot_step, market_step))
    assert step == Decimal(expected)
    assert minimum == Decimal('.01')
    assert maximum == Decimal('50')


def test_market_quantity_intersects_both_bounds():
    _, minimum, maximum = market_quantity_constraints(symbol(
        '.01', '.01', lot_min='.001', market_min='.02', lot_max='25', market_max='100'))
    assert minimum == Decimal('.02')
    assert maximum == Decimal('25')


def test_market_quantity_grid_is_exact_independent_of_decimal_context():
    with localcontext() as context:
        context.prec = 2
        step, _, _ = market_quantity_constraints(symbol('.000000123', '.000000127'))
    assert step == Decimal('.000015621')


@pytest.mark.parametrize('missing', ['LOT_SIZE', 'MARKET_LOT_SIZE', 'filters',
                                     'stepSize', 'minQty', 'maxQty'])
def test_market_quantity_requires_complete_rules(missing):
    info = symbol('.01', '.001')
    if missing == 'filters':
        info.pop('filters')
    elif missing in {'LOT_SIZE', 'MARKET_LOT_SIZE'}:
        info['filters'] = [f for f in info['filters'] if f['filterType'] != missing]
    else:
        info['filters'][1].pop(missing)
    with pytest.raises(Unavailable, match='UNKNOWN_QUANTITY_RULE'):
        market_quantity_constraints(info)


@pytest.mark.parametrize('original,remaining,expected', [
    ('1', '.05', 'APPROVED'),
    ('.05', '.01', 'QUANTITY_FILTER'),
])
def test_partial_fill_uses_remaining_risk_but_original_quantity_filters(original, remaining, expected):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    info = symbol('.01', '.01', lot_min='.1', market_min='.1')
    info.update(symbol='NVDAUSDT', status='TRADING', quoteAsset='USDT', marginAsset='USDT',
                orderTypes=['MARKET'])
    info['filters'].append(dict(filterType='MIN_NOTIONAL', notional='10'))
    brackets = dict(symbol='NVDAUSDT', brackets=[
        dict(notionalFloor='0', notionalCap='101', initialLeverage=1)])
    rules = RuleObservation.from_exchange(info, brackets, now, 'test')
    intent = OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', Side.BUY, OrderType.MARKET,
                         Decimal(original), None, 'partial-risk')
    decision = rules.validate(intent, mark_price=Decimal('100'), leverage=1,
                              available_margin=Decimal('5'), current_notional=Decimal('95'),
                              now=now, remaining_quantity=Decimal(remaining))
    assert decision.reason == expected
