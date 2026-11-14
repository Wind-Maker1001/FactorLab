from decimal import Decimal

import pandas as pd
import pytest

from factorlab.experiments.grid_exposure_benchmark import chunks, simulate, stats, fifo_control


def bars(prices, volume=100):
    return [dict(time=str(t), close=p, volume=volume) for t,p in
            zip(pd.date_range('2026-05-14',periods=len(prices),freq='h',tz='UTC'),prices)]


def test_later_bar_limits_cost_and_nav():
    nav, fills = simulate(bars([100,110,90]),quantity='.1')
    assert len(fills) == 1
    assert fills.iloc[0].execution_date == bars([100,110,90])[2]['time']
    price = 90*1.0006
    fee = .1*price*.0004
    assert fills.iloc[0].execution_price == pytest.approx(price)
    assert nav.equity.iloc[-1] == pytest.approx(1000-.1*(price-90)-fee)
    assert stats(nav,fills)['max_drawdown'] == pytest.approx((1000-nav.equity.iloc[-1])/1000)
    economics = fifo_control(fills,90)
    assert economics['residual_net'] == pytest.approx(nav.equity.iloc[-1]-1000)


def test_future_prices_and_targets_cannot_change_past():
    before, f1 = simulate(bars([100,90,95,80]),targets=[.1,.2,.1,0])
    after, f2 = simulate(bars([100,90,95,800]),targets=[.1,.2,.9,1])
    pd.testing.assert_frame_equal(before.iloc[:2],after.iloc[:2])
    pd.testing.assert_frame_equal(f1.iloc[:1],f2.iloc[:1])
    assert before.inventory.iloc[0] == 0
    assert before.inventory.iloc[1] == .1
    # Target at t=2 cannot change the fill at t=2.
    assert before.inventory.iloc[2] == after.inventory.iloc[2]


def test_quantity_minimum_and_partition():
    with pytest.raises(ValueError,match='minimum-notional'):
        simulate(bars([229,220]),quantity='.02')
    parts = chunks(Decimal('.11'),Decimal('229'))
    assert sum(parts) == Decimal('.11')
    assert all(p <= Decimal('.1') and p*229 >= 5 for p in parts)


def test_shared_volume_and_capital():
    nav,fills = simulate(bars([100,90,80,70],volume=15),quantity=1)
    assert fills.executed_shares.max() <= .1
    assert max(sum(Decimal(str(q)) for q in group.executed_shares)
               for _,group in fills.groupby('execution_date')) <= Decimal('.15')
    assert nav.inventory.max() <= 1
    assert nav.committed.max() <= 1000
    assert nav.cash.min() >= 0


def test_fifo_closed_and_residual_reconciles():
    nav,fills = simulate(bars([100,90,95,110,100]),targets=[.1,.1,0,0,0])
    econ = fifo_control(fills,nav.close.iloc[-1])
    assert econ['sold_fifo_net']+econ['residual_net'] == pytest.approx(nav.equity.iloc[-1]-1000)
    assert fills.side.tolist() == ['BUY','SELL']


def test_doubled_costs_and_flat_cash():
    nav1,f1 = simulate(bars([100,90,95]),quantity='.1')
    nav2,f2 = simulate(bars([100,90,95]),quantity='.1',fee_bps=8,slip_bps=12)
    assert stats(nav2,f2)['total_cost'] > stats(nav1,f1)['total_cost']
    assert nav2.equity.iloc[-1] < nav1.equity.iloc[-1]
    cash,empty = simulate(bars([100,110]),targets=[0,0])
    metrics = stats(cash,empty)
    assert metrics['net_pnl'] == 0
    assert metrics['sharpe'] is None
    assert metrics['sortino'] is None


def test_dynamic_dust_is_not_illegal_order():
    nav,fills = simulate(bars([229,220,230]),targets=[.02,.02,0])
    assert fills.empty
    assert nav.inventory.eq(0).all()
