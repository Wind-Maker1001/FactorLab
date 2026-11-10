"""Read-only bounded evidence inspection. Invoke with Python -I -B -X utf8."""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

import pandas as pd

from factorlab.data.evidence import sha256
from factorlab.data.research_input import ResearchInputAdapter, SAFETY, quality
from factorlab.data.tdx import audit_day
from factorlab.errors import SchemaError

ROOT = Path(__file__).resolve().parents[2]
KEYS = ['date', 'symbol']
OUTCOMES = ['MATCHED', 'MISMATCH', 'SOURCE_MISSING', 'SEMANTICALLY_INCOMPARABLE', 'INSUFFICIENT_EVIDENCE']
MATRIX_FIELDS = ['object', 'category', 'source', 'dataset', 'symbol', 'security_id',
                 'observation_window', 'field', 'native_field', 'unit', 'availability',
                 'source_manifest', 'file_sha256', 'raw_or_adjusted', 'status_semantics',
                 'known_at_evidence', 'revision_evidence', 'coverage_status', 'limitation']


def pinned(path, expected):
    payload = Path(path).read_bytes()
    if sha256(payload) != expected:
        raise SchemaError('input identity drift')
    return payload


def pinned_repository_text(path, expected_lf_sha256):
    """Bind tracked text independently of Git's checkout EOL conversion."""
    payload = Path(path).read_bytes()
    text = payload.replace(b'\r\n', b'\n')
    if sha256(text) != expected_lf_sha256:
        raise SchemaError('repository text identity drift')
    return text


def unique(frame):
    if frame.duplicated(KEYS).any() or frame[KEYS].isna().any().any():
        raise SchemaError('invalid or duplicate observation keys')


def compare(left, right, field, left_semantics, right_semantics):
    """Compare only explicitly equal units, price basis and field meaning."""
    unique(left)
    unique(right)
    result = dict.fromkeys(OUTCOMES, 0)
    joined = left[KEYS].merge(right[KEYS], on=KEYS, how='outer', indicator=True)
    result['SOURCE_MISSING'] = int(joined['_merge'].ne('both').sum())
    overlap = int(joined['_merge'].eq('both').sum())
    if field not in left or field not in right:
        result['INSUFFICIENT_EVIDENCE'] = overlap
    elif any(left_semantics.get(k) is None or right_semantics.get(k) is None
             for k in ('unit', 'basis', 'meaning')):
        result['SEMANTICALLY_INCOMPARABLE'] = overlap
    elif left_semantics != right_semantics:
        result['SEMANTICALLY_INCOMPARABLE'] = overlap
    else:
        values = left[KEYS + [field]].merge(right[KEYS + [field]], on=KEYS, validate='one_to_one')
        missing = values[field + '_x'].isna() | values[field + '_y'].isna()
        equal = values[field + '_x'].eq(values[field + '_y'])
        result['INSUFFICIENT_EVIDENCE'] = int(missing.sum())
        result['MATCHED'] = int((~missing & equal).sum())
        result['MISMATCH'] = int((~missing & ~equal).sum())
    return result


def status_association(raw, state):
    """Associate observed codes and nulls without certifying their causal meaning."""
    if 'suspended' not in state:
        return dict(status='UNKNOWN', reason='suspended field unavailable')
    merged = raw.merge(state[KEYS + ['suspended']], on=KEYS, how='inner', validate='one_to_one')
    suspended = merged.suspended.eq(1)
    missing = merged[['raw_open', 'raw_high', 'raw_low', 'raw_close']].isna().any(axis=1)
    return dict(overlap=len(merged), vendor_suspended_code_1=int(suspended.sum()),
                code_1_and_missing_raw_ohlc=int((suspended & missing).sum()),
                code_1_and_csmar_nontrading_proxy=int((suspended & merged.trade_status.eq(0)).sum()),
                missing_raw_ohlc_and_code_not_1=int((missing & ~suspended).sum()),
                meaning='OBSERVATIONAL_ASSOCIATION_ONLY; HISTORICAL_STATUS_CERTIFICATION_UNKNOWN')


