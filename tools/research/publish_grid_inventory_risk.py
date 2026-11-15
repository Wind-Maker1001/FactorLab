"""Publish compact R09 economics and compare frozen existing controls."""
import argparse
import json
from pathlib import Path
import shutil

import pandas as pd
from factorlab.experiments.grid_paper import _write


def publish(root, repository, output, old_grid, new_grid):
    frame = pd.read_csv(root/'metrics.csv')
    output.mkdir(parents=True, exist_ok=False)
    controls = pd.read_csv(repository/'docs/validation/p7-r07/original-control-funding.csv')
    controls = controls[controls.name.isin(['fixed_012','fixed_013','lag_B_geometric'])].copy()
    controls['window'] = 'R04B'
    newer = pd.read_csv(repository/'docs/validation/p7-r07/metrics.csv')
    newer = newer[newer.name.isin(['cash','fixed_012','fixed_013','lag_B_geometric'])].copy()
    newer['window'] = 'R07'
    benchmarks = pd.concat([controls, newer], ignore_index=True)
    comparisons = []
    checks = []
    for row in frame.itertuples():
        directory = root/row.window/row.cost/row.rule
        nav = pd.read_csv(directory/'daily.csv')
        trades = pd.read_csv(directory/'trades.csv')
        fifo = json.loads((directory/'fifo.json').read_text())
        for name in ('inventory-risk.json',):
            source = directory/name
            if source.exists():
                shutil.copyfile(source,output/f'{row.window}-{row.cost}-{row.rule}-{name}')
        lots = pd.DataFrame(fifo['lots'])
        lots.to_csv(output/f'{row.window}-{row.cost}-{row.rule}-lots.csv',index=False)
        if row.rule == 'baseline':
            old = (old_grid if row.window=='R04B' else new_grid)/row.cost/'B/geometric'
            prior = pd.read_csv(old/'daily.csv')
            pd.testing.assert_frame_equal(nav,prior)
            prior_trades = pd.read_csv(old/'trades.csv')
            cols = [c for c in trades if c!='event_id']
            pd.testing.assert_frame_equal(trades[cols],prior_trades[cols])
            checks.append(dict(window=row.window,cost=row.cost,check='disabled_rule_matches_original_hourly_and_fills',passed=True))
        for control in benchmarks[(benchmarks.window==row.window)&(benchmarks.scenario==row.cost)].itertuples():
            comparisons.append(dict(window=row.window,cost=row.cost,rule=row.rule,benchmark=control.name,
                adjusted_advantage=row.adjusted_profit-control.adjusted_profit,
                adjusted_drawdown_difference_pct=row.adjusted_hourly_drawdown_pct-control.adjusted_hourly_drawdown_pct))
        if row.window=='R04B':
            comparisons.append(dict(window=row.window,cost=row.cost,rule=row.rule,benchmark='cash',
                adjusted_advantage=row.adjusted_profit,adjusted_drawdown_difference_pct=row.adjusted_hourly_drawdown_pct))
        # Publish authoritative hourly economics, but keep bulky SQLite journals local.
        nav[['date','close','equity','cash','inventory','committed','unrealized',
             'realized_gross','state','state_reason','drawdown','fee','slippage']].to_csv(
                 output/f'{row.window}-{row.cost}-{row.rule}-hourly.csv',index=False)
        trades.to_csv(output/f'{row.window}-{row.cost}-{row.rule}-fills.csv',index=False)
        pd.DataFrame(fifo['matches']).to_csv(output/f'{row.window}-{row.cost}-{row.rule}-matches.csv',index=False)
        for name in ('nav','settlements'):
            shutil.copyfile(directory/'funding'/f'{name}.csv',
                            output/f'{row.window}-{row.cost}-{row.rule}-funding-{name}.csv')
    frame.to_csv(output/'metrics.csv',index=False)
    pd.DataFrame(comparisons).to_csv(output/'comparisons.csv',index=False)
    benchmarks.to_csv(output/'existing-controls.csv',index=False)
    _write(output/'validation.json',dict(checks=checks,accounts=16,
        max_cash_equity_residual=float(frame.max_cash_equity_residual.max()),
        max_fifo_residual=float(frame.fifo_reconciliation_residual.abs().max()),
        max_reported_equity_difference=float(frame.reported_equity_difference.abs().max())))
    shutil.copyfile(root/'design.json',output/'design.json')
    print(frame[['window','cost','rule','adjusted_profit','adjusted_hourly_drawdown_pct','completed_cycles','residual_net','risk_exit_net','final_inventory']].to_string(index=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(__doc__)
    for key in ('root','repository','output','old-grid','new-grid'):
        parser.add_argument('--'+key,required=True,type=Path)
    args=parser.parse_args()
    publish(args.root,args.repository,args.output,args.old_grid,args.new_grid)
