"""Recompute PR #238's affected funding outputs, retaining certified inputs.

Run with the same --r04b-snapshot/--cache-dir/--output-dir as run_p8_r10_study.
Historical funding rows and historical sensitivity are unchanged and reused
from the specified baseline commit. All non-flat synthetic accounting and all
MC paths are rerun. No approximate aggregate funding adjustment is performed.
"""
from __future__ import annotations

import argparse
import ast
from functools import wraps
import hashlib
import io
from importlib.metadata import version
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
os.environ["PYTHONPATH"] = str(ROOT / "src")
for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[variable] = "1"

from factorlab.experiments import grid_r10_study as study
from factorlab.experiments.grid_funding_adaptive import compute_equal_bh_benchmark, load_and_audit_r10_dataset

BASELINE = "a5cd71d3589272ca3ede272d3159ae8b0bbef633"


def baseline_bytes(name):
    return subprocess.check_output(["git", "show", f"{BASELINE}:docs/validation/p8-r10/{name}"], cwd=ROOT)


def checkpoint_identity(snapshot, cache, audit):
    sources = [ROOT / "src/factorlab/experiments" / n for n in
               ("grid_funding_adaptive.py", "grid_r10_study.py", "grid_funding.py")]
    sources += [snapshot / "bars.parquet", snapshot / "manifest.json"]
    sources += sorted(cache.glob("nvdausdt_*"))
    sources += sorted((ROOT / "configs/research").glob("p8_r10_*.json"))
    sources += [ROOT / "docs/validation/p7-r09" / n for n in (
        "R04B-base-baseline-funding-settlements.csv", "R07-base-baseline-funding-settlements.csv", "R07-base-baseline-hourly.csv")]
    runtime = {name: version(name) for name in ("numpy", "pandas", "scipy", "pyarrow")}
    digest = hashlib.sha256(baseline_bytes("summary.json") + json.dumps(audit, sort_keys=True).encode()
                            + sys.version.encode() + json.dumps(runtime, sort_keys=True).encode()
                            + b"".join(x.read_bytes() for x in sources)
                            + Path(__file__).read_bytes()).hexdigest()
    return digest, sources, runtime


def validate_recomputed_results(old, result, output_dir):
    import numpy as np
    import pandas as pd
    assert result["data_audit"] == old["data_audit"]
    assert result["walk_forward_mainnet"] == old["walk_forward_mainnet"]
    assert result["funding_sensitivity"][:24] == old["funding_sensitivity"][:24]
    checked = 0
    for name in ("paired_windows_mainnet.csv", "paired_windows_demo_stress.csv", "paired_windows_regime_switch.csv"):
        before = pd.read_csv(io.BytesIO(baseline_bytes(name)), keep_default_na=False)
        after = pd.read_csv(output_dir / name, keep_default_na=False)
        pd.testing.assert_frame_equal(before[["strategy_id", "window_id", "start_utc", "end_exclusive_utc"]],
                                      after[["strategy_id", "window_id", "start_utc", "end_exclusive_utc"]])
        unchanged = ["trade_net_pnl_usdt", "fees_usdt", "slippage_usdt", "fills_count", "grid_cycles",
                     "residual_inventory", "final_nav_ex_funding_usdt", "fifo_reconciliation_residual"]
        np.testing.assert_allclose(before[unchanged], after[unchanged], rtol=0, atol=1e-8)
        assert after["fifo_reconciliation_residual"].abs().max() < 1e-8
        np.testing.assert_allclose(after["final_net_pnl_usdt"], after["trade_net_pnl_usdt"] + after["funding_pnl_usdt"], atol=1.5e-8, rtol=0)
        np.testing.assert_allclose(after["final_nav_with_funding_usdt"], after["final_nav_ex_funding_usdt"] + after["funding_pnl_usdt"], atol=1.5e-8, rtol=0)
        checked += len(after)
    assert len(result["tail_risk_mc"]) == 36 and all(r["n_paths"] == 64 for r in result["tail_risk_mc"])
    for before, after in zip(old["tail_risk_mc"], result["tail_risk_mc"]):
        assert all(before[k] == after[k] for k in ("scenario", "funding_regime", "strategy_id", "n_paths",
                                                  "mean_fills", "mean_holding_hours", "mean_paused_inv_hours"))
    for before, after in zip(old["deterministic_stress"], result["deterministic_stress"]):
        assert all(before[k] == after[k] for k in ("scenario_id", "strategy_id", "trade_net_pnl_usdt", "fees_usdt",
                                                  "slippage_usdt", "fills_count", "fifo_reconciliation_residual"))
    original = subprocess.check_output(["git", "show", f"{BASELINE}:src/factorlab/experiments/grid_r10_study.py"], cwd=ROOT).decode()
    current = (ROOT / "src/factorlab/experiments/grid_r10_study.py").read_text(encoding="utf-8")
    def definitions(text):
        return {x.name: ast.dump(x, include_attributes=False) for x in ast.parse(text).body if isinstance(x, ast.FunctionDef)}
    a, b = definitions(original), definitions(current)
    for name in ("summarize_window_group", "paired_statistical_test", "determine_verdict", "run_stochastic_mc_tail_risk"):
        assert a[name] == b[name], f"Statistical rule/path generator changed: {name}"
    return {"paired_rows_reconciled": checked, "historical_results_identical": True,
            "trade_costs_and_fills_identical": True, "mc_36_groups_64_paths_regenerated": True,
            "statistics_verdict_and_mc_generator_ast_identical": True}


