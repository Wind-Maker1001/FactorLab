"""Independently compare full JUnit outcomes against the current-layer inventory."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def outcomes(path):
    result = {}
    for case in ET.parse(path).getroot().iter('testcase'):
        classname, name = case.get('classname', ''), case.get('name')
        node = classname.replace('.', '/')+'.py::'+name if classname else name.replace('.', '/')+'.py'
        if node in result:
            raise ValueError('duplicate JUnit node: '+node)
        result[node] = ('ERROR' if case.find('error') is not None else
                        'FAIL' if case.find('failure') is not None else
                        'SKIP' if case.find('skipped') is not None else 'PASS')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full', type=Path, required=True, help='Full suite output directory')
    parser.add_argument('--current', type=Path, required=True, help='Current suite output directory')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    matrix = Path(__file__).resolve().parents[2]/'docs/ci/CI_BASELINE_FAILURE_MATRIX.csv'
    with matrix.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    historical = {row['test_id']: row['current_failure_status'] for row in rows}
    full, current = outcomes(args.full/'tests.xml'), outcomes(args.current/'tests.xml')
    inventory = json.loads((args.current/'selection.json').read_text(encoding='utf-8'))
    full_receipt = json.loads((args.full/'receipt.json').read_text(encoding='utf-8'))
    current_receipt = json.loads((args.current/'receipt.json').read_text(encoding='utf-8'))
    expected_current = set(full) - set(historical)
    missing = sorted(expected_current - set(current))
    extra = sorted(set(current) - expected_current)
    status_changes = {node: dict(full=full[node], current=current[node])
                      for node in set(current) & expected_current if full[node] != current[node]}
    historical_drift = {node: dict(expected=status, actual=full.get(node, 'NOT_OBSERVED'))
                        for node, status in historical.items() if full.get(node) != status}
    same_subject = (full_receipt['subject_sha'] == current_receipt['subject_sha'] and
                    full_receipt['validation_input_sha256'] == current_receipt['validation_input_sha256'] and
                    full_receipt['validation_inputs_unchanged'] and current_receipt['validation_inputs_unchanged'])
    inventory_matches = set(inventory['current']) == set(current)
    historical_partition = (set(inventory['historical_collected']) |
                            set(inventory['historical_collection_errors'])) == set(historical)
    boundary_pass = (same_subject and inventory_matches and historical_partition and
                     not missing and not extra and not status_changes and not historical_drift and len(historical) == 186)
    report = dict(schema='FACTORLAB_INDEPENDENT_LAYER_COMPARISON_V1',
                  status='PASS' if boundary_pass else 'FAIL',
                  subject_sha=full_receipt['subject_sha'], same_frozen_inputs=same_subject,
                  matrix_sha256=hashlib.sha256(matrix.read_bytes()).hexdigest(),
                  full_count=len(full), current_count=len(current), historical_count=len(historical),
                  missing_current=missing, extra_current=extra, status_changes=status_changes,
                  historical_status_drift=historical_drift, inventory_matches=inventory_matches,
                  historical_partition_matches=historical_partition,
                  nonhistorical_nonpassing={n: s for n, s in full.items() if n not in historical and s != 'PASS'},
                  full_exit_code=full_receipt['exit_code'], current_exit_code=current_receipt['exit_code'],
                  independence='Separate outcome-set comparison, not a third-party approval or historical certification',
                  financial_authorization='BLOCKED')
    args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))
    return int(not boundary_pass)


if __name__ == '__main__':
    raise SystemExit(main())
