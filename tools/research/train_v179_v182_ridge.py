"""Offline monthly Ridge model trainer & V2 StrategyPackage builder for native V179/V182.

Enforces the FactorLab cloud boundary and original monthly causal training timing:
- Model training runs strictly offline in research environments (uses scikit-learn lazily).
- Preserves the exact 5-trading-day forward label (LABEL_MATURITY_SESSIONS=5), 508-session
  lookback window, and Ridge(alpha=80.0).
- Monthly training MUST run AFTER the first trading session of month M completes close and
  all 8 equities + ^SP500TR benchmark close bars are available (so the 5-trading-day label
  for train_cutoff = calendar[i - 5] has matured at d_0 = calendar[i]), and BEFORE the first
  natural-week open rebalance that uses a month-M signal (which always occurs strictly after d_0).
- Produces a lightweight, SHA-256 content-addressed JSON model artifact (~170 KB) with
  strict per-month causal metadata (train_start <= train_cutoff < label_maturity_date ==
  effective_from_signal_date).
- Optionally builds content-addressed V2 StrategyPackage archives (default migration='artifact_refresh')
  that embed the frozen artifact as a pure-Python data module for position-preserving monthly refreshes.

Example usage:
  python tools/research/train_v179_v182_ridge.py \
    --market-dir tests/fixtures/v179_v182_original/fixtures/target \
    --benchmark tests/fixtures/v179_v182_original/fixtures/target/benchmark.parquet \
    --output examples/v179_v182_alpaca/artifacts/v179_v182_ridge_target.json
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

from factorlab.strategies.qlib_blend_ridge import train_monthly_ridge_artifact
from factorlab.strategies.v179_v182_native import TICKERS
from factorlab.strategies.v179_v182_runtime import build_v179_package, build_v182_package


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def load_frozen_market(market_dir: Path, benchmark_path: Path, *, end: str | None = None):
    bars = {}
    file_hashes = {}
    for symbol in TICKERS:
        p = market_dir / f'{symbol}.parquet'
        if p.exists():
            df = pd.read_parquet(p)
            if end is not None:
                df = df.loc[:end]
            bars[symbol] = df
            file_hashes[f'{symbol}.parquet'] = _file_sha256(p)
    close = pd.DataFrame({s: q['close'] for s, q in bars.items()}).reindex(columns=TICKERS)
    bench_df = pd.read_parquet(benchmark_path)
    if end is not None:
        bench_df = bench_df.loc[:end]
    file_hashes[benchmark_path.name] = _file_sha256(benchmark_path)
    return close, bench_df['close'], file_hashes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market-dir', type=Path, required=True)
    parser.add_argument('--benchmark', type=Path, required=True)
    parser.add_argument('--end', default=None, help='Optional inclusive YYYY-MM-DD session cutoff')
    parser.add_argument('--omit-sndk', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--build-packages-dir', type=Path, default=None)
    parser.add_argument('--account-id', default='paper-example')
    parser.add_argument('--capital', default='50000')
    parser.add_argument('--code-commit', default='0' * 40)
    parser.add_argument(
        '--migration',
        default='artifact_refresh',
        choices=('artifact_refresh', 'wait_flat'),
        help='StrategyPackage migration policy (default: artifact_refresh for position-preserving monthly refresh)',
    )
    parser.add_argument('--generated-at', default=None, help='Optional ISO-8601 UTC generation timestamp')
    args = parser.parse_args(argv)

    close, market_index, file_hashes = load_frozen_market(args.market_dir, args.benchmark, end=args.end)
    generated_at = args.generated_at or datetime.now(timezone.utc).isoformat()
    artifact = train_monthly_ridge_artifact(
        close,
        market_index,
        omit_sndk=args.omit_sndk,
        provenance={
            'benchmark_series': '^SP500TR',
            'market_dir': args.market_dir.name,
            'benchmark_file': args.benchmark.name,
            'input_sha256': file_hashes,
            'requested_end': args.end,
            'generated_at': generated_at,
        },
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')
    latest_month = sorted(artifact['models'])[-1] if artifact['models'] else None
    latest_entry = artifact['models'][latest_month] if latest_month else None
    summary = {
        'output': str(args.output),
        'artifact_sha256': artifact['artifact_sha256'],
        'omit_sndk': artifact['omit_sndk'],
        'covered_start': artifact['covered_start'],
        'covered_end': artifact['covered_end'],
        'generated_at': generated_at,
        'fitted_months': len(artifact['models']),
        'insufficient_months': len(artifact['insufficient_months']),
        'latest_model_month': latest_month,
        'latest_train_cutoff': None if latest_entry is None else latest_entry['train_cutoff'],
        'latest_label_maturity_date': None if latest_entry is None else latest_entry.get('label_maturity_date'),
        'latest_effective_from_signal_date': None if latest_entry is None else latest_entry['effective_from_signal_date'],
        'size_bytes': args.output.stat().st_size,
    }
    if args.build_packages_dir is not None:
        args.build_packages_dir.mkdir(parents=True, exist_ok=True)
        evidence = {
            'offline_training': True,
            'benchmark_series': '^SP500TR',
            'covered_end': artifact['covered_end'],
            'generated_at': generated_at,
        }
        v179_manifest = build_v179_package(
            account_id=args.account_id,
            capital=args.capital,
            model_artifact=artifact,
            code_commit=args.code_commit,
            destination=args.build_packages_dir / 'v179_trend_blend.zip',
            evidence=evidence,
            omit_sndk=args.omit_sndk,
            migration=args.migration,
        )
        v182_manifest = build_v182_package(
            account_id=args.account_id,
            capital=args.capital,
            model_artifact=artifact,
            code_commit=args.code_commit,
            destination=args.build_packages_dir / 'v182_recovery_blend.zip',
            evidence=evidence,
            omit_sndk=args.omit_sndk,
            migration=args.migration,
        )
        summary['v179_package_id'] = v179_manifest['package_id']
        summary['v182_package_id'] = v182_manifest['package_id']
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
