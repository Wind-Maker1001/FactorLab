"""Predeclared lifecycle diagnosis on the reused hourly Paper sample."""
from collections import deque
from dataclasses import replace
import json
from pathlib import Path
from time import perf_counter

import pandas as pd

from factorlab.experiments.grid_empirical import load_hourly, summarize
from factorlab.experiments.grid_paper import D, _write, run_experiment
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.contracts import Market


def lifecycle_metrics(directory):
    directory = Path(directory)
    metrics = summarize(directory)
    nav = pd.read_csv(directory / 'daily.csv')
    trades = pd.read_csv(directory / 'trades.csv')
    result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
    lots = deque()
    ages = []
    cycles = exit_sells = 0
    end = pd.Timestamp(nav.date.iloc[-1]) + pd.Timedelta(hours=1)
    for trade in trades.itertuples():
        time = pd.Timestamp(trade.event_time)
        quantity = D(trade.executed_shares)
        if trade.side == 'BUY':
            lots.append([quantity, time, False])
        else:
            risk_exit = result['orders'][trade.order_id].get('purpose') == 'RISK_EXIT'
            exit_sells += int(risk_exit)
            while quantity:
                lot = lots[0]
                matched = min(quantity, lot[0])
                ages.append((float(matched), (time-lot[1]).total_seconds()/3600))
                quantity -= matched
                lot[0] -= matched
                lot[2] = lot[2] or risk_exit
                if not lot[0]:
                    lots.popleft()
                    cycles += int(not lot[2])
    for quantity, time, _ in lots:
        ages.append((float(quantity), (end-time).total_seconds()/3600))
    total = sum(q for q, _ in ages)
    triggers = [r for r in result['risk'] if r['kind'] == 'RISK_TRIGGER' and r.get('state') == 'EXITING']
    exit_orders = {key: order for key, order in result['orders'].items() if order.get('purpose') == 'RISK_EXIT'}
    exit_risk = [r for r in result['risk'] if r.get('order_id') in exit_orders]
    return metrics | dict(
        exit_orders_created=len(exit_orders),
        exit_orders_rejected=sum(o['status']=='REJECTED' for o in exit_orders.values()),
        exit_fill_admission_denials=sum(r['kind']=='FILL_ADMISSION_DENIED' for r in exit_risk),
        exit_volume_blocked_bars=sum(r['kind']=='EXIT_NO_EXECUTABLE_VOLUME' for r in exit_risk),
        exit_orders_unfilled=sum(D(o['cumulative_filled_quantity'])==0 for o in exit_orders.values()),
        grid_cycles=cycles, risk_exit_sell_fills=exit_sells,
        position_hours=sum(q*h for q, h in ages),
        inventory_mean_age_hours=sum(q*h for q, h in ages)/total if total else 0,
        inventory_max_age_hours=max((h for _, h in ages), default=0),
        ttl_confirmed_cancellations=sum(e['cause']=='TTL' and e['status']=='CANCELED' for e in result['lifecycle']),
        natural_expirations=0,
        strategy_cancellations=sum(e['cause']!='TTL' and e['status']=='CANCELED' for e in result['lifecycle']),
        expiry_reseeds=int(nav.expiry_reseeds.iloc[-1]),
        range_rebuilds=int(nav.range_rebuilds.iloc[-1]),
        paused_hours=int(nav.state.eq('PAUSED').sum()),
        exiting_hours=int(nav.state.eq('EXITING').sum()),
        exit_triggers=len(triggers),
        exit_success_rate=float(nav.state.eq('EXITED').any())/len(triggers) if triggers else None,
        final_state=nav.state.iloc[-1],
        final_reason=result['final']['state_reason'],
        exposure_status='EXPOSURE_NOT_MATCHED')


