"""Read-only, partitioned relation diagnostics; never changes research inputs."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from io import BytesIO
import json
from pathlib import Path
import sys
import time
import zipfile

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(Path(__file__).resolve().parent)]
from tri_source_market_audit import workbook_rows

pa.set_cpu_count(1)
pa.set_io_thread_count(1)
GATES = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']
START, END = '2021-09-13', '2026-09-11'
PRICE_STATES = ['NO_PRICE_KEY', 'MISSING_OHLC', 'DUPLICATE_PRICE_KEY', 'MATCHED']


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def write_csv(path, records):
    columns = list(dict.fromkeys(k for row in records for k in row))
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)


def price_join(members, prices):
    """Count candidates before joining; never pick a duplicate's preferred row."""
    counts = prices.groupby(['date', 'symbol']).size().rename('price_candidates')
    unique = prices.loc[~prices.duplicated(['date', 'symbol'], keep=False)]
    missing = unique.set_index(['date', 'symbol'])[['open', 'high', 'low', 'close']].isna().any(axis=1)
    result = members.merge(counts, on=['date', 'symbol'], how='left', validate='many_to_one')
    result['price_candidates'] = result.price_candidates.fillna(0).astype(int)
    result['price_state'] = 'MATCHED'
    result.loc[result.price_candidates.eq(0), 'price_state'] = 'NO_PRICE_KEY'
    result.loc[result.price_candidates.gt(1), 'price_state'] = 'DUPLICATE_PRICE_KEY'
    keys = pd.MultiIndex.from_frame(result[['date', 'symbol']])
    result.loc[result.price_candidates.eq(1) & missing.reindex(keys, fill_value=False).to_numpy(), 'price_state'] = 'MISSING_OHLC'
    return result


def knowledge_state(public_at, available_at, decision_at, *, version_linked):
    """Date-only disclosure does not prove intraday availability or value vintage."""
    if public_at is not None and pd.Timestamp(public_at) > pd.Timestamp(decision_at):
        return 'FUTURE_PUBLICATION'
    if available_at is not None and pd.Timestamp(available_at) > pd.Timestamp(decision_at):
        return 'FUTURE_PROVIDER_AVAILABILITY'
    if public_at is None or available_at is None or not version_linked:
        return 'PIT_UNKNOWN'
    return 'KNOWN_BY_DECISION'


def event_state(*, observed, terms_matched=False, implementation_verified=False, payment_verified=False):
    """A matching price/date or planned payment never supplies settlement proof."""
    if not observed:
        return 'UNRESOLVED'
    if not implementation_verified:
        return 'TERMS_MATCHED' if terms_matched else 'EVENT_CANDIDATE'
    return 'PAYMENT_VERIFIED' if payment_verified else 'IMPLEMENTATION_VERIFIED'


def interval_conflicts(frame, symbol='instrument', start='start_date', end='end_date'):
    """Inclusive-end overlap candidates; endpoint convention still needs a contract."""
    result = []
    for key, group in frame.groupby(symbol):
        group = group.sort_values(start)
        max_end = None
        for row in group.to_dict('records'):
            lo, hi = row[start], row[end]
            if pd.notna(lo) and max_end is not None and lo <= max_end:
                result.append((key, str(lo)[:10]))
            if pd.isna(hi):
                hi = pd.Timestamp.max
            max_end = hi if max_end is None else max(max_end, hi)
    return result


