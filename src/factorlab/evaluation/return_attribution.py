"""Observed-path attribution for synthetic accounts without external cash flows."""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from factorlab.dataset.identity import frame_hash


COST_COLUMNS = ('commission', 'stamp_duty', 'transfer_fee', 'side_fee', 'slippage')


@dataclass(frozen=True)
class ReturnAttribution:
    daily: pd.DataFrame
    execution: pd.DataFrame
    summary: dict


def attribute_execution(result, *, initial_cash, calendar):
    """Reconcile an existing, initially empty synthetic execution account.

    Gross P&L adds observed explicit charges back to net P&L on the SAME fills;
    it is not a zero-fee re-execution. Cash opportunity contribution uses previous
    close cash times benchmark return, not an executable fully-invested portfolio.
    Execution weights are posttrade-open weights; concentration uses close marks.
    ``calendar`` contains return sessions, excluding the initial signal session.
    """
    nav, trades, positions = (getattr(result, k).copy() for k in ('nav', 'trades', 'positions'))
    nav['date'] = pd.to_datetime(nav.date)
    expected = pd.DatetimeIndex(calendar)
    if nav.date.duplicated().any() or not pd.DatetimeIndex(nav.date).equals(expected):
        raise ValueError('execution calendar must match exactly, including fold gaps')
    if initial_cash <= 0 or not np.isfinite(initial_cash):
        raise ValueError('positive initial cash required')
    for frame, columns in ((trades, ('signal_date', 'execution_date')), (positions, ('date',))):
        for column in columns:
            frame[column] = pd.to_datetime(frame[column])
    if not trades.execution_date.isin(expected).all() or not positions.date.isin(expected).all():
        raise ValueError('ledger record outside execution calendar')
    if positions.duplicated(['date', 'symbol']).any():
        raise ValueError('duplicate holding identity')
    if not (trades.signal_date < trades.execution_date).all():
        raise ValueError('fills must follow their signal session')
    trades = trades.astype({k: float for k in (*COST_COLUMNS, 'trade_notional', 'transaction_cost',
                                                'cash_after', 'executed_shares')})
    positions = positions.astype({k: float for k in ('shares', 'market_value', 'weight')})
    signed = np.where(trades.side.eq('sell'), 1., -1.) * trades.trade_notional
    np.testing.assert_allclose(trades[list(COST_COLUMNS)].sum(axis=1), trades.transaction_cost,
                               rtol=0, atol=1e-7)
    np.testing.assert_allclose(initial_cash + (signed - trades.transaction_cost).cumsum(),
                               trades.cash_after, rtol=0, atol=1e-7)
    daily = nav.set_index('date').copy()
    flows = pd.Series((signed - trades.transaction_cost).to_numpy(), index=trades.execution_date).groupby(level=0).sum()
    cash = initial_cash + flows.reindex(expected, fill_value=0).cumsum()
    np.testing.assert_allclose(daily.cash, cash, rtol=0, atol=1e-7)
    costs = trades.groupby('execution_date')[list(COST_COLUMNS) + ['transaction_cost']].sum().reindex(expected, fill_value=0)
    np.testing.assert_allclose(daily.transaction_cost, costs.transaction_cost, rtol=0, atol=1e-7)
    value = positions.groupby('date').market_value.sum().reindex(expected, fill_value=0)
    np.testing.assert_allclose(daily.portfolio_value, daily.cash + value, rtol=0, atol=1e-7)
    np.testing.assert_allclose(daily.strategy_nav, daily.portfolio_value / initial_cash, rtol=0, atol=1e-10)
    shares = trades.assign(signed_shares=np.where(trades.side.eq('buy'), 1, -1) * trades.executed_shares)
    filled = shares.pivot_table(index='execution_date', columns='symbol', values='signed_shares', aggfunc='sum', fill_value=0)
    held = positions.pivot_table(index='date', columns='symbol', values='shares', aggfunc='sum', fill_value=0)
    symbols = sorted(set(trades.symbol) | set(positions.symbol))
    filled = filled.reindex(index=expected, columns=symbols, fill_value=0).cumsum()
    held = held.reindex(index=expected, columns=symbols, fill_value=0)
    np.testing.assert_allclose(filled, held, rtol=0, atol=0)
    previous_value = daily.portfolio_value.shift(1, fill_value=initial_cash)
    previous_cash = daily.cash.shift(1, fill_value=initial_cash)
    daily['net_pnl'] = daily.portfolio_value - previous_value
    daily['gross_pnl_same_fills'] = daily.net_pnl + daily.transaction_cost
    daily['gross_return_same_fills'] = daily.gross_pnl_same_fills / previous_value
    daily['net_return'] = daily.net_pnl / previous_value
    np.testing.assert_allclose(daily.net_return, daily.strategy_return, rtol=0, atol=1e-10)
    daily['fee_return'] = daily.transaction_cost / previous_value
    daily['cash_weight'] = daily.cash / daily.portfolio_value
    daily['cash_opportunity_pnl'] = -previous_cash * daily.benchmark_return
    daily['gross_security_turnover'] = (daily.buy_notional + daily.sell_notional) / daily.turnover_denominator
    daily['close_security_hhi'] = positions.assign(square=positions.weight ** 2).groupby('date').square.sum().reindex(expected, fill_value=0)
    for column in COST_COLUMNS:
        daily[column] = costs[column].to_numpy()
    comparison = result.target_comparison.copy()
    comparison['signal_date'] = pd.to_datetime(comparison.signal_date)
    comparison['execution_date'] = pd.to_datetime(comparison.execution_date)
    if comparison.duplicated(['signal_date', 'execution_date', 'symbol']).any():
        raise ValueError('duplicate target identity')
    if not (comparison.signal_date < comparison.execution_date).all():
        raise ValueError('targets must execute after the signal session')
    np.testing.assert_allclose(comparison.actual_weight - comparison.target_weight,
                               comparison.weight_deviation, rtol=0, atol=1e-10)
    keys = ['signal_date', 'execution_date', 'symbol']
    recorded = trades[keys].merge(comparison[keys], on=keys, how='left', indicator=True, validate='many_to_one')
    if not recorded._merge.eq('both').all():
        raise ValueError('fill has no matching target identity')
    actual = comparison.merge(positions[['date', 'symbol', 'shares']],
        left_on=['execution_date', 'symbol'], right_on=['date', 'symbol'], how='left', validate='many_to_one')
    np.testing.assert_allclose(actual.actual_shares, actual.shares.fillna(0), rtol=0, atol=0)
    execution = comparison.groupby(['signal_date', 'execution_date']).agg(
        target_investment=('target_weight', 'sum'), actual_investment=('actual_weight', 'sum'),
        weight_deviation_l1=('weight_deviation', lambda x: x.abs().sum())).reset_index()
    order_telemetry = trades.groupby(['signal_date', 'execution_date']).agg(
        requested_shares_on_filled_records=('requested_shares', 'sum'),
        executed_shares=('executed_shares', 'sum'), filled_records=('symbol', 'size')).reset_index()
    rejected = result.rejections.copy()
    for column in ('signal_date', 'execution_date'):
        rejected[column] = pd.to_datetime(rejected[column])
    rejection_telemetry = rejected.groupby(['signal_date', 'execution_date']).agg(
        rejected_shares=('rejected_shares', 'sum'), rejected_records=('symbol', 'size')).reset_index()
    execution = execution.merge(order_telemetry, on=['signal_date','execution_date'], how='left', validate='one_to_one').merge(
        rejection_telemetry, on=['signal_date','execution_date'], how='left', validate='one_to_one')
    telemetry = ['requested_shares_on_filled_records','executed_shares','filled_records','rejected_shares','rejected_records']
    execution[telemetry] = execution[telemetry].astype(float).fillna(0)
    final = float(daily.portfolio_value.iloc[-1])
    summary = dict(net_return=final / initial_cash - 1,
        gross_pnl_same_fills=float(daily.gross_pnl_same_fills.sum()),
        costs=float(daily.transaction_cost.sum()), net_pnl=final-initial_cash,
        gross_contribution=float(daily.gross_pnl_same_fills.sum()/initial_cash),
        fee_contribution=float(-daily.transaction_cost.sum()/initial_cash),
        mean_cash_weight=float(daily.cash_weight.mean()),
        cash_opportunity_contribution=float(daily.cash_opportunity_pnl.sum()/initial_cash),
        gross_security_turnover=float(daily.gross_security_turnover.sum()),
        legacy_one_way_turnover=float(daily.turnover.sum()),
        mean_close_security_hhi=float(daily.close_security_hhi.mean()),
        rejections=len(result.rejections), fills=len(trades),
        rejection_reasons=result.rejections.reason.value_counts().to_dict(),
        execution_identity=frame_hash(nav),
        gross_estimand='same observed fills, explicit costs added back; not zero-cost counterfactual',
        cash_estimand='previous-close cash times negative benchmark return; descriptive opportunity cost',
        target_turnover_status='NOT_COMPUTABLE: requires decision-time actual-weight snapshot',
        expected_return_status='NOT_COMPUTABLE: model scores are not qualified expected returns')
    np.testing.assert_allclose(summary['gross_pnl_same_fills'] - summary['costs'], summary['net_pnl'], rtol=0, atol=1e-7)
    return ReturnAttribution(daily.reset_index(), execution, summary)


def paired_daily_difference(left, right):
    """Compare full calendars and the same benchmark; never intersect dates."""
    if not left.date.equals(right.date):
        raise ValueError('paired return calendars differ')
    np.testing.assert_allclose(left.benchmark_return, right.benchmark_return, rtol=0, atol=0)
    return pd.DataFrame(dict(date=left.date, net_difference=left.net_return-right.net_return,
        gross_difference_same_fills=left.gross_return_same_fills-right.gross_return_same_fills,
        fee_difference=left.fee_return-right.fee_return))
