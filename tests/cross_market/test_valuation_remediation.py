"""Deterministic economic identities and fail-closed evidence regressions."""
import numpy as np
import pandas as pd
import pytest

from factorlab.cross_market.adapters import ActionLedger, ChinaAshareAdapter, DatedFees, FeePeriod
from factorlab.cross_market.calendar import cn_calendar
from factorlab.cross_market.contracts import ContractError, ValuationError
from factorlab.cross_market.corporate_actions import EvidenceActionLedger
from factorlab.cross_market.engine import ExecutionEngine, fast_backtest


DAY = pd.Timestamp('2024-06-05')


def event(kind, **kwargs):
    return dict(event_id='one', instrument_id='a', kind=kind, effective_date=DAY,
        available_at=pd.Timestamp('2024-06-01T00:00Z'), ratio=1., cash_per_share=0.,
        pay_date=DAY, target_id=None, evidence='SYNTHETIC_ONLY', source_version='SYNTHETIC_V2',
        evidence_status='VERIFIED', **kwargs)


def make_event(kind, **kwargs):
    r=event(kind)
    r.update(kwargs)
    return r


def adapter(events):
    cal=cn_calendar(pd.bdate_range('2024-06-03','2024-06-12'),'SYNTHETIC_ONLY')
    ledger=ActionLedger(pd.DataFrame(events,columns=list(dict.fromkeys(ActionLedger.COLUMNS+['quantity_date','rights_policy','subscription_price','source_version','evidence_status']))),
        coverage_start='2024-06-03',coverage_end='2024-06-12',instrument_ids=['a','b'],version='SYNTHETIC_COMPLETE')
    class Universe:
        def members(self, *args):
            return pd.DataFrame({'instrument_id':['a','b'],'board':['main','main']})
    return ChinaAshareAdapter(cal,Universe(),ledger,DatedFees([FeePeriod('2024-01-01','2024-12-31')],'SYNTHETIC_ZERO_FEES'))


def book(events):
    e=ExecutionEngine(adapter(events),initial_cash=1000.)
    e.lots['a']=[dict(quantity=100.,basis=1000.,date=pd.Timestamp('2024-06-03'))]
    return e


@pytest.mark.parametrize('kind,ratio,expected,mark',[
    ('split',2.,200.,5.),('reverse_split',.5,50.,20.),
    ('bonus_share',.2,120.,10/1.2),('capitalization',.1,110.,10/1.1)])
def test_share_events_preserve_equity_and_total_cost_basis(kind,ratio,expected,mark):
    e=book([make_event(kind,ratio=ratio,quantity_date=DAY)])
    e.apply_actions(DAY,pd.DataFrame())
    assert e.quantity('a')==pytest.approx(expected)
    assert e.cost_basis('a')==pytest.approx(1000.)
    assert e.cash+e.quantity('a')*mark==pytest.approx(2000.)


@pytest.mark.parametrize('payment',['2024-06-05','2024-06-07'])
def test_dividend_ex_date_and_pay_date_conserve_equity_without_double_count(payment):
    e=book([make_event('cash_dividend',cash_per_share=.5,pay_date=pd.Timestamp(payment))])
    e.apply_actions(DAY,pd.DataFrame())
    assert e.cash+sum(r['amount'] for r in e.receivables)+e.quantity('a')*9.5==pytest.approx(2000.)
    if payment=='2024-06-05':
        assert e.cash==1050. and not e.receivables
    e.settle(pd.Timestamp('2024-06-07'))
    assert e.cash==1050. and not e.receivables


def test_dividend_on_split_day_uses_pre_session_entitlement_regardless_of_event_id_order():
    for dividend_id in ['a','z']:
        dividend=make_event('cash_dividend',cash_per_share=1.,event_id=dividend_id)
        split=make_event('split',ratio=2.,event_id='m')
        e=book([dividend,split])
        e.apply_actions(DAY,pd.DataFrame())
        assert e.quantity('a')==200. and e.cash==1100.


