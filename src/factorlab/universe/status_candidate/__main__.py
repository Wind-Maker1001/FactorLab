"""Offline candidate export; never publishes formal qualifications."""
import argparse, csv, json, sys
from pathlib import Path
from .adapter import Adapter, EvidenceError
from .ingest import sha256

def flatten(row):
    native = row['native_observation'] or {}
    output = {k: v for k, v in row.items() if k not in ['native_observation', 'official_evidence', 'role_statuses', 'conflict_fields']}
    for k in ['native_trdsta','native_trdsta_raw','native_label','native_state_code','native_filling','native_source_sha256','native_source_coordinate','native_state_source_sha256','native_state_source_coordinate','record_source_id','record_coordinate','membership_source_coordinate']:
        output[k] = native.get(k, 'UNKNOWN')
    for field in ['official_evidence', 'role_statuses', 'conflict_fields']:
        output[field] = json.dumps(row[field], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return output

def run(bundle, pin, output):
    adapter = Adapter.load(Path(bundle), pin)
    rows = adapter.evaluate_all()
    target = Path(output).resolve()
    for root in adapter.forbidden_output_roots:
        if target == root or root in target.parents:
            raise EvidenceError('PROTECTED_OUTPUT_ROOT')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise EvidenceError('OUTPUT_EXISTS_REFUSE_OVERWRITE')
    target.mkdir(parents=True, exist_ok=True)
    csvpath = target/'candidate_status.csv'
    flattened = [flatten(row) for row in rows]
    with csvpath.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flattened[0]) if flattened else ['security','trading_date'])
        writer.writeheader();writer.writerows(flattened)
    jsonpath=target/'candidate_status.json'
    jsonpath.write_text(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':')), encoding='utf-8')
    receipt={'schema':'ISOLATED_CANDIDATE_EXECUTION_RECEIPT_V1','bundle_sha256':pin,'rows':len(rows),'output_csv_sha256':sha256(csvpath),'output_json_sha256':sha256(jsonpath),'source_hash_metrics':adapter.metrics,'role_statuses':rows[0]['role_statuses'] if rows else {},'qualification_granted':False,'execution_true_count':sum(r['execution_eligible']=='TRUE' for r in rows)}
    (target/'candidate_receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2),encoding='utf-8')
    return receipt

def main():
    parser=argparse.ArgumentParser(description='Offline field-scoped candidates only')
    parser.add_argument('--bundle',required=True);parser.add_argument('--expected-sha256',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(run(args.bundle,args.expected_sha256,args.output),ensure_ascii=False))
    except (EvidenceError,OSError,ValueError) as exc:
        print(str(exc),file=sys.stderr);return 2
    return 0
if __name__=='__main__':raise SystemExit(main())
