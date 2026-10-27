"""Causal order decisions, explicit uncertainty, economic floors and isolation."""
from dataclasses import replace
import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.cross_market.calendar import cn_calendar
from factorlab.cross_market.engine import fast_backtest, BacktestResult, ExecutionEngine
from factorlab.cross_market.adapters import ChinaFees, ChinaAshareAdapter, UnknownActions
from factorlab.cross_market.contracts import ContractError
from factorlab.errors import ProviderError
from factorlab.security import assert_independent_admission
from factorlab.exploratory.scenario import (Assumptions, Constraints, ScenarioError,
    run_scenario, cost_parts, prepare_scores)


def fixture():
    dates = pd.bdate_range('2024-06-03', periods=15)
    cal = cn_calendar(dates,'SYNTHETIC_ONLY')
    bars = pd.DataFrame([dict(date=d,instrument_id=i,adj_open=10.,adj_close=10.) for d in dates for i in ['a','b','c']])
    scores = pd.DataFrame([dict(date=d,instrument_id=i,score=(3-j if n%2==0 else j+1),signal_at=cal.at(d,'close'))
        for n,d in enumerate(dates[:-1]) for j,i in enumerate(['a','b','c'])])
    assumptions = Assumptions('synthetic adjusted','UNKNOWN','UNKNOWN','synthetic','unverified scenario')
    return bars,scores,cal,assumptions


def run(c=None,a=None,initial_cash=10000.):
    bars,scores,cal,default = fixture()
    return run_scenario(bars,scores,cal,c or Constraints(top_k=1,frequency=1),a or default,initial_cash=initial_cash)


def test_baseline_independently_matches_unchanged_fast_on_synthetic_path():
    b,s,c,a = fixture()
    from factorlab.cross_market.engine import target_weights
    legacy = fast_backtest(b,target_weights(s,c,1,1),c,ChinaFees(),s.date.min(),c.schedule.index[-1],initial_cash=10000.)
    result = run_scenario(b,s,c,Constraints(top_k=1,frequency=1),a,initial_cash=10000.)
    assert np.allclose(result.ledger.conditional_equity,legacy.nav.nav*10000,rtol=0,atol=1e-8)
    assert len(result.fills)==len(legacy.trades)>0
    assert not isinstance(result,BacktestResult)
    assert result.metadata['trusted_return']=='UNKNOWN'


def test_future_signals_labels_and_nonfinite_scores_are_refused():
    b,s,c,a = fixture()
    with pytest.raises(ScenarioError,match='labels forbidden'):
        run_scenario(b,s.assign(target=100.),c,Constraints(),a)
    s.loc[0,'signal_at']=c.at(c.schedule.index[1],'close')
    with pytest.raises(ScenarioError,match='unavailable'):
        prepare_scores(s,c)
    s.loc[0,'signal_at']=c.at(c.schedule.index[0],'close')
    s['score']=s.score.astype(float)
    s.loc[0,'score']=np.inf
    with pytest.raises(ScenarioError,match='nonfinite'):
        prepare_scores(s,c)


def test_future_scores_and_prices_do_not_change_prior_fills():
    b,s,c,a = fixture()
    kwargs=dict(calendar=c,constraints=Constraints(top_k=1,frequency=1),assumptions=a,initial_cash=10000.)
    x=run_scenario(b,s,**kwargs)
    boundary=c.schedule.index[8]
    changed=s.copy()
    changed.loc[changed.date>=boundary,'score']*=-10
    changed_b=b.copy()
    changed_b.loc[changed_b.date>boundary,['adj_open','adj_close']]*=3
    y=run_scenario(changed_b,changed,**kwargs)
    pd.testing.assert_frame_equal(x.fills.loc[x.fills.date<=boundary].reset_index(drop=True),y.fills.loc[y.fills.date<=boundary].reset_index(drop=True))


def test_missing_close_never_changes_same_day_open_fills():
    b,s,c,a=fixture()
    day=c.schedule.index[2]
    x=run_scenario(b,s,c,Constraints(top_k=1,frequency=1),a,initial_cash=10000.)
    b.loc[b.date.eq(day),'adj_close']=np.nan
    y=run_scenario(b,s,c,Constraints(top_k=1,frequency=1),a,initial_cash=10000.)
    pd.testing.assert_frame_equal(x.fills.loc[x.fills.date<=day].reset_index(drop=True),y.fills.reset_index(drop=True))
    assert y.metadata['status']=='UNKNOWN_VALUATION_STOP'
    assert y.ledger.date.max()<day and set(y.uncertainty.phase)=={'CLOSE'}


@pytest.mark.parametrize('policy',['FROZEN_LAST_UNTRADEABLE','ZERO_CLAIM_UNTRADEABLE'])
def test_assumed_claim_has_no_fake_sale_or_terminal_cash(policy):
    b,s,c,a=fixture()
    b=b.loc[~(b.instrument_id.eq('a')&b.date.ge(c.schedule.index[2]))]
    x=run_scenario(b,s,c,Constraints(top_k=1,frequency=1),replace(a,missing_marks=policy),initial_cash=10000.)
    assert len(x.uncertainty)>0 and x.metadata['trusted_equity_range']=='UNKNOWN'
    assert not ((x.fills.date>=c.schedule.index[2])&x.fills.instrument_id.eq('a')).any()
    assert x.holdings.loc[x.holdings.instrument_id.eq('a'),'quantity'].nunique()==1
    assert x.ledger.cash.min()>=0


