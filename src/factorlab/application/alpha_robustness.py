"""Negative controls over the existing walk-forward and evaluation machinery."""
from dataclasses import replace

import numpy as np
import pandas as pd

from factorlab.validation import WalkForwardRunner
from factorlab.evaluation.research import compare_results, paired_inference


def shuffle_training_labels(train, *, seed, target='EXCESS_RET5'):
    """Permute finite training targets within date; preserve missing-label slots."""
    shuffled = train.copy()
    for date, group in train.groupby('date', sort=True):
        indices = group.sort_values('symbol').index
        indices = indices[train.loc[indices, target].notna()]
        rng = np.random.default_rng(np.random.SeedSequence([seed, pd.Timestamp(date).value // 86400000000000]))
        shuffled.loc[indices, target] = rng.permutation(train.loc[indices, target].to_numpy())
    return shuffled


class ShuffledTrainingRunner(WalkForwardRunner):
    """Research-local coupling to the runner's partition hook, not a new fitter."""
    def __init__(self, *args, shuffle_seed=17, **kwargs):
        super().__init__(*args, **kwargs)
        self.shuffle_seed = shuffle_seed

    def _fold_frames(self, prepared, folds):
        for fold, train, validation, test in super()._fold_frames(prepared, folds):
            yield fold, shuffle_training_labels(train, seed=self.shuffle_seed,
                target=self.target_col), validation, test


def random_scores(predictions, *, seed):
    result = predictions.sort_values(['date', 'symbol']).reset_index(drop=True).copy()
    finite = np.isfinite(result.score)
    result.loc[finite, 'score'] = np.random.default_rng(seed).normal(size=int(finite.sum()))
    return result


def delayed_scores(predictions, calendar, *, sessions=5):
    """Join source scores by full-calendar position, retaining destination outcomes."""
    if sessions < 1:
        raise ValueError('sessions must be positive')
    calendar = pd.DatetimeIndex(calendar)
    mapping = pd.DataFrame({'date': calendar[sessions:], 'source_date': calendar[:-sessions]})
    source = predictions[['date', 'symbol', 'score', 'fold']].rename(
        columns={'date': 'source_date', 'score': 'source_score', 'fold': 'source_fold'})
    result = predictions.merge(mapping, on='date', how='inner', validate='many_to_one').merge(
        source, on=['source_date', 'symbol'], how='inner', validate='many_to_one')
    result['score'] = result.pop('source_score')
    return result


def cost_accounting(nav, trades, *, initial_cash, schedule):
    """Reconcile a synthetic ledger without dividends or external cash flows."""
    signed_notional = np.where(trades.side.str.upper().eq('SELL'),
        trades.trade_notional, -trades.trade_notional)
    expected_cash = initial_cash + np.cumsum(signed_notional - trades.transaction_cost)
    np.testing.assert_allclose(trades.cash_after, expected_cash, atol=1e-8, rtol=1e-12)
    flows = pd.Series(signed_notional - trades.transaction_cost.to_numpy(),
        index=pd.DatetimeIndex(trades.execution_date)).groupby(level=0).sum()
    daily_cash = initial_cash + flows.reindex(pd.DatetimeIndex(nav.date), fill_value=0).cumsum()
    np.testing.assert_allclose(nav.cash, daily_cash, atol=1e-8, rtol=1e-12)
    np.testing.assert_allclose(nav.transaction_cost.sum(), trades.transaction_cost.sum(), atol=1e-8)
    for trade in trades.itertuples():
        fee = schedule.calculate(trade.execution_date, trade.side, trade.trade_notional)
        np.testing.assert_allclose(trade.transaction_cost, fee.total, atol=1e-8)
    return float(np.max(np.abs(nav.cash.to_numpy() - daily_cash.to_numpy())))


def calendar_paired_inference(results, *, calendar, baseline, metric, options):
    """Preserve non-OOS trading slots as missing rather than compressing fold gaps."""
    results = list(results)
    compare_results(results)
    calendar = pd.DatetimeIndex(calendar)
    scheduled = []
    for result in results:
        daily = result.tables['daily'].set_index('date').reindex(calendar)
        daily.index.name = 'date'
        scheduled.append(replace(result,
            context=replace(result.context, calendar=tuple(calendar.strftime('%Y-%m-%d'))),
            tables={**result.tables, 'daily': daily.reset_index()}))
    return paired_inference(scheduled, baseline=baseline, metric=metric, options=options)


def common_prediction_samples(arms):
    """Keep common finite-score keys; evaluator checks labels and fold receipts."""
    keys = ['date', 'symbol']
    common = None
    for frame in arms.values():
        index = pd.MultiIndex.from_frame(frame.loc[np.isfinite(frame.score), keys])
        common = index if common is None else common.intersection(index)
    return {name: frame.set_index(keys).loc[common].reset_index().sort_values(keys).reset_index(drop=True)
            for name, frame in arms.items()}