class Inputs:
    def __init__(self):
        self.records = []

    def parquet(self, archive, member, columns=None):
        raw = archive.read(member)
        table = pq.read_table(BytesIO(raw), columns=columns, use_threads=False)
        receipt_name = member + '.receipt.json'
        receipt = json.loads(archive.read(receipt_name))
        self.records.append(dict(source=Path(archive.filename).name, member=member,
                                 source_sha256=receipt['sha256'], identity_kind='SAVED_RECEIPT',
                                 rows_read=table.num_rows, columns=columns or table.column_names,
                                 exported_at=receipt.get('exported_at_utc'),
                                 public_at='UNKNOWN', provider_first_available='UNKNOWN'))
        return table.to_pandas()

    def table(self, archive, prefix, columns=None):
        return pd.concat([self.parquet(archive, n, columns) for n in archive.namelist()
                          if n.startswith(prefix + '/') and n.endswith('.parquet')], ignore_index=True)

    def workbook(self, path):
        with zipfile.ZipFile(path) as archive:
            member, = [n for n in archive.namelist() if n.endswith('.xlsx')]
            records, headers = [], {}
            for sheet, number, values in workbook_rows(archive.read(member)):
                if number == 1:
                    headers[sheet] = values
                elif number > 3:
                    records.append(dict(zip(headers[sheet], values)))
            dictionary = next(n for n in archive.namelist() if n.endswith('.txt'))
            self.records.append(dict(source=path.name, member=member, rows_read=len(records),
                                     identity_kind='DIRECT_NATIVE_WORKBOOK', crc32=archive.getinfo(member).CRC,
                                     dictionary_member=dictionary, dictionary=archive.read(dictionary).decode('utf-8-sig'),
                                     public_at='UNKNOWN', provider_first_available='UNKNOWN'))
        return pd.DataFrame(records)


def native_month(root, source, month, codes, inputs):
    pieces = []
    for p in sorted((root / 'monthly').glob(source + '-*/' + month + '.parquet')):
        receipt = json.loads((root / (p.parent.name + '.json')).read_text(encoding='utf-8'))
        columns = ['date', 'code', 'symbol', 'name', 'open', 'high', 'low', 'close', 'filling']
        table = ds.dataset(p).to_table(columns=columns, filter=ds.field('code').isin(codes), use_threads=False)
        inputs.records.append(dict(source=source, member=receipt['member'], checkpoint=p.parent.name + '/' + p.name,
                                   source_sha256=receipt['sha256'], identity_kind='SAVED_NATIVE_CHECKPOINT',
                                   rows_selected=table.num_rows, columns=columns))
        pieces.append(table.to_pandas())
    if not pieces:
        return pd.DataFrame(columns=['date', 'code', 'symbol', 'name', 'open', 'high', 'low', 'close', 'filling'])
    frame = pd.concat(pieces, ignore_index=True)
    frame[['open', 'high', 'low', 'close']] = frame[['open', 'high', 'low', 'close']].replace({'': None, '-': None, '--': None})
    return frame


def identity_timeline(private, output):
    """Join every member-day to name/lifecycle intervals without a Cartesian product."""
    from bisect import bisect_right
    members = pd.read_parquet(private/'members.parquet')
    names = pd.read_parquet(private/'identity-names.parquet')
    basic = pd.read_parquet(private/'identity-basic.parquet').set_index('instrument')
    groups = {symbol: g.sort_values('start_date').to_dict('records') for symbol,g in names.groupby('instrument')}
    records = []
    for symbol,g in members.groupby('symbol'):
        intervals = groups.get(symbol,[])
        starts = [str(r['start_date'])[:10] for r in intervals]
        for d in g.date:
            index = bisect_right(starts,d)-1
            state = 'NAME_INTERVAL_UNKNOWN'
            if index>=0:
                hi = intervals[index]['end_date']
                state = 'DATED_NAME_OBSERVATION' if pd.isna(hi) or d<=str(hi)[:10] else 'NAME_INTERVAL_GAP'
            listed = None
            if symbol in basic.index:
                listed = basic.at[symbol,'list_date']
            records.append(dict(date=d,symbol=symbol,name_state=state,
                                before_snapshot_listing=bool(pd.notna(listed) and d<str(listed)[:10])))
    frame = pd.DataFrame(records)
    frame.to_parquet(private/'membership-name-timeline.parquet',index=False)
    summary = dict(denominator=len(frame),name_states=frame.name_state.value_counts().to_dict(),
                   before_snapshot_listing=int(frame.before_snapshot_listing.sum()),
                   interval_convention='INCLUSIVE_CANDIDATE; SUPPLIER_ENDPOINT_CONVENTION_UNKNOWN',
                   identity_certification='IDENTITY_UNKNOWN')
    verification = json.loads((output/'verification.json').read_text(encoding='utf-8'))
    verification['identity']['member_name_timeline'] = summary
    dump(output/'verification.json',verification)
    coverage = pd.read_csv(output/'join-coverage.csv',keep_default_na=False).to_dict('records')
    for state,count in summary['name_states'].items():
        coverage.append(dict(relation='membership_name_timeline',source='BigQuant:name_change',scope=START+'..'+END,
                             status=state,denominator=len(frame),count=count,unit='member_days'))
    write_csv(output/'join-coverage.csv',coverage)
    print(json.dumps(summary),flush=True)