def test_unknown_missing_target_cannot_be_invented_and_original_gate_remains_closed():
    b,s,c,a=fixture()
    b=b.loc[~(b.instrument_id.eq('c')&b.date.eq(c.schedule.index[2]))]
    x=run_scenario(b,s,c,Constraints(top_k=1,frequency=1),a)
    assert x.metadata['status']=='UNKNOWN_VALUATION_STOP'
    with pytest.raises(ContractError,match='CORPORATE_ACTION_COVERAGE_UNKNOWN'):
        ExecutionEngine(ChinaAshareAdapter(c,None,UnknownActions())).run(
            b.assign(raw_open=10.,raw_close=10.,halted=False),pd.DataFrame(columns=['date','instrument_id','target_weight']),c.schedule.index[0],c.schedule.index[-1])
    with pytest.raises(ProviderError,match='FORMAL_ADMISSION_BLOCKED'):
        assert_independent_admission()


def test_min_notional_applies_after_cash_capping():
    x=run(Constraints(top_k=1,frequency=1,min_notional=10000.),initial_cash=10000.)
    assert x.fills.empty
    assert 'MIN_NOTIONAL_AFTER_CASH_CAP' in set(x.decisions.reason)


def test_small_weight_changes_buffer_reversals_equity_frequency_have_effect():
    base=run()
    cooldown=run(Constraints(top_k=1,frequency=1,reversal_sessions=10))
    assert len(cooldown.fills)<len(base.fills) and 'REVERSAL_COOLDOWN' in set(cooldown.decisions.reason)
    floor=run(Constraints(top_k=1,frequency=1,min_equity=10001.))
    assert floor.fills.empty and floor.ledger.conditional_equity.eq(10000).all()
    slow=run(Constraints(top_k=1,frequency=5))
    assert slow.ledger.rebalance.sum()<base.ledger.rebalance.sum()
    # A two-name universe alternates rank 1/2. Buffer keeps the ACTUAL holding.
    b,s,c,a=fixture()
    s=s.loc[s.instrument_id.isin(['a','c'])]
    kept=run_scenario(b,s,c,Constraints(top_k=1,frequency=1,buffer_fraction=1.),a,initial_cash=10000.)
    assert kept.holdings.instrument_id.nunique()==1
    weighted=run_scenario(b,s,c,Constraints(top_k=1,frequency=1,buffer_fraction=1.,min_weight_delta=.005),a,initial_cash=10000.)
    assert len(weighted.fills)==1 and 'MIN_WEIGHT_DELTA' in set(weighted.decisions.reason)
    # Two held names drift by 0.1%; small sells really execute without the band.
    s['score']=s.instrument_id.map({'a':2.,'c':1.})
    b.loc[b.instrument_id.eq('a')&b.date.ge(c.schedule.index[2]),['adj_open','adj_close']]=10.02
    plain=run_scenario(b,s,c,Constraints(top_k=2,frequency=1),a,initial_cash=100000.)
    band=run_scenario(b,s,c,Constraints(top_k=2,frequency=1,min_weight_delta=.005),a,initial_cash=100000.)
    assert len(band.fills)<len(plain.fills)


@pytest.mark.parametrize('destination',['LIVE','FORMAL_FORWARD','FINANCIAL_ADMISSION','EXECUTION_ADMISSION','anything'])
def test_no_promotion_and_schema_cannot_be_used_as_strict_targets(tmp_path,destination):
    x=run()
    with pytest.raises(ScenarioError,match='prohibited consumer'):
        x.for_consumer(destination)
    p=x.save(tmp_path/'exploratory-scenarios','case')
    f=pd.read_parquet(p/'decisions.parquet')
    assert all(k.startswith('scenario_') for k in f) and 'target_weight' not in f and 'date' not in f
    with pytest.raises(ScenarioError):
        x.save(tmp_path/'strict-validated','case')
    with pytest.raises(ScenarioError):
        x.save(tmp_path/'exploratory-scenarios','../escape')


def test_scenario_does_not_import_formal_engines_admission_or_forward():
    import factorlab.exploratory.scenario as m
    tree=ast.parse(Path(m.__file__).read_text())
    modules=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
    assert not any(any(x in module for x in ('cross_market','security','forward','backtest','admission')) for module in modules)


def test_floor_excess_is_incremental_and_costs_reconcile():
    p=cost_parts('2024-06-03','sell',100.)
    assert p['floor_excess']==pytest.approx(4.97)
    assert p['fee']==pytest.approx(5.1)
    assert sum(p[k] for k in ['proportional_commission','floor_excess','stamp','slippage'])==pytest.approx(p['fee'])


@pytest.mark.parametrize('kwargs',[{'frequency':0},{'min_notional':-1},{'min_weight_delta':np.nan},{'top_k':True},{'reversal_sessions':1.5}])
def test_invalid_constraints_rejected(kwargs):
    with pytest.raises(ScenarioError):
        Constraints(**kwargs)
