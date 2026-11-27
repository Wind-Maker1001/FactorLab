"""Phase 1: Source package data audit, V69 compatibility verification, and isolated Qlib replay."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from time import perf_counter
from typing import Any

import numpy as np
import pandas as pd

from .config import DEFAULT_TICKERS, ExternalValidationConfig


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_v179_v182_package(package_dir: Path) -> dict[str, Any]:
    """Verify SHA256SUMS.txt, manifest metadata, and source structure of the V179/V182 delivery package."""
    sums_path = package_dir / "SHA256SUMS.txt"
    entries = [
        line.strip().split("  ", 1)
        for line in sums_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    mismatches = []
    missing = []
    for expected, rel in entries:
        target = package_dir / rel
        if not target.is_file():
            missing.append(rel)
        elif _sha256(target) != expected:
            mismatches.append(rel)

    source_manifest = json.loads((package_dir / "source_manifest.json").read_text(encoding="utf-8"))
    replay_config = json.loads((package_dir / "replay_config.json").read_text(encoding="utf-8"))
    protocol = json.loads((package_dir / "protocol.json").read_text(encoding="utf-8"))
    verification = json.loads((package_dir / "verification.json").read_text(encoding="utf-8"))
    target_manifest = json.loads((package_dir / "fixtures" / "target" / "manifest.json").read_text(encoding="utf-8"))
    history_manifest = json.loads((package_dir / "fixtures" / "history" / "manifest.json").read_text(encoding="utf-8"))
    sp500_sources = json.loads(
        (package_dir / "original_configs" / "continuous_research" / "twenty_year" / "sp500" / "sources.json").read_text(
            encoding="utf-8"
        )
    )

    accounts = sorted(
        f.parent.relative_to(package_dir / "accounts").as_posix()
        for f in (package_dir / "accounts").glob("*/*/*/result.json")
    )

    return {
        "package_dir": str(package_dir),
        "sha256sums_total": len(entries),
        "sha256sums_missing": missing,
        "sha256sums_mismatches": mismatches,
        "integrity_passed": not missing and not mismatches,
        "archived_accounts": accounts,
        "archived_account_count": len(accounts),
        "supplied_verification_count": verification.get("verified_accounts"),
        "source_manifest": source_manifest,
        "replay_config": replay_config,
        "protocol_summary": {
            "cash": protocol["cash"],
            "tickers": list(protocol["universe"]),
            "first_listed": protocol["first_listed"],
            "trend_days": protocol["trend_days"],
            "slow_days": protocol["slow_days"],
            "rebalance_deadband": protocol["rebalance_deadband"],
            "costs": protocol["costs"],
        },
        "target_manifest_captured_ny": target_manifest.get("captured_ny"),
        "target_data_sources": {
            k: {
                "url": v.get("url"),
                "source_format": v.get("source_format"),
                "confirmed_vs": v.get("confirmed_vs"),
                "max_relative_ohlc_error": v.get("max_relative_ohlc_error", v.get("max_relative_close_error")),
                "factor": v.get("factor"),
            }
            for k, v in target_manifest.get("data", {}).items()
        },
        "history_manifest_summary": {
            "first": history_manifest.get("first"),
            "last": history_manifest.get("last"),
            "sessions": history_manifest.get("sessions"),
            "first_by_symbol": history_manifest.get("first_by_symbol"),
        },
        "sp500_cross_check": {
            "fred_max_diff": sp500_sources.get("FRED_price_cross_check", {}).get("max_yahoo_difference"),
            "fred_diffs_over_0_03": sp500_sources.get("FRED_price_cross_check", {}).get("yahoo_differences_over_0_03"),
            "spy_daily_corr": sp500_sources.get("SPY_cross_check", {}).get("correlation"),
            "spy_monthly_corr": sp500_sources.get("SPY_cross_check", {}).get("monthly_correlation"),
            "tr_vs_price_daily_corr": sp500_sources.get("TR_price_daily_correlation"),
        },
    }


def audit_market_fixtures(v179_v182_dir: Path, v69_dir: Path) -> dict[str, Any]:
    """Audit OHLCV schemas, listing dates, missing values, corporate actions, and V69 data compatibility."""
    fixtures_report: dict[str, Any] = {}
    for subdir in ("target", "history"):
        fdir = v179_v182_dir / "fixtures" / subdir
        per_ticker: dict[str, Any] = {}
        calendar_union: pd.DatetimeIndex | None = None
        for symbol in DEFAULT_TICKERS:
            df = pd.read_parquet(fdir / f"{symbol}.parquet")
            idx = pd.DatetimeIndex(df.index)
            if calendar_union is None or len(idx) > len(calendar_union):
                calendar_union = idx
            ret = df["close"].pct_change(fill_method=None)
            factor_unique = sorted(float(x) for x in df["factor"].dropna().unique()[:5]) if "factor" in df else []
            # Detect large single-day moves or potential corporate action discontinuities
            extreme_moves = int((ret.abs() > 0.25).sum())
            ohlc_violations = int(
                (
                    (df["low"] > df[["open", "close"]].min(axis=1) + 1e-8)
                    | (df["high"] < df[["open", "close"]].max(axis=1) - 1e-8)
                    | (df["close"] <= 0)
                    | (df["open"] <= 0)
                ).sum()
            )
            per_ticker[symbol] = {
                "rows": int(len(df)),
                "first_date": str(idx.min().date()),
                "last_date": str(idx.max().date()),
                "columns": list(df.columns),
                "nan_count": int(df.isna().sum().sum()),
                "zero_or_neg_volume_days": int((df["volume"] <= 0).sum()),
                "min_volume": float(df["volume"].min()),
                "median_volume": float(df["volume"].median()),
                "min_close": float(df["close"].min()),
                "max_close": float(df["close"].max()),
                "factor_all_one": bool("factor" in df and np.allclose(df["factor"].to_numpy(), 1.0)),
                "factor_sample": factor_unique,
                "ohlc_violations": ohlc_violations,
                "extreme_abs_return_gt_25pct_days": extreme_moves,
            }
        # Check internal calendar gaps relative to reference calendar (QQQ)
        ref_cal = pd.DatetimeIndex(pd.read_parquet(fdir / "QQQ.parquet").index)
        for symbol, info in per_ticker.items():
            df = pd.read_parquet(fdir / f"{symbol}.parquet")
            expected = ref_cal[ref_cal >= df.index.min()]
            missing_days = expected.difference(df.index)
            info["missing_sessions_after_listing"] = int(len(missing_days))

        benchmarks: dict[str, Any] = {}
        for bp in sorted(fdir.glob("*benchmark*.parquet")):
            bdf = pd.read_parquet(bp)
            benchmarks[bp.name] = {
                "rows": int(len(bdf)),
                "first_date": str(bdf.index.min().date()),
                "last_date": str(bdf.index.max().date()),
                "nan_count": int(bdf.isna().sum().sum()),
            }
        fixtures_report[subdir] = {
            "tickers": per_ticker,
            "benchmarks": benchmarks,
        }

    # Overlap consistency between fixtures/target and fixtures/history
    overlap_diff: dict[str, Any] = {}
    for symbol in DEFAULT_TICKERS:
        dt = pd.read_parquet(v179_v182_dir / "fixtures" / "target" / f"{symbol}.parquet")
        dh = pd.read_parquet(v179_v182_dir / "fixtures" / "history" / f"{symbol}.parquet")
        common = dt.index.intersection(dh.index)
        overlap_diff[symbol] = {
            "common_sessions": int(len(common)),
            "common_start": str(common.min().date()),
            "common_end": str(common.max().date()),
            "max_rel_diff_close": float(
                ((dt.loc[common, "close"] - dh.loc[common, "close"]).abs() / dh.loc[common, "close"].abs()).max()
            ),
            "max_rel_diff_open": float(
                ((dt.loc[common, "open"] - dh.loc[common, "open"]).abs() / dh.loc[common, "open"].abs()).max()
            ),
            "max_rel_diff_volume": float(
                ((dt.loc[common, "volume"] - dh.loc[common, "volume"]).abs() / dh.loc[common, "volume"].abs()).max()
            ),
        }

    # V69 package integrity and byte-for-byte comparison with V179/V182 shared files
    v69_manifest = json.loads((v69_dir / "source-manifest.json").read_text(encoding="utf-8"))
    v69_mismatches = []
    shared_exact = []
    shared_diff = []
    for rel, meta in sorted(v69_manifest["files"].items()):
        p_v69 = v69_dir / rel
        if not p_v69.is_file() or _sha256(p_v69) != meta["sha256"]:
            v69_mismatches.append(rel)
        p_new = v179_v182_dir / rel
        if p_new.is_file():
            if _sha256(p_v69) == _sha256(p_new):
                shared_exact.append(rel)
            else:
                shared_diff.append(rel)

    # Check V69 original fixture alignment against V179/V182 fixtures/target (float64 parquet & float32 Qlib bin)
    v69_fills = pd.read_csv(v69_dir / "fixture" / "v69_target_normal" / "fills.csv", parse_dates=["date"])
    v69_daily = pd.read_parquet(v69_dir / "fixture" / "v69_target_normal" / "daily.parquet")
    dt_qqq = pd.read_parquet(v179_v182_dir / "fixtures" / "target" / "QQQ.parquet")
    max_open_f64_diff = 0.0
    max_open_f32_diff = 0.0
    for symbol, grp in v69_fills.groupby("ticker"):
        dt = pd.read_parquet(v179_v182_dir / "fixtures" / "target" / f"{symbol}.parquet")
        opens_f64 = dt.loc[grp["date"], "open"].to_numpy(dtype=np.float64)
        opens_f32 = opens_f64.astype(np.float32).astype(np.float64)
        fill_px = grp["price"].to_numpy(dtype=np.float64)
        max_open_f64_diff = max(max_open_f64_diff, float(np.abs(fill_px - opens_f64).max()))
        max_open_f32_diff = max(max_open_f32_diff, float(np.abs(fill_px - opens_f32).max()))

    qqq_close_diff = float(np.abs(v69_daily["QQQ"].to_numpy() - dt_qqq.loc[v69_daily.index, "close"].to_numpy()).max())

    # Inheritance chain audit
    inheritance_chains = {
        "v69": "v69.Strategy -> v61.Strategy -> v51.Strategy -> risk_research.parent.BandParent -> v50.V50Strategy(rule='band') -> v48_strategy.V48Strategy -> qlib.BaseStrategy",
        "v125_growth_leg": "v125.Strategy(Refinement[defence='recovery', allocation='relative'], v105) -> v105.Strategy(ExposurePolicy[scale=4]) -> v100.Strategy(Policy[giveback, ridge_confirm, dual_strength]) -> v69.Strategy",
        "v165_defensive_leg": "v165.Strategy(Reserve[fixed], v161) -> v161.Strategy(StockRisk[tail_budget], v150) -> v150.Strategy(EntryPolicy[half]) -> v125.Strategy",
        "v179": "v179.Strategy(Blend[blend_rule='trend']) -> blends v125 (75%/25%) & v165 (25%/75%) by SP500TR > MA200 & breadth >= 50%",
        "v182": "v182.Strategy(Blend[blend_rule='market_recovery']) -> blends v125 (75%/40%) & v165 (25%/60%) by breadth_diff_10 > 0.10 & SP500TR > MA63",
    }

    return {
        "fixtures": fixtures_report,
        "target_vs_history_overlap": overlap_diff,
        "v69_package_audit": {
            "manifest_files_checked": len(v69_manifest["files"]),
            "manifest_mismatches": v69_mismatches,
            "shared_files_exact_byte_match": shared_exact,
            "shared_files_differing": shared_diff,
            "v69_daily_qqq_close_max_abs_diff_vs_target_fixture": qqq_close_diff,
            "v69_fill_price_max_abs_diff_vs_target_open_float64": max_open_f64_diff,
            "v69_fill_price_max_abs_diff_vs_target_open_float32": max_open_f32_diff,
            "data_version_compatible_with_v179_v182_target_fixture": (qqq_close_diff == 0.0 and max_open_f32_diff < 1e-6),
        },
        "inheritance_chains": inheritance_chains,
        "selection_bias_and_data_limitations": [
            "Universe of 8 tickers (AAPL, SNDK, BA, C, JPM, GE, TQQQ, QQQ) is retrospectively user-specified; survivor and ex-post thematic bias are present (not a PIT market-wide universe).",
            "SNDK only lists on 2025-02-24 (441 sessions in target fixture, 440 in history fixture) and exhibits extreme post-spinoff rally; strategies entering SNDK after 28-session warmup derive a disproportionate share of 2023-2026 net profit from this single ticker.",
            "TQQQ starts 2010-02-11 (absent during 2001-2006 challenge and 2006-2009 GFC).",
            "Fixtures store split- and dividend-adjusted OHLCV with factor=1.0; Qlib binary .day.bin stores float32, introducing ~1e-7 relative (~2.8e-5 USD) open-price quantization compared to float64 parquet.",
            "JPM and QQQ target fixture prices are sourced from StockAnalysis (S&P Global Market Intelligence) cross-checked against Yahoo Finance (max relative error ~5e-8); remaining tickers and ^SP500TR come from Yahoo Finance.",
            "No interest is earned on uninvested cash (0% cash yield); execution assumes exact daily open fill capped at 0.1% of prior-session split-adjusted volume with symmetric 0.1% (normal) or 0.3% (stress) one-way fee.",
        ],
    }


def run_isolated_qlib_reference_replays(
    config: ExternalValidationConfig,
    output_dir: Path,
    *,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Run all 23 archived V179/V182 accounts + V69 original account using isolated Qlib Python."""
    qlib_py = config.packages.qlib_python
    if not qlib_py or not Path(qlib_py).is_file():
        return {
            "status": "SKIPPED_NO_QLIB_ENV",
            "reason": f"Configured qlib_python not found: {qlib_py}",
            "accounts": [],
            "v69_replay": None,
        }

    v182_dir = Path(config.packages.v179_v182_dir)
    v69_dir = Path(config.packages.v69_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Discover exact archived account list via replay.py --list
    list_proc = subprocess.run(
        [qlib_py, str(v182_dir / "replay.py"), "--list"],
        cwd=str(v182_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    account_specs = [line.strip() for line in list_proc.stdout.splitlines() if line.strip()]
    supplied_verif = json.loads((v182_dir / "verification.json").read_text(encoding="utf-8"))
    supplied_by_key = {
        f"{r['version']}/{r['window']}/{r['case']}": r for r in supplied_verif["results"]
    }

    started = perf_counter()

    def _run_one(spec: str) -> dict[str, Any]:
        version, window, case = spec.split("/")
        t0 = perf_counter()
        with tempfile.TemporaryDirectory(prefix=f"qlib-{version}-{window}-{case}-", dir=str(output_dir)) as tmp:
            proc = subprocess.run(
                [
                    qlib_py,
                    str(v182_dir / "replay.py"),
                    "--version",
                    version,
                    "--window",
                    window,
                    "--case",
                    case,
                    "--output",
                    tmp,
                ],
                cwd=str(v182_dir),
                capture_output=True,
                text=True,
            )
            elapsed = perf_counter() - t0
            if proc.returncode != 0:
                return {
                    "account": spec,
                    "version": version,
                    "window": window,
                    "case": case,
                    "status": "FAILED",
                    "elapsed_seconds": elapsed,
                    "stderr": proc.stderr[-1000:],
                    "stdout": proc.stdout[-500:],
                }
            verif_files = list(Path(tmp).glob("*/verification.json"))
            verif = json.loads(verif_files[0].read_text(encoding="utf-8"))
            ref = supplied_by_key.get(spec, {})
            max_metric_diff = max(
                (
                    abs(float(verif[k]) - float(ref[k]))
                    for k in ("net_return", "max_dd", "cagr", "sharpe", "sortino", "calmar", "beta", "alpha", "fees", "exposure")
                    if k in ref and k in verif
                ),
                default=0.0,
            )
            return {
                "account": spec,
                "version": version,
                "window": window,
                "case": case,
                "status": "PASSED",
                "elapsed_seconds": elapsed,
                "days": verif["days"],
                "fills": verif["fills"],
                "max_nav_diff_usd": verif["max_nav_diff_usd"],
                "max_metric_diff_vs_supplied_verification": max_metric_diff,
                "net_return": verif["net_return"],
                "cagr": verif["cagr"],
                "max_dd": verif["max_dd"],
                "sharpe": verif["sharpe"],
                "sortino": verif["sortino"],
                "calmar": verif["calmar"],
                "alpha": verif["alpha"],
                "beta": verif["beta"],
                "exposure": verif["exposure"],
                "fees": verif["fees"],
                "benchmark_return": verif["benchmark_return"],
            }

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(account_specs)))) as pool:
        futs = {pool.submit(_run_one, spec): spec for spec in account_specs}
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: r["account"])

    # 2. Run V69 original strategy via isolated Qlib Provider using V179/V182 fixtures/target
    #    Note: fixtures/target/manifest.json lists 7 raw/*.json HTTP snapshots stripped from the ZIP;
    #    we explicitly verify all 60 present parquet & provider files against manifest.json and record the 7 omitted raw JSONs.
    v69_t0 = perf_counter()
    target_dir = v182_dir / "fixtures" / "target"
    target_manifest = json.loads((target_dir / "manifest.json").read_text(encoding="utf-8"))
    present_checked = 0
    present_mismatches = []
    omitted_raw_entries = []
    for rel, expected_sha in target_manifest.get("hashes", {}).items():
        fp = target_dir / rel
        if not fp.is_file():
            omitted_raw_entries.append(rel)
        else:
            present_checked += 1
            if _sha256(fp) != expected_sha:
                present_mismatches.append(rel)

    with tempfile.TemporaryDirectory(prefix="qlib-v69-verify-", dir=str(output_dir)) as tmp_v69:
        v69_out = Path(tmp_v69) / "out"
        v69_script = Path(tmp_v69) / "run_v69_qlib.py"
        v69_script.write_text(
            """import json, sys
from pathlib import Path
import numpy as np, pandas as pd

v69_dir = Path(sys.argv[1])
market_dir = Path(sys.argv[2])
out_dir = Path(sys.argv[3])
sys.path.insert(0, str(v69_dir))

from beta_research.engine import separate, simulate
from v50.experiment import risk_metrics

plan = json.loads((v69_dir / 'original_configs' / 'research_2023_2026' / 'protocol.json').read_text(encoding='utf-8'))
start, end = plan['target']
market = separate(market_dir, start, end)
stats = simulate('v69', 'target', 'normal', market, output=out_dir, signature='p10-04-v69-qlib-verify', cap=0.375, omit_sndk=False, cost=0.001)

ref = v69_dir / 'fixture' / 'v69_target_normal'
nav = pd.read_parquet(out_dir / 'daily.parquet')
orig_nav = pd.read_parquet(ref / 'daily.parquet')
pd.testing.assert_frame_equal(nav, orig_nav, atol=1e-8, rtol=0, check_exact=False)
for fn in ('targets.csv', 'fills.csv', 'attribution.csv'):
    pd.testing.assert_frame_equal(pd.read_csv(out_dir / fn), pd.read_csv(ref / fn), atol=1e-8, rtol=0, check_exact=False)

sp500 = pd.read_parquet(market_dir / 'benchmark.parquet').close
b_daily = sp500.pct_change(fill_method=None).reindex(nav.index)
marked = risk_metrics(nav.net_daily, b_daily)
marked['benchmark_return'] = float((1 + b_daily).prod() - 1)
ref_sp = pd.read_csv(ref / 'sp500_report.csv').query("case == 'normal'").iloc[0]
for k, v in marked.items():
    assert np.isclose(v, float(ref_sp[k]), atol=1e-7, rtol=0), f'SP500 mismatch {k}: {v} vs {ref_sp[k]}'

out = {
    'reference_replay_verified': True,
    'max_nav_diff_usd': float((nav['nav'] - orig_nav['nav']).abs().max()),
    'qlib_qqq_metrics': stats,
    'sp500_total_return_metrics': marked,
}
(out_dir / 'comparison.json').write_text(json.dumps(out, indent=2), encoding='utf-8')
""",
            encoding="utf-8",
        )
        v69_proc = subprocess.run(
            [qlib_py, str(v69_script), str(v69_dir), str(target_dir), str(v69_out)],
            cwd=str(v69_dir),
            capture_output=True,
            text=True,
        )
        v69_elapsed = perf_counter() - v69_t0
        if v69_proc.returncode == 0:
            v69_comp = json.loads((v69_out / "comparison.json").read_text(encoding="utf-8"))
            v69_replay = {
                "status": "PASSED",
                "verified": bool(v69_comp.get("reference_replay_verified")),
                "elapsed_seconds": v69_elapsed,
                "market_dir_used": str(target_dir),
                "manifest_present_files_verified": present_checked,
                "manifest_present_mismatches": present_mismatches,
                "manifest_omitted_raw_json_files": omitted_raw_entries,
                "max_nav_diff_usd": v69_comp["max_nav_diff_usd"],
                "qlib_qqq_metrics": v69_comp["qlib_qqq_metrics"],
                "sp500_total_return_metrics": v69_comp["sp500_total_return_metrics"],
            }
        else:
            v69_replay = {
                "status": "FAILED",
                "verified": False,
                "elapsed_seconds": v69_elapsed,
                "stderr": v69_proc.stderr[-1000:],
                "stdout": v69_proc.stdout[-500:],
            }

    passed = sum(1 for r in results if r["status"] == "PASSED")
    return {
        "status": "PASSED" if passed == len(account_specs) and v69_replay["status"] == "PASSED" else "PARTIAL_OR_FAILED",
        "elapsed_seconds": perf_counter() - started,
        "qlib_python": qlib_py,
        "total_accounts": len(account_specs),
        "passed_accounts": passed,
        "max_nav_diff_usd_across_23": max(
            (r["max_nav_diff_usd"] for r in results if r["status"] == "PASSED"), default=None
        ),
        "max_metric_diff_vs_supplied_verification": max(
            (r["max_metric_diff_vs_supplied_verification"] for r in results if r["status"] == "PASSED"), default=None
        ),
        "accounts": results,
        "v69_replay": v69_replay,
    }
