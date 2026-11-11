"""Finish DQ-03 using frozen event indexes, identities and saved positive holdings."""
from __future__ import annotations

import argparse
from collections import Counter
from io import BytesIO
import json
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dq03_cross_table_audit import ROOT, Inputs, dump, event_state, identity_timeline, price_join, write_csv
from factorlab.data.csmar.profiles import MARKETS


def day(value):
    return str(value)[:10] if pd.notna(value) else None


def january_code_risks(lineage, output):
    """Project a completed private lineage index to per-code counts, without prices."""
    counts, keys = Counter(), 0
    with lineage.open(encoding='utf-8') as stream:
        for line in stream:
            record = json.loads(line)
            keys += 1
            if record['classification']=='IDENTITY_AMBIGUITY':
                counts[record['code']] += 1
    conflicts = pd.read_csv(output/'identity-conflicts.csv',keep_default_na=False).to_dict('records')
    for code,n in sorted(counts.items()):
        conflicts.append(dict(source='PR87:COMPLETED_PRIVATE_LINEAGE_INDEX',subject=code,issue='JANUARY_SAME_CODE_DIFFERENT_NAMES',
                              count=n,status='IDENTITY_UNKNOWN',evidence='2021-01; per-code aggregate only; not independent issuer proof'))
    write_csv(output/'identity-conflicts.csv',conflicts)
    verification = json.loads((output/'verification.json').read_text(encoding='utf-8'))
    verification['identity']['january_lineage_keys_projected'] = keys
    verification['identity']['january_identity_ambiguous_codes'] = len(counts)
    verification['identity']['january_identity_ambiguous_key_count'] = sum(counts.values())
    dump(output/'verification.json',verification)
    print(json.dumps(dict(january_keys=keys,ambiguous_codes=len(counts),identity_ambiguous_keys=sum(counts.values()))),flush=True)


def temporal_links(output, events_path, distributions_path):
    """Keep declared payment dates separate from independently verified receipts."""
    from dq03_cross_table_audit import knowledge_state
    links = pd.read_csv(output/'corporate-action-links.csv',keep_default_na=False)
    pilot = pd.read_csv(ROOT/'docs/validation/tri-source/semantics/corporate-actions.csv',keep_default_na=False)
    scheduled = {'601857:'+r.effective_date:r.payment_date for r in pilot.loc[pilot.source.eq('CSMAR') & pilot.event_type.eq('CASH_DIVIDEND')].itertuples()}
    for r in json.loads(distributions_path.read_text(encoding='utf-8')):
        scheduled['PR89:'+str(r['row'])] = r['record']['Paydt']
    candidates = {r['event_id']:r['record'] for r in json.loads(events_path.read_text(encoding='utf-8'))}
    links['source_declared_payment_date'] = links.event_id.map(scheduled).fillna('UNKNOWN')
    links['payment_chronology'] = ['UNKNOWN' if r.source_declared_payment_date=='UNKNOWN' else
                                   ('SOURCE_PAYMENT_ON_OR_AFTER_EX' if r.source_declared_payment_date>=r.ex_date else 'INVALID_SOURCE_PAYMENT_ORDER')
                                   for r in links.itertuples()]
    links['decision_time_state'] = [knowledge_state(day(candidates[r.event_id]['publish_date']),None,r.record_date,version_linked=False)
                                     if r.event_id in candidates else 'PIT_UNKNOWN' for r in links.itertuples()]
    write_csv(output/'corporate-action-links.csv',links.to_dict('records'))
    verification = json.loads((output/'verification.json').read_text(encoding='utf-8'))
    verification['corporate_actions']['source_payment_chronology'] = links.payment_chronology.value_counts().to_dict()
    verification['corporate_actions']['register_day_knowledge_tests'] = len(candidates)
    verification['corporate_actions']['register_day_knowledge_states'] = links.loc[links.event_id.isin(candidates),'decision_time_state'].value_counts().to_dict()
    dump(output/'verification.json',verification)
    print(json.dumps(verification['corporate_actions']),flush=True)