def test_dividend_after_identity_retirement_keeps_pre_session_entitlement():
    for kind in ['delisting','identity_change']:
        for dividend_id in ['a','z']:
            dividend=make_event('cash_dividend',event_id=dividend_id,cash_per_share=1.)
            transfer=make_event(kind,event_id='m',target_id='b' if kind=='identity_change' else None,
                cash_per_share=10. if kind=='delisting' else 0.)
            e=book([dividend,transfer])
            e.apply_actions(DAY,pd.DataFrame({'raw_open':[10.]},index=['b']))
            assert e.cash==1100.+(1000. if kind=='delisting' else 0.)
            assert e.quantity('a')==0.


def test_ambiguous_same_identity_share_actions_fail_before_mutation():
    e=book([make_event('split',event_id='a',ratio=2.),make_event('bonus_share',event_id='b',ratio=.1,quantity_date=DAY)])
    with pytest.raises(ContractError,match='ambiguous simultaneous'):
        e.apply_actions(DAY,pd.DataFrame())
    assert e.quantity('a')==100. and e.cash==1000.


@pytest.mark.parametrize('kind',['bonus_share','capitalization','rights_issue'])
def test_delayed_delivery_is_blocked_without_entitlement_valuation(kind):
    e=book([make_event(kind,ratio=.2,quantity_date=pd.Timestamp('2024-06-06'),rights_policy='exercise',subscription_price=2.)])
    with pytest.raises(ContractError,match='ENTITLEMENT_VALUATION_UNKNOWN'):
        e.apply_actions(DAY,pd.DataFrame())
    assert e.quantity('a')==100. and e.cash==1000.


def test_rights_subscription_preserves_equity_and_books_cash_basis_and_cn_sellability():
    e=book([make_event('rights_issue',ratio=.25,quantity_date=DAY,rights_policy='exercise',subscription_price=2.)])
    e.apply_actions(DAY,pd.DataFrame())
    assert e.quantity('a')==125. and e.cash==950. and e.cost_basis('a')==1050.
    assert e.cash+e.quantity('a')*8.4==pytest.approx(2000.)
    assert sum(l['quantity'] for l in e.lots['a'] if e.adapter.rules.can_sell(l['date'],DAY))==100.


@pytest.mark.parametrize('policy,cash_change',[('sell',40.),('expire',0.)])
def test_rights_sale_and_explicit_expiry_are_distinct_policies(policy,cash_change):
    e=book([make_event('rights_issue',ratio=.2,quantity_date=DAY,rights_policy=policy,cash_per_share=cash_change/100)])
    e.apply_actions(DAY,pd.DataFrame())
    assert e.quantity('a')==100. and e.cash==1000.+cash_change
    assert e.action_log[-1]['cash_change']==cash_change


def test_unknown_rights_policy_and_unfunded_subscription_never_silently_expire():
    with pytest.raises(ContractError,match='explicit rights policy'):
        book([make_event('rights_issue',quantity_date=DAY,rights_policy='unknown')])
    e=book([make_event('rights_issue',ratio=1.,quantity_date=DAY,rights_policy='exercise',subscription_price=20.)])
    with pytest.raises(ContractError,match='UNFUNDED'):
        e.apply_actions(DAY,pd.DataFrame())
    assert e.cash==1000. and e.quantity('a')==100.


def test_rights_sale_delayed_payment_is_a_receivable_not_available_cash():
    e=book([make_event('rights_issue',ratio=.2,quantity_date=DAY,rights_policy='sell',cash_per_share=.4,pay_date=pd.Timestamp('2024-06-07'))])
    e.apply_actions(DAY,pd.DataFrame())
    assert e.cash==1000. and e.available_cash()==1000.
    assert sum(p['amount'] for p in e.receivables)==40.
    e.settle(pd.Timestamp('2024-06-07'))
    assert e.cash==1040. and not e.receivables


@pytest.mark.parametrize('kind,ratio,cash,target_price',[
    ('identity_change',1.,0.,10.),('merger',.4,2.,20.)])
