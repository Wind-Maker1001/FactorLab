"""One-shot entry point suitable for existing cron / Windows Task Scheduler."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from factorlab.errors import FactorLabError
from factorlab.research.automatic import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded historical Champion selection")
    for name in ("config", "store", "state"):
        parser.add_argument("--"+name, required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output")
    destination.add_argument("--output-root", help="Scheduled runs create <output-root>/<study_id>")
    args = parser.parse_args(argv)
    try:
        output = args.output
        if args.output_root:
            config = json.loads(Path(args.config).read_text(encoding="utf-8"))
            study_id = config.get("study_id") if isinstance(config, dict) else None
            if not isinstance(study_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", study_id):
                raise ValueError("invalid study_id")
            output = Path(args.output_root) / study_id
        result = run(args.config, output=output, store=args.store, state_path=args.state,
                     command=[sys.executable, *sys.argv])
    except (ValueError, OSError, FactorLabError) as exc:
        print(json.dumps(dict(status="REJECTED", reason=str(exc))), file=sys.stderr)
        return 2
    print(json.dumps({k: result[k] for k in ("study_id", "version", "selected", "promoted", "reason")}, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
