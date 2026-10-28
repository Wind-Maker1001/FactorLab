"""Independent full/current outcome comparison; cannot issue migration approval."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT/'docs/validation/v2/BASELINE_4157.json'
BASELINE_SHA256 = '0624b879d923c839b85b87d932ae2f181d4f289c4f31f40d13f211b0c7c6ce8d'
MATRIX = ROOT/'docs/ci/CI_BASELINE_FAILURE_MATRIX.csv'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def outcomes(path):
    result = {}
    for case in ET.parse(path).getroot().iter('testcase'):
        cls, name = case.get('classname', ''), case.get('name')
        node = cls.replace('.', '/')+'.py::'+name if cls else name.replace('.', '/')+'.py'
        if node in result:
            raise ValueError('duplicate JUnit node: '+node)
        result[node] = ('ERROR' if case.find('error') is not None else
                        'FAIL' if case.find('failure') is not None else
                        'SKIP' if case.find('skipped') is not None else 'PASS')
    if not result:
        raise ValueError('empty JUnit')
    return result


def compare(baseline, historical, full, current, inventory):
    """Use outcome sets, independent of the pytest selection plugin."""
    old = set(baseline['current_nodes'])
    critical = set().union(*(set(n) for n in baseline['critical_nodes'].values()))
    expected = set(full)-set(historical)
    return dict(
        baseline_count=len(old), baseline_missing=sorted(old-set(current)),
        critical_missing=sorted(critical-set(current)),
        missing_current=sorted(expected-set(current)), extra_current=sorted(set(current)-expected),
        outcome_drift={n: dict(full=full[n], current=current[n]) for n in expected & set(current)
                       if full[n] != current[n]},
        current_nonpassing={n: s for n, s in current.items() if s != 'PASS'},
        historical_drift={n: dict(expected=s, actual=full.get(n, 'NOT_OBSERVED'))
                          for n, s in historical.items() if full.get(n) != s},
        inventory_drift=sorted(set(inventory['current']) ^ set(current)),
        historical_partition_drift=sorted((set(inventory['historical_collected']) |
                                           set(inventory['historical_collection_errors'])) ^ set(historical)),
        new_nodes=sorted(set(current)-old))


def comparison_pass(result):
    return result['baseline_count'] == 4157 and not any(result[k] for k in (
        'baseline_missing','critical_missing','missing_current','extra_current','outcome_drift',
        'current_nonpassing','historical_drift','inventory_drift','historical_partition_drift'))


def preservation(baseline):
    """Check original Git blob bytes; report checkout EOL conversion separately.

    V1 raw-byte verifiers are not changed or resealed by this software review.
    Git text checkout conversion is not a new historical byte authorization.
    """
    drift, eol_only = {}, []
    process = subprocess.Popen(['git','cat-file','--batch'], cwd=ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for name, expected in baseline['preserved_path_sha256'].items():
            process.stdin.write(('HEAD:'+name+'\n').encode('utf-8'))
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[1] != b'blob':
                drift[name] = 'ORIGINAL_GIT_BLOB_MISSING'
                continue
            raw = process.stdout.read(int(header[2]))
            assert process.stdout.read(1) == b'\n'
            if hashlib.sha256(raw).hexdigest() != expected:
                drift[name] = 'ORIGINAL_GIT_BLOB_CHANGED'
            elif not (ROOT/name).is_file():
                drift[name] = 'CHECKOUT_FILE_MISSING'
            else:
                working = (ROOT/name).read_bytes()
                if working != raw:
                    if working.replace(b'\r\n',b'\n') == raw.replace(b'\r\n',b'\n'):
                        eol_only.append(name)
                    else:
                        drift[name] = 'CHECKOUT_CONTENT_CHANGED'
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()
    return drift, eol_only


def coverage_rows(baseline, historical_rows, current):
    rows = []
    for node in baseline['current_nodes']:
        rows.append(dict(old_test_id=node, old_status='PASS', layer='CURRENT_SOFTWARE_VALIDATION_V2',
                         v2_test_id=node, relation='IDENTICAL_NODE_AND_ORIGINAL_TEST_BYTES',
                         archive_gap='', observed_status=current.get(node, 'NOT_OBSERVED')))
    for row in historical_rows:
        node = row['test_id']
        rows.append(dict(old_test_id=node, old_status=row['current_failure_status'],
                         layer='HISTORICAL_ARCHIVE_VALIDATION_V1', v2_test_id='',
                         relation='RETAINED_NOT_REPLACED; ORIGINAL_REFUSAL_ASSERTION_NOT_REACHED',
                         archive_gap=row['historical_subcategory'], observed_status='SEE_FULL_JUNIT'))
    for node in sorted(set(current)-set(baseline['current_nodes'])):
        rows.append(dict(old_test_id='', old_status='', layer='CURRENT_SOFTWARE_VALIDATION_V2',
                         v2_test_id=node, relation='ADDITIONAL_RISK_COVERAGE', archive_gap='',
                         observed_status=current[node]))
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--full', type=Path, required=True)
    p.add_argument('--current', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--additional-current', type=Path)
    a = p.parse_args()
    if digest(BASELINE) != BASELINE_SHA256:
        raise ValueError('coverage baseline bytes changed')
    baseline = json.loads(BASELINE.read_bytes())
    with MATRIX.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    historical = {r['test_id']: r['current_failure_status'] for r in rows}
    if len(rows) != 186 or len(historical) != 186 or Counter(historical.values()) != {'FAIL':50,'ERROR':136}:
        raise ValueError('original historical inventory drift')
    full, current = outcomes(a.full/'tests.xml'), outcomes(a.current/'tests.xml')
    inventory = json.loads((a.current/'selection.json').read_bytes())
    full_receipt = json.loads((a.full/'receipt.json').read_bytes())
    current_receipt = json.loads((a.current/'receipt.json').read_bytes())
    result = compare(baseline, historical, full, current, inventory)
    preservation_drift, checkout_eol_only = preservation(baseline)
    same_inputs = (full_receipt['subject_sha'] == current_receipt['subject_sha'] and
                   full_receipt['validation_input_sha256'] == current_receipt['validation_input_sha256'] and
                   full_receipt['validation_inputs_unchanged'] and current_receipt['validation_inputs_unchanged'])
    # Match the checkout to executed inputs, including new tests and the comparator.
    executed = current_receipt['validation_input_sha256']
    source_drift = {n: 'CHECKOUT_DIFFERS_FROM_EXECUTED_INPUTS' for n, h in executed.items()
                    if not (ROOT/n).is_file() or digest(ROOT/n) != h}
    additional = None
    if a.additional_current:
        other = outcomes(a.additional_current/'tests.xml')
        receipt = json.loads((a.additional_current/'receipt.json').read_bytes())
        additional = dict(count=len(other), nonpassing={n:s for n,s in other.items() if s!='PASS'},
                          node_drift=sorted(set(other)^set(current)),
                          same_subject=receipt['subject_sha']==current_receipt['subject_sha'],
                          same_inputs=receipt['validation_input_sha256']==executed and receipt['validation_inputs_unchanged'],
                          exit_code=receipt['exit_code'])
    additional_ok = additional is None or (not additional['nonpassing'] and not additional['node_drift'] and
                                          additional['same_subject'] and additional['same_inputs'] and additional['exit_code']==0)
    passed = (comparison_pass(result) and same_inputs and not preservation_drift and not source_drift and
              full_receipt['exit_code']==1 and current_receipt['exit_code']==0 and additional_ok)
    report = dict(schema='CURRENT_SOFTWARE_VALIDATION_V2_INDEPENDENT_REVIEW',
                  status='PASS' if passed else 'FAIL', subject_sha=full_receipt['subject_sha'],
                  pr_head=current_receipt.get('github_pr_head_sha'),
                  full_count=len(full), current_count=len(current), historical_count=len(historical),
                  full_outcomes=dict(Counter(full.values())), current_outcomes=dict(Counter(current.values())),
                  historical_outcomes=dict(Counter(historical.values())), same_frozen_inputs=same_inputs,
                  preservation_drift=preservation_drift, executed_source_drift=source_drift,
                  checkout_eol_only=checkout_eol_only,
                  additional_current=additional, comparison=result,
                  full_exit_code=full_receipt['exit_code'], current_exit_code=current_receipt['exit_code'],
                  evidence_sha256={str(folder.name)+'/'+name:digest(folder/name)
                                   for folder in (a.full,a.current) for name in ('receipt.json','tests.xml','tests.log')},
                  baseline_sha256=digest(BASELINE), historical_matrix_sha256=digest(MATRIX),
                  independent_human_review='PENDING', HISTORICAL_MIGRATION_APPROVAL='PENDING',
                  historical_archive_certification='BLOCKED', financial_authorization='BLOCKED',
                  pit='PIT_UNVERIFIED', hfq='HFQ_UNVERIFIED', formal_admission='FORMAL_ADMISSION_BLOCKED', no_live_money=True)
    a.output.mkdir(parents=True, exist_ok=False)
    (a.output/'coverage-review.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    with (a.output/'HISTORICAL_V1_V2_COVERAGE_MATRIX.csv').open('w',encoding='utf-8',newline='') as stream:
        mapping = coverage_rows(baseline, rows, current)
        writer = csv.DictWriter(stream, fieldnames=list(mapping[0]))
        writer.writeheader()
        writer.writerows(mapping)
    print(json.dumps({k:report[k] for k in ('status','subject_sha','current_count','historical_count','full_outcomes','same_frozen_inputs')}))
    return int(not passed)


if __name__ == '__main__':
    raise SystemExit(main())