def test_identity_transfer_and_merger_preserve_economic_equity(kind,ratio,cash,target_price):
    e=book([make_event(kind,target_id='b',ratio=ratio,cash_per_share=cash)])
    e.apply_actions(DAY,pd.DataFrame({'raw_open':[target_price]},index=['b']))
    assert e.quantity('a')==0. and e.quantity('b')==100.*ratio
    assert e.cash+e.quantity('b')*target_price==pytest.approx(2000.)
    assert e.cost_basis('b')==pytest.approx(1000.*(ratio*target_price)/(ratio*target_price+cash))


def test_symbol_change_retains_id_and_delisting_requires_explicit_consideration():
    e=book([make_event('symbol_change')])
    e.apply_actions(DAY,pd.DataFrame())
    assert e.quantity('a')==100. and e.cost_basis('a')==1000.
    with pytest.raises(ContractError,match='cash consideration'):
        book([make_event('delisting',cash_per_share=np.nan)])
    e=book([make_event('delisting',cash_per_share=10.)])
    e.apply_actions(DAY,pd.DataFrame())
    assert e.quantity('a')==0. and e.cash==2000.


def coverage(state='VERIFIED_NO_EVENT',**kwargs):
    r=dict(instrument_id='a',start='2024-06-03',end='2024-06-12',state=state,
        source_version='SYNTHETIC_ONLY',available_at='2024-06-01T00:00Z',evidence_status='VERIFIED',evidence='SYNTHETIC_SCOPE_ASSERTION')
    r.update(kwargs)
    return r


def strict_ledger(events=(),rows=None):
    events=pd.DataFrame(events,columns=ActionLedger.COLUMNS+['source_version','evidence_status'])
    return EvidenceActionLedger(events,pd.DataFrame(rows or [coverage()]),version='SYNTHETIC_V2')


def test_known_no_event_and_unknown_are_not_interchangeable():
    ledger=strict_ledger()
    ledger.require_coverage('2024-06-03','2024-06-12',['a'])
    assert ledger.coverage_state('a',DAY,'2024-06-05T01:30Z')=='VERIFIED_NO_EVENT'
    unknown=strict_ledger(rows=[coverage('UNKNOWN',available_at=None,evidence_status='UNVERIFIED',evidence=None)])
    with pytest.raises(ContractError,match='COVERAGE_UNKNOWN'):
        unknown.require_coverage('2024-06-03','2024-06-12',['a'])
    assert unknown.coverage_state('a',DAY,'2024-06-05T01:30Z')=='UNKNOWN'


def test_event_conflicts_gaps_future_evidence_and_unverified_records_fail_closed():
    with pytest.raises(ContractError,match='conflicts'):
        strict_ledger([make_event('split',ratio=2.)])
    ledger=strict_ledger([make_event('split',ratio=2.)],[coverage('VERIFIED_EVENT')])
    ledger.require_coverage('2024-06-03','2024-06-12',['a'])
    assert ledger.coverage_state('a',DAY,'2024-06-05T01:30Z')=='VERIFIED_EVENT'
    ledger=strict_ledger(rows=[coverage(end='2024-06-04'),coverage(start='2024-06-06')])
    with pytest.raises(ContractError,match='COVERAGE_UNKNOWN'):
        ledger.require_coverage('2024-06-03','2024-06-12',['a'])
    ledger=strict_ledger(rows=[coverage(available_at='2024-06-06T00:00Z')])
    with pytest.raises(ContractError,match='UNKNOWN_AT_EXECUTION'):
        ledger.require_session(DAY,['a'],'2024-06-05T01:30Z')
    with pytest.raises(ContractError,match='unverified candidate'):
        strict_ledger([make_event('split',evidence_status='OBSERVED_EVENT_UNVERIFIED')],[coverage('VERIFIED_EVENT')])
    with pytest.raises(ContractError,match='aware'):
        strict_ledger(rows=[coverage(available_at='2024-06-01')])


