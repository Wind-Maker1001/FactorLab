"""Publish compact P7-R14 synthetic evidence without account databases."""
import argparse
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

import pandas as pd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--smoke', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--tests', nargs='+', required=True)
    args = parser.parse_args()
    source, output = Path(args.source), Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    for name in ('definition.json', 'generation.json', 'calibration.json', 'paths.csv', 'paired.csv',
                 'summary.json', 'reconciliation.json', 'evaluation.json', 'representative-tails.json'):
        shutil.copyfile(source / name, output / name)
    smoke = Path(args.smoke)
    for name in ('summary.json', 'paths.csv', 'reconciliation.json'):
        shutil.copyfile(smoke / name, output / ('smoke-' + name))
    audits = json.loads((source / 'reconciliation.json').read_text())
    checks = dict(accounts=len(audits),
        maximum_fifo_residual=max(abs(r['fifo_residual']) for r in audits),
        maximum_nav_difference=max(abs(r['terminal_nav'] - r['exported_terminal_equity']) for r in audits),
        maximum_fee_difference=max(abs(r['actual_fees'] - r['ledger_fees']) for r in audits),
        maximum_slippage_difference=max(abs(r['actual_slippage'] - r['ledger_slippage']) for r in audits),
        maximum_inventory_difference=max(abs(r['terminal_inventory'] - r['exported_terminal_inventory']) for r in audits),
        fill_counts_equal=all(r['fills'] == r['ledger_fills'] for r in audits))
    assert checks['maximum_fifo_residual'] < 1e-8
    assert checks['maximum_nav_difference'] < 1e-8
    assert checks['maximum_fee_difference'] < 1e-8
    assert checks['maximum_slippage_difference'] < 1e-8
    assert checks['maximum_inventory_difference'] < 1e-8
    assert checks['fill_counts_equal']
    rows = pd.read_csv(source / 'paths.csv')
    assert rows.groupby(['model', 'path_id']).path_hash.nunique().eq(1).all()
    assert rows.groupby(['model', 'path_id']).arm.nunique().eq(3).all()
    tests = []
    for path in args.tests:
        tree = ET.parse(path)
        suites = list(tree.getroot().iter('testsuite'))
        tests.append(dict(file=Path(path).name, tests=sum(int(s.attrib['tests']) for s in suites),
            failures=sum(int(s.attrib['failures']) for s in suites),
            errors=sum(int(s.attrib['errors']) for s in suites),
            seconds=sum(float(s.attrib['time']) for s in suites)))
    checks.update(paired_paths=rows.groupby(['model', 'path_id']).ngroups,
        paired_price_identity_equal=True, tests=tests)
    (output / 'checks.json').write_text(json.dumps(checks, indent=2) + '\n')
    columns = ['terminal_nav', 'net_return', 'maximum_drawdown', 'average_exposure',
        'participation_rate', 'zero_fills', 'traded_quantity', 'fees', 'risk_exit_quantity',
        'unfinished_exit_quantity', 'residual_inventory_loss']
    rows.groupby(['model', 'arm'])[columns].mean().to_csv(output / 'attribution.csv')
    print(json.dumps(checks, indent=2))


if __name__ == '__main__':
    main()
