"""Frozen temporal replication using existing Paper execution and R06 controls."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import get_context
from pathlib import Path
import json

import numpy as np
import pandas as pd

from factorlab.experiments.grid_empirical import load_hourly
from factorlab.experiments.grid_independent import load_design, _execute_account
from factorlab.experiments.grid_exposure_benchmark import simulate, stats, fifo_control
from factorlab.experiments.grid_funding import settlements, analyze_account, cashflow, drawdown, timestamp
from factorlab.experiments.grid_paper import D, _write

START = pd.Timestamp('2026-08-12', tz='UTC')
END = pd.Timestamp('2026-11-10', tz='UTC')
ARMS = [('B', 'geometric'), ('D', 'geometric'), ('D', 'atr')]
BOUNDARIES = ['NO LIVE MONEY', 'EXPLORATORY_RESEARCH']


def load_inputs(snapshot, funding):
    bars, identity = load_hourly(snapshot)
    if (len(bars) != 2160 or pd.Timestamp(identity['start']) != START
            or pd.Timestamp(identity['end_exclusive']) != END):
        raise ValueError('R07 requires the fixed complete 90-day window')
    frame = pd.read_parquet(funding)
    if (frame.isna().any().any() or not frame.funding_time.is_monotonic_increasing
            or not frame.funding_rate.map(lambda x: D(x).is_finite()).all()
            or not frame.mark_price.map(lambda x: D(x).is_finite() and D(x) > 0).all()):
        raise ValueError('invalid funding time, rate or historical mark price')
    rows = [dict(symbol='NVDAUSDT', fundingTime=int(r.funding_time.value // 1000000),
                 fundingRate=str(r.funding_rate), markPrice=str(r.mark_price), rateType=r.rate_type)
            for r in frame.itertuples()]
    events = settlements(rows, START.to_pydatetime(), END.to_pydatetime())
    if not events:
        raise ValueError('observed funding events required')
    identity.update(study='P7-R07', generation=Path(snapshot).name)
    return bars, identity, events


def observed_times(values):
    return pd.to_datetime(values, utc=True) + pd.Timedelta(hours=1) - pd.Timedelta(microseconds=1)


def control_funding(nav, fills, events, directory):
    """Use completed-hour executions, not the exported bar-open labels."""
    fill_times = observed_times(fills.execution_date)
    quantities = [D(r.executed_shares) * (1 if r.side == 'BUY' else -1)
                  for r in fills.itertuples()]
    flows = []
    for time, price, rate, kind in events:
        position = sum((q for t, q in zip(fill_times, quantities) if t < time), D(0))
        flows.append(dict(time=time.isoformat(), quantity=position, mark_price=price,
                          rate=rate, rate_type=kind, funding=cashflow(position, price, rate)))
    times = observed_times(nav.date)
    adjusted = []
    for row, time in zip(nav.itertuples(), times):
        inventory = sum((q for t,q in zip(fill_times, quantities) if t <= time), D(0))
        if float(inventory) != float(row.inventory):
            raise ValueError('control funding position differs from hourly inventory')
        amount = sum((r['funding'] for r in flows if timestamp(r['time']) <= time), D(0))
        adjusted.append(dict(time=time.isoformat(), original_nav=D(row.equity),
                             cumulative_funding=amount, research_nav=D(row.equity)+amount))
    total = sum((r['funding'] for r in flows), D(0))
    directory.mkdir(parents=True)
    pd.DataFrame(flows).to_csv(directory/'settlements.csv', index=False)
    pd.DataFrame(adjusted).to_csv(directory/'nav.csv', index=False)
    return dict(funding=float(total), adjusted_profit=float(D(nav.equity.iloc[-1])-1000+total),
                adjusted_hourly_drawdown_pct=float(drawdown([r['research_nav'] for r in adjusted])))


def record_control(name, scenario, nav, fills, events, output):
    directory = output/scenario/name
    directory.mkdir(parents=True)
    nav.to_csv(directory/'hourly.csv', index=False)
    fills.to_csv(directory/'fills.csv', index=False)
    s = stats(nav, fills)
    fifo = fifo_control(fills, nav.close.iloc[-1])
    if abs(fifo['sold_fifo_net']+fifo['residual_net']-s['net_pnl']) > 1e-8:
        raise ValueError('control FIFO/NAV mismatch')
    return dict(name=name, scenario=scenario, kind='CONTROL', **s, **fifo,
                **control_funding(nav, fills, events, directory/'funding'))


def summarize_grids(output, events):
    rows, posthoc = [], []
    for scenario in ('base', 'stress'):
        for arm, mode in ARMS:
            source = output/'accounts'/scenario/arm/mode
            nav, fills = pd.read_csv(source/'daily.csv'), pd.read_csv(source/'trades.csv')
            fifo = json.loads((source/'fifo.json').read_text())['summary']
            overlay = analyze_account(source, events, output/'funding'/scenario/arm/mode,
                                      end=END.to_pydatetime())
            s = stats(nav, fills)
            row = dict(name=f'{arm}_{mode}', scenario=scenario, kind='GRID', **s,
                       **{k:float(v) for k,v in overlay.items() if v != ''},
                       completed_cycles=fifo['grid_cycles'],
                       completed_cycle_net=float(fifo['completed_cycle']['net']),
                       completed_cycle_adjusted=float(fifo['completed_cycle']['net'])+float(overlay['funding_completed_cycle']),
                       risk_exit_net=float(fifo['risk_exit']['net']),
                       residual_net=float(fifo['residual']['net']), final_inventory=float(nav.inventory.iloc[-1]))
            rows.append(row)
            carry = float(np.sum(nav.inventory.to_numpy()[:-1]*np.diff(nav.close.to_numpy())))
            posthoc.append(dict(scenario=scenario, name=row['name'], prior_inventory_price_pnl=carry,
                                execution_reference_gross=s['net_pnl']-carry+s['total_cost'],
                                fees=s['fees'], slippage=s['slippage'], net_pnl=s['net_pnl']))
    return rows, posthoc


def finish(output, bars, events, old):
    rows, posthoc = summarize_grids(output, events)
    comparisons = []
    for scenario, fee, slip in [('base',4,6), ('stress',8,12)]:
        controls = []
        for name, q in [('fixed_012',D('.12')), ('fixed_013',D('.13')), ('cash',D(0))]:
            nav, fills = simulate(bars, quantity=q, fee_bps=fee, slip_bps=slip)
            controls.append(record_control(name, scenario, nav, fills, events, output/'controls'))
        for grid in [r for r in rows if r['scenario'] == scenario]:
            source = output/'accounts'/scenario/grid['name'].replace('_','/')
            targets = pd.read_csv(source/'daily.csv').inventory.tolist()
            nav, fills = simulate(bars, targets=targets, fee_bps=fee, slip_bps=slip)
            lag = record_control('lag_'+grid['name'], scenario, nav, fills, events, output/'controls')
            for control in controls+[lag]:
                comparisons.append(dict(scenario=scenario, grid=grid['name'], benchmark=control['name'],
                    raw_advantage=grid['net_pnl']-control['net_pnl'],
                    adjusted_advantage=grid['adjusted_profit']-control['adjusted_profit'],
                    grid_mean=grid['mean_notional'], benchmark_mean=control['mean_notional'],
                    mean_exposure_difference=grid['mean_notional']-control['mean_notional']))
            rows.append(lag)
        rows.extend(controls)
    pd.DataFrame(rows).to_csv(output/'metrics.csv', index=False)
    pd.DataFrame(comparisons).to_csv(output/'comparisons.csv', index=False)
    pd.DataFrame(posthoc).to_csv(output/'posthoc.csv', index=False)
    # Existing R04B exports only: no account execution in the original window.
    old_rows = []
    for scenario in ('base','stress'):
        for arm, mode in ARMS:
            source = old/scenario/arm/mode
            nav, fills = pd.read_csv(source/'daily.csv'), pd.read_csv(source/'trades.csv')
            fifo = json.loads((source/'fifo.json').read_text())['summary']
            old_rows.append(dict(window='R04B', scenario=scenario, name=f'{arm}_{mode}',
                                **stats(nav,fills), completed_cycles=fifo['grid_cycles'],
                                completed_cycle_net=float(fifo['completed_cycle']['net']),
                                risk_exit_net=float(fifo['risk_exit']['net']), residual_net=float(fifo['residual']['net'])))
    pd.DataFrame(old_rows + [dict(window='R07', **r) for r in rows if r['kind']=='GRID']).to_csv(output/'cross-window.csv',index=False)
    geometric = [r for r in rows if r['kind']=='GRID' and r['name'].endswith('geometric')]
    positive_closed = all(r['completed_cycle_adjusted'] > 0 for r in geometric)
    positive_accounts = all(r['adjusted_profit'] > 0 for r in geometric)
    status = ('TEMPORAL_REPLICATION_SUPPORTED' if positive_closed and positive_accounts
              else 'TEMPORAL_REPLICATION_MIXED' if positive_closed or positive_accounts
              else 'TEMPORAL_REPLICATION_NOT_SUPPORTED')
    _write(output/'result.json', dict(status=status, boundaries=BOUNDARIES,
            positive_funded_closed_cycles_both_costs=positive_closed,
            positive_funded_accounts_both_costs=positive_accounts,
            funding='POSTHOC_NO_CASH_CONSTRAINT_FEEDBACK',
            sample='PREVIOUSLY_USED_R02_WINDOW_NOT_STRICT_OOS', accounts=6))


def run(args):
    output = Path(args.output)
    bars, identity, events = load_inputs(args.snapshot, args.funding)
    prereg, lifecycle, config, rules, hashes = load_design(args.repository)
    output.mkdir(parents=True, exist_ok=False)
    inputs = dict(bars=bars, identity=identity, prereg=prereg, lifecycle=lifecycle,
                  config=config, rules=rules, hashes=hashes)
    regular = [t for t,_,_,kind in events if kind == 'Regular']
    _write(output/'design.json', dict(study='P7-R07', boundaries=BOUNDARIES, accounts=ARMS,
        costs=prereg['cost_scenarios_bps'], config=config.__dict__, rules=rules.__dict__,
        lifecycle=lifecycle, initial_cash=1000, initial_inventory=0, initial_orders=0, initial_fifo=[],
        atr_start='original empty indicator state, 72 completed in-window bars', identity=identity,
        funding_rows=len(events), first_funding=events[0][0].isoformat(), last_funding=events[-1][0].isoformat(),
        funding_types={k:sum(e[3]==k for e in events) for k in set(e[3] for e in events)},
        regular_gap_hours=sorted(set((b-a).total_seconds()/3600 for a,b in zip(regular,regular[1:])))))
    matrix = [(s,a,m) for s in ('base','stress') for a,m in ARMS]
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=get_context('spawn')) as pool:
        pending = {pool.submit(_execute_account, output/'accounts', *entry, **inputs):entry for entry in matrix}
        results = []
        for future in as_completed(pending):
            row, _ = future.result()
            results.append(row)
            print(f'completed {pending[future]} equity={row["final_equity"]}', flush=True)
    pd.DataFrame(results).to_csv(output/'account-summary.csv',index=False)
    finish(output, bars, events, Path(args.old))
    print('R07 economics complete', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(__doc__)
    for key in ('snapshot','funding','repository','output','old'):
        parser.add_argument('--'+key, required=True)
    parser.add_argument('--workers', type=int, default=4, choices=range(1,5))
    run(parser.parse_args())
