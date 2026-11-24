from decimal import Decimal as D

import pytest

from factorlab.integrations.alpaca.strategy_orders import adjusted_order_deltas


def test_adjusted_share_conversion_preserves_cash_value_and_volume_cap():
    ratio = D('.4')
    raw_volume = D(10000)
    adjusted_volume = raw_volume / ratio
    adjusted_quantity = adjusted_volume * D('.001')
    order = dict(symbol='AAPL', adjusted_quantity=float(adjusted_quantity), side='buy')
    deltas = adjusted_order_deltas([order], adjustment_ratios={'AAPL': ratio})
    assert deltas == {'AAPL': D(10)}
    raw_price = D(200)
    assert adjusted_quantity * (raw_price * ratio) == deltas['AAPL'] * raw_price
    order['side'] = 'sell'
    assert adjusted_order_deltas([order], adjustment_ratios={'AAPL': ratio}) == {'AAPL': D(-10)}


def test_native_orders_are_not_aggregated_or_replaced():
    order = dict(symbol='AAPL', adjusted_quantity=2.5, side='buy')
    assert adjusted_order_deltas([], adjustment_ratios={}) == {}
    with pytest.raises(ValueError, match='one original'):
        adjusted_order_deltas([order, order], adjustment_ratios={'AAPL': D(1)})
    with pytest.raises(KeyError):
        adjusted_order_deltas([order], adjustment_ratios={})
