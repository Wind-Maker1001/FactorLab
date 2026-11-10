"""Prepare the fixed retrospective multi-security input from existing assets."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from factorlab.data.research_input import prepare

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'source-root', 'raw-root', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    manifest, report = prepare(args.config, args.source_root, args.raw_root, args.output)
    print(json.dumps(dict(manifest=manifest, quality=report), ensure_ascii=False, indent=2))
