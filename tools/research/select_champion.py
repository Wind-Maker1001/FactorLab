"""One-shot entry point suitable for existing cron / Windows Task Scheduler."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from factorlab.errors import FactorLabError
from factorlab.research.automatic import replay, run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded historical Champion selection")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config")
    source.add_argument("--replay-from", help="Original research run directory")
    parser.add_argument("--audit-dir", help="New directory for replay output, tracker and state")
    parser.add_argument("--store")
    parser.add_argument("--state")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--output")
    destination.add_argument("--output-root", help="Scheduled runs create <output-root>/<study_id>")
    args = parser.parse_args(argv)
    if args.replay_from:
        if not args.audit_dir or any((args.output, args.output_root, args.store, args.state)):
            parser.error("--replay-from requires --audit-dir and uses its own output, store and state")
    elif args.audit_dir or not (args.store and args.state and (args.output or args.output_root)):
        parser.error("--config requires --store, --state and --output or --output-root")
    try:
        command = [sys.executable, *sys.argv]
        if args.replay_from:
            result = replay(args.replay_from, audit_dir=args.audit_dir, command=command)
        else:
            output = args.output
            if args.output_root:
                config = json.loads(Path(args.config).read_text(encoding="utf-8"))
                study_id = config.get("study_id") if isinstance(config, dict) else None
                if not isinstance(study_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", study_id):
                    raise ValueError("invalid study_id")
                output = Path(args.output_root) / study_id
            result = run(args.config, output=output, store=args.store, state_path=args.state, command=command)
    except (ValueError, OSError, FactorLabError) as exc:
        print(json.dumps(dict(status="REJECTED", reason=str(exc))), file=sys.stderr)
        return 2
    print(json.dumps({k: result[k] for k in ("study_id", "version", "selected", "promoted", "reason")}, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
