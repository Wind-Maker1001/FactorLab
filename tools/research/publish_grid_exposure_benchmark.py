"""Publish bounded R06 evidence tables and accounting receipts, without replay."""
import argparse
import json
from pathlib import Path
import shutil

import pandas as pd

from factorlab.experiments.grid_exposure_benchmark import conclusions


def publish(evidence, output):
    evidence, output = Path(evidence), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    metrics = pd.read_csv(evidence/'metrics.csv')
    checks = {}
    controls = metrics.loc[metrics.kind.ne('EXISTING_R04B') & metrics.status.eq('OK')]
    discrepancy = float((controls.net_pnl-controls.sold_fifo_net-controls.residual_net).abs().max())
    if discrepancy > 1e-8:
        raise ValueError('benchmark FIFO does not reconcile')
    checks['max_benchmark_fifo_nav_difference'] = discrepancy
    checks['fixed_reference'] = {}
    for scenario in ('base','stress'):
        m = metrics.loc[metrics.scenario.eq(scenario)].set_index('name')
        checks['fixed_reference'][scenario] = {}
        for key in ('net_pnl','max_drawdown','mean_notional','max_notional','max_committed',
                    'holding_fraction','fills','turnover_usdt','total_cost'):
            difference = float(m.loc['fixed_100',key]-m.loc['fixed_budget_buy_hold_arithmetic',key])
            if abs(difference) > 1e-8:
                raise ValueError(f'reused Buy & Hold mismatch: {scenario}/{key}')
            checks['fixed_reference'][scenario][key] = difference
    (output/'accounting-checks.json').write_text(json.dumps(checks,indent=2)+'\n',encoding='utf-8')
    for name in ('design.json','metrics.csv','comparisons.csv','posthoc.csv'):
        shutil.copyfile(evidence/name,output/name)
    result = conclusions(metrics.to_dict('records'),
                         pd.read_csv(evidence/'comparisons.csv').to_dict('records'))
    (output/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    columns = ['scenario','name','net_pnl','net_return','max_drawdown','mean_notional',
               'max_notional','max_committed','holding_fraction','fills','turnover_usdt',
               'total_cost','completed_cycle_net','residual_net']
    selected = metrics.loc[metrics.kind.eq('EXISTING_R04B') | metrics.name.isin([
        'fixed_012','fixed_013','lag_B_geometric','lag_D_geometric'])]
    def table(frame):
        header = '| '+' | '.join(frame.columns)+' |\n'
        header += '| '+' | '.join(['---']*len(frame.columns))+' |\n'
        for row in frame.itertuples(index=False,name=None):
            header += '| '+' | '.join('—' if pd.isna(v) else f'{v:.6f}' if isinstance(v,float) else str(v) for v in row)+' |\n'
        return header
    (output/'tables.md').write_text('# R06 收益与风险表\n\n'
        'NO LIVE MONEY · EXPLORATORY_RESEARCH\n\n'
        '金额 USDT；收益率/回撤/持仓比例为小数；全部 100 个固定目标、所有动态对照及风险统计见 metrics.csv。\n\n'
        +table(selected[columns])+'\n## 同口径收益差\n\n'
        +table(pd.read_csv(evidence/'comparisons.csv')),encoding='utf-8')
    print(json.dumps(checks,indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--evidence',required=True)
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    publish(args.evidence,args.output)
