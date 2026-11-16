"""Run with: python docs/validation/p7-m14/benchmark.py [--output FILE]."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ, FACTORLAB_M14_MEASURE='1')
    command = [sys.executable, '-m', 'pytest', '-q', '-s',
        'tests/integrations/test_demo_reconciliation_m14.py',
        'tests/integrations/test_demo_observability.py::test_historical_orders_measurement',
        'tests/integrations/test_demo_observability.py::test_idle_recovery_measurement']
    result = subprocess.run(command, cwd=root, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    print(result.stdout, end='')
    if args.output:
        args.output.write_text(json.dumps(dict(command=command, returncode=result.returncode,
                                              output=result.stdout), indent=2) + '\n', encoding='utf-8')
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
