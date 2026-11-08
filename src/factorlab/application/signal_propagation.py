"""Synthetic signal-to-execution contrasts over existing domain engines."""
from itertools import combinations

import numpy as np
import pandas as pd

from factorlab.dataset.identity import frame_hash
from factorlab.portfolio import OptimizationInput, OptimizerConfig, PortfolioOptimizer

BOUNDARY = 'NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED'
OBJECTIVES = ('equal', 'score', 'minimum_variance')


def prediction_targets(predictions, returns, *, objective, lookback=40, every=5,
                       investment=.8, max_weight=.15):
    """Use close-known risk and average percentile ranks, never expected returns.

    Every objective receives the complete same universe. Decision timestamps are
    not shifted: the execution engine schedules the next trading session.
    """
    optimizer = PortfolioOptimizer(OptimizerConfig(objective=objective,
        investment=investment, max_weight=max_weight, shrinkage=.2))
    targets, receipts, rankings = [], [], []
    for date, group in list(predictions.groupby('date', sort=True))[::every]:
        group = group.sort_values('symbol').copy()
        scores = group.set_index('symbol').score.rank(method='average', pct=True)
        risk = returns.loc[returns.index <= date, list(scores.index)].tail(lookback)
        result = optimizer.optimize(OptimizationInput(symbols=tuple(scores.index),
            decision_time=date, returns=risk, scores=scores))
        target = result.target_weights.rename_axis('symbol').rename('target_weight').reset_index()
        target['date'] = date
        targets.append(target)
        group['rank'] = group.score.rank(method='average', ascending=False)
        group['allocation_score'] = group.symbol.map(scores)
        rankings.append(group)
        receipts.append(dict(date=date, prediction_identity=frame_hash(
            group[['date', 'symbol', 'fold', 'score']].reset_index(drop=True)),
            input_identity=result.input_identity, config_identity=result.config_identity,
            risk_first_date=risk.index.min(), risk_last_date=risk.index.max(),
            risk_rows=len(risk)))
    return pd.concat(targets, ignore_index=True), pd.DataFrame(receipts), pd.concat(rankings, ignore_index=True)


def pairwise_transmission(targets, executions, *, predictions=None, tolerance=1e-8):
    """Research diagnostic, not a standard financial statistic.

    Each event is a model pair at a common decision date. A layer's denominator
    counts events differing at its preceding layer; numerator counts those also
    differing at that layer. Zero denominators produce null rates.
    """
    rows = []
    for left, right in combinations(sorted(targets), 2):
        a, b = targets[left], targets[right]
        for date in sorted(set(a.date) & set(b.date)):
            aw = a.loc[a.date.eq(date)].set_index('symbol').target_weight
            bw = b.loc[b.date.eq(date)].set_index('symbol').target_weight.reindex(aw.index)
            weight_l1 = float((aw - bw).abs().sum())
            ea, eb = executions[left], executions[right]
            def fills(result):
                f = result.trades.loc[result.trades.signal_date.eq(date)]
                return f.assign(signed=np.where(f.side.eq('buy'), 1, -1) * f.executed_shares).groupby('symbol').signed.sum()
            fa, fb = fills(ea).align(fills(eb), fill_value=0)
            ca = ea.target_comparison.loc[ea.target_comparison.signal_date.eq(date)]
            cb = eb.target_comparison.loc[eb.target_comparison.signal_date.eq(date)]
            execution_date = ca.execution_date.iloc[0]
            def vector(frame, column):
                return frame.set_index('symbol')[column].sort_index()
            qa, qb = vector(ca, 'target_shares').align(vector(cb, 'target_shares'), fill_value=0)
            ha, hb = vector(ea.positions.loc[ea.positions.date.eq(execution_date)], 'shares').align(
                vector(eb.positions.loc[eb.positions.date.eq(execution_date)], 'shares'), fill_value=0)
            nav_a = float(ea.nav.loc[ea.nav.date.eq(execution_date), 'strategy_nav'].iloc[0])
            nav_b = float(eb.nav.loc[eb.nav.date.eq(execution_date), 'strategy_nav'].iloc[0])
            extra = {}
            if predictions is not None:
                pa = predictions[left].loc[predictions[left].date.eq(date)].set_index('symbol').score.sort_index()
                pb = predictions[right].loc[predictions[right].date.eq(date)].set_index('symbol').score.reindex(pa.index)
                extra = dict(scores_changed=not np.allclose(pa, pb, rtol=0, atol=tolerance),
                    ranking_changed=not pa.rank(method='average').equals(pb.rank(method='average')))
            rows.append(dict(left=left, right=right, date=date, execution_date=execution_date,
                target_l1=weight_l1, target_changed=weight_l1 > tolerance,
                orders_changed=bool((qa-qb).abs().sum() > 0),
                executed_shares_l1=float((fa-fb).abs().sum()),
                trades_changed=bool((fa-fb).abs().sum() > 0),
                holdings_changed=bool((ha-hb).abs().sum() > 0),
                nav_difference=nav_a-nav_b, nav_changed=abs(nav_a-nav_b) > tolerance, **extra))
    frame = pd.DataFrame(rows)
    denominator = int(frame.target_changed.sum())
    numerator = int((frame.target_changed & frame.trades_changed).sum())
    rates = dict(target_to_trades_numerator=numerator,
        target_to_trades_denominator=denominator,
        target_to_trades_rate=numerator / denominator if denominator else None)
    layers = [('target_changed', 'orders_changed'), ('orders_changed', 'trades_changed'),
              ('trades_changed', 'holdings_changed'), ('holdings_changed', 'nav_changed')]
    if predictions is not None:
        layers = [('scores_changed', 'ranking_changed'), ('ranking_changed', 'target_changed')] + layers
    for before, after in layers:
        denominator = int(frame[before].sum())
        numerator = int((frame[before] & frame[after]).sum())
        rates[before + '_to_' + after] = dict(numerator=numerator, denominator=denominator,
            rate=numerator / denominator if denominator else None)
    return frame, rates
