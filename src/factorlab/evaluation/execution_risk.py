"""Research execution diagnostics over existing execution and R14 receipts."""
from dataclasses import replace

import numpy as np
import pandas as pd


ORDER_KEYS = ['signal_date', 'execution_date', 'symbol', 'side']


def order_outcomes(result):
    """One receipt per engine order identity; partial refusals are not new orders."""
    fills = result.trades
    rejected = result.rejections.loc[result.rejections.side.isin(['buy', 'sell'])]
    columns = ORDER_KEYS + ['requested_shares']
    submitted = [frame[columns] for frame in (fills, rejected) if not frame.empty]
    records = (pd.concat(submitted) if submitted else fills[columns]).drop_duplicates()
    if records.duplicated(ORDER_KEYS).any():
        raise ValueError('conflicting requested quantities for one order')
    quantities = fills.groupby(ORDER_KEYS).executed_shares.sum()
    refusals = rejected.groupby(ORDER_KEYS).rejected_shares.sum()
    orders = records.set_index(ORDER_KEYS)
    orders['executed_shares'] = quantities.reindex(orders.index, fill_value=0)
    orders['rejected_shares'] = refusals.reindex(orders.index, fill_value=0)
    orders = orders.astype({column: float for column in
                            ('requested_shares', 'executed_shares', 'rejected_shares')})
    np.testing.assert_allclose(orders.requested_shares,
        orders.executed_shares + orders.rejected_shares, rtol=0, atol=0)
    orders['status'] = np.where(orders.executed_shares.eq(0), 'REJECTED',
        np.where(orders.rejected_shares.gt(0), 'PARTIAL', 'FILLED'))
    return orders.reset_index()


def execution_diagnostics(result, *, initial_cash, calendar, attribution, predictions=None,
                          decisions=None):
    """Extend the injected R14 ReturnAttribution, without duplicating its ledger.

    ``attribution`` is R14's attribute_execution callable. Predictions retain
    their original scale; optimizer preferences are not expected returns.
    """
    base = attribution(result, initial_cash=initial_cash, calendar=calendar)
    daily, execution, summary = base.daily.copy(), base.execution.copy(), dict(base.summary)
    for column in ('research_fixed_slippage', 'market_impact'):
        charges = result.trades.groupby('execution_date')[column].sum()
        daily[column] = daily.date.map(charges).fillna(0)
        summary[column] = float(daily[column].sum())
    daily['base_slippage'] = daily.slippage - daily.research_fixed_slippage - daily.market_impact
    daily['benchmark_excess_return'] = daily.net_return - daily.benchmark_return
    orders = order_outcomes(result)
    requested, executed = orders.requested_shares.sum(), orders.executed_shares.sum()
    summary.update(fill_rate_shares=float(executed / requested) if requested else None,
        rejection_rate_orders=float(orders.status.eq('REJECTED').mean()) if len(orders) else None,
        partial_rate_orders=float(orders.status.eq('PARTIAL').mean()) if len(orders) else None,
        orders=len(orders),
        benchmark_excess_return=float(daily.strategy_nav.iloc[-1] - daily.benchmark_nav.iloc[-1]),
        constraint_estimand='NOT_COMPUTABLE: path differences require an explicitly paired counterfactual')
    if decisions is not None:
        required = ['signal_date', 'target_turnover']
        target_turnover = decisions[required].copy()
        if target_turnover.signal_date.duplicated().any():
            raise ValueError('one target-turnover receipt required per decision')
        if not np.isfinite(target_turnover.target_turnover).all() or (target_turnover.target_turnover < 0).any():
            raise ValueError('target turnover must be finite and nonnegative')
        execution = execution.merge(target_turnover, on='signal_date', how='left', validate='many_to_one')
        if execution.target_turnover.isna().any():
            raise ValueError('missing decision-time turnover receipt')
        summary['target_turnover_status'] = 'COMPUTED_FROM_DECISION_SNAPSHOT'
        summary['target_turnover'] = float(target_turnover.target_turnover.sum())
    # Security-level target/actual weights remain in result.target_comparison.
    if predictions is not None:
        required = ['date', 'symbol', 'score']
        signals = predictions[required]
        if signals.duplicated(['date', 'symbol']).any():
            raise ValueError('one prediction required per date/security')
        summary['prediction_rows'] = len(signals)
    return replace(base, daily=daily, execution=execution, summary=summary)


def paired_constraint_difference(constrained, reference):
    """Hypothetical scenario comparison, NOT an additive execution contribution."""
    if not constrained.daily.date.equals(reference.daily.date):
        raise ValueError('counterfactual calendars must match')
    np.testing.assert_allclose(constrained.daily.benchmark_return,
                               reference.daily.benchmark_return, rtol=0, atol=0)
    return pd.DataFrame(dict(date=constrained.daily.date,
        net_return_difference=constrained.daily.net_return - reference.daily.net_return,
        scope='HYPOTHETICAL_REEXECUTION_DIFFERENCE_NOT_ATTRIBUTION'))
