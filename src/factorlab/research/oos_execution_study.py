"""Frozen P9-01 inference and execution adapters; no admission certification."""
from copy import copy
import hashlib

import numpy as np
import pandas as pd

from factorlab.dataset.identity import file_hash, frame_hash
from factorlab.exploratory.factor_discovery.contracts import FIELDS
from factorlab.research.real_model_study import KEYS, PRICES, TARGET, raw_registry
from factorlab.factors.registry_v2 import FactorDataContext
from factorlab.portfolio.topk import select_top_k


def verify_predictions(path, config):
    if file_hash(path) != config['prediction_sha256']:
        raise ValueError('PREDICTION_SHA256_DRIFT')
    predictions = pd.read_parquet(path)
    if len(predictions) != 440982 or set(predictions.model_id) != {
            group + '_' + model for group in ('A', 'B')
            for model in ('factor_score', 'ridge', 'lightgbm', 'xgboost')} | {'C_factor_score'}:
        raise ValueError('ORIGINAL_PREDICTION_IDENTITY_MISMATCH')
    for _, part in predictions.groupby('model_id'):
        sample = part.rename(columns={'signal_date': 'date'})[['fold', *KEYS, TARGET]]
        if frame_hash(sample) != config['common_test_hash']:
            raise ValueError('ORIGINAL_COMMON_TEST_MISMATCH')
        if len(part) != 48998 or part.signal_date.nunique() != 726 or set(part.fold) != {0, 1, 2}:
            raise ValueError('ORIGINAL_FOLD_IDENTITY_MISMATCH')
    return predictions


def signal_candidates(bars, members, calendar):
    """Compute only trailing signal features, independently of label availability."""
    dates = pd.DatetimeIndex(calendar)
    if bars.duplicated(KEYS).any() or members.duplicated(KEYS).any():
        raise ValueError('duplicate signal input keys')
    grid = pd.MultiIndex.from_product([sorted(bars.symbol.unique()), dates], names=['symbol', 'date'])
    panel = bars.set_index(['symbol', 'date']).reindex(grid).reset_index()
    values = panel[PRICES + ['volume']].to_numpy(dtype=float)
    ready = (np.isfinite(values).all(axis=1) & panel[PRICES].gt(0).all(axis=1)
        & panel.volume.gt(0) & panel.suspended.eq(0) & panel.st_status.eq(0)
        & panel.raw_low.le(panel[['raw_open', 'raw_close']].min(axis=1))
        & panel.raw_high.ge(panel[['raw_open', 'raw_close']].max(axis=1))
        & panel.raw_low.le(panel.raw_high))
    panel.loc[~ready, PRICES + ['volume']] = np.nan
    for name in FIELDS:
        if name != 'RET1' and name not in panel:
            panel[name] = np.nan
    snapshot, refs = raw_registry()
    features = snapshot.compute(panel, refs, calendar=dates,
        context=FactorDataContext(data_source='REAL_CANDIDATE_BIGQUANT_RAW'))
    names = [ref.factor_id for ref in refs]
    trailing = ready.groupby(panel.symbol, sort=False).transform(lambda v: v.rolling(61, min_periods=61).sum().eq(61))
    eligible = members.merge(panel[KEYS].assign(signal_ready=trailing.to_numpy()), on=KEYS, validate='one_to_one')
    eligible = eligible.merge(features, on=KEYS, how='left', validate='one_to_one')
    data = eligible.loc[eligible.signal_ready.eq(True) & eligible[names].notna().all(axis=1), KEYS + names].copy()
    grouped = data.groupby('date')[names]
    data[names] = (data[names] - grouped.transform('mean')) / grouped.transform(lambda v: v.std(ddof=0)).replace(0, np.nan)
    return data.loc[data[names].notna().all(axis=1)].sort_values(KEYS).reset_index(drop=True)


class FittedFoldCapture:
    """Retain existing runner fit results, including copies made by Train selection."""
    def __init__(self, runner):
        self.runner = runner
        self.fitted = {}

    def run(self, data, calendar):
        # The runner shallow-copies itself for selected features. A subclass keeps
        # each fitted model without changing its training or prediction machinery.
        base = type(self.runner)
        captured = self.fitted

        class CapturingRunner(base):
            def _fit_and_select(self, fold, train, validation, **kwargs):
                result = super()._fit_and_select(fold, train, validation, **kwargs)
                captured[fold] = (result[0], tuple(self.feature_names))
                return result

        runner = copy(self.runner)
        runner.__class__ = CapturingRunner
        return runner.run(data, calendar=calendar)


def compare_reconstruction(original, rebuilt):
    old = original.rename(columns={'signal_date': 'date', 'prediction': 'score'})
    merged = old[['fold', *KEYS, 'score']].merge(rebuilt[['fold', *KEYS, 'score']],
        on=['fold', *KEYS], how='outer', validate='one_to_one', suffixes=('_old', '_new'), indicator=True)
    if not merged._merge.eq('both').all():
        raise ValueError('RECONSTRUCTION_TEST_KEYS_MISMATCH')
    rows = []
    for fold, part in merged.groupby('fold'):
        delta = np.abs(part.score_old - part.score_new)
        matched = np.allclose(part.score_old, part.score_new, rtol=1e-10, atol=1e-12)
        rows.append(dict(fold=int(fold), rows=len(part), maximum_absolute_difference=float(delta.max()), matched=bool(matched)))
    if not all(row['matched'] for row in rows):
        raise ValueError('RECONSTRUCTION_PREDICTION_MISMATCH: ' + str(rows))
    return rows


def make_targets(scores, calendar, symbols, config, scheme):
    """Anchor to Test session zero; only open positions with a full in-Test horizon."""
    dates = pd.DatetimeIndex(calendar)
    rows, decisions = [], []
    for position in range(0, len(dates) - 1, config['rebalance_sessions']):
        day = dates[position]
        part = scores.loc[scores.date.eq(day), ['date', 'symbol', 'score']].copy()
        reason = 'RANKED'
        if position + 6 >= len(dates):
            selected = set()
            reason = 'TERMINAL_ZERO_TARGET'
        elif len(part) < config['minimum_cross_section']:
            selected = set()
            reason = 'MISSING_OR_SPARSE_SIGNAL_CASH'
        else:
            if scheme == 'hash_equal':
                part['hash'] = part.symbol.map(lambda s: hashlib.sha256(s.encode()).hexdigest())
                part = part.sort_values(['hash', 'symbol'])
                part['score'] = np.arange(len(part), 0, -1, dtype=float)
            selected = set(select_top_k(part, top_k=config['top_k'],
                minimum_cross_section=config['minimum_cross_section']).symbol)
        rows.extend(dict(date=day, symbol=symbol, target_weight=config['target_weight'] if symbol in selected else 0.)
            for symbol in sorted(symbols))
        decisions.append(dict(date=day, reason=reason, candidates=len(part), selected=len(selected)))
    return pd.DataFrame(rows), pd.DataFrame(decisions)


def execution_bars(bars):
    data = bars.copy()
    data['trade_status'] = data.suspended.map({0: 1, 1: 0})
    data['is_st'] = data.st_status.where(data.st_status.isin([0, 1]))
    return data


def valuation_gaps(result, bars):
    """Identify actual held-security missing closes, not future candidate exclusions."""
    marks = result.positions.merge(bars[KEYS + ['raw_close']], on=KEYS, how='left', validate='one_to_one')
    bad = ~np.isfinite(marks.raw_close) | marks.raw_close.le(0)
    return marks.loc[bad, KEYS + ['shares', 'mark_price', 'raw_close']].copy()
