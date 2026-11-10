"""Run pinned independent-account research or record explicit missing-input blockage."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from factorlab.strategies.robustness import blocked_report


def main(argv=None):
    parser = argparse.ArgumentParser(description="P6-R01: WAITING_FOR_P6_D01_INPUT; NO LIVE MONEY")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--research-root", type=Path)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args(argv)
    if args.research_root is not None and args.source_root is not None:
        from factorlab.strategies.robustness_real import run
        summary = run(args.config, args.research_root, args.source_root, args.output)
        print(json.dumps({k: v for k, v in summary.items() if k != 'tables'}, ensure_ascii=False))
        return 0
    configuration = json.loads(args.config.read_text(encoding="utf-8"))
    if configuration["input_status"] != "WAITING_FOR_P6_D01_INPUT" or configuration["qualified_windows"]:
        parser.error("ready inputs require both --research-root and --source-root")
    args.output.mkdir(parents=True, exist_ok=False)
    report = blocked_report(configuration)
    (args.output / "config.json").write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
