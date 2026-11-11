"""Strict observed-calendar adapter over the existing shared-account executor."""
from datetime import date

import numpy as np
import pandas as pd

from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.errors import SchemaError
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation


class InputBlocked(SchemaError):
    """The annual account cannot be valued without inventing observations."""


def validate_panel(bars, calendar, symbols):
    """Require observed positive OHLC for every security/session; never carry marks."""
    dates = pd.DatetimeIndex(calendar)
    expected = pd.MultiIndex.from_product([dates, sorted(symbols)], names=['date', 'symbol'])
    if bars.duplicated(['date', 'symbol']).any():
        raise InputBlocked('DUPLICATE_SECURITY_SESSION')
    index = pd.MultiIndex.from_frame(bars[['date', 'symbol']])
    if len(expected.difference(index)) or len(index.difference(expected)):
        raise InputBlocked('UNALIGNED_OBSERVED_CALENDAR')
    prices = bars[['raw_open', 'raw_high', 'raw_low', 'raw_close']].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise InputBlocked('MISSING_OR_INVALID_OHLC_NO_STALE_VALUATION')
    if ((bars.raw_low > bars[['raw_open', 'raw_close']].min(axis=1)).any()
            or (bars.raw_high < bars[['raw_open', 'raw_close']].max(axis=1)).any()
            or not bars.trade_status.isin([0, 1]).all() or not bars.is_st.isin([0, 1]).all()):
        raise InputBlocked('INVALID_OHLC_OR_STATUS')


def monthly_targets(history, calendar, symbols, scheme, *, lookback=20, maximum=5, cap=.2):
    """Close-to-close t/t-20 momentum; lexical tie-break; no future observations."""
    dates = pd.DatetimeIndex(calendar)
    signal_days = dates.to_series().groupby(dates.to_period('M')).first()
    close = history.pivot(index='date', columns='symbol', values='raw_close').sort_index()
    rows, decisions = [], []
    for day in signal_days:
        if day == dates[-1]:
            decisions.append(dict(date=day, reason='NO_NEXT_OBSERVED_OPEN', eligible=0, selected=0))
            continue
        selected = list(sorted(symbols))
        reason = 'EQUAL_WEIGHT'
        eligible = len(selected)
        if scheme == 'momentum':
            observed = close.loc[:day, sorted(symbols)]
            if len(observed) <= lookback:
                selected, eligible, reason = [], 0, 'INSUFFICIENT_HISTORY_CASH'
            else:
                momentum = observed.iloc[-1] / observed.iloc[-1-lookback] - 1
                ranked = momentum[np.isfinite(momentum)].rename('momentum').reset_index()
                ranked = ranked.sort_values(['momentum', 'symbol'], ascending=[False, True])
                eligible = len(ranked)
                selected = ranked.symbol.head(maximum).tolist()
                reason = 'RANKED_RAW_CLOSE_MOMENTUM' if selected else 'NO_ELIGIBLE_SECURITY_CASH'
        elif scheme != 'equal_weight':
            raise ValueError('unknown portfolio scheme')
        weight = (min(1 / len(selected), cap) if scheme == 'momentum' else 1 / len(selected)) if selected else 0
        # Explicit zeros encode an empty complete snapshot without synthetic securities.
        rows.extend(dict(date=day, symbol=symbol, target_weight=weight if symbol in selected else 0.)
                    for symbol in sorted(symbols))
        decisions.append(dict(date=day, reason=reason, eligible=eligible, selected=len(selected)))
    return pd.DataFrame(rows, columns=['date', 'symbol', 'target_weight']), pd.DataFrame(decisions)


def audit_accounting(result, bars, initial_cash):
    """Reconcile shared-account receipts independently of valuation eligibility."""
    nav, trades, positions = result.nav, result.trades, result.positions
    flow = trades.assign(date=trades.execution_date,
        cash_flow=np.where(trades.side.eq('buy'), -trades.trade_notional, trades.trade_notional) - trades.transaction_cost)
    if not np.allclose(trades.cash_after, initial_cash + flow.cash_flow.cumsum(), rtol=1e-12, atol=1e-7):
        raise SchemaError('per-trade cash receipt does not reconcile')
    if not np.allclose(trades.trade_notional, trades.executed_shares * trades.execution_price, rtol=1e-12):
        raise SchemaError('executed notional does not reconcile')
    components = trades[['commission', 'stamp_duty', 'transfer_fee', 'side_fee', 'slippage']].sum(axis=1)
    if not np.allclose(components, trades.transaction_cost, rtol=1e-12, atol=1e-7):
        raise SchemaError('fee components do not reconcile')
    cash = initial_cash + flow.groupby('date').cash_flow.sum().reindex(nav.date, fill_value=0).cumsum().to_numpy()
    if not np.allclose(cash, nav.cash, rtol=1e-12, atol=1e-7):
        raise SchemaError('cash ledger does not reconcile to executed trades and fees')
    value = positions.groupby('date').market_value.sum().reindex(nav.date, fill_value=0).to_numpy()
    if not np.allclose(nav.cash + value, nav.portfolio_value, rtol=1e-12, atol=1e-7):
        raise SchemaError('equity ledger does not reconcile')
    if not np.allclose(nav.strategy_nav, nav.portfolio_value / initial_cash, rtol=1e-12):
        raise SchemaError('normalized equity does not reconcile')
    signed = trades.assign(date=trades.execution_date,
        shares=np.where(trades.side.eq('buy'), trades.executed_shares, -trades.executed_shares))
    symbols = sorted(set(bars.symbol))
    balances = signed.pivot_table(index='date', columns='symbol', values='shares', aggfunc='sum')
    balances = balances.reindex(index=pd.DatetimeIndex(nav.date), columns=symbols).fillna(0).cumsum()
    recorded = positions.pivot_table(index='date', columns='symbol', values='shares', aggfunc='sum')
    recorded = recorded.reindex(index=pd.DatetimeIndex(nav.date), columns=symbols).fillna(0)
    if not np.array_equal(balances.to_numpy(), recorded.to_numpy()):
        raise SchemaError('position ledger does not reconcile to executions')
    costs = trades.groupby('execution_date').transaction_cost.sum().reindex(nav.date, fill_value=0)
    if not np.allclose(costs, nav.transaction_cost, rtol=1e-12, atol=1e-7):
        raise SchemaError('daily costs do not reconcile')
    return dict(status='PASSED', cash=True, shares=True, equity=True, fees=True)


