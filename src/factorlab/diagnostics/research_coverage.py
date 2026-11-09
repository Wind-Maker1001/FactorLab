"""Shared retrospective coverage calculations; no financial certification."""
from dataclasses import asdict
import json
import numpy as np
import pandas as pd
from factorlab.factors.candidates import register_candidates
from factorlab.factors.registry_v2 import FactorRegistryV2
from factorlab.validation.splitter import ExpandingWalkForwardSplitter

KEYS = ["date", "symbol"]
OHLC = ["open", "high", "low", "close"]

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


def window_profiles(frame, calendar, candidates=(('minimum_12_3_3', 12), ('expanded_24_3_3', 24))):
    rows = []
    for name, train_months in candidates:
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

