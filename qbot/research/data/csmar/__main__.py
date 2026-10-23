"""Offline CLI: explicit operator roots; references never choose a trust root."""

import argparse
import json
from pathlib import Path

from .binding import replay, verify_candidate, write_candidate


def main():
    parser = argparse.ArgumentParser(description="CSMAR局部候选重放；NO LIVE MONEY")
    parser.add_argument("operation", choices=["replay", "build", "verify"])
    parser.add_argument("input", type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    kwargs = {"source_root": args.source_root, "scratch": args.scratch}
    if args.operation == "verify":
        result = verify_candidate(args.input, **kwargs)
    else:
        ref = json.loads(args.input.read_bytes())
        if args.operation == "build":
            if args.output is None:
                parser.error("build requires --output (a new directory)")
            _, _, result = write_candidate(ref, output_root=args.output, **kwargs)
        else:
            result = replay(ref, **kwargs)
    print(json.dumps(result.metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