def audit_ledger(result, bars, initial_cash):
    """Reconcile execution receipts against cash, cumulative shares and raw marks."""
    audit = audit_accounting(result, bars, initial_cash)
    marked = result.positions.merge(bars[['date', 'symbol', 'raw_close']], on=['date', 'symbol'], how='left', validate='one_to_one')
    if not np.allclose(marked.mark_price, marked.raw_close, rtol=0, atol=0):
        raise SchemaError('position uses an unobserved close mark')
    if not np.allclose(marked.market_value, marked.shares * marked.raw_close, rtol=1e-12):
        raise SchemaError('position valuation does not reconcile')
    return dict(audit, raw_close_marks=True)


def execute(history, calendar, symbols, reference, scheme, config):
    dates = pd.DatetimeIndex(calendar)
    bars = history.loc[history.date.isin(dates)].copy()
    validate_panel(bars, dates, symbols)
    validate_panel(reference, dates, [config['execution_reference_symbol']])
    targets, decisions = monthly_targets(history, dates, symbols, scheme,
        lookback=config['momentum_lookback'], maximum=config['momentum_max_holdings'], cap=config['momentum_weight_cap'])
    if targets.empty:
        raise InputBlocked('NO_SIGNAL_WITH_NEXT_OBSERVED_OPEN')
    engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=config['initial_cash'],
        benchmark_open_price_column='raw_open', benchmark_close_price_column='raw_close'),
        FeeSchedule([FeeRule(effective_from=date(1990, 1, 1), **config['fees'])]))
    result = engine.run_target_weights(targets, bars, reference)
    audit = audit_ledger(result, bars, config['initial_cash'])
    return result, targets, decisions, audit


def evaluate(result, calendar, config, scheme, *, source_kind='REAL_CANDIDATE'):
    """Use zero-interest cash as the evaluation comparator, not a market index."""
    dates = pd.DatetimeIndex(calendar)
    nav = result.nav.set_index('date').reindex(dates)
    # The first close signal precedes any order: the account is genuinely cash.
    leading = nav.strategy_nav.isna()
    if not dates[leading].equals(dates[:1]):
        raise SchemaError('executor omitted sessions other than the initial cash signal day')
    nav.loc[leading, ['strategy_nav', 'portfolio_value', 'cash', 'holdings', 'transaction_cost', 'turnover',
                      'buy_notional', 'sell_notional', 'executed_orders']] = [1., config['initial_cash'], config['initial_cash'], 0, 0., 0., 0., 0., 0]
    nav['strategy_return'] = nav.strategy_nav.pct_change(fill_method=None).fillna(0.)
    nav['execution_reference_nav'] = nav.benchmark_nav
    nav['benchmark_nav'], nav['benchmark_return'] = 1., 0.
    nav['excess_nav'] = nav.strategy_nav
    nav['exposure'] = 1 - nav.cash / nav.portfolio_value
    weights = result.positions.groupby('date').weight
    nav['concentration_hhi'] = weights.apply(lambda s: float(s.pow(2).sum())).reindex(dates, fill_value=0.)
    nav['maximum_security_weight'] = weights.max().reindex(dates, fill_value=0.)
    nav = nav.rename_axis('date').reset_index()
    context = EvaluationContext(dataset_id=config['bars_sha256'], universe_id='P6_R01_FROZEN_ANNUAL',
        label=config['return_basis'], label_id='RAW_PRICE', benchmark_id='MODEL_CASH_ZERO_RETURN_NOT_INDEX',
        calendar=tuple(dates.strftime('%Y-%m-%d')), holding_days=1, source_kind=source_kind)
    evaluation = ResearchEvaluation(context).portfolio(scheme, nav, trades=result.trades, positions=result.positions)
    metrics = {name: metric.value for name, metric in evaluation.metrics.items()}
    metrics.update(average_exposure=float(nav.exposure.mean()), average_concentration_hhi=float(nav.concentration_hhi.mean()),
        maximum_security_weight=float(nav.maximum_security_weight.max()), rejection_count=len(result.rejections),
        rejection_reasons=result.rejections.reason.value_counts().to_dict(),
        mean_absolute_target_deviation=float(result.target_comparison.weight_deviation.abs().mean()),
        maximum_absolute_target_deviation=float(result.target_comparison.weight_deviation.abs().max()),
        fees={name: float(result.trades[name].sum()) for name in ['commission', 'stamp_duty', 'transfer_fee', 'side_fee', 'slippage', 'transaction_cost']},
        market_index_status='UNAVAILABLE', total_return_status='TOTAL_RETURN_UNAVAILABLE', total_return=None)
    return nav, evaluation, metrics
