"""Independent EXPLORATORY command; never invokes the formal research CLI."""

import argparse
import json
from pathlib import Path

from .runner import run_research, verify_receipt


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="EXPLORATORY / UNVERIFIED / NO LIVE MONEY"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="evaluate an authorized real private dataset")
    run.add_argument("--manifest", required=True, type=Path)
    run.add_argument("--output", type=Path, default=Path("results/exploratory"))
    verify = commands.add_parser("verify", help="verify a derived-artifact receipt")
    verify.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "verify":
        payload = verify_receipt(args.receipt)
        print(
            json.dumps(
                {
                    "verified": True,
                    "status": payload["status"],
                    "purpose": payload["purpose"],
                }
            )
        )
        return 0
    result = run_research(args.manifest, args.output)
    print(
        json.dumps(
            {
                "status": result.status,
                "output_dir": str(result.output_dir),
                "reasons": result.reasons,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.status == "SUCCESS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
