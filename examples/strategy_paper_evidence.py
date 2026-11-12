"""Bounded NVDA real-history evidence; simulated fills are not observed trades."""
import argparse
import json
from pathlib import Path
import subprocess

from factorlab.trading.strategy import DualMAStrategy
from factorlab.trading.strategy_replay import load_snapshot_bars, fit_frozen_ridge, replay_history
from decimal import Decimal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hourly-snapshot', required=True, type=Path)
    parser.add_argument('--daily-snapshot', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--max-bars', type=int, default=2160)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    reader, bars = load_snapshot_bars(args.hourly_snapshot, max_bars=args.max_bars)
    commit = subprocess.check_output(['git', '-C', str(Path(__file__).resolve().parents[1]),
                                      'rev-parse', 'HEAD'], text=True).strip()
    strategy, research, start = fit_frozen_ridge(reader, bars, args.output, commit)
    symbol = reader.manifest['contract']['exchange_info']
    evidence = dict(source='BINANCE_USDM_PUBLIC_REST', data_version=reader.manifest['data_version'],
                    bars=len(bars), research=research,
                    hourly_dual_ma=replay_history(args.output / 'dual-ma.sqlite',
                        DualMAStrategy(max_weight=Decimal('.05')), bars, symbol, start_index=start),
                    hourly_frozen_ridge=replay_history(args.output / 'ridge.sqlite',
                        strategy, bars, symbol, start_index=start))
    if args.daily_snapshot:
        daily_reader, daily_bars = load_snapshot_bars(args.daily_snapshot, max_bars=args.max_bars)
        evidence['daily_dual_ma'] = replay_history(args.output / 'daily-dual-ma.sqlite',
            DualMAStrategy(interval_seconds=86400, max_weight=Decimal('.05')), daily_bars,
            daily_reader.manifest['contract']['exchange_info'])
    path = args.output / 'evidence.json'
    path.write_text(json.dumps(evidence, indent=2, allow_nan=False, default=str), encoding='utf-8')
    print(path)


if __name__ == '__main__':
    main()
