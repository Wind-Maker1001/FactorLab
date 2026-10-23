"""Query pinned historical STATUS candidates. NO LIVE MONEY."""

import argparse
import json
from pathlib import Path

from qbot.research.data.evidence import SourceArtifact
from qbot.research.universe.historical_status import HistoricalStatus


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--acquired-at', required=True)
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--decision-at', required=True)
    args = parser.parse_args()
    source = SourceArtifact(str(args.bundle), args.expected_sha256,
                            args.bundle.stat().st_size, args.source_id,
                            args.acquired_at, args.bundle.name)
    adapter = HistoricalStatus.load(source)
    print(json.dumps(adapter.query(args.symbol, args.date,
                                   decision_at=args.decision_at),
                     sort_keys=True, ensure_ascii=False))


if __name__ == '__main__':
    main()