def financial_checks(frame, members, output, private):
    keys = ['Stkcd', 'Accper'] if 'Accper' in frame else ['Stkcd', 'EndDate']
    typed = keys + ['Typrep']
    counts = frame.groupby(keys).size()
    typed_counts = frame.groupby(typed).size()
    corrected = frame.IfCorrect.eq('1') | frame.IfCorrect.eq('1.0')
    frame.to_parquet(private / 'bank-financial-native.parquet', index=False)
    rows = [dict(source='CSMAR:FS_BComins', check='company_period_join', denominator=len(counts),
                 affected=int(counts.gt(1).sum()), status='REPORT_TYPE_MUST_BE_KEY',
                 observation='A/B are distinct legal report scopes, not duplicate financial facts'),
            dict(source='CSMAR:FS_BComins', check='company_period_type_join', denominator=len(typed_counts),
                 affected=int(typed_counts.gt(1).sum()), status='VERSION_UNKNOWN',
                 observation='remaining multiplicity requires explicit original/correction value versions'),
            dict(source='CSMAR:FS_BComins', check='correction_flag', denominator=len(frame),
                 affected=int(corrected.sum()), status='PIT_UNKNOWN', observation='correction date is not first publication')]
    # Each selected financial record is tested at the first research session on/after its report period.
    from bisect import bisect_left
    by_code = {str(code): sorted(g.date.unique()) for code, g in members.groupby(members.symbol.str[:6])}
    tests = []
    for i, row in frame.iterrows():
        period = str(row[keys[1]])[:10]
        symbol_code = str(row.Stkcd).zfill(6)
        possible = by_code.get(symbol_code, [])
        position = bisect_left(possible, period)
        if position == len(possible):
            continue
        decision = possible[position]
        correction_date = row.DeclareDate
        tests.append(dict(evidence_id=f'FS_BComins:{i+4}', period=period, decision=decision,
                          correction_later_than_decision=bool(pd.notna(correction_date) and str(correction_date)[:10] > decision),
                          state=knowledge_state(None, None, decision, version_linked=False)))
    write_csv(private / 'financial-decision-tests.csv', tests)
    rows.append(dict(source='CSMAR:FS_BComins', check='actual_member_signal_day_read', denominator=len(tests),
                     affected=len(tests), status='PIT_UNKNOWN',
                     observation=f"{sum(t['correction_later_than_decision'] for t in tests)} records contain a later correction-date candidate; no first-publication/value-version linkage"))
    write_csv(output / 'financial-temporal-checks.csv', rows)
    return dict(rows=len(frame), company_periods=len(counts), typed_keys=len(typed_counts),
                multi_company_periods=int(counts.gt(1).sum()), multi_typed_keys=int(typed_counts.gt(1).sum()),
                corrected_rows=int(corrected.sum()), signal_tests=len(tests), signal_pit_unknown=len(tests),
                later_correction_candidates=sum(t['correction_later_than_decision'] for t in tests),
                columns=frame.columns.tolist(), report_types=frame.Typrep.value_counts().to_dict())