def finalize_review_evidence(old, result, output_dir, receipt):
    """Export a before/after receipt and regenerate every affected report claim."""
    import pandas as pd
    receipt["validation"] = validate_recomputed_results(old, result, output_dir)
    comparisons = []

    def add(scope, arm, before, after, keys, dd_basis, net_basis="total"):
        r = {"scope": scope, "strategy_id": arm, "drawdown_basis": dd_basis, "net_pnl_basis": net_basis}
        for label, source in (("before", before), ("after", after)):
            for metric, field in keys.items():
                r[f"{label}_{metric}"] = source.get(field) if field else None
        comparisons.append(r)

    group_keys = {"net_pnl_usdt": "total_final_net_pnl_usdt", "funding_pnl_usdt": "total_funding_pnl_usdt",
                  "cvar95_pct": "cvar95_with_funding_pct", "max_drawdown_pct": "mean_max_drawdown_with_funding_pct"}
    for regime, field in (("Mainnet", "walk_forward_mainnet"), ("Switch48h", "walk_forward_regime_switch"),
                          ("DemoStress", "walk_forward_demo_stress")):
        for kind in ("oos_summary", "full_summary"):
            for arm, after in result[field][kind].items():
                add(f"{regime}_{'OOS18' if kind == 'oos_summary' else 'Full27'}", arm,
                    old[field][kind][arm], after, group_keys, "mean_window_max_drawdown")
    mc_keys = {"net_pnl_usdt": "mean_net_usdt", "funding_pnl_usdt": "mean_funding_usdt",
               "cvar95_pct": "cvar95_with_funding_pct", "max_drawdown_pct": "p95_max_drawdown_with_funding_pct"}
    for before, after in zip(old["tail_risk_mc"], result["tail_risk_mc"]):
        assert (before["scenario"], before["funding_regime"], before["strategy_id"]) == (
            after["scenario"], after["funding_regime"], after["strategy_id"])
        add(f"MC_{after['scenario']}_{after['funding_regime']}", after["strategy_id"],
            dict(before, mean_net_usdt=round(before["mean_return_with_funding_pct"] * 7 / 100, 6)),
            dict(after, mean_net_usdt=round(after["mean_return_with_funding_pct"] * 7 / 100, 6)),
            mc_keys, "p95_path_max_drawdown", "mean_derived_from_rounded_return_at_7_USDT")
    stress_keys = {"net_pnl_usdt": "final_net_pnl_usdt", "funding_pnl_usdt": "funding_pnl_usdt",
                   "cvar95_pct": None, "max_drawdown_pct": "max_drawdown_with_funding_pct"}
    for before, after in zip(old["deterministic_stress"], result["deterministic_stress"]):
        assert (before["scenario_id"], before["strategy_id"]) == (after["scenario_id"], after["strategy_id"])
        add(after["scenario_id"], after["strategy_id"], before, after, stress_keys, "single_path_max_drawdown")
    pd.DataFrame(comparisons).to_csv(output_dir / "review_comparison.csv", index=False)
    selected = [r for r in comparisons if
                (r["scope"] in {"Mainnet_OOS18", "Switch48h_OOS18", "DemoStress_OOS18"} and r["strategy_id"] in {"A", "B_wf"})
                or (r["scope"] == "MC_gbm_high_vol_2.0x_switch_demo_22.97bps_h_at_48h" and r["strategy_id"] in {"A_FIXED", "B_B5"})]
    old_b = old["walk_forward_regime_switch"]["oos_summary"]["B_wf"]
    new_b = result["walk_forward_regime_switch"]["oos_summary"]["B_wf"]
    receipt.update(key_comparisons=selected, interpretation=(
        f"旧结算时序夸大了突发高资金费下策略 B 的防御幅度：Switch48h OOS B 总资金费从 "
        f"{old_b['total_funding_pnl_usdt']:+.6f} 变为 {new_b['total_funding_pnl_usdt']:+.6f} USDT，"
        f"总净收益从 {old_b['total_final_net_pnl_usdt']:+.6f} 变为 {new_b['total_final_net_pnl_usdt']:+.6f} USDT。"
        "持续 Demo 高费率下 B 的空仓防御仍为零收益、零资金费；修复后仍有条件性的减亏效果。"
        f"原规范结论仍为 {result['verdict']['canonical_status']}，主网 OOS 的统计证据未变。"
        "结果仅适用于 NO_F11_NO_F08_STRANDED_POSITION_BASELINE，不代表完整 F11/F08，也不能把离线防御解释为已证明的正 Alpha。"
        "表中 MC 净收益均值由已报告的四位小数收益率 × 7 USDT 换算，存在至多 0.0000035 USDT 的舍入误差。"))
    result["review_recompute"] = receipt
    (output_dir / "review_recompute.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output_dir / "REPORT.md").write_text(study.render_report_md(result), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--r04b-snapshot", type=Path, required=True)
    p.add_argument("--cache-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--stage-dir", type=Path, required=True, help="Local trusted checkpoint directory, outside source tree")
    p.add_argument("--workers", type=int, default=12)
    args = p.parse_args()
    old = json.loads(baseline_bytes("summary.json"))
    _, _, audit = load_and_audit_r10_dataset(repo_root=ROOT, r04b_snapshot=args.r04b_snapshot,
                                          cache_dir=args.cache_dir, allow_network_supplement=False)
    if audit != old["data_audit"]:
        raise ValueError("Baseline snapshot/audit changed: reuse is forbidden")
    changed = subprocess.check_output(["git", "diff", BASELINE, "--name-only", "--", "src", "configs"], cwd=ROOT).decode().splitlines()
    allowed = {"src/factorlab/experiments/grid_funding_adaptive.py", "src/factorlab/experiments/grid_r10_study.py"}
    if set(changed) - allowed:
        raise ValueError("Execution engine/config changes invalidate baseline-result reuse")
    for name in ("R04B-base-baseline-funding-settlements.csv", "R07-base-baseline-funding-settlements.csv", "R07-base-baseline-hourly.csv"):
        path = f"docs/validation/p7-r09/{name}"
        original = subprocess.check_output(["git", "show", f"{BASELINE}:{path}"], cwd=ROOT)
        if original.decode("utf-8").splitlines() != (ROOT / path).read_text(encoding="utf-8").splitlines():
            raise ValueError(f"Certified reference changed: {path}")
    # Pin source, baseline, audit and runtime for safe interruption/resumption.
    digest, sources, runtime = checkpoint_identity(args.r04b_snapshot, args.cache_dir, audit)
    stages = args.stage_dir / digest
    stages.mkdir(parents=True, exist_ok=True)
    counts = {"historical_rows_reused": 0, "flat_synthetic_rows_reused": 0, "b_rows_rerun": 0}
    previous = {}
    import pandas as pd
    for regime, name in (("mainnet_actual", "paired_windows_mainnet.csv"),
                         ("switch_0.125_to_22.97_at_48h", "paired_windows_regime_switch.csv"),
                         ("demo_stress_22.97bps_h", "paired_windows_demo_stress.csv")):
        rows = pd.read_csv(io.BytesIO(baseline_bytes(name)), keep_default_na=False).to_dict("records")
        previous[regime] = {(r["strategy_id"], r["window_id"]): r for r in rows}
    execute = study._execute_tasks_parallel

    def reuse_b_rows(tasks, max_workers):
        result, pending, indices = [], [], []
        for t in tasks:
            _, _, w, arm, _, _, _, scale, const, switch, capital, _ = t
            regime = "demo_stress_22.97bps_h" if const is not None else (
                "switch_0.125_to_22.97_at_48h" if switch is not None else "mainnet_actual")
            row = dict(previous[regime][(arm, w["window_id"])])
            if const is None and switch is None:
                if scale != 1:
                    raise ValueError("Only unchanged historical accounting can be reused")
                counts["historical_rows_reused"] += 1
            elif row["fills_count"] == 0:
                assert row["funding_pnl_usdt"] == row["final_net_pnl_usdt"] == row["residual_inventory"] == 0
                # B&H's first settlement now precedes entry, including flat arms.
                bh = compute_equal_bh_benchmark(w["bars"], capital=float(capital),
                                               constant_hourly_bps=const, regime_switch_bps=switch)
                row.update(bh_net_pnl_with_funding_usdt=round(bh["bh_net_pnl_with_funding_usdt"], 8),
                           bh_return_with_funding_pct=round(bh["bh_return_with_funding_pct"], 4),
                           excess_vs_equal_bh_usdt=round(-bh["bh_net_pnl_with_funding_usdt"], 8),
                           excess_vs_equal_bh_pct=round(-bh["bh_return_with_funding_pct"], 4))
                counts["flat_synthetic_rows_reused"] += 1
            else:
                indices.append(len(result))
                pending.append(t)
                row = None
                counts["b_rows_rerun"] += 1
            result.append(row)
        for i, row in zip(indices, execute(pending, max_workers)):
            result[i] = row
        return result

    study._execute_tasks_parallel = reuse_b_rows
    completed_wf = {}
    # Trusted, source-bound local checkpoints. These are never committed.
    def checkpoint(name, fn, key_fn=lambda kw: ""):
        @wraps(fn)
        def call(*a, **kw):
            key = key_fn(kw)
            path = stages / f"{name}{key}.pickle"
            if path.exists():
                print(f"[review] reuse completed stage {path.name}", flush=True)
                value = pickle.loads(path.read_bytes())
            else:
                value = fn(*a, **kw)
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(pickle.dumps(value))
                temporary.replace(path)
            if name == "wf":
                completed_wf[key] = value
            return value
        return call

    study.precompute_ac_multi_regime = checkpoint("ac", study.precompute_ac_multi_regime)
    study.run_expanding_walk_forward = checkpoint("wf", study.run_expanding_walk_forward, lambda kw: kw["regime_label"])

    def sensitivity(*a, **kw):
        rows = []
        mapping = {"demo_22.97bps_h": "demo_stress_22.97bps_h",
                   "switch_0.125_to_22.97_at_48h": "switch_0.125_to_22.97_at_48h"}
        arms = {"A_FIXED": "A_FIXED", "B_ENTRY_B2": "B2", "B_EXIT_B5": "B5", "C_ADAPT_C5": "C5"}
        for r in old["funding_sensitivity"]:
            if r["funding_regime"] not in mapping:
                rows.append(r)
            else:
                wf = completed_wf[mapping[r["funding_regime"]]]
                s = wf["full_summary"][arms[r["strategy_id"]]]
                rows.append({"funding_regime": r["funding_regime"], "strategy_id": r["strategy_id"], **s})
        return rows

    study.run_funding_sensitivity_sweep = sensitivity
    study.run_deterministic_stress_suite = checkpoint("stress", study.run_deterministic_stress_suite)
    study.run_stochastic_mc_tail_risk = checkpoint("mc", study.run_stochastic_mc_tail_risk)
    result = study.run_full_r10_study(repo_root=ROOT, r04b_snapshot=args.r04b_snapshot, cache_dir=args.cache_dir,
                                     output_dir=args.output_dir, mc_paths=64, mc_steps=168,
                                     max_workers=args.workers, allow_network_supplement=False)
    receipt = {"baseline_head": BASELINE, "source_and_input_key": digest, "data_audit_identical": True,
               "reused": "Unchanged historical B rows and six historical sensitivity regimes; flat synthetic B rows with corrected benchmark",
               "recomputed": "All A/C overlays including causal fold fits, non-flat synthetic B, WF selections/statistics, 9 stress scenarios and all 6x64 MC paths under both funding schedules",
               "counts_this_invocation": counts, "mc_seed": 810, "mc_paths": 64, "mc_steps": 168,
               "statistics_and_verdict_rules_changed": False}
    sources += [Path(__file__)]
    receipt["runtime_packages"] = runtime
    receipt["source_and_data_sha256"] = {str(x): hashlib.sha256(x.read_bytes()).hexdigest() for x in sources}
    finalize_review_evidence(old, result, args.output_dir, receipt)
    print(json.dumps(result["verdict"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
