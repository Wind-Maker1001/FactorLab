"""Reproducible P10-03 differential, runtime resource, and Alpaca acceptance report generator.

Usage:
  PYTHONPATH=src python tools/research/validate_v179_v182_migration.py
  PYTHONPATH=src python tools/research/validate_v179_v182_migration.py --full-archive-dir <extracted_Qlib_v182_v179_dir>
"""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal as D
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

from factorlab.strategies.qlib_blend_ridge import train_monthly_ridge_artifact
from factorlab.strategies.v179_v182_native import (
    NativeV125,
    NativeV165,
    NativeV179,
    NativeV182,
    Order,
    TICKERS,
    TradeDecisionWO,
    V179Planner,
    V182Planner,
)
from factorlab.strategies.v179_v182_runtime import build_v179_package, build_v182_package

FIXTURE_ROOT = ROOT / 'tests' / 'fixtures' / 'v179_v182_original'
ARTIFACT_DIR = ROOT / 'examples' / 'v179_v182_alpaca' / 'artifacts'
OUT_DIR = ROOT / 'docs' / 'validation' / 'p10-03'


def _load_market(base: Path, market_dir: str, index_path: str, end: str | None = None):
    bars = {}
    for s in TICKERS:
        p = base / market_dir / f'{s}.parquet'
        if p.exists():
            df = pd.read_parquet(p)
            bars[s] = df.loc[:end] if end is not None else df
    close = pd.DataFrame({s: q['close'] for s, q in bars.items()}).reindex(columns=TICKERS)
    volume = pd.DataFrame({s: q['volume'] for s, q in bars.items()}).reindex(columns=TICKERS)
    opening = pd.DataFrame({s: q['open'] for s, q in bars.items()}).reindex(columns=TICKERS)
    idx_df = pd.read_parquet(base / index_path)
    index = (idx_df.loc[:end] if end is not None else idx_df)['close']
    return close, volume, opening, index


def _replay_account(
    base: Path,
    version: str,
    window: str,
    case: str,
    spec: dict,
    *,
    fee_rate: float,
    capital_usd: float,
    artifact_cache: dict,
    write_csvs: bool = False,
    weight_rows: list | None = None,
    order_rows: list | None = None,
) -> dict:
    omit_sndk = window.startswith('dev') or case == 'no_sndk'
    close, volume, opening, index = _load_market(base, spec['market'], spec['index'], spec['end'])
    cache_key = (str(base), spec['market'], spec['index'], spec['end'], omit_sndk)
    if cache_key not in artifact_cache:
        artifact_cache[cache_key] = train_monthly_ridge_artifact(close, index, omit_sndk=omit_sndk)
    artifact = artifact_cache[cache_key]

    PlannerCls = V179Planner if version == 'v179' else V182Planner
    planner = PlannerCls(close, volume, opening, market_index=index, omit_sndk=omit_sndk, ridge_artifact=artifact)

    acct_dir = base / 'accounts' / version / window / case
    orig_targets = pd.read_csv(acct_dir / 'targets.csv', parse_dates=['date', 'signal_date']).set_index('date')
    orig_fills = pd.read_csv(acct_dir / 'fills.csv', parse_dates=['date'])
    orig_daily = pd.read_parquet(acct_dir / 'daily.parquet')

    cash = float(capital_usd)
    holdings = {s: 0.0 for s in TICKERS}
    sim_fills = []
    sim_nav = []
    days = close.loc[spec['start']:spec['end']].index
    for day in days:
        planner.trade_calendar = SimpleNamespace(get_step_time=lambda d=day: (d, d))
        planner.trade_position = SimpleNamespace(
            get_stock_amount_dict=lambda h=holdings: {k: v for k, v in h.items() if v != 0.0},
            get_cash=lambda c=cash: c,
        )
        decision = planner.generate_trade_decision()
        for o in decision.order_list:
            # Match Qlib float32 binary provider open fill price.
            px = float(np.float32(opening.at[day, o.stock_id]))
            if o.direction == Order.SELL:
                qty = min(o.amount, holdings[o.stock_id])
                turnover = qty * px
                fee = turnover * fee_rate
                holdings[o.stock_id] -= qty
                cash += turnover - fee
                side = 'sell'
            else:
                qty = min(o.amount, max(0.0, cash / (px * (1.0 + fee_rate))))
                turnover = qty * px
                fee = turnover * fee_rate
                holdings[o.stock_id] += qty
                cash -= turnover + fee
                side = 'buy'
            if qty > 0:
                sim_fills.append(dict(
                    date=day, ticker=o.stock_id, side=side, quantity=qty,
                    requested=o.amount, price=px, turnover=turnover, fee=fee,
                ))
        eod_val = sum(holdings[s] * float(close.at[day, s]) for s in TICKERS if holdings[s] != 0.0)
        sim_nav.append(cash + eod_val)

    sim_targets = pd.DataFrame(planner.decisions).set_index('date')
    assert list(sim_targets.index) == list(orig_targets.index)
    assert list(sim_targets['signal_date']) == list(orig_targets['signal_date'])
    w_diff = float((sim_targets[TICKERS] - orig_targets[TICKERS]).abs().max().max())

    sim_fills_df = pd.DataFrame(sim_fills)
    assert len(sim_fills_df) == len(orig_fills)
    assert list(sim_fills_df['ticker']) == list(orig_fills['ticker'])
    assert list(sim_fills_df['side']) == list(orig_fills['side'])
    req_diff = float((sim_fills_df['requested'] - orig_fills['requested']).abs().max())
    qty_diff = float((sim_fills_df['quantity'] - orig_fills['quantity']).abs().max())
    nav_diff = float((pd.Series(sim_nav, index=days) - orig_daily['nav']).abs().max())

    if write_csvs and weight_rows is not None and order_rows is not None:
        for dt, row in sim_targets.iterrows():
            ref = orig_targets.loc[dt]
            for s in TICKERS:
                weight_rows.append([
                    version, window, case, str(dt.date()), str(pd.Timestamp(row['signal_date']).date()),
                    s, float(ref[s]), float(row[s]), float(row[s] - ref[s]),
                ])
        for idx_i in range(len(sim_fills_df)):
            srow = sim_fills_df.iloc[idx_i]
            rrow = orig_fills.iloc[idx_i]
            order_rows.append([
                version, window, case, str(pd.Timestamp(srow['date']).date()), srow['ticker'], srow['side'],
                float(rrow['requested']), float(srow['requested']), float(srow['requested'] - rrow['requested']),
                float(rrow['quantity']), float(srow['quantity']), float(srow['quantity'] - rrow['quantity']),
            ])

    return {
        'version': version,
        'window': window,
        'case': case,
        'days': int(len(days)),
        'weeks': int(len(sim_targets)),
        'fills': int(len(sim_fills_df)),
        'max_weight_diff': w_diff,
        'max_requested_qty_diff': req_diff,
        'max_filled_qty_diff': qty_diff,
        'max_nav_diff_usd': nav_diff,
    }