def industry_checks(archive, inputs, members, output, private):
    tree = inputs.table(archive, 'industry/cn_stock_industry')
    mapper = inputs.table(archive, 'industry/cn_stock_industry_index_mapper')
    change = inputs.table(archive, 'industry/cn_stock_industry_change')
    component = inputs.parquet(archive, 'industry/cn_stock_industry_component/2023-03-01_2023-03-31.parquet')
    keys = ['date', 'instrument', 'industry']
    counts = component.groupby(keys).size()
    membership = members.rename(columns={'symbol': 'instrument'}).copy()
    membership.date = pd.to_datetime(membership.date)
    march = membership.loc[membership.date.between('2023-03-01', '2023-03-31')]
    same_day = march.merge(component, on=['date', 'instrument'], how='left')
    per_scheme = component.groupby('industry').size().to_dict()
    checks = [dict(source='BigQuant', table='cn_stock_industry', check='snapshot_history', denominator=len(tree), affected=len(tree),
                   status='PIT_UNKNOWN', observation='no date or validity interval; historical tree cannot be inferred'),
              dict(source='BigQuant', table='cn_stock_industry_index_mapper', check='snapshot_history', denominator=len(mapper), affected=len(mapper),
                   status='PIT_UNKNOWN', observation='kind is a namespace; mapping has no historical validity'),
              dict(source='BigQuant', table='cn_stock_industry_component:2023-03', check='date_instrument_industry_multiplicity', denominator=len(counts),
                   affected=int(counts.gt(1).sum()), status='OBSERVED_KEY_CHECK', observation='industry namespace retained; version and date semantics UNKNOWN'),
              dict(source='BigQuant', table='cn_stock_industry_component:2023-03', check='unqualified_join_expansion', denominator=len(march),
                   affected=len(same_day)-len(march), status='CLASSIFICATION_NAMESPACE_REQUIRED', observation='extra output rows when industry is omitted; not proof of conflicting same-system membership'),
              dict(source='BigQuant', table='cn_stock_industry_change', check='change_history', denominator=len(change), affected=len(change),
                   status='PIT_UNKNOWN', observation='change_flag dictionary, initial state, effective-vs-publication semantics and vintage UNKNOWN'),
              dict(source='BigQuant', table='cn_stock_industry_component:2023-03', check='effective_intervals_overlap_gap', denominator=len(counts), affected=len(counts),
                   status='MEMBERSHIP_TIME_UNKNOWN', observation='dated observations lack explicit interval and revision contract; do not manufacture full historical intervals')]
    for scheme, g in component.groupby('industry'):
        sc = g.groupby(['date', 'instrument']).size()
        covered = march.merge(g[['date', 'instrument']].drop_duplicates(), on=['date', 'instrument'], how='inner')
        checks.append(dict(source='BigQuant', table='cn_stock_industry_component:2023-03', check='scheme:' + scheme,
                           denominator=len(march), affected=len(march)-len(covered), status='OBSERVED_KEY_GAPS',
                           observation=f'rows={len(g)}; multiple_same_system_keys={int(sc.gt(1).sum())}; version UNKNOWN'))
    component.to_parquet(private / 'industry-2023-03.parquet', index=False)
    write_csv(output / 'industry-history-checks.csv', checks)
    return dict(tree_rows=len(tree), mapper_rows=len(mapper), change_rows=len(change),
                change_flags={str(k): int(v) for k,v in change.change_flag.value_counts().items()},
                component_rows=len(component), component_keys=len(counts), duplicate_keys=int(counts.gt(1).sum()),
                component_scheme_rows=per_scheme, member_days=len(march), unqualified_extra_rows=len(same_day)-len(march))


