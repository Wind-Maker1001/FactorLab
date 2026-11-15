"""Frozen R09 matrix; existing Paper execution, FIFO and funding only."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import json
from pathlib import Path

import pandas as pd

from factorlab.experiments.grid_independent import load_design, load_sample
from factorlab.experiments.grid_temporal_stability import load_inputs
from factorlab.experiments.grid_paper import run_experiment, D, _write
from factorlab.experiments.grid_fifo import attribution
from factorlab.experiments.grid_funding import settlements, analyze_account
from factorlab.experiments.grid_exposure_benchmark import stats
from factorlab.trading.contracts import Market


def run_account(task):
    root, repository, window, cost, rule, bars, identity, events = task
    prereg, lifecycle, config, rules, _ = load_design(repository)
    fee, slip = prereg['cost_scenarios_bps'][cost]
    config = replace(config, mode='geometric', breakout='pause', fee_bps=fee,
        slippage_bps=slip, expiry_reseed_limit=lifecycle['expiry_reseed_limit'],
        expiry_reseed_interval=lifecycle['expiry_reseed_interval'])
    directory = root/window/cost/rule
    run_experiment(directory, bars=bars, config=config, rules=rules,
        capital=1000, fee_rate=D(fee)/10000, slippage=D(slip)/10000,
        participation=D(prereg['risk']['participation']), interval='1h',
        market=Market.BINANCE_USDM, instrument_id=identity['instrument_id'],
        data_identity=identity, account_id=f'r09-{window}-{cost}-{rule}',
        max_exit_quantity=D(prereg['risk']['max_order_base_units']),
        inventory_risk=None if rule=='baseline' else rule)
    return summarize(directory, window, cost, rule, bars, events)


def summarize(directory, window, cost, rule, bars, events, *, funding_directory=None):
    fifo = attribution(directory)
    _write(directory/'fifo.json', fifo)
    end = (pd.Timestamp(bars[-1]['time']) + pd.Timedelta(hours=1)).to_pydatetime()
    overlay = analyze_account(directory, events, funding_directory or directory/'funding', end=end)
    nav, trades = pd.read_csv(directory/'daily.csv'), pd.read_csv(directory/'trades.csv')
    result = json.loads((directory/'result.json').read_text())
    economics = fifo['summary']
    # Every matched fragment and right-censored remainder uses its true fill time.
    by_id = {r.event_id:r for r in trades.itertuples()}
    holding = []
    for match in fifo['matches']:
        hours = (pd.Timestamp(by_id[match['sell_event_id']].event_time)
                 - pd.Timestamp(by_id[match['buy_event_id']].event_time)).total_seconds()/3600
        holding.append((float(match['quantity']), hours))
    cutoff = end - pd.Timedelta(microseconds=1)
    for lot in fifo['lots']:
        if lot['remaining']:
            hours = (cutoff - pd.Timestamp(by_id[lot['event_id']].event_time)).total_seconds()/3600
            holding.append((float(lot['remaining']), hours))
    exits = trades.loc[trades.order_id.map(lambda k: result['orders'][k]['purpose']=='RISK_EXIT').astype(bool)]
    triggers = [r for r in result['risk'] if r.get('state')=='EXITING'
                and r.get('reason') in {'INVENTORY_AGE', 'LOSS_BUDGET'}]
    completion = None
    if triggers and nav.inventory.iloc[-1] == 0:
        trigger = pd.Timestamp(triggers[0]['time'])
        flat = nav[(pd.to_datetime(nav.date, utc=True)>=trigger) & nav.inventory.eq(0)]
        if len(flat):
            completion = (pd.Timestamp(flat.date.iloc[0])-trigger).total_seconds()/3600
    row = dict(window=window, cost=cost, rule=rule, **stats(nav,trades),
        **{k:float(v) for k,v in overlay.items() if v!=''},
        completed_cycles=economics['grid_cycles'],
        completed_cycle_net=float(economics['completed_cycle']['net']),
        risk_exit_net=float(economics['risk_exit']['net']),
        normal_sell_net=float(economics['normal_sell']['net']),
        residual_net=float(economics['residual']['net']),
        fifo_reconciliation_residual=float(economics['reconciliation_residual']),
        reported_equity_difference=float(economics['reported_equity_difference']),
        max_cash_equity_residual=float((nav.equity-nav.cash-nav.unrealized).abs().max()),
        max_holding_hours=max((h for q,h in holding), default=0),
        mean_holding_hours=sum(q*h for q,h in holding)/sum(q for q,h in holding) if holding else 0,
        final_inventory=float(nav.inventory.iloc[-1]),
        final_cash=float(nav.cash.iloc[-1]), final_state=nav.state.iloc[-1],
        exit_count=len(triggers), exit_completion_hours=completion,
        incomplete_exit=bool(triggers and nav.inventory.iloc[-1]),
        exit_fees=float(exits.fee.sum()), exit_slippage=float(exits.slippage.sum()),
        exit_notional=float(exits.trade_notional.sum()))
    assert abs(row['fifo_reconciliation_residual']) < 1e-8
    assert abs(row['reported_equity_difference']) < 1e-8
    assert (nav.equity-nav.cash-nav.unrealized).abs().max() < 1e-8
    _write(directory/'metrics.json', row)
    print(f'{window}/{cost}/{rule}: adjusted={row["adjusted_profit"]:.9f}', flush=True)
    return row


def main(args):
    root = Path(args.output)
    if not args.summarize_only and not args.only:
        root.mkdir(parents=True, exist_ok=False)
    old, old_id = load_sample(args.old_snapshot)
    new, new_id, new_events = load_inputs(args.new_snapshot, args.new_funding)
    old_events = settlements(json.loads(Path(args.old_funding).read_text()))
    tasks = [(root, args.repository, window, cost, rule, bars, identity, events)
        for window,bars,identity,events in [('R04B',old,old_id,old_events),('R07',new,new_id,new_events)]
        for cost in ('base','stress') for rule in ('baseline','age','loss','cap')]
    if args.only:
        tasks = [task for task in tasks if '/'.join(task[2:5]) in args.only]
    if args.summarize_only:
        rows = [summarize(root/window/cost/rule, window, cost, rule, bars, events,
                          funding_directory=root/window/cost/rule/'funding-final')
                for _,_,window,cost,rule,bars,identity,events in tasks]
    else:
        if not args.only:
            _write(root/'design.json', dict(rules=['baseline','age100_exit200','loss_0.005_equity_realized','cap_0.2'],
                accounts=16, old=old_id, new=new_id, funding='POSTHOC_NO_CASH_FEEDBACK'))
        with ProcessPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(run_account,tasks))
    pd.DataFrame(rows).to_csv(root/'metrics.csv',index=False)


if __name__=='__main__':
    parser = argparse.ArgumentParser(__doc__)
    for name in ('repository','output','old-snapshot','new-snapshot','old-funding','new-funding'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--summarize-only', action='store_true',
                        help='attribute already completed accounts without executing trades')
    parser.add_argument('--only', action='append', help='execute specified window/cost/rule in an existing study root')
    main(parser.parse_args())
