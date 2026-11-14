"""Publish compact R07 evidence; large account exports stay outside Git."""
import argparse
import json
from pathlib import Path
import shutil

import pandas as pd

from factorlab.experiments.grid_temporal_stability import (
    load_inputs, control_funding, START, END, ARMS,
)
from factorlab.experiments.grid_funding import settlements
from factorlab.experiments.grid_paper import _write


def publish(args):
    evidence, dest = Path(args.evidence), Path(args.output)
    dest.mkdir(parents=True, exist_ok=False)
    bars, identity, events = load_inputs(args.snapshot, args.funding)
    root = Path(args.funding).parent
    manifest = json.loads((root/'manifest.json').read_text())
    receipts = [r for r in manifest['raw_receipts'] if r['endpoint']=='fundingRate']
    intervals = sorted((r['params']['startTime'], r['params']['endTime']+1) for r in receipts if r['status']==200)
    covered = START.value//1000000
    for start,end in intervals:
        if start <= covered:
            covered = max(covered,end)
    if covered < END.value//1000000:
        raise ValueError('funding requests do not cover the research window')
    raw = {}
    for r in receipts:
        payload = json.loads((root/r['raw_path']).read_text())
        for event in payload:
            key = (event['fundingTime'],event.get('rateType'))
            if key in raw and raw[key] != event:
                raise ValueError('conflicting raw funding event')
            raw[key] = event
    raw_events = settlements(list(raw.values()),START.to_pydatetime(),END.to_pydatetime())
    if raw_events != events:
        raise ValueError('projected funding differs from saved API events')
    _write(dest/'coverage.json', dict(hourly=identity, funding_rows=len(events),
        projected_equals_raw=True,request_intervals_ms=intervals,request_window_covered=True,
        funding_source=str(root),funding_receipts=receipts,
        mark_source='historical markPrice attached to each fundingRate event; not premiumIndex snapshot',
        prior_window_use='R02 data-identity uses this exact snapshot and window',
        boundaries=['NO LIVE MONEY','EXPLORATORY_RESEARCH']))
    for name in ('metrics.csv','comparisons.csv','posthoc.csv','cross-window.csv','design.json','result.json','account-summary.csv'):
        shutil.copyfile(evidence/name,dest/name)
    shutil.copyfile(Path(args.tests),dest/'targeted-tests.xml')
    # Apply funding only to saved original-window control ledgers, never rerun them.
    old_events = settlements(json.loads(Path(args.old_funding).read_text()))
    old_rows = []
    for scenario in ('base','stress'):
        for name in ['fixed_012','fixed_013']+['lag_'+a+'_'+m for a,m in ARMS]:
            source = Path(args.old_controls)/scenario/name
            nav, fills = pd.read_csv(source/'hourly.csv'),pd.read_csv(source/'fills.csv')
            old_rows.append(dict(scenario=scenario,name=name,
                original_profit=nav.equity.iloc[-1]-1000,
                **control_funding(nav,fills,old_events,evidence/'original-control-funding'/scenario/name)))
    pd.DataFrame(old_rows).to_csv(dest/'original-control-funding.csv',index=False)
    old_grid = pd.read_csv(Path(args.repository)/'docs/validation/p7-r05/summary.csv')
    old_grid = old_grid[old_grid.account.isin(['B/geometric','D/geometric','D/atr'])]
    old_grid.to_csv(dest/'original-grid-funding.csv',index=False)
    cross = pd.read_csv(dest/'cross-window.csv')
    for row in old_grid.itertuples():
        mask = (cross.window=='R04B') & (cross.scenario==row.cost) & (cross.name==row.account.replace('/','_'))
        cross.loc[mask,'funding'] = row.funding
        cross.loc[mask,'adjusted_profit'] = row.adjusted_profit
        cross.loc[mask,'completed_cycle_adjusted'] = cross.loc[mask,'completed_cycle_net']+row.funding_completed_cycle
        cross.loc[mask,'adjusted_hourly_drawdown_pct'] = row.adjusted_hourly_drawdown_pct
    cross.to_csv(dest/'cross-window.csv',index=False)
    checks = []
    for scenario in ('base','stress'):
        for arm,mode in ARMS:
            source = evidence/'accounts'/scenario/arm/mode
            result = json.loads((source/'result.json').read_text())
            original = json.loads((Path(args.old_grid)/scenario/arm/mode/'result.json').read_text())
            if any(result[key] != original[key] for key in ('config','rules','execution')):
                raise ValueError('frozen settings differ from original R04B account')
            nav = pd.read_csv(source/'daily.csv')
            trades = pd.read_csv(source/'trades.csv')
            if (result['config']['initial_inventory'] != 0 or float(result['capital']) != 1000
                    or nav.inventory.iloc[0] != 0 or nav.equity.iloc[0] != 1000
                    or (len(trades) and pd.to_datetime(trades.event_time,utc=True).min() <= START)):
                raise ValueError('fresh-account or first-bar causality violation')
            if mode == 'atr' and len(trades) and pd.to_datetime(trades.event_time,utc=True).min() < START+pd.Timedelta(hours=72):
                raise ValueError('ATR traded before original completed-bar warmup')
            fifo = json.loads((source/'fifo.json').read_text())['summary']
            checks.append(dict(scenario=scenario,arm=arm,mode=mode,initial_cash=1000,
                               initial_inventory=0,first_hour_equity=float(nav.equity.iloc[0]),
                               fifo_residual=fifo['reconciliation_residual'],
                               reported_nav_difference=fifo['reported_equity_difference'],
                               final_state=result['final']['state'],
                               configuration_rules_execution_equal_r04b=True))
    _write(dest/'accounting-checks.json',checks)
    frame = pd.read_csv(dest/'metrics.csv')
    cols = ['scenario','name','net_pnl','funding','adjusted_profit','max_drawdown',
            'adjusted_hourly_drawdown_pct','mean_notional','max_notional','max_committed',
            'fills','turnover_usdt','fees','slippage','completed_cycles','completed_cycle_net',
            'funding_completed_cycle','completed_cycle_adjusted','risk_exit_net','funding_risk_exit',
            'final_inventory','residual_net','funding_residual']
    (dest/'tables.md').write_text('# R07 economic metrics\n\nNO LIVE MONEY · EXPLORATORY_RESEARCH\n\n'
        +frame[cols].to_markdown(index=False,floatfmt='.6f')+'\n\n'
        +pd.read_csv(dest/'comparisons.csv').to_markdown(index=False,floatfmt='.6f')+'\n',encoding='utf-8')
    print(frame[['scenario','name','net_pnl','funding','adjusted_profit','completed_cycle_net',
                 'completed_cycle_adjusted','risk_exit_net','residual_net','mean_notional','max_drawdown']].to_string(index=False))
    print(pd.read_csv(dest/'comparisons.csv').to_string(index=False))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(__doc__)
    for key in ('evidence','output','snapshot','funding','tests','old-funding','old-controls','old-grid','repository'):
        parser.add_argument('--'+key,required=True)
    publish(parser.parse_args())
