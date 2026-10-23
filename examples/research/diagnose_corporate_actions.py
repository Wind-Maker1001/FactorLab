"""Small explicit evidence diagnostics; no dataset/Forward/trading publication."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from qbot.research.data.corporate_actions.sources.akshare import SinaFactorSource, parse_sina_factors
from qbot.research.data.corporate_actions.sources.local_tdx import LocalTdxActionSource
from qbot.research.data.corporate_actions.sources.official import OfficialNoticeEvidenceSource
from qbot.research.data.corporate_actions.store import publish_snapshot
from qbot.research.errors import FactorLabError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Corporate action evidence only; NO LIVE MONEY")
    parser.add_argument("--source", required=True, choices=("tdx-candidates", "sina-factors", "official-files"))
    parser.add_argument("--security", action="append", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    root = Path(args.output_root)
    try:
        if args.source == "tdx-candidates":
            if len(args.input) != 1:
                parser.error("tdx-candidates requires one --input hq_cache directory")
            source = LocalTdxActionSource(Path(args.input[0]))
        elif args.source == "sina-factors":
            source = SinaFactorSource(root / "source-bytes")
        else:
            source = OfficialNoticeEvidenceSource([Path(p) for p in args.input])
        snapshot = source.snapshot(args.security, args.start, args.end)
        folder = publish_snapshot(snapshot, root / "events")
        observations = []
        if args.source == "sina-factors":
            observations = [list(parse_sina_factors(a.read())) for a in snapshot.artifacts]
        print(json.dumps({"event_generation": str(folder.resolve()), "source": snapshot.source_id,
                          "events": len(snapshot.events), "issues": snapshot.issues,
                          "factor_observations": observations, "financial_research_eligible": False,
                          "no_live_money": True}, ensure_ascii=False))
        return 1 if snapshot.issues else 0
    except (FactorLabError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc), "financial_research_eligible": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
