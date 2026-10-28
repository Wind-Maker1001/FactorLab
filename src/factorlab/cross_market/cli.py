"""Explicit isolated research entrypoint, not a qualification/forward consumer."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd

from .contracts import ResearchSpec, ContractError
from .synthetic import build_us
from .research import run_research


def run_command(arguments):
    if arguments.scenario != 'us-synthetic':
        raise ContractError('use source-inspected CN experiment tool for unverified real inputs')
    spec = ResearchSpec('US', 'ALL_US_COMMON', arguments.start_date, arguments.end_date,
                        'SYNTHETIC_PRICE_INDEX', 'USD', train_months=3, validation_months=1, test_months=2)
    bars, adapter = build_us(spec.start_date, spec.end_date)
    dates = adapter.calendar.sessions(spec.start_date, spec.end_date).index
    benchmark = pd.DataFrame(dict(date=dates, close=100 * np.exp(np.cumsum(.0001 + .001 * np.sin(np.arange(len(dates)) / 17))),
                                 available_at=adapter.calendar.schedule.loc[dates, 'close'].to_numpy(), currency='USD',
                                 return_definition='price_return', benchmark_id=spec.benchmark))
    receipt = run_research(bars, adapter, spec, arguments.output, benchmark=benchmark, synthetic=True)
    print(json.dumps({'status': receipt['status'], 'output': str(arguments.output), 'classification': receipt['classification'], 'formal_admission': 'BLOCKED'}))
    return 0 if all(r['status'] == 'PASS' for r in receipt['backtests'] if r['mode'] == 'execution') else 1


def add_parser(subparsers):
    parser = subparsers.add_parser('cross-market', help='Isolated cross-market research (NO LIVE MONEY)')
    parser.add_argument('--scenario', choices=['us-synthetic'], required=True)
    parser.add_argument('--start-date', default='2023-09-01')
    parser.add_argument('--end-date', default='2025-02-28')
    parser.add_argument('--output', type=Path, required=True)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    add_parser(sub)
    return run_command(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