def run(args):
    begin = time.perf_counter()
    output, private = args.output, args.private
    output.mkdir(parents=True, exist_ok=True)
    private.mkdir(parents=True, exist_ok=False)
    inputs = Inputs()
    with zipfile.ZipFile(args.membership) as archive:
        members = pd.read_csv(BytesIO(archive.read('members_daily.csv')), encoding='utf-8-sig')
        member_manifest = json.loads(archive.read('manifest.json'))
    members['symbol'] = members.stock_code.str.replace('.XSHG', '.SH', regex=False).str.replace('.XSHE', '.SZ', regex=False)
    members = members.loc[members.date.between(START, END), ['date', 'symbol']].reset_index(drop=True)
    duplicate_members = int(members.duplicated(['date', 'symbol']).sum())
    members.to_parquet(private / 'members.parquet', index=False)
    symbols, codes = sorted(members.symbol.unique()), sorted(members.symbol.str[:6].unique())
    coverage, conflicts, joins = [], [], []
    with zipfile.ZipFile(args.stock) as stock:
        basic = inputs.table(stock, 'stock/cn_stock_basic_info')
        names = inputs.table(stock, 'stock/cn_stock_name_change')
        basic.to_parquet(private / 'identity-basic.parquet', index=False)
        names.to_parquet(private / 'identity-names.parquet', index=False)
        co = inputs.workbook(next(args.csmar.glob('公司文件*.zip')))
        co.to_parquet(private / 'identity-company.parquet', index=False)
        overlap = interval_conflicts(names)
        conflicts.extend(dict(source='BigQuant:name_change', subject=s, issue='INCLUSIVE_INTERVAL_OVERLAP_CANDIDATE',
                              date=d, count=1, status='UNRESOLVED', evidence='end_date convention UNKNOWN; not automatically distinct issuer') for s,d in overlap)
        basic_codes = basic.assign(code=basic.instrument.str[:6]).groupby('code').instrument.nunique()
        for code in basic_codes[basic_codes.gt(1)].index:
            conflicts.append(dict(source='BigQuant:basic_info', subject=code, issue='BARE_CODE_MULTIPLE_SYMBOLS', count=int(basic_codes[code]), status='IDENTITY_UNKNOWN'))
        for old, new, relation in [('300114.SZ', '302132.SZ', 'CODE_CHANGE_NOT_RETROACTIVE_ALIAS'), ('600068.SH', '601868.SH', 'EXCHANGE_SUCCESSION_NOT_1_TO_1_ALIAS')]:
            conflicts.append(dict(source='PR80:identity-membership', subject=old+'->'+new, issue=relation,
                                  count=1, status='HISTORICAL_CONTRACT_SCOPE_ONLY', evidence='tri-source/semantics/identity-membership.csv'))
        identity_counts = members.symbol.isin(set(basic.instrument)).value_counts()
        delisted = basic.loc[basic.delist_date.notna(), ['instrument', 'delist_date']]
        life = members.merge(delisted.rename(columns={'instrument':'symbol'}), on='symbol', how='inner')
        life = life.loc[pd.to_datetime(life.date).ge(life.delist_date)]
        life.to_csv(private / 'membership-after-delist.csv', index=False)
        # Full member-key joins; raw lexemes stay in private monthly results.
        all_results = []
        native_names = {}
        for month, chunk in members.groupby(members.date.str[:7], sort=True):
            raw_member = next(n for n in stock.namelist() if n.startswith('stock/cn_stock_real_bar1d/'+month) and n.endswith('.parquet'))
            raw = inputs.parquet(stock, raw_member, ['date','instrument','name','open','high','low','close'])
            raw['date'] = raw.date.dt.strftime('%Y-%m-%d')
            raw = raw.rename(columns={'instrument':'symbol'})
            cs = native_month(args.native, 'CSMAR', month, codes, inputs)
            cn = native_month(args.native, 'CNRDS', month, codes, inputs)
            # Mapping uses all observed BQ symbols plus selected native codes; no prefix-inferred exchange.
            observed = pd.concat([raw[['symbol']], cs[['symbol']]]).dropna().drop_duplicates()
            observed['code'] = observed.symbol.str[:6]
            cardinality = observed.groupby('code').symbol.nunique()
            mapping = observed.loc[observed.code.isin(cardinality[cardinality.eq(1)].index)].set_index('code').symbol.to_dict()
            cn['symbol'] = cn.code.map(mapping)
            for code, group in cn.groupby('code'):
                native_names.setdefault(code, set()).update(group.name.dropna().unique())
            for source, prices in [('BigQuant', raw), ('CSMAR', cs), ('CNRDS', cn)]:
                result = price_join(chunk, prices)
                result['source'] = source
                result['identity_state'] = 'IDENTITY_UNKNOWN'
                result['membership_time_state'] = 'MEMBERSHIP_TIME_UNKNOWN'
                all_results.append(result)
                for state in PRICE_STATES:
                    coverage.append(dict(relation='membership_price', source=source, scope=month, status=state,
                                         denominator=len(chunk), count=int(result.price_state.eq(state).sum()), unit='member_days'))
                print(source, month, result.price_state.value_counts().to_dict(), flush=True)
        results = pd.concat(all_results, ignore_index=True)
        results.to_parquet(private / 'membership-price-joins.parquet', index=False)
        for (source, symbol, state), group in results.groupby(['source','symbol','price_state']):
            joins.append(dict(source=source, symbol=symbol, status=state, count=len(group), date_min=group.date.min(), date_max=group.date.max(),
                              identity_state='IDENTITY_UNKNOWN', membership_time_state='MEMBERSHIP_TIME_UNKNOWN',
                              date_range_kind='ENVELOPE_NOT_CONTIGUOUS'))
        # Publish exact contiguous missing-key/missing-OHLC ranges using research-session ordinals.
        ordinal = {day:i for i,day in enumerate(sorted(members.date.unique()))}
        for (source,symbol,state), group in results.loc[~results.price_state.eq('MATCHED')].groupby(['source','symbol','price_state']):
            group = group.sort_values('date').copy()
            group['run'] = group.date.map(ordinal).diff().ne(1).cumsum()
            for _, interval in group.groupby('run'):
                joins.append(dict(source=source,symbol=symbol,status=state,count=len(interval),date_min=interval.date.min(),date_max=interval.date.max(),
                                  identity_state='IDENTITY_UNKNOWN',membership_time_state='MEMBERSHIP_TIME_UNKNOWN',date_range_kind='EXACT_CONTIGUOUS_RESEARCH_SESSIONS'))
        # Name disagreement over time is a candidate, never proof of two issuers.
        for code, observed_names in sorted(native_names.items()):
            if len(observed_names)>1:
                conflicts.append(dict(source='CNRDS:member_code_month_projection',subject=code,issue='MULTIPLE_HISTORICAL_NAMES',
                                      count=len(observed_names),status='IDENTITY_UNKNOWN',evidence='full projected window; name change may explain'))
        financial = financial_checks(inputs.workbook(next(args.csmar.glob('银行类利润表*.zip'))), members, output, private)
    with zipfile.ZipFile(args.industry) as archive:
        industry = industry_checks(archive, inputs, members, output, private)
    baseline = json.loads((ROOT/'docs/validation/tri-source/market/summary.json').read_text(encoding='utf-8'))['metrics']['csi500']
    totals = {source:{state:int(g.price_state.eq(state).sum()) for state in PRICE_STATES} for source,g in results.groupby('source')}
    regression = dict(expected_no_price_key=292, actual_no_price_key=totals['BigQuant'].get('NO_PRICE_KEY',0),
                      expected_missing_ohlc=564, actual_missing_ohlc=totals['BigQuant'].get('MISSING_OHLC',0),
                      prior_no_price_key=baseline['raw_missing_keys'], prior_missing_ohlc=baseline['raw_OHLC_missing_member_days'])
    for dimension in ['IDENTITY_UNKNOWN','MEMBERSHIP_TIME_UNKNOWN']:
        coverage.append(dict(relation='membership_identity' if dimension=='IDENTITY_UNKNOWN' else 'membership_time',source='ALL',scope=START+'..'+END,
                             status=dimension,denominator=len(members),count=len(members),unit='member_days'))
    write_csv(output/'membership-joins.csv',joins)
    write_csv(output/'identity-conflicts.csv',conflicts)
    write_csv(output/'join-coverage.csv',coverage)
    for row in inputs.records:
        row.pop('dictionary',None)
    dump(private/'input-manifest.json',dict(inputs=inputs.records,membership_manifest=member_manifest))
    verification = dict(task='DQ-03',status='REAL_DATA_RELATION_CHECKS_COMPLETED',baseline=args.baseline,
                        gates=GATES,window=[START,END],members=dict(rows=len(members),sessions=members.date.nunique(),symbols=len(symbols),duplicate_keys=duplicate_members,
                        basic_snapshot_keys_matched=int(identity_counts.get(True,0)),basic_snapshot_keys_missing=int(identity_counts.get(False,0)),
                        at_or_after_snapshot_delist_rows=len(life)),identity=dict(basic_rows=len(basic),name_rows=len(names),company_rows=len(co),
                        name_overlap_candidates=len(overlap),stable_cross_source_contract='UNKNOWN'),price_joins=totals,regression=regression,
                        financial=financial,industry=industry,resources=dict(workers=1,arrow_threads=1,seconds=time.perf_counter()-begin,
                        peak_bytes=getattr(psutil.Process().memory_info(),'peak_wset',psutil.Process().memory_info().rss)),
                        public_input_identity=inputs.records,tests='PENDING',corporate_actions='PENDING',post_exit_holdings='PENDING')
    dump(output/'verification.json',verification)
    print(json.dumps({k:v for k,v in verification.items() if k!='public_input_identity'},ensure_ascii=False),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['native','stock','industry','membership','csmar','private','output']:
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--baseline',required=True)
    run(p.parse_args())


if __name__=='__main__':
    main()