def event_versions(events):
    """Describe observed duplicates/revisions, never infer a complete revision chain."""
    keys = [(e['event_id'], e.get('revision')) for e in events]
    return dict(duplicate_versions=sum(n - 1 for n in Counter(keys).values() if n > 1),
                multiple_versions=sum(len({e.get('revision') for e in events if e['event_id'] == key}) > 1
                                      for key in {e['event_id'] for e in events}),
                revision_chain='UNKNOWN')


def return_status(receipt, evidence):
    """Reuse E01's saved holdings and accounting without recomputing a strategy."""
    if receipt['comparison_manifest_sha256'] != evidence['comparison_manifest_sha256']:
        raise SchemaError('wrong saved return source combination')
    versions = event_versions(evidence['events'])
    if versions['duplicate_versions'] or versions['multiple_versions']:
        raise SchemaError('ambiguous bounded event versions')
    results = {}
    for strategy, saved in receipt['strategies'].items():
        actual = {e['event_id']: e for e in saved['events']}
        if len(actual) != len(saved['events']) or set(actual) != {e['event_id'] for e in evidence['events']}:
            raise SchemaError('bounded event source mismatch')
        total = Decimal(0)
        for event in evidence['events']:
            held = actual[event['event_id']]
            if any(held[k] != event[k] for k in ('record_date', 'ex_date', 'scheduled_pay_date', 'cash_per_share')):
                raise SchemaError('bounded event terms mismatch')
            amount = Decimal(str(held['eligible_shares'])) * Decimal(event['cash_per_share'])
            if amount != Decimal(held['gross_entitlement']):
                raise SchemaError('saved entitlement mismatch')
            total += amount
        if total != Decimal(saved['bounded_gross_entitlement']):
            raise SchemaError('saved bounded total mismatch')
        results[strategy] = dict(
            raw_price_return_net_trading_costs=saved['raw_price_return_net_costs'],
            same_execution_path_explicit_costs=saved['trading_costs'],
            same_execution_path_price_pnl_before_explicit_costs=saved['raw_price_pnl_before_explicit_costs'],
            cost_return_effect=saved['trading_cost_return_effect'],
            bounded_gross_dividend_entitlement=str(total), record_close_holdings=saved['events'],
            actual_received_cash=None, tax_amount=None, annual_pretax_total_return=None,
            annual_aftertax_total_return=None)
    return dict(definition='RAW_PRICE_RETURN_NET_OF_TRADING_COSTS; NOT_TOTAL_RETURN',
                accounting_source='PINNED_P6_E01_SAVED_RECEIPT',
                coverage_status=evidence['coverage_status'], known_at_evidence='UNKNOWN',
                revision_evidence='UNKNOWN', account_tax_contract='UNKNOWN',
                account_receipt_contract='UNKNOWN', blockers=evidence['blockers'], strategies=results)


def archive_table(path, table, spec, symbols, start, end):
    """Project only pinned members; bind payload, receipt, schema and table identity."""
    pieces, bindings, missing_fields = [], [], set()
    with zipfile.ZipFile(path) as archive:
        schema = archive.read(table + '/schema.json')
        if sha256(schema) != spec['schema_sha256']:
            raise SchemaError('archive schema identity drift')
        for member in spec['members']:
            name = member['member']
            if not name.startswith(table + '/') or not name.endswith('.parquet') or '..' in Path(name).parts:
                raise SchemaError('wrong source table combination')
            lo, hi = Path(name).stem.split('_')
            if lo > end or hi < start:
                continue
            receipt_bytes = archive.read(name + '.receipt.json')
            if sha256(receipt_bytes) != member['receipt_sha256']:
                raise SchemaError('archive receipt identity drift')
            receipt = json.loads(receipt_bytes)
            if (receipt['table'] != table.split('/')[-1] or receipt['category'] != table.split('/')[0]
                    or receipt['sha256'] != member['sha256']):
                raise SchemaError('wrong source receipt combination')
            payload = archive.read(name)
            if sha256(payload) != member['sha256']:
                raise SchemaError('archive payload identity drift')
            # pandas projects available fields after the schema is read, so missing fields remain reportable.
            import pyarrow.parquet as pq
            schema_names = pq.read_schema(BytesIO(payload)).names
            required_keys = ['date', 'instrument'] if symbols is not None else ['date', 'market_code']
            if not set(required_keys).issubset(schema_names):
                raise SchemaError('source key fields missing')
            missing_fields.update(set(spec['columns']) - set(schema_names))
            columns = [c for c in spec['columns'] if c in schema_names]
            frame = pd.read_parquet(BytesIO(payload), columns=columns)
            frame['date'] = pd.to_datetime(frame.date)
            frame = frame.loc[frame.date.between(start, end)]
            if symbols is not None:
                frame = frame.loc[frame.instrument.isin(symbols)].rename(columns={'instrument': 'symbol'})
            else:
                frame = frame.loc[frame.market_code.eq('CN')]
            pieces.append(frame)
            bindings.append(dict(member=name, sha256=member['sha256'], receipt_sha256=member['receipt_sha256'],
                                 exported_at_utc=receipt['exported_at_utc']))
    frame = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(columns=spec['columns'])
    if symbols is not None:
        frame = frame.rename(columns={'instrument': 'symbol'})
    if symbols is not None and len(frame):
        unique(frame)
    elif symbols is None and frame.date.duplicated().any():
        raise SchemaError('duplicate supplier calendar dates')
    return frame, dict(schema_sha256=spec['schema_sha256'], members=bindings,
                       missing_fields=sorted(missing_fields), historical_known_at=None, revision_chain=None)