def finish(args):
    begin = time.perf_counter()
    private, output = args.private, args.output
    verification = json.loads((output/'verification.json').read_text(encoding='utf-8'))
    members = pd.read_parquet(private/'members.parquet')
    basic = pd.read_parquet(private/'identity-basic.parquet')
    names = pd.read_parquet(private/'identity-names.parquet')
    co = pd.read_parquet(private/'identity-company.parquet')
    conflicts = pd.read_csv(output/'identity-conflicts.csv',keep_default_na=False).to_dict('records')
    coverage = pd.read_csv(output/'join-coverage.csv',keep_default_na=False).to_dict('records')
    markets = MARKETS | {'64':'BJ'}
    co['symbol'] = [str(r.Stkcd)+'.'+markets.get(str(r.Markettype).removesuffix('.0'),'UNKNOWN') for r in co.itertuples()]
    cross = co.merge(basic[['instrument','full_name','name']],left_on='symbol',right_on='instrument',how='left',validate='many_to_one')
    cross['comparison'] = 'IDENTITY_UNKNOWN'
    cross.loc[cross.Conme.eq(cross.full_name) & cross.full_name.notna(),'comparison'] = 'CURRENT_FULL_NAME_MATCH'
    cross.loc[cross.full_name.notna() & cross.Conme.ne(cross.full_name),'comparison'] = 'CURRENT_FULL_NAME_DIFFERENCE'
    cross.to_parquet(private/'identity-cross-source.parquet',index=False)
    for r in cross.loc[cross.comparison.eq('CURRENT_FULL_NAME_DIFFERENCE')].itertuples():
        conflicts.append(dict(source='CSMAR:TRDNEW_Co|BigQuant:basic_info',subject=r.symbol,issue='CURRENT_FULL_NAME_DIFFERENCE',count=1,status='IDENTITY_UNKNOWN',evidence='snapshot name or version difference; no issuer ID equivalence inferred'))
    duplicate_codes = co.groupby('Stkcd').size()
    for code,n in duplicate_codes[duplicate_codes.gt(1)].items():
        conflicts.append(dict(source='CSMAR:TRDNEW_Co',subject=code,issue='MULTIPLE_COMPANY_ROWS',count=int(n),status='IDENTITY_UNKNOWN'))
    january = pd.read_csv(ROOT/'docs/validation/p10-d01/duplicate-classification.csv')
    for r in january.loc[january.classification.eq('IDENTITY_AMBIGUITY')].itertuples():
        conflicts.append(dict(source='PR87:CNRDS:2021-01',subject=r.evidence_id,issue='SAME_CODE_DIFFERENT_NATIVE_NAMES',count=r.candidate_count,status='IDENTITY_UNKNOWN',evidence='INHERITED_COMPLETE_CLASSIFICATION; QUARANTINE_ALL'))
    write_csv(output/'identity-conflicts.csv',conflicts)
    endpoint = set(members.loc[members.date.eq(members.date.max()),'symbol'])
    survivor = members.loc[~members.symbol.isin(endpoint)]
    # One row for each actual saved positive account/security/session, without publishing account quantities.
    holdings = []
    for account in ['B_factor_score-fold-0','B_ridge-fold-0']:
        positions = pd.read_parquet(args.accounts/account/'positions.parquet',columns=['date','symbol','shares'])
        positions = positions.loc[positions.shares.gt(0),['date','symbol']].copy()
        positions.date = pd.to_datetime(positions.date).dt.strftime('%Y-%m-%d')
        positions['account'] = account
        holdings.append(positions)
    holdings = pd.concat(holdings,ignore_index=True)
    membership_keys = pd.MultiIndex.from_frame(members[['date','symbol']])
    holdings['is_member'] = pd.MultiIndex.from_frame(holdings[['date','symbol']]).isin(membership_keys)
    first = members.groupby('symbol').date.min()
    holdings['post_exit'] = ~holdings.is_member & holdings.symbol.map(first).lt(holdings.date)
    outside = holdings.loc[holdings.post_exit].copy()
    events = json.loads(args.events.read_text(encoding='utf-8'))
    # Public historical evidence matrix is reused, not rescanned from the 7z archives.
    semantic = pd.read_csv(ROOT/'docs/validation/tri-source/semantics/corporate-actions.csv',keep_default_na=False)
    pilot = semantic.loc[semantic.event_type.eq('CASH_DIVIDEND')]
    event_rows = []
    for ex,g in pilot.groupby('effective_date'):
        implementation = ex in {'2018-06-21','2018-09-21'}
        bq = g.loc[g.source.eq('BigQuant')].iloc[0]
        cn = g.loc[g.source.eq('CNRDS')].iloc[0]
        event_rows.append(dict(event_id='601857:'+ex,symbol='601857.SH',ex_date=ex,record_date=day(bq.record_date),
                               event_type='CASH_DIVIDEND',state=event_state(observed=True,terms_matched=True,implementation_verified=implementation),
                               payment_state='UNRESOLVED',pit_state='PIT_UNKNOWN',identity_state='IDENTITY_UNKNOWN',
                               evidence_mode='INHERITED_PR80_AND_P3D03',
                               evidence='docs/data/p3-d03-summary.json#notice_text_checks' if implementation else 'tri-source/semantics/corporate-actions.csv',
                               source_count=3 if cn.comparison_status=='MATCHED_TERMS' else 2,
                               cnrds_state='TERMS_MATCHED' if cn.comparison_status=='MATCHED_TERMS' else 'UNRESOLVED',
                               account_links=0, accounting_contract='PAYMENT_UNVERIFIED' if implementation else 'IMPLEMENTATION_UNVERIFIED'))
    distributions = json.loads(args.distributions.read_text(encoding='utf-8'))
    for r in distributions:
        record = r['record']
        symbol = record['Stkcd']+'.'+markets[str(record['Markettype']).removesuffix('.0')]
        event_rows.append(dict(event_id='PR89:'+str(r['row']),symbol=symbol,ex_date=record['Exdistdt'],record_date=None,
                               event_type='CASH_DIVIDEND' if record['Disttyp']=='CA' else 'UNRESOLVED',
                               state='EVENT_CANDIDATE',payment_state='UNRESOLVED',pit_state='PIT_UNKNOWN',identity_state='IDENTITY_UNKNOWN',
                               evidence_mode='SAVED_NATIVE_EVENT_INDEX_PR89',evidence='TRDNEW_Cptl.xlsx:'+str(r['row']),
                               source_count=1,cnrds_state='UNRESOLVED',account_links=0,accounting_contract='IMPLEMENTATION_AND_RECORD_DATE_UNVERIFIED'))
    for item in events:
        r=item['record']
        eligible = holdings.loc[holdings.date.eq(day(r['register_date'])) & holdings.symbol.eq(r['instrument'])]
        event_rows.append(dict(event_id=item['event_id'],symbol=r['instrument'],ex_date=day(r['ex_date']),record_date=day(r['register_date']),
                               event_type='CASH_DIVIDEND',state='EVENT_CANDIDATE',payment_state='UNRESOLVED',pit_state='PIT_UNKNOWN',identity_state='IDENTITY_UNKNOWN',
                               evidence_mode='SAVED_NATIVE_CANDIDATE_PR93',evidence=item['identity']['source_member']+':'+item['identity']['payload_sha256'],
                               source_count=1,cnrds_state='UNRESOLVED',account_links=len(eligible),accounting_contract='IMPLEMENTATION_PUBLIC_TIME_PAYMENT_UNVERIFIED'))
    event_frame = pd.DataFrame(event_rows)
    # Same source event could be seen in two accounts: count account links, never duplicate the economic event.
    economic = ['symbol','ex_date','event_type']
    duplicate_events = int(event_frame.duplicated(economic).sum())
    keys = outside[['date','symbol']].drop_duplicates()
    for r in event_rows:
        keys = pd.concat([keys,pd.DataFrame([dict(date=r['ex_date'],symbol=r['symbol'])])],ignore_index=True)
    keys = keys.drop_duplicates()
    inputs, price_results = Inputs(), []
    with zipfile.ZipFile(args.stock) as archive:
        for month,g in keys.groupby(keys.date.str[:7]):
            member = next(n for n in archive.namelist() if n.startswith('stock/cn_stock_real_bar1d/'+month) and n.endswith('.parquet'))
            raw = inputs.parquet(archive,member,['date','instrument','open','high','low','close'])
            raw.date = raw.date.dt.strftime('%Y-%m-%d')
            raw = raw.rename(columns={'instrument':'symbol'})
            price_results.append(price_join(g,raw))
    observed = pd.concat(price_results,ignore_index=True)
    observed.to_parquet(private/'targeted-event-holding-price-joins.parquet',index=False)
    outside = outside.merge(observed,on=['date','symbol'],how='left',validate='many_to_one')
    outside.to_parquet(private/'post-exit-held-joins.parquet',index=False)
    held_stats = dict(positive_account_days=len(holdings),accounts=2,post_exit_account_days=len(outside),
                      post_exit_unique_security_days=len(outside[['date','symbol']].drop_duplicates()),
                      post_exit_price_states=outside.price_state.value_counts().to_dict(),
                      nonmember_account_days=int((~holdings.is_member).sum()),
                      other_nonmember_account_days=int(((~holdings.is_member)&(~holdings.post_exit)).sum()))
    event_frame = event_frame.merge(observed.rename(columns={'date':'ex_date'}),on=['ex_date','symbol'],how='left',validate='many_to_one')
    event_frame['chronology'] = [
        'RECORD_BEFORE_EX' if pd.notna(r.record_date) and r.record_date<r.ex_date else 'UNKNOWN' for r in event_frame.itertuples()]
    # A source Paydt field denotes a scheduled/source-declared payment, not a cash settlement receipt.
    event_frame['actual_payment_verified'] = False
    write_csv(output/'corporate-action-links.csv',event_frame.to_dict('records'))
    corporate = dict(economic_events=len(event_frame),duplicate_economic_keys=duplicate_events,
                     states=event_frame.state.value_counts().to_dict(),ex_date_price_states=event_frame.price_state.value_counts().to_dict(),
                     payment_verified=0,pit_unknown=len(event_frame),held_candidate_events=len(events),
                     account_event_intersections=sum(r['account_links'] for r in event_rows),
                     chronology=event_frame.chronology.value_counts().to_dict(),proposal_only_rows=1)
    financial_tests = pd.read_csv(private/'financial-decision-tests.csv')
    future_corrections = financial_tests.loc[financial_tests.correction_later_than_decision]
    risks = [dict(relation='membership',case='ENDPOINT_SURVIVOR_SUBSTITUTION',status='FUTURE_INFORMATION_RISK',
                  denominator=len(members),affected=len(survivor),scope='full research window',
                  observation='counterfactual endpoint-only selection drops actual historical member-days; current reader uses exact dates'),
             dict(relation='membership',case='HISTORICAL_AVAILABILITY',status='PIT_UNKNOWN',denominator=len(members),affected=len(members),
                  scope='full research window',observation='frozen manifest historical_available_at unknown; retrieval does not prove contemporaneous knowledge'),
             dict(relation='industry',case='CURRENT_TREE_HISTORICAL_BACKFILL',status='PIT_UNKNOWN',denominator=960,affected=960,scope='full available tree snapshot',observation='no historical interval or publication timestamp'),
             dict(relation='financial',case='LATEST_VALUE_AS_OF_REPORT_PERIOD',status='PIT_UNKNOWN',denominator=len(financial_tests),affected=len(financial_tests),scope='bank table member-decision tests',observation='report period and correction list cannot certify first published value'),
             dict(relation='corporate_action',case='EX_PRICE_JUMP_AS_CASH_RECEIPT',status='UNRESOLVED',denominator=len(event_frame),affected=len(event_frame),scope='16 targeted events',observation='zero verified actual payments; no account settlement evidence'),
             dict(relation='price',case='FUTURE_ADJUSTMENT_CHAIN_AS_RAW',status='HFQ_UNVERIFIED',denominator=4,affected=4,scope='PR80 four 601857 windows; inherited',observation='observed factor conflicts do not establish historical version of adjustment chain'),
             dict(relation='price_status',case='SUSPENSION_FILL_AS_EXECUTION_PRICE',status='CONFIRMED_NONTRADE_FILL',denominator=1,affected=1,scope='000540.SZ 2023-05-04; inherited PR94',observation='CSMAR Filling=1; BigQuant raw missing; valuation mark is not a real trade'),
             dict(relation='identity',case='POST_DELIST_NORMAL_TRADABLE_JOIN',status='UNRESOLVED',denominator=len(members),affected=verification['members']['at_or_after_snapshot_delist_rows'],scope='current lifecycle snapshot vs actual membership',observation='600068 transition: member record does not authorize tradability or a 1:1 successor alias')]
    for r in future_corrections.itertuples():
        risks.append(dict(relation='financial',case=r.evidence_id,status='PIT_UNKNOWN',denominator=1,affected=1,scope=r.decision,
                          observation='later correction-date candidate in selected actual record; first-publication/value versions absent; period='+r.period))
    write_csv(output/'pit-risk-cases.csv',risks)
    unresolved = [dict(relation='identity',status='IDENTITY_UNKNOWN',denominator=len(members),affected=len(members),minimum_evidence='cross-source stable issuer/security IDs and valid code lifecycle; snapshot full-name agreement insufficient'),
                  dict(relation='membership',status='MEMBERSHIP_TIME_UNKNOWN',denominator=len(members),affected=len(members),minimum_evidence='original effective adjustment versions plus contemporary membership publication and provider availability'),
                  dict(relation='cnrds_january',status='IDENTITY_UNKNOWN',denominator=len(january),affected=int(january.classification.eq('IDENTITY_AMBIGUITY').sum()),minimum_evidence='independent issuer/version evidence; keep all ambiguous pairs quarantined'),
                  dict(relation='financial',status='PIT_UNKNOWN',denominator=verification['financial']['rows'],affected=verification['financial']['rows'],minimum_evidence='first disclosures and per-revision values, timestamps and report type'),
                  dict(relation='industry',status='MEMBERSHIP_TIME_UNKNOWN',denominator=verification['industry']['change_rows'],affected=verification['industry']['change_rows'],minimum_evidence='change_flag/date definitions, initial state, classification version and revision chain'),
                  dict(relation='corporate_actions',status='UNRESOLVED',denominator=len(event_frame),affected=len(event_frame),minimum_evidence='settlement receipts, public timestamps, stable identity and correction/cancellation chain'),
                  dict(relation='proposal_only',status='EVENT_CANDIDATE',denominator=1,affected=1,minimum_evidence='601857 annual proposal is not a dated implementation event; need actual implementation record')]
    write_csv(output/'unresolved-relations.csv',unresolved)
    for relation,source,denominator,count,status in [
        ('identity_snapshot','BigQuant:basic',len(members),verification['members']['basic_snapshot_keys_matched'],'CURRENT_SNAPSHOT_KEY_MATCH'),
        ('identity_cross_source','CSMAR|BigQuant',len(cross),int(cross.comparison.eq('CURRENT_FULL_NAME_MATCH').sum()),'CURRENT_FULL_NAME_MATCH'),
        ('cnrds_january','PR87',len(january),len(january),'QUARANTINED_AMBIGUOUS_KEY'),
        ('financial','CSMAR:FS_BComins',verification['financial']['rows'],verification['financial']['rows'],'PIT_UNKNOWN'),
        ('industry_tree','BigQuant',960,960,'CURRENT_SNAPSHOT_ONLY'),
        ('industry_changes','BigQuant',verification['industry']['change_rows'],verification['industry']['change_rows'],'MEMBERSHIP_TIME_UNKNOWN'),
        ('industry_component','BigQuant:2023-03',verification['industry']['component_keys'],verification['industry']['component_keys'],'UNIQUE_WITH_SYSTEM_NAMESPACE'),
        ('corporate_actions','ALL',len(event_frame),len(event_frame),'TARGETED_EVENT_LINK_CHECKED'),
        ('held_post_exit','BigQuant',len(outside),int(outside.price_state.eq('MATCHED').sum()),'MATCHED')]:
        coverage.append(dict(relation=relation,source=source,scope='EXPLICIT_BOUNDED_OR_COMPLETE_INPUT_KEYS',status=status,denominator=denominator,count=count,unit='relation_specific_records'))
    write_csv(output/'join-coverage.csv',coverage)
    verification['identity'].update(company_snapshot_match_counts=cross.comparison.value_counts().to_dict(),
                                    company_duplicate_code_groups=int(duplicate_codes.gt(1).sum()),
                                    january_ambiguous_keys=len(january),january_identity_ambiguous_keys=int(january.classification.eq('IDENTITY_AMBIGUITY').sum()),
                                    endpoint_survivor_lost_member_days=len(survivor),endpoint_survivor_lost_symbols=survivor.symbol.nunique())
    verification['corporate_actions']=corporate
    verification['post_exit_holdings']=held_stats
    verification['public_input_identity'].extend(inputs.records)
    verification['additional_resources']=dict(seconds=time.perf_counter()-begin,workers=1,
                                              peak_bytes=getattr(psutil.Process().memory_info(),'peak_wset',psutil.Process().memory_info().rss))
    test_xml=ET.parse(private/'targeted-tests.xml').getroot()
    verification['tests']=[{k:s.attrib[k] for k in ['tests','failures','errors','skipped','time']} for s in test_xml.iter('testsuite')]
    verification['status']='DQ03_COMPLETED_WITH_EXPLICIT_UNKNOWNS'
    dump(output/'verification.json',verification)
    identity_timeline(private, output)
    temporal_links(output, args.events, args.distributions)
    january_code_risks(args.january_lineage, output)
    print(json.dumps(dict(identity=verification['identity'],corporate=corporate,held=held_stats,tests=verification['tests']),ensure_ascii=False),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['private','output','stock','accounts','events','distributions','january-lineage']:
        p.add_argument('--'+name,type=Path,required=True)
    finish(p.parse_args())


if __name__=='__main__':
    main()
