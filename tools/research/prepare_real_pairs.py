"""Replay a fixed CSMAR scope once per native workbook; keep all output private."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pandas as pd

from factorlab.data.csmar.binding import replay, reference
from factorlab.data.csmar.catalog import references, operator_paths
from factorlab.data.csmar.prepare import market_candidate
from factorlab.dataset.identity import file_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "catalog", "source-root", "scratch", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    root, scratch, output = operator_paths(args.source_root, args.scratch, args.output)
    output.mkdir(parents=True, exist_ok=False)
    symbols = sorted({s for pair in config["pairs"] for s in pair["symbols"]})
    refs = references(catalog, family="TRDNEW_Dalyr", codes=[s[:6] for s in symbols],
                      start=config["start"], end=config["end"])
    # Native replay has a 64 MiB selected-row budget. Six codes per scope
    # keep four years of full native rows within that existing contract.
    partitioned = []
    for ref in refs:
        entry = next(e for source in catalog["sources"] if source["name"] == ref["source_name"]
                     for e in source["members"] if e["name"] == ref["member"])
        codes = sorted(set(ref["scope"]["codes"]).intersection(entry["codes"]))
        for offset in range(0, len(codes), 6):
            partitioned.append(reference(**{k: ref[k] for k in (
                "source_name", "source_sha256", "source_bytes", "family", "member", "member_sha256",
                "sheet_records", "synthetic_only")}, codes=codes[offset:offset+6],
                start=config["start"], end=config["end"]))
    refs = partitioned
    frames, metrics = [], []
    (output / "partitions").mkdir()
    for index, ref in enumerate(refs):
        print("Replaying", ref["member"], flush=True)
        projection = replay(ref, source_root=root, scratch=scratch)
        metrics.append(projection.metrics)
        partition_frames = []
        for symbol in symbols:
            mask = projection.raw.symbol.eq(symbol)
            if mask.any():
                scoped = replace(projection, **{name: getattr(projection, name).loc[mask].reset_index(drop=True)
                                               for name in ("raw", "native", "lineage")})
                partition_frames.append(market_candidate(scoped, symbol))
        partition = pd.concat(partition_frames, ignore_index=True)
        partition.to_parquet(output / "partitions" / (str(index) + ".parquet"), index=False)
        (output / "partitions" / (str(index) + ".json")).write_text(
            json.dumps(dict(reference=ref, metrics=projection.metrics), ensure_ascii=False), encoding="utf-8")
        frames.append(partition)
        print("Completed", ref["member"], ref["scope"]["codes"], flush=True)
    bars = pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"]).reset_index(drop=True)
    if bars.duplicated(["date", "symbol"]).any():
        raise ValueError("overlapping CSMAR keys; select explicit nonoverlapping native sources")
    bars.to_parquet(output / "bars.parquet", index=False)
    manifest = dict(schema="P5_R03_CSMAR_PRIVATE_V1", references=refs, replay_metrics=metrics,
                    bars_sha256=file_hash(output / "bars.parquet"), config_sha256=file_hash(args.config),
                    catalog_sha256=file_hash(args.catalog), symbols=symbols,
                    start=config["start"], end=config["end"],
                    price_semantics="CSMAR Adjprcwd: comparable close with cash-dividend reinvestment, CNY/share; NOT fill prices",
                    raw_price_unit="CNY/share", execution="BLOCKED_CORPORATE_ACTION_AND_SHORT_LIABILITY_SEMANTICS")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(dict(rows=len(bars), securities=bars.symbol.nunique(), output=str(output))), flush=True)


if __name__ == "__main__":
    main()