def row(object_id, category, source, dataset, symbol, window, field, **kwargs):
    value = dict.fromkeys(MATRIX_FIELDS, None)
    value.update(object=object_id, category=category, source=source, dataset=dataset, symbol=symbol,
                 observation_window=window, field=field, availability='READABLE',
                 known_at_evidence='UNKNOWN', revision_evidence='UNKNOWN', coverage_status='UNKNOWN',
                 limitation='Historical availability and revision chain unverified')
    value.update(kwargs)
    return value


def capability(raw_ready, identity_bound, event_terms=False):
    """Task-specific conjunction, not a score or a financial admission grant."""
    level = 0 if not identity_bound else (3 if raw_ready and event_terms else (2 if raw_ready else 1))
    return dict(level=level, formal_admission=False, historical_pit=False, hfq_verified=False,
                level4='NOT_GRANTED_BY_THIS_TOOL', safety=SAFETY)


def assess(config, assets, selected):
    if set(selected) - set(config['objects']):
        raise SchemaError('unknown fixed research object')
    manifest = json.loads(pinned(Path(assets['research']) / 'manifest.json', config['research_manifest_sha256']))
    adapter = ResearchInputAdapter(assets['research'], assets['prices'], config['research_manifest_sha256'])
    if adapter.config['symbols'] != config['symbols']:
        raise SchemaError('fixed sample differs')
    receipt_bytes = pinned_repository_text(ROOT / config['returns']['receipt'], config['returns']['receipt_lf_sha256'])
    evidence_bytes = pinned_repository_text(ROOT / config['returns']['evidence'], config['returns']['evidence_lf_sha256'])
    receipt, evidence = json.loads(receipt_bytes), json.loads(evidence_bytes)
    if receipt['evidence_sha256'] != sha256(evidence_bytes):
        raise SchemaError('saved entitlement evidence identity drift')
    matrix, comparisons, objects, date_sets = [], [], {}, {}
    inputs = dict(research_manifest=config['research_manifest_sha256'],
                  source_manifest=manifest['source_manifest_sha256'], prices=manifest['bars_sha256'],
                  return_receipt=sha256(receipt_bytes), return_evidence=sha256(evidence_bytes))
    for object_id in selected:
        obj = config['objects'][object_id]
        start, end = obj['start'], obj['end']
        symbols = obj['symbols'] if 'symbols' in obj else config['symbols']
        window = start + '/' + end
        raw = adapter.bars.loc[adapter.bars.symbol.isin(symbols) & adapter.bars.date.between(start, end)]
        report = quality(raw, symbols, start, end)
        # Selected archive members are cached across the two objects by the caller's full fixed range.
        objects[object_id] = dict(quality=report, capabilities={s: capability(
            next(x for x in report['securities'] if x['symbol'] == s)['ready_rows'] > 0, True,
            object_id == 'petrochina_2018') for s in symbols})
        for symbol in symbols:
            part = raw.loc[raw.symbol.eq(symbol)]
            security = next(x for x in report['securities'] if x['symbol'] == symbol)
            for field, native in zip(['raw_open', 'raw_high', 'raw_low', 'raw_close', 'volume', 'trade_status', 'is_st'],
                                      ['Opnprc', 'Hiprc', 'Loprc', 'Clsprc', 'Dnshrtrd', 'volume>0 AND Filling=0', 'Trdsta mapping']):
                matrix.append(row(object_id, 'RAW_PRICE' if field.startswith('raw_') or field == 'volume' else 'STATUS',
                    'CSMAR', 'P6_D01/P5_R03_Dalyr', symbol, window, field, native_field=native,
                    unit='CNY/share' if field.startswith('raw_') else ('shares' if field == 'volume' else 'proxy_code'),
                    source_manifest=config['research_manifest_sha256'], file_sha256=manifest['bars_sha256'],
                    raw_or_adjusted='RAW' if field.startswith('raw_') else None,
                    status_semantics='volume>0 AND Filling=0; ST proxy, not independent historical qualification',
                    coverage_status='VERIFIED' if part[field].notna().all() else 'PARTIAL',
                    limitation='Observed rows only; execution restricted to existing qualified intervals'))
            for category, field in [('ADJUSTMENT', 'certified_adjustment'), ('CORPORATE_ACTION', 'all_category_coverage'),
                                    ('PIT', 'historical_membership_known_at'), ('SECURITY_IDENTITY', 'stable_historical_id'),
                                    ('CALENDAR', 'official_exchange_calendar')]:
                matrix.append(row(object_id, category, 'CSMAR', 'P6_D01', symbol, window, field,
                    source_manifest=config['research_manifest_sha256'], file_sha256=manifest['bars_sha256'],
                    raw_or_adjusted='ADJUSTMENT_UNVERIFIED' if category == 'ADJUSTMENT' else None,
                    limitation='Not established by current snapshot; not required for bounded raw numerical exploration'))
            matrix.append(row(object_id, 'SOURCE_BINDING', 'CSMAR', 'P6_D01', symbol, window, 'consumed_identity',
                              source_manifest=config['research_manifest_sha256'], file_sha256=manifest['bars_sha256'],
                              coverage_status='VERIFIED', limitation='Consumed bytes pinned; upstream ZIP/member identities inherited, not reread'))
    tables, binding = {}, {}
    selected_start = min(config['objects'][o]['start'] for o in selected)
    selected_end = max(config['objects'][o]['end'] for o in selected)
    selected_symbols = sorted({s for o in selected for s in config['objects'][o].get('symbols', config['symbols'])})
    for table, spec in config['bigquant_tables'].items():
        alias = 'common' if table.startswith('common/') else 'stock'
        if alias not in assets:
            binding[table] = dict(availability='NOT_AVAILABLE', limitation='Explicit archive argument not supplied')
            tables[table] = pd.DataFrame(columns=spec['columns']).rename(columns={'instrument': 'symbol'})
        else:
            tables[table], binding[table] = archive_table(assets[alias], table, spec,
                selected_symbols if alias == 'stock' else None, selected_start, selected_end)
    inputs['bigquant'] = binding
    for object_id in selected:
        obj = config['objects'][object_id]
        start, end = obj['start'], obj['end']
        symbols = obj.get('symbols', config['symbols'])
        window = start + '/' + end
        raw = adapter.bars.loc[adapter.bars.symbol.isin(symbols) & adapter.bars.date.between(start, end)]
        raw_table = 'stock/cn_stock_real_bar1d'
        state_table = 'stock/cn_stock_status'
        bq = tables[raw_table].rename(columns={c: 'raw_' + c for c in ('open', 'high', 'low', 'close')})
        bq = bq.loc[bq.symbol.isin(symbols) & bq.date.between(start, end)]
        state = tables[state_table]
        state = state.loc[state.symbol.isin(symbols) & state.date.between(start, end)]
        cal = tables['common/trading_days']
        cal = pd.DatetimeIndex(cal.loc[cal.date.between(start, end), 'date'])
        observed = pd.DatetimeIndex(sorted(raw.date.unique()))
        objects[object_id]['calendar'] = dict(observed=len(observed), supplier=len(cal),
            shared=len(observed.intersection(cal)), observed_only=len(observed.difference(cal)),
            supplier_only=len(cal.difference(observed)), official_completeness='UNKNOWN',
            limitation='Supplier export begins 2016-11-07; earlier source absence is not evidence of closed sessions')
        objects[object_id]['cross_source'] = dict(price_keys=len(bq), status_keys=len(state))
        objects[object_id]['key_gaps'] = {}
        for symbol in symbols:
            left, right = raw.loc[raw.symbol.eq(symbol)], bq.loc[bq.symbol.eq(symbol)]
            statuses = state.loc[state.symbol.eq(symbol)]
            left_dates = pd.DatetimeIndex(left.date)
            gaps = {
                'csmar_only_vs_bq_price': left_dates.difference(right.date),
                'bq_price_only_vs_csmar': pd.DatetimeIndex(right.date).difference(left_dates),
                'csmar_only_vs_bq_status': left_dates.difference(statuses.date),
                'bq_status_only_vs_csmar': pd.DatetimeIndex(statuses.date).difference(left_dates)}
            objects[object_id]['key_gaps'][symbol] = {}
            for kind, dates in gaps.items():
                dates = [str(d.date()) for d in dates]
                identity = sha256(json.dumps(dates).encode())
                date_sets[identity] = dates
                objects[object_id]['key_gaps'][symbol][kind] = dict(count=len(dates), date_set=identity)

            for field in ['raw_open', 'raw_high', 'raw_low', 'raw_close', 'volume']:
                sem = dict(unit='shares' if field == 'volume' else 'CNY/share', basis='RAW', meaning=field)
                outcomes = compare(left, right, field, sem, dict(unit=None, basis='RAW', meaning=field))
                comparisons.append(dict(object=object_id, symbol=symbol, left='CSMAR', right='BigQuant', field=field,
                                         **outcomes, reason='BigQuant economic unit/precision contract UNKNOWN; no value equality claim'))
                matrix.append(row(object_id, 'RAW_PRICE', 'BigQuant', raw_table, symbol, window, field,
                    native_field=field.removeprefix('raw_'), unit='UNKNOWN', raw_or_adjusted='RAW_TABLE_CANDIDATE',
                    availability='READABLE' if len(right) else 'NOT_AVAILABLE',
                    source_manifest='bigquant_tables:' + raw_table, file_sha256=json.dumps([m['sha256'] for m in binding[raw_table].get('members', [])]),
                    coverage_status='PARTIAL' if len(right) else 'NOT_AVAILABLE',
                    limitation='Date keys readable; economic unit contract unverified; no price fusion'))
            for field in ['suspended', 'st_status', 'is_risk_warning', 'price_limit_status', 'exdr']:
                count = int(statuses[field].notna().sum()) if field in statuses else 0
                matrix.append(row(object_id, 'STATUS', 'BigQuant', state_table, symbol, window, field,
                    native_field=field, unit='vendor_code_UNVERIFIED', status_semantics='Original codes retained; not mapped to certified eligibility',
                    availability='READABLE' if count else 'NOT_AVAILABLE', source_manifest='bigquant_tables:' + state_table,
                    file_sha256=json.dumps([m['sha256'] for m in binding[state_table].get('members', [])]),
                    coverage_status='PARTIAL' if count else 'NOT_AVAILABLE',
                    limitation=f'{count}/{len(left)} nonnull observations; original code semantics/PIT unverified'))
            # Counts compare existence of the same symbol/date key, not price or status meaning.
            key_compare = compare(left.assign(present=1), statuses.assign(present=1), 'present',
                                  dict(unit='key', basis='OBSERVATION', meaning='key_presence'),
                                  dict(unit='key', basis='OBSERVATION', meaning='key_presence'))
            comparisons.append(dict(object=object_id, symbol=symbol, left='CSMAR', right='BigQuant',
                                    field='status_key_presence', **key_compare, reason='Only literal symbol/date existence'))
            matrix.append(row(object_id, 'CALENDAR', 'BigQuant', 'common/trading_days', symbol, window, 'CN_sessions',
                source_manifest='bigquant_tables:common/trading_days', file_sha256=json.dumps([m['sha256'] for m in binding['common/trading_days'].get('members', [])]),
                coverage_status='PARTIAL', limitation='Independent table from same supplier, not official exchange calendar'))
        objects[object_id]['status_price_association'] = status_association(raw, state)
        objects[object_id]['status_observations'] = {
            s: {f: {str(k): int(v) for k, v in state.loc[state.symbol.eq(s), f].value_counts(dropna=False).items()}
                for f in ['suspended', 'st_status', 'is_risk_warning', 'price_limit_status', 'exdr'] if f in state}
            for s in symbols}
    ancillary = inspect_ancillary(config, assets, selected, matrix)
    returns = return_status(receipt, evidence) if 'petrochina_2018' in selected else None
    if returns is not None:
        for event in evidence['events']:
            matrix.append(row('petrochina_2018', 'CORPORATE_ACTION', 'ISSUER/P6_E01', 'verified_bounded_cash_events',
                '601857.SH', '2018-01-01/2018-12-31', event['event_id'],
                native_field='cash_per_share;record_date;ex_date;scheduled_pay_date', unit='CNY/old_share',
                source_manifest=config['returns']['receipt_lf_sha256'], file_sha256=event['source_sha256'],
                coverage_status='VERIFIED', limitation='Inherited verified event terms; not annual completeness, receipt or tax evidence'))
    new_evidence = any(objects[o]['cross_source']['status_keys'] > 0 for o in selected if o == 'fixed24_2015_2018')
    return dict(schema='P8_03_DATA_EVIDENCE_V1',
                status='P8_03_DATA_COVERAGE_ASSESSED' if new_evidence else 'P8_03_PARTIAL',
                safety=SAFETY, formal_admission=False, objects=objects, coverage=matrix, comparisons=comparisons,
                returns=returns, inputs=inputs, ancillary=ancillary, missing_date_sets=date_sets)


