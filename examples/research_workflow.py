"""Create and run a completely local synthetic two-engine research example."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from factorlab.data.research_input import prepare
from factorlab.dataset.identity import file_hash
from factorlab.research.config import EXECUTION, BOUNDARY
from factorlab.research.workflow import run, compare, verify, replay
from factorlab.strategies.dual_ma import DualMAConfig
from factorlab.strategies.dual_ma_cli import synthetic_bars


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description='Synthetic only; no network or financial evidence')
    parser.add_argument('--output', type=Path, required=True, help='new private directory')
    args = parser.parse_args(argv)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source, raw = root / 'source', root / 'raw'
    source.mkdir(); raw.mkdir()
    bars, calendar = synthetic_bars(DualMAConfig(start='2018-01-01', end='2018-04-30', long_window=60))
    bars = pd.concat([bars, bars.assign(symbol='601398.SH')], ignore_index=True)
    bars.to_parquet(source / 'bars.parquet', index=False)
    save(source / 'manifest.json', dict(schema='P5_R03_CSMAR_PRIVATE_V1', bars_sha256=file_hash(source / 'bars.parquet'), references=[]))
    warmup = str(calendar[0].date())
    preparation = save(root / 'prepare.json', dict(source_manifest_sha256=file_hash(source / 'manifest.json'),
        symbols=['601857.SH', '601398.SH'], start=warmup, end='2018-04-30'))
    research_root = root / 'research-input'
    prepare(preparation, source, raw, research_root)
    strategy = ROOT / 'examples/custom_strategy/ma_strategy.py'
    template = json.loads((ROOT / 'configs/research/p5-r02-601857-2018.json').read_text())
    base = dict(schema_version=1, task='custom_daily', execution=deepcopy(EXECUTION), boundary=deepcopy(BOUNDARY),
        data=dict(id='synthetic-offline-example', manifest_sha256=file_hash(research_root / 'manifest.json'), source_kind='SYNTHETIC'), fees=template['fees'])
    common = dict(initial_cash=100000.0, max_weight=1.0, board_lot=100)
    custom = save(root / 'custom.json', dict(base, research=dict(common, symbol='601857.SH', start='2018-01-01', end='2018-04-30',
        warmup_start=warmup, strategy={'class': 'MyStrategy', 'parameters': {'window': 5}, 'sha256': file_hash(strategy)})))
    window = dict(symbol='601857.SH', industry='synthetic', year=2018, start='2018-01-01', end='2018-04-30',
        warmup_start=warmup, query_start=warmup, query_end='2018-04-30')
    multi = save(root / 'multi.json', dict(base, task='p6_r01', research=dict(windows=[window, dict(window, symbol='601398.SH', industry='synthetic-bank')], common=common,
        strategies=template['strategies'], signal_price_column='raw_close')))
    runtime = save(root / 'runtime.json', dict(research_root=str(research_root), source_root=str(source), strategy_file=str(strategy)))
    multi_runtime = save(root / 'multi-runtime.json', dict(research_root=str(research_root), source_root=str(source)))
    store = root / 'tracker'
    receipts = {}
    for name, config, binding in [('custom-a', custom, runtime), ('custom-b', custom, runtime), ('multi', multi, multi_runtime)]:
        record = run(config, binding, output=root / name, store=store, experiment_id=name, run_id=name)
        payload = record['payload']['run']
        receipts[name] = dict(status=payload['status'], lifecycle=payload['validation']['lifecycle'], qualification=payload['qualification'])
        if payload['status'] != 'SUCCEEDED':
            save(root / 'example-receipt.json', receipts)
            raise RuntimeError(json.dumps(receipts[name]))
    receipts['comparison'] = compare(store, ['custom-a', 'custom-b'])
    receipts['verify'] = verify(store, 'custom-a', runtime_path=runtime)
    receipts['replay'] = replay(store, 'custom-a', runtime_path=runtime, output=root / 'replay', new_run_id='custom-replay')
    receipts['multi-replay'] = replay(store, 'multi', runtime_path=multi_runtime, output=root / 'multi-replay', new_run_id='multi-replay')
    save(root / 'example-receipt.json', receipts)
    print(json.dumps(dict(status='SYNTHETIC_EXAMPLE_COMPLETED', receipt=str(root / 'example-receipt.json'))))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
