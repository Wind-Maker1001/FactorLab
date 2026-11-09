"""Observed-path accounting and transmission, not investment performance tests."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from examples.research.return_attribution_deterministic import run_cases
from factorlab.evaluation.return_attribution import attribute_execution, paired_daily_difference
from tests.research.test_signal_propagation import allocate, execute, DAYS


def test_hand_accounting_and_paired_decomposition():
    target,_,_=allocate([1.,2.])
    result=execute(target,moving=True)
    a=attribute_execution(result,initial_cash=100000,calendar=result.nav.date)
    final=result.positions.loc[result.positions.date.eq(DAYS[-1])]
    gain=final.loc[final.symbol.eq('000001.SZ'),'shares'].sum()*2
    assert a.summary['gross_pnl_same_fills']==pytest.approx(gain)
    assert a.summary['net_pnl']==pytest.approx(gain-result.trades.transaction_cost.sum())
    assert a.summary['gross_contribution']+a.summary['fee_contribution']==pytest.approx(a.summary['net_return'])
    assert a.summary['expected_return_status'].startswith('NOT_COMPUTABLE')
    np.testing.assert_allclose(a.daily.gross_return_same_fills-a.daily.fee_return,a.daily.net_return,atol=1e-15)
    other=attribute_execution(execute(allocate([2.,1.])[0],moving=True),initial_cash=100000,calendar=result.nav.date)
    pair=paired_daily_difference(a.daily,other.daily)
    np.testing.assert_allclose(pair.gross_difference_same_fills-pair.fee_difference,pair.net_difference,atol=1e-15)


def test_calendar_and_identity_rejection():
    result=execute(allocate([1.,2.])[0])
    with pytest.raises(ValueError,match='calendar'):
        attribute_execution(result,initial_cash=100000,calendar=result.nav.date.iloc[1:])
    a=attribute_execution(result,initial_cash=100000,calendar=result.nav.date)
    with pytest.raises(ValueError,match='calendars'):
        paired_daily_difference(a.daily,a.daily.iloc[1:])
    broken=result.positions.copy()
    broken.loc[0,'shares']+=100
    with pytest.raises(AssertionError):
        attribute_execution(replace(result,positions=broken),initial_cash=100000,calendar=result.nav.date)


def test_cash_fee_and_weight_corruption_rejected():
    result=execute(allocate([1.,2.])[0])
    for field,column in (('nav','cash'),('trades','transaction_cost'),('target_comparison','actual_weight')):
        frame=getattr(result,field).copy()
        frame.loc[0,column]+=1
        with pytest.raises(AssertionError):
            attribute_execution(replace(result,**{field:frame}),initial_cash=100000,calendar=result.nav.date)


def test_four_objectives_and_lot_erasure():
    rows=pd.DataFrame(run_cases())
    for objective in ('equal','minimum_variance'):
        x=rows.loc[(rows.objective==objective)&(rows.initial_cash==100000)]
        assert x.final_nav.nunique()==1
    for objective in ('score','mean_variance'):
        x=rows.loc[(rows.objective==objective)&(rows.initial_cash==100000)]
        assert x.final_nav.nunique()==2
        assert x.iloc[0].filled_shares!=x.iloc[1].filled_shares
    small=rows.loc[rows.initial_cash==100]
    assert small.fills.eq(0).all()
    assert small.final_nav.eq(1).all()