def inspect_ancillary(config, assets, selected, matrix):
    summary = {}
    for key in ('tdx', 'cnrds'):
        spec = config['ancillary'][key]
        if key not in assets:
            summary[key] = dict(availability='NOT_AVAILABLE', limitation='Explicit file argument not supplied')
        else:
            if key == 'tdx':
                payload = pinned(assets[key], spec['sha256'])
                path = Path(assets[key])
                if path.name.lower() != 'sh601857.day':
                    raise SchemaError('wrong TDX security source')
                audit = audit_day(path, 'sh', path.parent, '2018-01-01', '2018-12-31', source_bytes=payload)
                dates = audit.dates
                count = int(((dates >= '2018-01-01') & (dates <= '2018-12-31')).sum()) if dates is not None else 0
                summary[key] = dict(availability='READABLE', sha256=spec['sha256'], rows_2018=count,
                                    layout_status=audit.inventory['audit_status'], unit='UNKNOWN', raw_or_adjusted='UNKNOWN')
            else:
                with zipfile.ZipFile(assets[key]) as archive:
                    members = [dict(member=i.filename, bytes=i.file_size, crc=i.CRC) for i in archive.infolist()]
                    directory_hash = sha256(json.dumps(members, sort_keys=True).encode())
                    if directory_hash != spec['directory_sha256']:
                        raise SchemaError('CNRDS archive directory identity drift')
                    dictionary = archive.read(spec['dictionary_member'])
                    if sha256(dictionary) != spec['dictionary_sha256']:
                        raise SchemaError('CNRDS dictionary identity drift')
                    summary[key] = dict(availability='READABLE_DIRECTORY_AND_DICTIONARY', directory_sha256=directory_hash,
                        dictionary_sha256=spec['dictionary_sha256'], dictionary_member=spec['dictionary_member'],
                        dictionary_fields=dictionary.decode('utf-8-sig').splitlines(),
                        members=members, target_rows=None,
                        limitation='Directory is not payload SHA256; 2018 dictionary units readable, target rows not scanned')
        for object_id in selected:
            obj = config['objects'][object_id]
            for symbol in obj.get('symbols', config['symbols']):
                matrix.append(row(object_id, 'RAW_PRICE', key.upper(), spec['dataset'], symbol,
                    obj['start'] + '/' + obj['end'], 'raw_price_candidate',
                    availability=('UNKNOWN' if key == 'tdx' and symbol != '601857.SH' else summary[key]['availability']),
                    file_sha256=spec.get('sha256') if key in assets and (key != 'tdx' or symbol == '601857.SH') else None,
                    coverage_status=('NOT_AVAILABLE' if key not in assets or
                        (key == 'tdx' and symbol == '601857.SH' and summary[key]['rows_2018'] == 0)
                        else 'UNKNOWN'),
                    limitation='TDX read limited to 601857; other symbols inventory-only' if key == 'tdx' and symbol != '601857.SH'
                    else ('No target 2018 observations in readable DAY file' if key == 'tdx' and key in assets and summary[key]['rows_2018'] == 0
                          else 'Target rows/adjustment meaning unverified; dictionary existence does not establish data coverage')))
                if key == 'cnrds' and key in assets:
                    matrix.append(row(object_id, 'SOURCE_BINDING', 'CNRDS', spec['dataset'], symbol,
                        obj['start'] + '/' + obj['end'], '2018_field_dictionary',
                        native_field='Dopnprc/Dhiprc/Dloprc/Dclsprc/Dtrdvol', unit='documented: CNY/share; shares',
                        file_sha256=spec['dictionary_sha256'], source_manifest='ancillary.cnrds.directory_sha256',
                        coverage_status='VERIFIED', limitation='Only 2018 field dictionary read; not target rows or historical first availability'))
    for object_id in selected:
        obj = config['objects'][object_id]
        for source in ('HBN', 'EXCHANGE', 'JY'):
            matrix.append(row(object_id, 'SOURCE_BINDING', source, 'existing_evidence_only', None,
                obj['start'] + '/' + obj['end'], 'target_asset', availability='UNKNOWN',
                limitation='No separately bound readable target asset supplied; does not assert asset absence'))
    return summary