def _benchmark_runtime_resources() -> dict:
    close, volume, _, index = _load_market(FIXTURE_ROOT, 'fixtures/target', 'fixtures/target/benchmark.parquet')
    artifact = json.loads((ARTIFACT_DIR / 'v179_v182_ridge_target.json').read_text(encoding='utf-8'))
    win_close = close.iloc[-252:]
    win_vol = volume.iloc[-252:]
    win_idx = index.reindex(win_close.index)
    t_day = win_close.index[-1]
    prices = win_close.loc[t_day]
    valid = np.isfinite(prices) & prices.gt(0)
    held = pd.Series(0.0, index=TICKERS)

    timings = {}
    peaks = {}
    for label, cls in (('v179_252d_inference', NativeV179), ('v182_252d_inference', NativeV182)):
        tracemalloc.start()
        t0 = time.perf_counter()
        eng = cls(win_close, win_vol, market_index=win_idx, ridge_artifact=artifact)
        _ = eng.targets(t_day, prices, valid, held)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        timings[label + '_ms'] = round(elapsed_ms, 2)
        peaks[label + '_peak_mib'] = round(peak / (1024 * 1024), 3)

    with tempfile.TemporaryDirectory(prefix='p10-03-pkg-size-') as tmp:
        tmp_path = Path(tmp)
        p179 = tmp_path / 'v179.zip'
        p182 = tmp_path / 'v182.zip'
        m179 = build_v179_package(
            account_id='paper-bench', capital='10000', model_artifact=artifact,
            code_commit='0' * 40, destination=p179, evidence={'bench': True},
        )
        m182 = build_v182_package(
            account_id='paper-bench', capital='10000', model_artifact=artifact,
            code_commit='0' * 40, destination=p182, evidence={'bench': True},
        )
        pkg_sizes = {
            'v179_package_zip_bytes': p179.stat().st_size,
            'v182_package_zip_bytes': p182.stat().st_size,
            'v179_package_id': m179['package_id'],
            'v182_package_id': m182['package_id'],
        }

    return {
        'runtime_dependencies': ['numpy', 'pandas'],
        'cloud_qlib_dependency': False,
        'cloud_sklearn_dependency': False,
        'offline_training_dependencies': ['numpy', 'pandas', 'scikit-learn'],
        'ridge_artifact_json_bytes': (ARTIFACT_DIR / 'v179_v182_ridge_target.json').stat().st_size,
        'ridge_artifact_fitted_months': len(artifact['models']),
        'online_window_sessions': 252,
        'timings': timings,
        'peak_memory': peaks,
        'packages': pkg_sizes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full-archive-dir', type=Path, default=None)
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = args.full_archive_dir if args.full_archive_dir is not None else FIXTURE_ROOT
    plan = json.loads((base / 'replay_config.json').read_text(encoding='utf-8'))

    # 1. Exact indicator differential against frozen original Qlib strategy classes on target fixture.
    for name in ['qlib', 'qlib.backtest', 'qlib.backtest.decision', 'qlib.strategy', 'qlib.strategy.base']:
        sys.modules[name] = ModuleType(name)
    sys.modules['qlib.backtest'].backtest = lambda *a, **kw: None
    sys.modules['qlib.backtest.decision'].Order = Order
    sys.modules['qlib.backtest.decision'].TradeDecisionWO = TradeDecisionWO
    sys.modules['qlib.strategy.base'].BaseStrategy = type('BaseStrategy', (), {})
    sys.path.insert(0, str(FIXTURE_ROOT))
    OrigV179 = importlib.import_module('v179.strategy').Strategy
    OrigV182 = importlib.import_module('v182.strategy').Strategy

    close, volume, opening, index = _load_market(FIXTURE_ROOT, 'fixtures/target', 'fixtures/target/benchmark.parquet')
    indicator_checks = []
    for omit_sndk in (False, True):
        art_file = 'v179_v182_ridge_target_no_sndk.json' if omit_sndk else 'v179_v182_ridge_target.json'
        artifact = json.loads((ARTIFACT_DIR / art_file).read_text(encoding='utf-8'))
        for ver, OrigCls, NatCls in (('v179', OrigV179, NativeV179), ('v182', OrigV182, NativeV182)):
            orig = OrigCls(close, volume, opening, market_index=index, omit_sndk=omit_sndk)
            nat = NatCls(close, volume, opening, market_index=index, omit_sndk=omit_sndk, ridge_artifact=artifact)
            max_diff = 0.0
            for key in [
                'ma28', 'ma63', 'qqq_ma63', 'vol', 'breadth', 'breadth_change', 'high', 'confirmed',
                'index', 'market_fast', 'market_slow', 'index_long', 'market_dd', 'market_trend',
                'risk', 'relative10', 'score', 'drawdown', 'downside', 'down_jump', 'market_down_jump',
                'fastmom', 'semideviation', 'forecast',
            ]:
                a, b = getattr(orig, key), getattr(nat, key)
                if isinstance(a, pd.Series):
                    if a.dtype != bool and a.notna().any():
                        max_diff = max(max_diff, float((a - b).abs().max()))
                elif not (a.dtypes == bool).all() and a.notna().any().any():
                    max_diff = max(max_diff, float((a - b).abs().max().max()))
            indicator_checks.append({
                'version': ver, 'omit_sndk': omit_sndk, 'sessions': int(len(close)),
                'fitted_months': len(artifact['models']), 'max_indicator_and_ridge_diff': max_diff,
            })

    # 2. Replay all available archived accounts (23 if --full-archive-dir provided, else 4 target accounts).
    accounts = sorted((base / 'accounts').glob('*/*/*/result.json'))
    artifact_cache = {}
    account_results = []
    weight_rows = []
    order_rows = []
    for res_path in accounts:
        ver, window, case = res_path.parent.relative_to(base / 'accounts').parts
        spec = plan['windows'][window]
        write_csvs = (window == 'target' and case == 'normal')
        row = _replay_account(
            base, ver, window, case, spec,
            fee_rate=plan['cost'][case], capital_usd=plan['capital_usd'],
            artifact_cache=artifact_cache, write_csvs=write_csvs,
            weight_rows=weight_rows, order_rows=order_rows,
        )
        account_results.append(row)

    with (OUT_DIR / 'target-weight-differences.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['version', 'window', 'case', 'date', 'signal_date', 'ticker', 'reference', 'native', 'delta'])
        w.writerows(weight_rows)

    with (OUT_DIR / 'order-intent-differences.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([
            'version', 'window', 'case', 'date', 'ticker', 'side',
            'ref_requested', 'native_requested', 'requested_delta',
            'ref_filled', 'native_filled', 'filled_delta',
        ])
        w.writerows(order_rows)

    diff_summary = {
        'archive_scope': 'full_23_accounts' if args.full_archive_dir is not None else 'repo_target_4_accounts',
        'accounts_verified': len(account_results),
        'indicator_checks': indicator_checks,
        'max_weight_diff': max(r['max_weight_diff'] for r in account_results),
        'max_requested_qty_diff': max(r['max_requested_qty_diff'] for r in account_results),
        'max_filled_qty_diff': max(r['max_filled_qty_diff'] for r in account_results),
        'max_nav_diff_usd': max(r['max_nav_diff_usd'] for r in account_results),
        'accounts': account_results,
    }
    (OUT_DIR / 'differential-summary.json').write_text(
        json.dumps(diff_summary, indent=2, ensure_ascii=False) + '\n', encoding='utf-8'
    )

    resources = _benchmark_runtime_resources()
    (OUT_DIR / 'runtime-dependencies-resources.json').write_text(
        json.dumps(resources, indent=2, ensure_ascii=False) + '\n', encoding='utf-8'
    )
    print(json.dumps({
        'accounts_verified': diff_summary['accounts_verified'],
        'max_weight_diff': diff_summary['max_weight_diff'],
        'max_requested_qty_diff': diff_summary['max_requested_qty_diff'],
        'max_nav_diff_usd': diff_summary['max_nav_diff_usd'],
        'resources': resources,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
