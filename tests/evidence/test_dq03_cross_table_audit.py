"""Synthetic relation-rule regressions, independent of paid source payloads."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pandas as pd

SPEC = spec_from_file_location('dq03', Path(__file__).resolve().parents[2] / 'tools/data/dq03_cross_table_audit.py')
AUDIT = module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def prices(records):
    return pd.DataFrame([dict(date=d, symbol=s, open=v, high=v, low=v, close=v) for d,s,v in records])


def test_duplicate_prices_are_quarantined_not_expanded():
    members = pd.DataFrame([dict(date='2023-03-13',symbol='600001.SH')])
    result = AUDIT.price_join(members, prices([('2023-03-13','600001.SH',1),('2023-03-13','600001.SH',2)]))
    assert len(result)==1
    assert result.price_candidates.tolist()==[2]
    assert result.price_state.tolist()==['DUPLICATE_PRICE_KEY']


def test_missing_key_differs_from_missing_ohlc():
    members = pd.DataFrame([dict(date='2023-03-13',symbol=s) for s in ['600001.SH','600002.SH','600003.SH']])
    result = AUDIT.price_join(members, prices([('2023-03-13','600002.SH',None),('2023-03-13','600003.SH',1)]))
    assert result.price_state.tolist()==['NO_PRICE_KEY','MISSING_OHLC','MATCHED']


def test_exchange_and_successor_are_not_implicit_aliases():
    members = pd.DataFrame([dict(date='2021-09-13',symbol='600068.SH'),dict(date='2025-02-14',symbol='300114.SZ')])
    result = AUDIT.price_join(members, prices([('2021-09-13','601868.SH',1),('2025-02-14','302132.SZ',1),('2021-09-13','600068.SZ',1)]))
    assert result.price_state.tolist()==['NO_PRICE_KEY','NO_PRICE_KEY']


def test_period_and_export_are_not_publication_evidence():
    assert AUDIT.knowledge_state(None,None,'2023-03-13',version_linked=False)=='PIT_UNKNOWN'
    assert AUDIT.knowledge_state('2023-03-14',None,'2023-03-13',version_linked=False)=='FUTURE_PUBLICATION'
    assert AUDIT.knowledge_state('2023-03-10','2023-03-14','2023-03-13',version_linked=True)=='FUTURE_PROVIDER_AVAILABILITY'
    assert AUDIT.knowledge_state('2023-03-10','2023-03-10','2023-03-13',version_linked=False)=='PIT_UNKNOWN'
    assert AUDIT.knowledge_state('2023-03-10','2023-03-10','2023-03-13',version_linked=True)=='KNOWN_BY_DECISION'


def test_interval_overlap_uses_running_max_not_only_previous_row():
    f=pd.DataFrame([dict(instrument='A',start_date=pd.Timestamp(a),end_date=pd.Timestamp(b)) for a,b in [('2020-01-01','2020-12-31'),('2020-02-01','2020-03-01'),('2020-04-01','2020-05-01')]])
    assert AUDIT.interval_conflicts(f)==[('A','2020-02-01'),('A','2020-04-01')]


def test_member_name_timeline_distinguishes_interval_gaps_and_unknown_symbols(tmp_path):
    import json
    private=tmp_path/'private'
    output=tmp_path/'output'
    private.mkdir()
    output.mkdir()
    pd.DataFrame([dict(date=d,symbol=s) for d,s in [('2023-03-13','A'),('2023-03-15','A'),('2023-03-13','B')]]).to_parquet(private/'members.parquet')
    pd.DataFrame([dict(instrument='A',start_date=pd.Timestamp('2023-03-01'),end_date=pd.Timestamp('2023-03-13'))]).to_parquet(private/'identity-names.parquet')
    pd.DataFrame([dict(instrument='A',list_date=pd.Timestamp('2020-01-01'))]).to_parquet(private/'identity-basic.parquet')
    AUDIT.dump(output/'verification.json',dict(identity={}))
    AUDIT.write_csv(output/'join-coverage.csv',[dict(relation='fixture',count=0)])
    AUDIT.identity_timeline(private,output)
    result=json.loads((output/'verification.json').read_text())['identity']['member_name_timeline']
    assert result['denominator']==3
    assert result['name_states']==dict(DATED_NAME_OBSERVATION=1,NAME_INTERVAL_GAP=1,NAME_INTERVAL_UNKNOWN=1)
    assert result['identity_certification']=='IDENTITY_UNKNOWN'


def test_event_terms_and_price_link_do_not_verify_implementation_or_payment():
    assert AUDIT.event_state(observed=False)=='UNRESOLVED'
    assert AUDIT.event_state(observed=True)=='EVENT_CANDIDATE'
    assert AUDIT.event_state(observed=True,terms_matched=True)=='TERMS_MATCHED'
    assert AUDIT.event_state(observed=True,terms_matched=True,payment_verified=True)=='TERMS_MATCHED'
    assert AUDIT.event_state(observed=True,implementation_verified=True)=='IMPLEMENTATION_VERIFIED'
    assert AUDIT.event_state(observed=True,implementation_verified=True,payment_verified=True)=='PAYMENT_VERIFIED'


def test_unmatched_prices_do_not_convert_null_ohlc_to_zero():
    m=pd.DataFrame([dict(date='2023-03-13',symbol='A')])
    p=prices([('2023-03-13','A',0)])
    assert AUDIT.price_join(m,p).price_state.tolist()==['MATCHED']
    p.loc[0,'high']=None
    assert AUDIT.price_join(m,p).price_state.tolist()==['MISSING_OHLC']
