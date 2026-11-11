"""Verify public DQ02 coverage conservation and record focused validation."""
import argparse
import csv
import json
from pathlib import Path
import subprocess
import sys


def load(path):
    with path.open(encoding='utf-8',newline='') as stream:
        return list(csv.DictReader(stream))


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--private-root',type=Path,required=True)
    args=p.parse_args()
    coverage=load(args.output/'scan-coverage.csv')
    tables=load(args.output/'table-quality.csv')
    discovery=json.loads((args.private_root/'discovery.json').read_text(encoding='utf-8'))
    expected={j['id'] for j in discovery['jobs']}
    found={r['partition'] for r in coverage}
    if expected!=found or len(coverage)!=len(expected):
        raise ValueError('Physical member denominator conservation failed')
    for row in coverage:
        if row['status']=='FULL_CONTENT_SCANNED' and row['checked_rows']!=row['physical_data_rows']:
            raise ValueError('Full-content row conservation failed')
    total=sum(int(r['checked_rows']) for r in coverage)
    if total!=sum(int(r['readable_scanned_rows']) for r in tables):
        raise ValueError('Table/partition row conservation failed')
    command=[sys.executable,'-I','-m','pytest','tests/evidence/test_dq02_native.py','tests/evidence/test_dq02_parquet.py','tests/evidence/test_dq02_finalize.py','-q']
    result=subprocess.run(command,capture_output=True,text=True)
    record=dict(status='PASS' if result.returncode==0 else 'FAIL',physical_members=len(expected),checked_rows=total,
                coverage_contract='FULL_CONTENT_SCANNED means all physical data rows/columns visited; does not certify every economic rule',
                tests=dict(command=command[1:],returncode=result.returncode,stdout=result.stdout,stderr=result.stderr),
                scope=dict(active_sources=['CSMAR','CNRDS'],BigQuant='DEFERRED_GENERATED_INTERMEDIATES_DELETED'),
                limits=['NO LIVE MONEY','PIT_UNVERIFIED','HFQ_UNVERIFIED','FORMAL_ADMISSION_BLOCKED'])
    (args.output/'verification.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False))
    if result.returncode:
        raise SystemExit(result.returncode)


if __name__=='__main__':
    main()
