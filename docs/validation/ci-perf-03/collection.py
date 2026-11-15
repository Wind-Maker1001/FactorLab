"""Collect and freeze the current nonhistorical target without running tests."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    selection = output / 'selection.json'
    command = [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'tools.ci.current_validation',
               '--current-layer-inventory=' + str(selection), '--execution-inventory=' + str(output / 'execution.json'),
               '-o', 'cache_dir=' + str(output / 'cache')]
    with (output / 'collection.log').open('wb') as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    inventory = json.loads(selection.read_bytes())
    inventory['module_sha256'] = {m: hashlib.sha256((ROOT / m).read_bytes()).hexdigest()
                                  for m in sorted({n.split('::')[0] for n in inventory['current']})}
    inventory['subject_sha'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    inventory['collection_python'] = sys.version
    (output / 'target-inventory.json').write_text(json.dumps(inventory, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(tests=len(inventory['current']), modules=len(inventory['module_sha256']),
                         subject_sha=inventory['subject_sha'], python=sys.version)))


if __name__ == '__main__':
    main()
