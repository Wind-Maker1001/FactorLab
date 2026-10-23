"""Summarize final actual jobs; preserve full FAIL and unavailable control cases."""
import hashlib
import json
from pathlib import Path
import runpy
import re
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
EVIDENCE=ROOT/'docs/migration/evidence'
CATALOG=runpy.run_path(str(ROOT/'tools/ci/run_checks.py'),run_name='factorlab_ci_catalog')
JOBS=('runtime314-final-r5','runtime313-final-r4','full314-final-r4')


def reason(case):
    classname=case.get('classname','') or case.get('name','')
    module=classname.replace('.','/')+'.py'
    for key,description in dict(CATALOG['UPSTREAM_BOUND'],**CATALOG['PUBLISHED_HISTORY']).items():
        if module==key:
            return 'BLOCKED_UPSTREAM_EVIDENCE: '+description
    node=module+'::'+case.get('name','')
    if any(node==old or node.startswith(old+'[') for old in CATALOG['HISTORY_NODES']):
        return 'BLOCKED_ORIGINAL_HISTORY_OR_BASELINE_LAYOUT'
    return 'UNRESOLVED_SOFTWARE_FAILURE'


def main():
    results=[]
    for name in JOBS:
        directory=ROOT/'local-ci'/name
        receipt=json.loads((directory/'receipt.json').read_text(encoding='utf-8'))
        tree=ET.parse(directory/'tests.xml')
        counts=dict(passed=0,failed=0,errors=0,skipped=0)
        nonpassing=[]
        for case in tree.getroot().iter('testcase'):
            if case.find('failure') is not None:
                counts['failed']+=1
            elif case.find('error') is not None:
                counts['errors']+=1
            elif case.find('skipped') is not None:
                counts['skipped']+=1
                continue
            else:
                counts['passed']+=1
                continue
            detail=case.find('failure')
            if detail is None:
                detail=case.find('error')
            nonpassing.append({'classname':case.get('classname'),'name':case.get('name'),
                               'classification':reason(case),'classification_method':'module/node catalog; trace review separately required',
                               'message':detail.get('message','') if detail is not None else ''})
        coverage=ET.parse(directory/'coverage.xml').getroot()
        log=(directory/'tests.log').read_text(encoding='utf-8')
        deselected=re.findall(r'(\d+) deselected',log)
        warning_count=re.findall(r'(\d+) warnings?',log)
        results.append({'job':name,'actual_status':receipt['status'],'exit_code':receipt['exit_code'],
            'subject_sha':receipt['subject_sha'],'end_sha':receipt['end_sha'],'python':receipt['python'],
            'elapsed_seconds':receipt['elapsed_seconds'],'counts':counts,
            'deselected':int(deselected[-1]) if deselected else 0,
            'warnings':int(warning_count[-1]) if warning_count else 0,
            'validation_inputs_unchanged':receipt['validation_inputs_unchanged'],
            'coverage_line_rate':float(coverage.get('line-rate')),
            'receipt_path':(directory/'receipt.json').relative_to(ROOT).as_posix(),
            'receipt_sha256':hashlib.sha256((directory/'receipt.json').read_bytes()).hexdigest(),
            'junit_sha256':hashlib.sha256((directory/'tests.xml').read_bytes()).hexdigest(),
            'nonpassing':nonpassing})
    unresolved=[dict(job=result['job'],**case) for result in results for case in result['nonpassing']
                if case['classification']=='UNRESOLVED_SOFTWARE_FAILURE']
    packaging=json.loads((ROOT/'local-ci/packaging314-final-r7/receipt.json').read_text(encoding='utf-8'))
    attacks=json.loads((ROOT/'local-ci/archive-attacks314-r6/receipt.json').read_text(encoding='utf-8'))
    compact=json.loads((EVIDENCE/'test-summary-compact.json').read_text(encoding='utf-8'))
    review=json.loads((EVIDENCE/'full-failure-review.json').read_text(encoding='utf-8'))
    assert review['junit_sha256']==results[2]['junit_sha256']
    report={'scope':'Actual final local Windows jobs; overlapping targeted runs are not added to full counts',
            'software_status':'PASS' if all(r['actual_status']=='PASS' for r in results[:2]) and not unresolved else 'FAIL',
            'complete_security_history_status':results[2]['actual_status'],'jobs':results,
            'full_failure_trace_review':review,
            'unresolved_software_failures':unresolved,'packaging':packaging,'archive_attack_checks':attacks,
            'baseline_and_other_rounds':compact,
            'blocked_not_run':{'upstream_revision_contract_cases':27,'reason':'trusted controller absent at module collection'},
            'not_run':['2 network tests deliberately excluded','Linux platform execution','remote GitHub Actions'],
            'formal_admission':'FORMAL_ADMISSION_BLOCKED','pit':'PIT_UNVERIFIED','live_money':'NO LIVE MONEY',
            'license':'BLOCKED_SCOPE_UNRESOLVED','public_release':'BLOCKED'}
    (EVIDENCE/'final-validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    for result in results:
        print(result['job'],result['actual_status'],result['counts'],'coverage',result['coverage_line_rate'])
    print('Unresolved software failures:',len(unresolved))
    return bool(unresolved or report['software_status']!='PASS')


if __name__=='__main__':
    raise SystemExit(main())