def reproduce_accounts(study, output, *, snapshot):
    """Replay the three distinct active lifecycle paths, comparing economic outputs."""
    bars, identity = load_hourly(snapshot)
    study, output = Path(study), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    checks = []
    for arm, mode in (('B', 'arithmetic'), ('B', 'geometric'), ('D', 'atr')):
        original = study/'base'/arm/mode
        manifest = json.loads((original/'manifest.json').read_text(encoding='utf-8'))
        destination = output/arm/mode
        run_experiment(destination, bars=bars, config=GridConfig(**manifest['config']),
            rules=GridRules(**{k: D(v) for k, v in manifest['rules'].items()}),
            capital=D(manifest['capital']), fee_rate=D(manifest['fee_rate']),
            slippage=D(manifest['slippage']), participation=D(manifest['participation']),
            interval='1h', market=Market(manifest['market']), instrument_id=manifest['instrument_id'],
            data_identity=identity)
        before, after = lifecycle_metrics(original), lifecycle_metrics(destination)
        if before != after:
            raise ValueError(f'nonreproducible lifecycle account: {arm}/{mode}')
        for filename in ('daily.csv', 'trades.csv'):
            pd.testing.assert_frame_equal(pd.read_csv(original/filename), pd.read_csv(destination/filename))
        checks.append(dict(arm=arm, mode=mode, economic_metrics_equal=True, hourly_path_equal=True,
                           fills_equal=True))
        print(f'reproduced {arm}/{mode}', flush=True)
    _write(output/'reproduction.json', checks)
    return checks


def run_study(output, *, snapshot, design_path):
    start = perf_counter()
    design_path = Path(design_path)
    root = design_path.resolve().parent.parent
    design = json.loads(design_path.read_text(encoding='utf-8'))
    config = GridConfig(**json.loads((root/design['base_config']).read_text(encoding='utf-8')))
    rules = GridRules(**{k: D(v) for k, v in json.loads((root/design['rules']).read_text(encoding='utf-8')).items()})
    bars, identity = load_hourly(snapshot)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    _write(output/'design.json', design)
    _write(output/'data-identity.json', identity)
    rows = []

    def run(scenario, arm, mode, reseed=False, breakout='pause'):
        fee, slip = design[scenario+'_cost_bps']
        settings = replace(config, mode=mode, breakout=breakout, fee_bps=fee, slippage_bps=slip,
            expiry_reseed_limit=design['expiry_reseed_limit'] if reseed else 0,
            expiry_reseed_interval=design['expiry_reseed_interval'])
        directory = output/scenario/arm/mode
        clock = perf_counter()
        run_experiment(directory, bars=bars, config=settings, rules=rules,
            capital=design['capital'], fee_rate=D(fee)/10000, slippage=D(slip)/10000,
            participation=D(design['participation']), interval='1h', market=Market.BINANCE_USDM,
            instrument_id=identity['instrument_id'], data_identity=identity,
            no_trade=arm=='no-trade', buy_hold=arm=='buy-hold')
        metrics = lifecycle_metrics(directory)
        row = dict(scenario=scenario, arm=arm, mode=mode, wall_seconds=perf_counter()-clock, **metrics)
        rows.append(row)
        pd.DataFrame(rows).to_csv(output/'summary.csv', index=False)
        print(f'{scenario}/{arm}/{mode}: equity={metrics["final_equity"]:.9f}, buys={metrics["buy_fills"]}, sells={metrics["sell_fills"]}', flush=True)
        return metrics

    for arm in ('no-trade', 'buy-hold'):
        run('base', arm, 'arithmetic')
    for arm, (reseed, breakout) in design['arms'].items():
        for mode in design['modes']:
            run('base', arm, mode, reseed, breakout)
    table = pd.DataFrame(rows)
    changed = any(
        tuple(table.loc[(table.arm==arm)&(table['mode']==mode), ['final_equity', 'fills']].iloc[0]) !=
        tuple(table.loc[(table.arm=='A')&(table['mode']==mode), ['final_equity', 'fills']].iloc[0])
        for arm in ('B', 'C') for mode in design['modes'])
    if changed:
        for arm, (reseed, breakout) in design['arms'].items():
            for mode in design['modes']:
                run('stress', arm, mode, reseed, breakout)
    _write(output/'study.json', dict(status='P7_R03_GRID_LIFECYCLE_EVALUATED',
        wall_seconds=perf_counter()-start, runs=len(rows), stress_triggered=changed,
        historical_reuse=True, exposure_status=design['exposure_status']))
    return pd.DataFrame(rows)
