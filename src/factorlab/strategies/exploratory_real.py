"""Explicit retrospective raw-price research, separate from formal admission."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from io import BytesIO
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys

import pandas as pd

from factorlab.data.evidence import sha256
from factorlab.errors import FactorLabError, SchemaError
from factorlab.backtest.fees import FeeSchedule
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.strategies.dual_ma import DualMAConfig, run_software_backtest
from factorlab.strategies.dual_ma_cli import _clean, _json, metrics

SCOPE = "EXPLORATORY_REAL_DATA"
LIMITATIONS = [
    "RAW_PRICE_RETURN_NET_OF_TRADING_COSTS; NOT_DIVIDEND_TOTAL_RETURN",
    "TOTAL_RETURN_UNAVAILABLE; CORPORATE_ACTION_COVERAGE_PARTIAL_OR_UNKNOWN",
    "RAW_CLOSE_SIGNALS; NO_VENDOR_ADJUSTMENT_FACTORS_CONSUMED",
    "RETROSPECTIVE_VENDOR_OBSERVATIONS; HISTORICAL_VINTAGE_UNKNOWN",
    "OBSERVED_SECURITY_SESSIONS_ONLY; OFFICIAL_CALENDAR_UNKNOWN; NO_FILL",
    "STATUS_RETROSPECTIVE_UNCERTIFIED; DAILY_OHLC_CANNOT_PROVE_QUEUE_FILLS",
    "PRICE_LIMITS_USE_PREVIOUS_RAW_CLOSE; EX_DATE_REFERENCE_NOT_EVENT_ADJUSTED",
    "RESEARCH_FEE_SCHEDULE; NOT_CERTIFIED_HISTORICAL_BROKER_COSTS",
    "NO_LIVE_MONEY; NO_FORWARD_OR_FORMAL_ADMISSION",
]


def load_csmar(root, expected_manifest):
    """Read the existing prepared contract; bind every consumed payload."""
    root = Path(root)
    payload = (root / "manifest.json").read_bytes()
    if sha256(payload) != expected_manifest:
        raise SchemaError("CSMAR manifest differs from pinned configuration")
    manifest = json.loads(payload)
    if (manifest["schema"] != "CSMAR_LOCAL_RESEARCH_CANDIDATE_V1"
            or manifest["source"] != "CSMAR_ONLY"):
        raise SchemaError("explicit CSMAR prepared candidate contract required")
    consumed = {}
    for name in ("market/bars.parquet", "calendar/observed_sessions.parquet",
                 "corporate_actions/events.json", "provenance/references.json"):
        data = (root / name).read_bytes()
        if sha256(data) != manifest["files"][name]:
            raise SchemaError("CSMAR payload differs from manifest: " + name)
        consumed[name] = data
    bars = pd.read_parquet(BytesIO(consumed["market/bars.parquet"]))
    calendar = pd.DatetimeIndex(pd.read_parquet(BytesIO(consumed["calendar/observed_sessions.parquet"]))["date"])
    return bars, calendar, manifest, json.loads(consumed["corporate_actions/events.json"])


def run_price_research(bars, calendar, config, fees):
    """Use raw prices deliberately; supplied adjusted closes are never signals."""
    raw = bars.copy()
    raw["adj_close"] = raw["raw_close"]
    result = run_software_backtest(raw, calendar, config, fee_schedule=fees)
    result.update(scope=SCOPE, return_basis="RAW_PRICE_RETURN_NET_OF_TRADING_COSTS",
                  total_return={"status": "UNAVAILABLE", "value": None,
                                "reason": "incomplete corporate-action coverage; no event ledger consumed"},
                  limitations=LIMITATIONS.copy())
    return result, raw


def run(config_path, source_root, output):
    configuration = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if configuration["source"] != "CSMAR_PREPARED":
        raise SchemaError("source must explicitly select CSMAR_PREPARED")
    config = DualMAConfig(**configuration["strategy"])
    fees = FeeSchedule.a_share_default(**configuration["fees"])
    bars, calendar, source, events = load_csmar(source_root, configuration["manifest_sha256"])
    warmup = pd.Timestamp(configuration["warmup_start"])
    if warmup >= pd.Timestamp(config.start):
        raise SchemaError("warmup_start must precede research start")
    bars = bars.loc[pd.to_datetime(bars.date).between(warmup, pd.Timestamp(config.end))].reset_index(drop=True)
    calendar = calendar[(calendar >= warmup) & (calendar <= pd.Timestamp(config.end))]
    result, raw = run_price_research(bars, calendar, config, fees)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    _json(output / "config.json", configuration)
    raw.to_csv(output / "input_bars.csv", index=False, float_format="%.17g")
    pd.DataFrame({"date": calendar}).to_csv(output / "calendar.csv", index=False)
    result["signals"].to_csv(output / "signals.csv", index=False, float_format="%.17g")
    stats = {}
    context = EvaluationContext(dataset_id=configuration["manifest_sha256"],
        universe_id=config.symbol, label="raw_price_return", label_id="raw_close_no_distributions",
        benchmark_id="buy_and_hold_same_execution", calendar=tuple(result["signals"].date.dt.strftime("%Y-%m-%d")),
        holding_days=1, source_kind="REAL_CANDIDATE")
    evaluator = ResearchEvaluation(context)
    for name in ("strategy", "benchmark"):
        leg = result[name]
        for key in ("nav", "trades", "rejections"):
            leg[key].to_csv(output / (name + "_" + key + ".csv"), index=False, float_format="%.17g")
        stats[name] = _clean(metrics(leg))
        evaluation_nav = leg["nav"].copy()
        benchmark = result["benchmark"]["nav"]
        evaluation_nav["benchmark_nav"] = benchmark.strategy_nav
        evaluation_nav["benchmark_return"] = benchmark.strategy_return
        costs = leg["trades"].groupby("execution_date").transaction_cost.sum()
        evaluation_nav["transaction_cost"] = evaluation_nav.date.map(costs).fillna(0.0)
        evaluated = evaluator.portfolio(name, evaluation_nav, trades=leg["trades"])
        _json(output / (name + "_evaluation.json"), evaluated.to_dict())
    _json(output / "metrics.json", stats)
    package = Path(__file__).resolve().parents[1]
    repo = package.parent.parent
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], text=True).strip()
    manifest = dict(schema="FACTORLAB_EXPLORATORY_REAL_V1", scope=SCOPE, actual_real_run="SUCCEEDED",
        source=source, source_manifest_sha256=configuration["manifest_sha256"], known_action_candidates=events,
        return_basis=result["return_basis"], total_return=result["total_return"], limitations=LIMITATIONS,
        formal_admission="FORMAL_ADMISSION_BLOCKED", execution_admission=False, forward_admission=False,
        no_live_money=True, pit_status="PIT_UNVERIFIED", hfq_status="HFQ_UNVERIFIED",
        rows=len(raw), warmup_rows=int((raw.date < pd.Timestamp(config.start)).sum()),
        research_rows=len(result["signals"]), first_observed=str(raw.date.min().date()),
        last_observed=str(raw.date.max().date()), config=configuration,
        fees=[{k: str(v) if k.startswith("effective_") and v is not None else v for k, v in asdict(rule).items()} for rule in fees.rules],
        code_commit=commit, code_tree=tree,
        source_sha256={p.relative_to(package).as_posix(): sha256(p.read_bytes()) for p in sorted(package.rglob("*.py"))},
        environment={"python": sys.version, "platform": platform.platform(),
                     "packages": {name: importlib.metadata.version(name) for name in ("numpy", "pandas", "pyarrow")}},
        reproduction=[sys.executable, "-I", "-B", "-X", "utf8", str(repo / "tools/research/run_exploratory_real.py"), "--config",
                      str((output / "config.json").resolve()), "--source-root", str(Path(source_root).resolve()),
                      "--output", "<NEW_OUTPUT_DIRECTORY>"],
        artifacts_sha256={p.name: sha256(p.read_bytes()) for p in sorted(output.iterdir())})
    _json(output / "manifest.json", manifest)
    tracker = ExperimentTracker(output / "tracker")
    tracker.create_experiment("price-research", dict(model="dual_ma_vs_buy_hold", parameters=asdict(config),
        factors=[], dataset={"source": configuration["source"], "manifest_sha256": configuration["manifest_sha256"]},
        label={"return_basis": result["return_basis"]}, windows={"train": None, "validation": None,
        "test": [config.start, config.end]}, walk_forward={"enabled": False}, costs=configuration["fees"]))
    tracker.append_run("price-research", "comparison", code={"commit": commit, "tree": tree},
        environment=manifest["environment"], command=manifest["reproduction"], status="SUCCEEDED",
        qualification="REAL_CANDIDATE", portfolio_metrics={name + ":" + key: value
            for name, values in stats.items() for key, value in values.items()},
        artifacts={"manifest": {"path": str((output / "manifest.json").resolve())}},
        validation={"scope": SCOPE, "total_return": result["total_return"], "limitations": LIMITATIONS},
        timestamp=datetime.now(timezone.utc).isoformat())
    return manifest, stats


def main(argv=None):
    parser = argparse.ArgumentParser(description="EXPLORATORY_REAL_DATA: retrospective CSMAR raw-price research only")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest, stats = run(args.config, args.source_root, args.output)
        print(json.dumps({"actual_real_run": manifest["actual_real_run"], "scope": SCOPE, "metrics": stats}, ensure_ascii=False))
        return 0
    except (FactorLabError, ValueError, KeyError, TypeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
