"""Attribute existing conditional accounts without replaying execution."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from factorlab.evaluation.return_attribution import attribute_execution, COST_COLUMNS

SCENARIOS = dict(A='LAST_CLOSE_CARRY_SENSITIVITY',
    B1='EXTERNAL_FUND_MARK_SENSITIVITY_HINDSIGHT',
    B2='EXTERNAL_FUND_MARK_SENSITIVITY_PUBLICATION_BOUNDED', C='ZERO_MARK_STRESS')
LABELS = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED',
          'SCENARIO_CONDITIONAL_NOT_CERTIFIED', 'NOT_HISTORICAL_VALUATION_CERTIFIED']
START = pd.Timestamp('2023-05-18')


def run(root, output):
    output.mkdir(parents=True, exist_ok=True)
    daily_all, securities, summaries, residuals = [], [], {}, {}
    tables = {k: [] for k in ('trades', 'rejections', 'target_comparison', 'positions')}
    for label, directory in SCENARIOS.items():
        frames = {name: pd.read_parquet(root / directory / f'fold-0-{name}.parquet')
                  for name in ('nav', 'positions', 'trades', 'rejections', 'target_comparison')}
        account = SimpleNamespace(**frames)
        evaluated = attribute_execution(account, initial_cash=1_000_000,
                                        calendar=pd.to_datetime(account.nav.date))
        d = evaluated.daily.set_index('date')
        positions = frames['positions']
        values = positions.pivot(index='date', columns='symbol', values='market_value').fillna(0).reindex(d.index, fill_value=0)
        trades = frames['trades'].assign(net_purchase=lambda x: np.where(x.side.eq('buy'), 1, -1) * x.trade_notional)
        purchases = trades.pivot_table(index='execution_date', columns='symbol', values='net_purchase', aggfunc='sum', fill_value=0).reindex(index=d.index, columns=values.columns, fill_value=0)
        pnl = values.diff().fillna(values) - purchases
        d['mark_000540_pnl'] = pnl['000540.SZ']
        d['other_holdings_gross_pnl'] = pnl.drop(columns='000540.SZ').sum(axis=1)
        d['fees_ex_slippage'] = d[['commission', 'stamp_duty', 'transfer_fee', 'side_fee']].sum(axis=1)
        d['attributed_pnl'] = d.mark_000540_pnl + d.other_holdings_gross_pnl - d.fees_ex_slippage - d.slippage
        d['residual'] = d.net_pnl - d.attributed_pnl
        np.testing.assert_allclose(d.attributed_pnl, d.net_pnl, atol=1e-7, rtol=0)
        for name, frame in [('fills', frames['trades']), ('rejections', frames['rejections'])]:
            d[name] = frame.groupby('execution_date').size().reindex(d.index, fill_value=0)
        keys = ['signal_date', 'execution_date', 'symbol', 'side']
        order_rows = pd.concat([frames['trades'][keys + ['requested_shares']],
                                frames['rejections'][keys + ['requested_shares']]])
        orders = order_rows.drop_duplicates(keys)
        d['attempted_orders'] = orders.groupby('execution_date').size().reindex(d.index, fill_value=0)
        d['cash_constraint_records'] = frames['rejections'].loc[frames['rejections'].reason.eq('cash_constraint')].groupby('execution_date').size().reindex(d.index, fill_value=0)
        orders = orders.loc[orders.execution_date.gt(START)].copy()
        orders.insert(0, 'scenario', label)
        orders.to_csv(output / f'orders-{label}.csv', index=False)
        d['cash_change'] = d.cash.diff().fillna(d.cash - 1_000_000)
        d['capital_utilization'] = 1 - d.cash_weight
        d['max_security_weight'] = positions.groupby('date').weight.max().reindex(d.index)
        d['conditional_nav'] = d.portfolio_value / d.loc[START, 'portfolio_value']
        window = d.loc[START:].copy()
        window['drawdown'] = window.conditional_nav / window.conditional_nav.cummax() - 1
        window['scenario'] = label
        daily_all.append(window.reset_index())
        security = pnl.loc[pnl.index > START].stack().rename('gross_pnl').reset_index()
        security['scenario'] = label
        securities.append(security)
        for name in tables:
            frame = frames[name].copy()
            date_col = 'date' if name == 'positions' else 'execution_date'
            frame = frame.loc[frame[date_col].gt(START)]
            frame.insert(0, 'scenario', label)
            tables[name].append(frame)
        w = window.loc[window.index > START]
        costs = w[list(COST_COLUMNS)].sum()
        summaries[label] = dict(end_nav=float(w.portfolio_value.iloc[-1]),
            conditional_return=float(w.conditional_nav.iloc[-1] - 1),
            max_drawdown=float(window.drawdown.min()), daily_volatility=float(w.net_return.std(ddof=1)),
            annualized_volatility_252=float(w.net_return.std(ddof=1)*np.sqrt(252)), sessions=len(w),
            gross_security_turnover=float(w.gross_security_turnover.sum()),
            mean_daily_gross_turnover=float(w.gross_security_turnover.mean()),
            mean_hhi=float(w.close_security_hhi.mean()), max_security_weight=float(w.max_security_weight.max()),
            mean_capital_utilization=float(w.capital_utilization.mean()),
            end_cash=float(w.cash.iloc[-1]), cash_change=float(w.cash_change.sum()),
            mark_000540_pnl=float(w.mark_000540_pnl.sum()),
            other_holdings_gross_pnl=float(w.other_holdings_gross_pnl.sum()),
            costs={k: float(v) for k,v in costs.items()})
        residuals[label] = float(d.residual.abs().max())
    daily = pd.concat(daily_all, ignore_index=True)
    daily.to_csv(output / 'daily-risk-attribution.csv', index=False)
    pd.concat(securities, ignore_index=True).to_csv(output / 'security-pnl.csv', index=False)
    for name, frames in tables.items():
        pd.concat(frames, ignore_index=True).to_csv(output / f'{name}.csv', index=False)
    pairs = []
    additive = ['mark_000540_pnl', 'other_holdings_gross_pnl', 'fees_ex_slippage', 'slippage']
    for left, right in [('B1','A'), ('B2','A'), ('C','A'), ('B2','B1')]:
        l = daily.loc[daily.scenario.eq(left)].set_index('date')
        r = daily.loc[daily.scenario.eq(right)].set_index('date')
        pair = l[additive] - r[additive]
        pair['nav_difference'] = l.portfolio_value - r.portfolio_value
        pair['cash_difference'] = l.cash - r.cash
        pair['attributed_daily_difference'] = pair.mark_000540_pnl + pair.other_holdings_gross_pnl - pair.fees_ex_slippage - pair.slippage
        pair.loc[START, 'attributed_daily_difference'] = 0
        pair['cumulative_attribution'] = pair.attributed_daily_difference.cumsum()
        pair['residual'] = pair.nav_difference - pair.cumulative_attribution
        np.testing.assert_allclose(pair.nav_difference, pair.cumulative_attribution, atol=1e-7, rtol=0)
        pair['pair'] = left + '-' + right
        pairs.append(pair.reset_index())
    pd.concat(pairs, ignore_index=True).to_csv(output / 'paired-attribution.csv', index=False)
    summary = dict(labels=LABELS, baseline='633308b6b169279a7aaeafdd0ed9987640222b7c',
        input=str(root), period=['2023-05-19','2023-06-14'], anchor='2023-05-18', scenarios=summaries)
    (output / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    checks = dict(labels=LABELS, full_ledger_existing_evaluator_passed=True,
        daily_security_cash_cost_identity=True, pairwise_cumulative_nav_identity=True,
        tolerance_yuan=1e-7, max_daily_residual_yuan=residuals,
        max_pair_residual_yuan=float(pd.concat(pairs).residual.abs().max()))
    (output / 'verification.json').write_text(json.dumps(checks, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(summary, indent=2))
    for label in ('B1','B2'):
        print(label, daily.loc[daily.scenario.eq(label) & daily.date.eq('2023-05-22'),
            ['portfolio_value','cash','turnover_denominator','buy_notional','sell_notional']].to_string(index=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.input, args.output)
