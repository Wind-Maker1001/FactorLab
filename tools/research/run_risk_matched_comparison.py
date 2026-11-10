"""Run P7-02 frozen-account entry-budget research; NO LIVE MONEY."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from factorlab.strategies.risk_comparison import run, COMPLETED


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'research-root', 'source-root', 'baseline-root', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    receipt = run(args.config, args.research_root, args.source_root, args.baseline_root, args.output)
    print(json.dumps({k: receipt[k] for k in ('status', 'real_accounts_run')}, ensure_ascii=False))
    return 0 if receipt['status'] == COMPLETED else 2


if __name__ == '__main__':
    raise SystemExit(main())