def write_outputs(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    summary = {k: v for k, v in report.items() if k not in ('coverage', 'comparisons', 'returns', 'inputs')}
    summary['artifacts'] = dict(coverage='coverage.csv', comparisons='comparisons.csv',
                               returns='return-status.json', inputs='input-identities.json', capabilities='capabilities.csv')
    for name, value in [('summary.json', summary), ('return-status.json', report['returns']),
                        ('input-identities.json', report['inputs'])]:
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    pd.DataFrame(report['coverage'], columns=MATRIX_FIELDS).to_csv(output / 'coverage.csv', index=False)
    pd.DataFrame(report['comparisons']).to_csv(output / 'comparisons.csv', index=False)
    pd.DataFrame([dict(object=o, symbol=s, **{k: v for k, v in c.items() if k != 'safety'})
                  for o, obj in report['objects'].items() for s, c in obj['capabilities'].items()]).to_csv(
                      output / 'capabilities.csv', index=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--research-root', type=Path, required=True)
    parser.add_argument('--price-root', type=Path, required=True)
    for arg in ('stock', 'common', 'tdx', 'cnrds'):
        parser.add_argument('--' + arg, type=Path)
    parser.add_argument('--object', action='append', choices=['petrochina_2018', 'fixed24_2015_2018'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_bytes())
    assets = dict(research=args.research_root, prices=args.price_root,
                  **{k: getattr(args, k) for k in ('stock', 'common', 'tdx', 'cnrds') if getattr(args, k) is not None})
    inaccessible = {}
    for key in ('stock', 'common', 'tdx', 'cnrds'):
        if key in assets:
            try:
                with assets[key].open('rb'):
                    pass
            except (FileNotFoundError, PermissionError) as exc:
                inaccessible[key] = type(exc).__name__
                del assets[key]
    try:
        report = assess(config, assets, args.object or list(config['objects']))
    except (FileNotFoundError, PermissionError) as exc:
        report = dict(schema='P8_03_DATA_EVIDENCE_V1', status='P8_03_BLOCKED_DATA',
                      safety=SAFETY, formal_admission=False, objects={}, coverage=[], comparisons=[],
                      returns=None, inputs={}, data_access_error=type(exc).__name__)
    report['inaccessible_inputs'] = inaccessible
    report['inputs']['config_sha256'] = sha256(args.config.read_bytes())
    report['inputs']['implementation_sha256'] = sha256(Path(__file__).read_bytes())
    write_outputs(report, args.output)
    print(report['status'])


if __name__ == '__main__':
    main()
