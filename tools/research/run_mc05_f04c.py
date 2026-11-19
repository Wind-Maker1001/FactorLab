"""Run the bounded NVDAUSDT close-observation study offline. NO LIVE MONEY."""
import argparse
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
import platform
from time import perf_counter, process_time

from factorlab._identity import source_context_root
from factorlab.dataset.partitioned import directory_bytes
from factorlab.experiments.grid_execution_audit import audit_account, pair_orders
from factorlab.experiments.grid_execution_clock import ResearchExecution
from factorlab.experiments.grid_paper import D, run_experiment
from factorlab.experiments.mc_grid_tail_f04a import read_json, study_definition
from factorlab.experiments.mc_grid_tail_f04c_data import admit_market_data
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.contracts import Market


def _encode(value):
    return (json.dumps(value, default=str, sort_keys=True, ensure_ascii=False, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def _paper_inputs(market_data):
    root = source_context_root()
    baseline_path = root / 'configs/mc05_f04a_grid_tail.json'
    definition = study_definition(read_json(baseline_path), root)
    grid = GridConfig(**definition['grid'])
    anchor = market_data.hourly[0]['close']
    anchor_ratio = anchor / D(grid.initial_price)
    config = replace(grid, initial_price=float(anchor), lower=float(D(grid.lower) * anchor_ratio),
                     upper=float(D(grid.upper) * anchor_ratio))
    rules = GridRules(**{key: D(value) for key, value in definition['rules'].items()})
    bars = [{key: str(value) if isinstance(value, Decimal) else value for key, value in bar.items()}
            for bar in market_data.hourly]
    settings = dict(bars=bars, config=config, rules=rules, capital=D(grid.initial_cash),
        fee_rate=D(definition['costs']['fee_rate']), slippage=D(definition['costs']['slippage']),
        interval='1h', market=Market.BINANCE_USDM, instrument_id=market_data.evidence['instrument_id'],
        data_identity=dict(source=market_data.evidence['source'],
                           data_version=market_data.evidence['hourly_source']['data_version']),
        research_wal=True)
    return settings, D(definition['capacity'])


def run(output, market_data, *, account_prefix='mc05-f04c'):
    started, cpu_started = perf_counter(), process_time()
    output.mkdir(parents=True, exist_ok=False)
    settings, capacity = _paper_inputs(market_data)
    evidence = market_data.evidence
    start = datetime.fromisoformat(evidence['start'])
    end = datetime.fromisoformat(evidence['end_exclusive'])
    minutes_by_hour = {bar['time']: [] for bar in market_data.hourly}
    for minute in market_data.minutes:
        hour = datetime.fromisoformat(minute['time']).replace(minute=0).isoformat()
        minutes_by_hour[hour].append(minute)

    accounts = {}
    quote_traces = {}
    for arm, clock in (
        ('A', ResearchExecution(None, capacity, diagnostic_minutes=minutes_by_hour)),
        ('B', ResearchExecution(minutes_by_hour, capacity)),
    ):
        directory = output / arm
        run_experiment(directory, account_id=f'{account_prefix}-{arm}', research_execution=clock, **settings)
        account = audit_account(directory, start=start, end=end, fill_references=clock.fill_references)
        observations = clock.summary()
        observations['quote_observations'] -= 1  # The pre-window hour seeds otherwise empty accounts.
        account['observations'] = observations
        accounts[arm] = account
        quote_traces[arm] = clock.events
        if arm == 'A':
            diagnostic = clock.price_touches

    pairing = pair_orders(accounts['A'], accounts['B'])
    trace = gzip.compress(_encode(dict(quotes=quote_traces, touches=diagnostic)), mtime=0)
    (output / 'OBSERVATIONS.json.gz').write_bytes(trace)
    inputs = dict(grid=asdict(settings['config']), rules=asdict(settings['rules']), capital=settings['capital'],
                  fee_rate=settings['fee_rate'], slippage=settings['slippage'], capacity=capacity,
                  market=settings['market'].value, instrument_id=settings['instrument_id'],
                  funding='NOT_SIMULATED')
    identity = dict(data=evidence, configuration=inputs, accounts=accounts, order_pairing=pairing,
                    observations_sha256=sha256(trace).hexdigest())
    sensitivity = ('CLOSE_VS_MINUTE_SENSITIVITY_ESTABLISHED'
                   if pairing['order_trajectories_with_different_fills'] else 'INCONCLUSIVE')
    audit = dict(research='MC-05-F04C', **identity, result_sha256=sha256(_encode(identity)).hexdigest(),
        states=[evidence['status'], sensitivity, 'REAL_FILL_NOT_VERIFIED',
                'FINANCIAL_NOT_ESTABLISHED', 'NO LIVE MONEY'],
        resources=dict(replay_and_audit_wall_seconds=perf_counter()-started,
                       replay_and_audit_cpu_seconds=process_time()-cpu_started,
                       local_result_bytes=directory_bytes(output), python=platform.python_version()))
    (output / 'AUDIT.json').write_bytes(_encode(audit))
    print(json.dumps(dict(states=audit['states'], result_sha256=audit['result_sha256'],
        resources=audit['resources']), ensure_ascii=False))
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hourly-snapshot', required=True, type=Path)
    parser.add_argument('--archive-root', required=True, type=Path)
    parser.add_argument('--hourly-archive-root', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--start', default='2026-08-05')
    parser.add_argument('--end', default='2026-08-12')
    args = parser.parse_args()
    options = {}
    if args.hourly_archive_root is not None:
        options['hourly_archive_root'] = args.hourly_archive_root
    market_data = admit_market_data(args.hourly_snapshot, args.archive_root, args.start, args.end, **options)
    run(args.output, market_data)


if __name__ == '__main__':
    main()
