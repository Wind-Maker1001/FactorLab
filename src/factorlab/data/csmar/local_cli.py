"""Small explicit-source interface: python -m factorlab.data.csmar.local_cli."""
import argparse
import json
from pathlib import Path

from .catalog import catalog_source, operator_paths, write_json


def main():
    parser = argparse.ArgumentParser(description="Read-only CSMAR candidates; NO LIVE MONEY")
    subs = parser.add_subparsers(dest="command", required=True)
    listing = subs.add_parser("list")
    listing.add_argument("--source-root", required=True, type=Path)
    listing.add_argument("--scratch", required=True, type=Path)
    listing.add_argument("--output", required=True, type=Path)
    listing.add_argument("--scan-family", action="append", default=[])
    listing.add_argument("--source-name", action="append", default=[])
    prep = subs.add_parser("prepare")
    prep.add_argument("--catalog", required=True, type=Path)
    prep.add_argument("--source-root", required=True, type=Path)
    prep.add_argument("--scratch", required=True, type=Path)
    prep.add_argument("--output", required=True, type=Path)
    prep.add_argument("--symbol", required=True)
    prep.add_argument("--start", required=True)
    prep.add_argument("--end", required=True)
    prep.add_argument("--warmup-start", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        from .prepare import prepare
        path = prepare(json.loads(args.catalog.read_bytes()), source_root=args.source_root,
                       scratch=args.scratch, output=args.output, symbol=args.symbol,
                       start=args.start, end=args.end, warmup_start=args.warmup_start)
        print(path)
        return
    args.source_root, args.scratch, args.output = operator_paths(args.source_root, args.scratch, args.output)
    args.output.mkdir(parents=True, exist_ok=False)
    paths = sorted(args.source_root.glob("*.zip"))
    if args.source_name:
        paths = [p for p in paths if p.name in args.source_name]
        if set(p.name for p in paths) != set(args.source_name):
            parser.error("one or more explicit source names absent")
    sources = []
    # New small-date shard first; each source checkpoint survives interruption.
    paths.sort(key=lambda p: (not p.name.startswith("日个股回报率文件000"), p.name))
    for i, path in enumerate(paths):
        print("SCANNING", path.name, flush=True)
        source = catalog_source(path, scratch=args.scratch, scan_families=args.scan_family)
        sources.append(source)
        write_json(args.output / (str(i).zfill(3) + ".json"), source)
        print("DONE", path.name, flush=True)
    write_json(args.output / "catalog.json", dict(schema="CSMAR_LOCAL_CATALOG_V1", sources=sources,
               source_priority="EXPLICIT_CSMAR_ONLY; overlapping keys rejected", formal_status="FORMAL_ADMISSION_BLOCKED"))


if __name__ == "__main__":
    main()