def test_verified_delisting_with_deferred_cash_cannot_credit_cash_early():
    with pytest.raises(ContractError,match='DEFERRED_CONSIDERATION_UNKNOWN'):
        strict_ledger([make_event('delisting',cash_per_share=10.,pay_date=pd.Timestamp('2024-06-07'))],[coverage('VERIFIED_EVENT')])


def fast_fixture():
    a=adapter([])
    dates=a.calendar.schedule.index
    bars=pd.DataFrame([dict(date=d,instrument_id=i,adj_open=10.,adj_close=10.) for d in dates for i in ['a','b']])
    targets=pd.DataFrame(dict(date=[dates[0],dates[0]],instrument_id=['a','b'],target_weight=[.5,.5]))
    return a,dates,bars,targets


@pytest.mark.parametrize('bad',[np.nan,0.,-1.,np.inf])
def test_new_purchase_with_invalid_close_is_blocked_before_trading(bad):
    a,dates,bars,targets=fast_fixture()
    bars.loc[bars.date.eq(dates[1])&bars.instrument_id.eq('b'),'adj_close']=bad
    with pytest.raises(ValuationError) as caught:
        fast_backtest(bars,targets,a.calendar,a.fees,dates[0],dates[-1])
    error=caught.value
    assert error.date==dates[1] and error.cash==1e6 and not error.quantities
    assert len(error.partial.nav)==1 and error.partial.trades.empty
    assert error.issues[0]['instrument_id']=='b' and error.issues[0]['field']=='adj_close'


def test_multiple_missing_holdings_preserve_completed_ledger_without_fake_marks():
    a,dates,bars,targets=fast_fixture()
    bars=bars.loc[~bars.date.eq(dates[2])]
    with pytest.raises(ValuationError) as caught:
        fast_backtest(bars,targets,a.calendar,a.fees,dates[0],dates[-1])
    e=caught.value
    assert e.date==dates[2] and len(e.issues)==4
    assert len(e.partial.nav)==2 and len(e.partial.trades)==2 and len(e.partial.positions)==2
    assert e.partial.positions.market_value.sum()+e.cash==pytest.approx(e.partial.nav.nav.iloc[-1]*1e6)
    assert set(e.partial.positions.date)=={dates[1]}


def test_held_zero_price_requires_explicit_terminal_event_instead_of_zero_nav():
    a,dates,bars,targets=fast_fixture()
    bars.loc[bars.date.eq(dates[2])&bars.instrument_id.eq('b'),'adj_close']=0.
    with pytest.raises(ValuationError) as caught:
        fast_backtest(bars,targets,a.calendar,a.fees,dates[0],dates[-1])
    assert len(caught.value.partial.nav)==2 and caught.value.quantities['b']==50000.


def test_fast_adjusted_units_do_not_apply_a_second_split_or_cash_dividend():
    a,dates,bars,targets=fast_fixture()
    bars['raw_open']=np.where(bars.date>=dates[2],5.,10.)
    bars['raw_close']=bars.raw_open
    r=fast_backtest(bars,targets,a.calendar,a.fees,dates[0],dates[-1])
    assert r.nav.nav.eq(1.).all() and r.actions.empty
    assert r.positions.quantity.eq(50000.).all()


@pytest.mark.parametrize('field,bad',[('raw_close',np.nan),('raw_close',0.),('raw_open',np.nan),('raw_open',-1.)])
def test_execution_new_target_requires_both_positive_raw_prices_before_any_fill(field,bad):
    a,dates,bars,targets=fast_fixture()
    bars['raw_open']=10.
    bars['raw_close']=10.
    bars['halted']=False
    bars['no_price_limit']=True
    bars.loc[bars.date.eq(dates[1])&bars.instrument_id.eq('b'),field]=bad
    e=ExecutionEngine(a)
    with pytest.raises(ContractError,match='target instrument valuation missing'):
        e.run(bars,targets,dates[0],dates[-1])
    assert e.cash==1e6 and not e.trades
