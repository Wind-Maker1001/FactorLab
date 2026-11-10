"""Synthetic-only daily strategy entry point; real-data admission remains blocked."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from factorlab.backtest.fees import FeeSchedule
from factorlab.errors import FactorLabError
from factorlab.experiments.provenance import capture_provenance
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.strategies.dual_ma_cli import _clean, synthetic_bars
from factorlab.strategies.technical import BollingerConfig, RSIConfig, MACDConfig, CTAConfig
from factorlab.strategies.technical_research import run_technical_backtest, record_technical_run, SAFETY

CONFIGS = {"bollinger": BollingerConfig, "rsi": RSIConfig, "macd": MACDConfig, "cta": CTAConfig}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Phase 5A synthetic daily software research")
    parser.add_argument("strategy", choices=CONFIGS)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if not args.synthetic:
            from factorlab.security import assert_independent_admission
            assert_independent_admission()
        parameters = {} if args.config is None else json.loads(args.config.read_text(encoding="utf-8"))
        config = CONFIGS[args.strategy](**parameters)
        bars, calendar = synthetic_bars(config)
        fees = FeeSchedule.a_share_default()
        result = run_technical_backtest(bars, calendar, config, fee_schedule=fees, source_kind="SYNTHETIC")
        args.output.mkdir(parents=True, exist_ok=False)
        result["signals"].to_csv(args.output / "signals.csv", index=False)
        stats = {}
        for name in ("strategy", "benchmark"):
            leg = result[name]
            for key in ("trades", "nav", "rejections"):
                leg[key].to_csv(args.output / (name + "_" + key + ".csv"), index=False)
            stats[name] = result["evaluations"][name].to_dict()
            stats[name]["rejection_count"] = len(leg["rejections"])
            stats[name]["rejections_by_reason"] = leg["rejections"].reason.value_counts().to_dict()
        report = args.output / "evaluation.json"
        report.write_text(json.dumps(_clean(dict(config=asdict(config), safety=SAFETY,
            synthetic=True, results=stats)), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        provenance = capture_provenance(Path(__file__).resolve().parents[3])
        record_technical_run(ExperimentTracker(args.output / "tracker"), args.strategy, "synthetic",
            result, config, fees, provenance=provenance,
            command=[sys.executable, "-m", "factorlab.strategies.technical_cli", *(sys.argv[1:] if argv is None else argv)],
            artifacts={"evaluation": {"path": str(report.resolve())}})
        print(SAFETY)
        print("SYNTHETIC / SOFTWARE_ONLY:", args.output.resolve())
        return 0
    except (FactorLabError, ValueError, TypeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
