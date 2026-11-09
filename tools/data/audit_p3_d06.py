"""Aggregate-only retrospective CSI500 diagnostics; never publish vendor rows.

Run with the existing environment: python -I -B -X utf8 tools/data/audit_p3_d06.py.
Price-basis mappings are supplier-table candidates, not HFQ/PIT certification.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import sys
import time
import zipfile

import numpy as np
import pandas as pd
import psutil
import pyarrow as pa
import pyarrow.parquet as pq

# Isolated Python ignores PYTHONPATH; import only this checkout's trusted source.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from factorlab.factors.candidates import register_candidates
from factorlab.factors.registry_v2 import FactorRegistryV2
from factorlab.universe.filters import build_filter_flags, UniverseFilterConfig
from factorlab.validation.splitter import ExpandingWalkForwardSplitter

KEYS = ['date', 'symbol']
OHLC = ['open', 'high', 'low', 'close']
STATUS = ['st_status', 'is_risk_warning', 'suspended', 'price_limit_status', 'exdr']
LIMITS = {'money': 'NO LIVE MONEY', 'pit': 'PIT_UNVERIFIED',
          'hfq': 'HFQ_UNVERIFIED', 'formal': 'FORMAL_ADMISSION_BLOCKED'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def normalize(codes):
    return codes.str.replace('.XSHG', '.SH', regex=False).str.replace('.XSHE', '.SZ', regex=False)


def join_members(members, source):
    return members.merge(source, on=KEYS, how='left', validate='one_to_one', indicator=True)


def field_profile(joined, fields, source):
    present = joined['_merge'].eq('both')
    rows = []
    for field in fields:
        values = joined[field]
        finite = np.isfinite(values.to_numpy(dtype=float, na_value=np.nan))
        rows.append({'source': source, 'field': field, 'member_days': len(joined),
                     'missing_keys': int((~present).sum()),
                     'null_on_present_keys': int((present & values.isna()).sum()),
                     'finite_member_days': int(finite.sum()),
                     'unavailable_member_days': int((~finite).sum()),
                     'unavailable_rate': float((~finite).mean()),
                     'infinite': int(np.isinf(values.to_numpy(dtype=float, na_value=np.nan)).sum()),
                     'duplicate_policy': 'REJECT_NO_FUSION', 'historical_available_at': 'UNKNOWN'})
    return rows


class Archive:
    def __init__(self, root, category, manifest):
        self.category = category
        self.zip = zipfile.ZipFile(root / (category + '.zip'))
        self.names = self.zip.namelist()
        self.manifest = manifest
        self.physical_rows = 0
        self.bytes = 0
        self.parts = 0

    def metadata(self):
        tables = {}
        for name in self.names:
            if not name.endswith('/schema.json'):
                continue
            table = name.split('/')[1]
            schema = self.zip.read(name)
            prefix = f'{self.category}/{table}/'
            receipts = [json.loads(self.zip.read(n)) for n in self.names
                        if n.startswith(prefix) and n.endswith('.receipt.json')]
            tables[table] = {'fields': json.loads(schema)['fields'], 'schema_sha256': digest(schema),
                             'partitions': len(receipts),
                             'receipt_rows_claim': sum(r['rows'] for r in receipts),
                             'query_start_claim': min((r['window_start'] for r in receipts if r.get('window_start')), default=None),
                             'query_end_claim': max((r['window_end'] for r in receipts if r.get('window_end')), default=None),
                             'evidence_level': 'SCHEMA_AND_RECEIPTS_NOT_ROW_COVERAGE'}
        return tables

    def read(self, table, columns=None, start=None, end=None, symbols=None):
        prefix = f'{self.category}/{table}/'
        frames = []
        for name in self.names:
            if not name.startswith(prefix) or not name.endswith('.parquet'):
                continue
            receipt_name = name + '.receipt.json'
            receipt = json.loads(self.zip.read(receipt_name))
            lo, hi = receipt.get('window_start'), receipt.get('window_end')
            if lo and start is not None and pd.Timestamp(hi) < start:
                continue
            if hi and end is not None and pd.Timestamp(lo) > end:
                continue
            raw = self.zip.read(name)
            actual_hash = digest(raw)
            data = pq.read_table(pa.BufferReader(raw), columns=columns)
            self.manifest.append({'archive': self.category + '.zip', 'member': name,
                                  'actual_sha256': actual_hash, 'receipt_sha256': receipt['sha256'],
                                  'hash_matches_receipt': actual_hash == receipt['sha256'],
                                  'exported_at_utc': receipt['exported_at_utc'],
                                  'physical_rows': data.num_rows, 'uncompressed_bytes': len(raw),
                                  'selected_columns': columns})
            self.physical_rows += data.num_rows
            self.bytes += len(raw)
            self.parts += 1
            frame = data.to_pandas().rename(columns={'instrument': 'symbol'})
            if 'date' in frame:
                frame['date'] = pd.to_datetime(frame['date'])
                if start is not None:
                    frame = frame.loc[frame.date.ge(start)]
                if end is not None:
                    frame = frame.loc[frame.date.le(end)]
            if symbols is not None:
                frame = frame.loc[frame.symbol.isin(symbols)]
            frames.append(frame)
        return pd.concat(frames, ignore_index=True)


def label_availability(bars, members, calendar, benchmark_dates, holding_days=5):
    """Check next-open endpoints only, without calculating any return."""
    calendar = pd.DatetimeIndex(calendar)
    endpoints = pd.DataFrame({'date': calendar,
                              'entry_date': pd.Series(calendar).shift(-1).to_numpy(),
                              'exit_date': pd.Series(calendar).shift(-(holding_days + 1)).to_numpy()})
    result = members.merge(endpoints, on='date', validate='many_to_one')
    positive = bars.loc[np.isfinite(bars.adj_open) & bars.adj_open.gt(0), KEYS]
    for stage in ['entry', 'exit']:
        key = stage + '_date'
        observed = positive.rename(columns={'date': key}).assign(**{stage + '_open_available': True})
        result = result.merge(observed, on=[key, 'symbol'], how='left', validate='many_to_one')
    result['label_endpoints_available'] = (
        result.entry_open_available.eq(True) & result.exit_open_available.eq(True)
        & result.entry_date.isin(benchmark_dates) & result.exit_date.isin(benchmark_dates))
    return result.drop(columns=['entry_open_available', 'exit_open_available'])


def window_profiles(frame, calendar):
    rows = []
    for name, train_months in [('minimum_12_3_3', 12), ('expanded_24_3_3', 24)]:
        splitter = ExpandingWalkForwardSplitter(initial_train_months=train_months)
        for fold in splitter.split(frame, calendar=calendar):
            for stage, indices, dates, window in [
                ('Train', fold.train_indices, fold.train_dates, fold.train_window),
                ('Validation', fold.validation_indices, fold.validation_dates, fold.validation_window),
                ('Test', fold.test_indices, fold.test_dates, fold.test_window),
            ]:
                subset = frame.iloc[indices]
                nominal = frame.loc[frame.date.ge(window[0]) & frame.date.lt(window[1])]
                before_gaps = int(len(nominal))
                row = {'candidate': name, 'fold': fold.fold, 'stage': stage,
                       'window_start': str(window[0].date()), 'window_end_exclusive': str(window[1].date()),
                       'nominal_member_days': before_gaps, 'after_split_member_days': len(subset),
                       'removed_by_gaps_or_label_dates': before_gaps - len(subset),
                       'signal_dates': len(dates), 'securities': int(subset.symbol.nunique()),
                       'purge_days_config': splitter.purge_days, 'embargo_days_config': splitter.embargo_days,
                       'purged_dates_in_fold': len(fold.purged_dates),
                       'embargoed_dates_in_fold': len(fold.embargoed_dates),
                       'train_overlap_removed_fold': fold.audit.train_overlap_rows_removed,
                       'validation_overlap_removed_fold': fold.audit.validation_overlap_rows_removed}
                for group in ['all14', 'all64', 'all78']:
                    row[group + '_finite'] = int(subset[group].sum())
                    row[group + '_with_label_endpoints'] = int((subset[group] & subset.label_endpoints_available).sum())
                    eligible = subset[group] & subset.label_endpoints_available & subset.structural_eligible
                    row[group + '_structurally_eligible'] = int(eligible.sum())
                    daily = eligible.groupby(subset.date).sum()
                    row[group + '_daily_min'] = int(daily.min())
                    row[group + '_days_below_20'] = int(daily.lt(20).sum())
                row['all78_eligible_coverage'] = row['all78_structurally_eligible'] / len(subset)
                row['label_endpoints_missing'] = int((~subset.label_endpoints_available).sum())
                row['structural_ineligible'] = int((~subset.structural_eligible).sum())
                row['financial_admitted_days'] = 0
                rows.append(row)
    return rows


def market_quality(bars, members):
    """Quarantine securities rejected by the existing numeric market contract.

    No price is replaced. Whole-security exclusion is deliberately coarser than
    formula-specific contamination; reported coverage is the executable subset,
    not a claim that every excluded factor/date is intrinsically impossible.
    """
    reasons = {}
    for prefix in ['raw_', 'adj_']:
        block = bars[[prefix + f for f in OHLC]]
        o, h, l, c = (block[prefix + f] for f in OHLC)
        reasons[prefix + 'ohlc_invalid'] = ((block <= 0).any(axis=1) | (h < o) | (h < c)
                                             | (l > o) | (l > c) | (h < l))
    for name in ['volume', 'amount', 'turnover']:
        reasons[name + '_negative'] = bars[name].lt(0)
    numeric = bars.drop(columns=KEYS)
    reasons['infinite_input'] = pd.Series(np.isinf(numeric.to_numpy(dtype=float)).any(axis=1), index=bars.index)
    masks = pd.DataFrame(reasons)
    bad = masks.any(axis=1)
    symbols = sorted(bars.loc[bad, 'symbol'].unique())
    rows = members.symbol.isin(symbols)
    summary = {'invalid_input_rows': int(bad.sum()), 'quarantined_securities': len(symbols),
               'quarantined_member_days': int(rows.sum()),
               'invalid_rows_within_research_dates': int((bad & bars.date.isin(members.date)).sum()),
               'invalid_rows_outside_research_dates': int((bad & ~bars.date.isin(members.date)).sum()),
               'by_reason': {name: int(value.sum()) for name, value in reasons.items()},
               'quarantined_codes': symbols,
               'policy': 'WHOLE_SECURITY_COMPUTATION_BLOCKED; originals unchanged; coverage is a lower bound'}
    return symbols, summary


def factor_diagnostics(bars, members, calendar, batch_symbols=32):
    registry = FactorRegistryV2.with_builtins()
    register_candidates(registry)
    snapshot = registry.freeze()
    definitions = snapshot.definitions
    groups = {d.factor_id: ('candidates64' if d.kind == 'expression' else 'original14') for d in definitions}
    records = {d.factor_id: {**asdict(d), 'group': groups[d.factor_id], 'finite': 0,
                             'nan': 0, 'infinite': 0, 'constant_securities': 0,
                             'low_variation_securities': 0} for d in definitions}
    daily = {d.factor_id: pd.DataFrame({'n': 0., 'min': np.inf, 'max': -np.inf},
                                       index=members.date.drop_duplicates().sort_values()) for d in definitions}
    outputs = []
    quarantined, quality = market_quality(bars, members)
    executable_bars = bars.loc[~bars.symbol.isin(quarantined)]
    symbols = sorted(members.symbol.unique())
    for offset in range(0, len(symbols), batch_symbols):
        batch = symbols[offset:offset + batch_symbols]
        source = executable_bars.loc[executable_bars.symbol.isin(batch)]
        target = members.loc[members.symbol.isin(batch)]
        common = target.copy()
        common['all14'] = True
        common['all64'] = True
        for begin in range(0, len(definitions), 16):
            selected = definitions[begin:begin + 16]
            if source.empty:
                joined = target.assign(**{d.factor_id: np.nan for d in selected})
            else:
                computed = snapshot.compute(source, [d.ref for d in selected], calendar=calendar)
                joined = target.merge(computed, on=KEYS, how='left', validate='one_to_one')
            for d in selected:
                name = d.factor_id
                values = joined[name]
                array = values.to_numpy(dtype=float)
                finite = np.isfinite(array)
                record = records[name]
                record['finite'] += int(finite.sum())
                record['nan'] += int(np.isnan(array).sum())
                record['infinite'] += int(np.isinf(array).sum())
                observed = joined.loc[finite, ['date', 'symbol', name]]
                temporal = observed.groupby('symbol')[name].agg(['count', 'min', 'max', 'std'])
                record['constant_securities'] += int(((temporal['count'] >= 2) & temporal['min'].eq(temporal['max'])).sum())
                record['low_variation_securities'] += int(((temporal['count'] >= 2) & temporal['std'].le(1e-12)).sum())
                stats = observed.groupby('date')[name].agg(['count', 'min', 'max'])
                accumulated = daily[name]
                accumulated.loc[stats.index, 'n'] += stats['count']
                accumulated.loc[stats.index, 'min'] = np.minimum(accumulated.loc[stats.index, 'min'], stats['min'])
                accumulated.loc[stats.index, 'max'] = np.maximum(accumulated.loc[stats.index, 'max'], stats['max'])
                common.loc[:, 'all64' if d.kind == 'expression' else 'all14'] &= finite
        common['all78'] = common.all14 & common.all64
        outputs.append(common)
        print(f'factor securities {min(offset + batch_symbols, len(symbols))}/{len(symbols)}', flush=True)
    rows = []
    for d in definitions:
        row = records[d.factor_id]
        stats = daily[d.factor_id]
        spread = stats['max'] - stats['min']
        row['daily_constant_n_ge_20'] = int(((stats.n >= 20) & spread.eq(0)).sum())
        row['daily_low_range_n_ge_20'] = int(((stats.n >= 20) & spread.le(1e-12)).sum())
        row['dates_fewer_than_20_finite'] = int(stats.n.lt(20).sum())
        row['member_days'] = len(members)
        row['finite_coverage'] = row['finite'] / len(members)
        row['calculation_status'] = 'ACTUAL_REGISTRY_V2_NONQUARANTINED_SUBSET'
        row['source_quarantined_member_days'] = quality['quarantined_member_days']
        row['source_quarantined_securities'] = quality['quarantined_securities']
        row['price_semantics'] = 'SUPPLIER_ADJUSTED_TABLE_CANDIDATE_HFQ_UNVERIFIED'
        row['financial_admitted_days'] = 0
        for field in ['input_fields', 'price_basis', 'dependencies']:
            row[field] = json.dumps(row[field], ensure_ascii=False)
        rows.append(row)
    return pd.DataFrame(rows), pd.concat(outputs).sort_values(KEYS).reset_index(drop=True), snapshot.snapshot_hash, quality


def run(args):
    started = time.perf_counter()
    process = psutil.Process()
    manifest = []
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    # Six calendar months exceed the catalog's maximum prior-session lookback
    # and the default 120-session listing-age threshold for the chosen window.
    warm_start = start - pd.DateOffset(months=8)
    tail_end = end + pd.DateOffset(months=1)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    metadata = {}
    archives = {}
    for category in ['stock', 'common', 'index', 'industry', 'factor']:
        archives[category] = Archive(Path(args.bigquant_root), category, manifest)
        metadata[category] = archives[category].metadata()
    stock, common = archives['stock'], archives['common']
    with zipfile.ZipFile(args.membership_zip) as archive:
        raw = archive.read('members_daily.csv')
        manifest.append({'archive': 'csi500_full_history.zip', 'member': 'members_daily.csv',
                         'actual_sha256': digest(raw), 'uncompressed_bytes': len(raw)})
        members = pd.read_csv(pa.BufferReader(raw), encoding='utf-8-sig')
        members['date'] = pd.to_datetime(members.date)
        original_range = [str(members.date.min().date()), str(members.date.max().date())]
        members = members.loc[members.date.between(start, end)].copy()
        members['symbol'] = normalize(members.stock_code)
        members = members[KEYS].sort_values(KEYS).reset_index(drop=True)
        membership_manifest = archive.read('manifest.json')
        manifest.append({'archive': 'csi500_full_history.zip', 'member': 'manifest.json',
                         'actual_sha256': digest(membership_manifest)})
    dates = common.read('trading_days', ['date', 'market_code'], warm_start, tail_end)
    calendar = pd.DatetimeIndex(dates.loc[dates.market_code.eq('CN'), 'date'].drop_duplicates().sort_values())
    calendar_missing = len(pd.Index(members.date.unique()).difference(calendar))
    research_calendar = calendar[(calendar >= start) & (calendar <= end)]
    symbols = set(members.symbol)
    print('reading projected raw, adjusted and status partitions', flush=True)
    raw = stock.read('cn_stock_real_bar1d', ['date', 'instrument', *OHLC, 'volume', 'amount', 'turn', 'pre_close', 'adjust_factor'], warm_start, tail_end, symbols)
    adjusted = stock.read('cn_stock_bar1d', ['date', 'instrument', *OHLC], warm_start, tail_end, symbols)
    status = stock.read('cn_stock_status', ['date', 'instrument', *STATUS], warm_start, tail_end, symbols)
    listings = stock.read('cn_stock_basic_info', ['instrument', 'list_date', 'delist_date', 'industry'], symbols=symbols)
    benchmark = archives['index'].read('cn_stock_index_bar1d', ['date', 'instrument', 'open'], warm_start, tail_end, {'000905.SH'})
    joins = {'raw': join_members(members, raw), 'adjusted': join_members(members, adjusted),
             'status': join_members(members, status)}
    field_rows = (field_profile(joins['raw'], [*OHLC, 'volume', 'amount', 'turn', 'pre_close', 'adjust_factor'], 'cn_stock_real_bar1d')
                  + field_profile(joins['adjusted'], OHLC, 'cn_stock_bar1d')
                  + field_profile(joins['status'], STATUS, 'cn_stock_status'))
    pd.DataFrame(field_rows).to_csv(out / 'assets-fields.csv', index=False)
    adjusted = adjusted.rename(columns={f: 'adj_' + f for f in OHLC})
    bars = raw.rename(columns={f: 'raw_' + f for f in OHLC}).rename(columns={'turn': 'turnover'}).merge(
        adjusted, on=KEYS, how='outer', validate='one_to_one')
    # Do not synthesize a complete input for codes with no observations.
    # Registry emits only observed keys; the member left-join retains their loss.
    factors, finite, snapshot_hash, quality = factor_diagnostics(bars, members, calendar, args.symbols_per_batch)
    factors.to_csv(out / 'factors.csv', index=False)
    labels = label_availability(bars, members, calendar, benchmark.loc[benchmark.open.gt(0), 'date'])
    flag_bars = raw[KEYS + OHLC + ['volume']]
    mapped_status = status.assign(trade_status=status.suspended.eq(0).astype(int),
                                  is_st=status.st_status.ne(0))[KEYS + ['trade_status', 'is_st']]
    flags = build_filter_flags(flag_bars, mapped_status,
                              listings.rename(columns={'list_date': 'listing_date'}), calendar, UniverseFilterConfig())
    member_flags = members.merge(flags, on=KEYS, how='left', validate='one_to_one')
    labels['structural_eligible'] = member_flags.eligible.eq(True).to_numpy()
    frame = finite.merge(labels, on=KEYS, validate='one_to_one').sort_values(KEYS).reset_index(drop=True)
    windows = window_profiles(frame, research_calendar)
    pd.DataFrame(windows).to_csv(out / 'walk-forward-windows.csv', index=False)
    missing = joins['raw'].loc[joins['raw']['_merge'].eq('left_only')]
    raw_present = joins['raw']['_merge'].eq('both')
    null_ohlc = raw_present & joins['raw'][OHLC].isna().any(axis=1)
    null_any = raw_present & joins['raw'][OHLC + ['amount', 'pre_close']].isna().any(axis=1)
    # Current metadata extent is deliberately not inferred from query receipts.
    edge_ranges = []
    for lo, hi in [('2016-11-07', '2016-11-30'), ('2026-11-01', '2026-11-06')]:
        edge = stock.read('cn_stock_real_bar1d', ['date', 'instrument'], pd.Timestamp(lo), pd.Timestamp(hi))
        edge_ranges.append({'observed_start': str(edge.date.min().date()), 'observed_end': str(edge.date.max().date()),
                            'rows': len(edge), 'securities': int(edge.symbol.nunique())})
    indexed = pd.read_csv(Path(args.inventory_root) / 'files.csv', encoding='utf-8-sig')
    inventory = indexed.groupby('source').size().to_dict()
    catalog_path = Path(args.csmar_catalog)
    catalog_raw = catalog_path.read_bytes()
    manifest.append({'asset': 'CSMAR_existing_catalog', 'actual_sha256': digest(catalog_raw),
                     'evidence_level': 'EXISTING_CATALOG_NOT_NATIVE_RESCAN'})
    catalog = json.loads(catalog_raw)
    # Keep the full catalog private; expose only top-level structure for replay.
    inventory['csmar_catalog_top_level_keys'] = list(catalog)
    assets = {'bigquant_tables': metadata, 'indexed_historical_file_counts': inventory,
              'raw_table_observed_edge_partitions': edge_ranges}
    write_json(out / 'asset-metadata.json', assets)
    current_missing = int(len(missing))
    current_null = int(null_ohlc.sum())
    counts = members.groupby('date').size()
    result = {'schema': 'P3D06_AGGREGATE_V1', **LIMITS, 'status': 'PARTIAL',
              'window': {'start': args.start, 'end': args.end, 'warm_start': str(warm_start.date()),
                         'tail_end': str(tail_end.date()), 'calendar_source': 'BigQuant trading_days market_code=CN',
                         'calendar_sessions': len(calendar), 'research_calendar_sessions': len(research_calendar),
                         'member_dates_absent_from_vendor_calendar': calendar_missing},
              'membership': {'source_observed_range': original_range, 'member_days': len(members),
                             'unique_keys': int(len(members.drop_duplicates(KEYS))), 'dates': int(members.date.nunique()),
                             'securities': len(symbols), 'min_daily_members': int(counts.min()), 'max_daily_members': int(counts.max()),
                             'known_at_certified_member_days': 0, 'stable_identity_certified_member_days': 0},
              'baseline_recheck': {'missing_keys': current_missing, 'null_ohlc_present_keys': current_null,
                                   'any_ohlc_amount_preclose_null_present_keys': int(null_any.sum()),
                                   'old_605500_292_564_exact': len(members) == 605500 and current_missing == 292 and current_null == 564,
                                   'missing_by_code': {str(k): int(v) for k, v in missing.groupby('symbol').size().items()},
                                   'complete_raw_ohlc_member_days': int((raw_present & ~null_ohlc).sum())},
              'status': 'PARTIAL', 'status_observations': {
                  name: {str(k): int(v) for k, v in joins['status'][name].value_counts(dropna=False).items()} for name in STATUS},
              'listing_snapshot': {'matched_securities': int(members[['symbol']].drop_duplicates().merge(listings, on='symbol', how='inner').shape[0]),
                                   'listing_date_nulls_in_snapshot': int(listings.list_date.isna().sum()),
                                   'industry_nulls_in_snapshot': int(listings.industry.isna().sum()),
                                   'historical_version': 'UNKNOWN_CURRENT_EXPORT_NOT_HISTORICAL_IDENTITY'},
              'source_quality': quality,
              'factors': {'catalog_snapshot_hash': snapshot_hash, 'computed': len(factors),
                          'with_any_finite_member_value': int(factors.finite.gt(0).sum()),
                          'all14_finite_member_days': int(frame.all14.sum()),
                          'all64_finite_member_days': int(frame.all64.sum()),
                          'all78_finite_member_days': int(frame.all78.sum()),
                          'all78_with_label_endpoints': int((frame.all78 & frame.label_endpoints_available).sum()),
                          'all78_structurally_eligible_with_label_endpoints': int((frame.all78 & frame.label_endpoints_available & frame.structural_eligible).sum()),
                          'financially_admitted': 0},
              'label_endpoints': {'holding_days': 5, 'entry_offset': 1, 'exit_offset': 6,
                                  'available': int(frame.label_endpoints_available.sum()),
                                  'missing': int((~frame.label_endpoints_available).sum()),
                                  'returns_computed': False,
                                  'future_status_execution_and_corporate_actions_certified': False},
              'structural_flags': {'eligible': int(frame.structural_eligible.sum()),
                                   'not_eligible': int((~frame.structural_eligible).sum()),
                                   'mapping': 'suspended==0 -> trade_status=1; st_status!=0 -> is_st; supplier-contract-unverified',
                                   'listing_age': '120 vendor sessions; current listing snapshot, not PIT'},
              'windows': {'rows': len(windows), 'candidates': ['minimum_12_3_3', 'expanded_24_3_3']},
              'resources': {'wall_seconds': time.perf_counter() - started,
                            'process_cpu_seconds': sum(process.cpu_times()[:2]),
                            'peak_working_set_bytes': process.memory_info().peak_wset,
                            'projected_parquet_physical_rows': sum(a.physical_rows for a in archives.values()),
                            'parquet_uncompressed_bytes_consumed_once': sum(a.bytes for a in archives.values()),
                            'parquet_partitions': sum(a.parts for a in archives.values()),
                            'factor_symbol_batch': args.symbols_per_batch, 'factor_batch': 16,
                            'large_intermediate_files': 0, 'python': platform.python_version(),
                            'pandas': pd.__version__, 'numpy': np.__version__}}
    write_json(out / 'diagnostics.json', result)
    write_json(out / 'input-manifest.json', {'schema': 'P3D06_INPUT_MEMBERS_V1', 'entries': manifest,
                                            'receipt_hash_mismatches': sum(not x['hash_matches_receipt'] for x in manifest if 'hash_matches_receipt' in x),
                                            'basis': 'SHA256 of each consumed ZIP member in its single read; no full ZIP rehash'})
    for archive in archives.values():
        archive.zip.close()
    print(json.dumps({'member_days': len(members), 'factors': len(factors), 'wall_seconds': result['resources']['wall_seconds']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bigquant-root', required=True)
    parser.add_argument('--membership-zip', required=True)
    parser.add_argument('--inventory-root', required=True)
    parser.add_argument('--csmar-catalog', required=True)
    parser.add_argument('--start', default='2021-09-13')
    parser.add_argument('--end', default='2026-09-11')
    parser.add_argument('--symbols-per-batch', type=int, default=32)
    parser.add_argument('--output', required=True, help='New aggregate-only output directory')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
