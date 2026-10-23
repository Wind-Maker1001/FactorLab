"""Summarize actual JUnit and file hashes; never infer success from old reports."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
EVIDENCE=ROOT/'docs/migration/evidence'


def junit(path):
    tree=ET.parse(path)
    cases=list(tree.getroot().iter('testcase'))
    failures=[]
    passed=skipped=errors=failed=0
    for case in cases:
        children=list(case)
        status='PASS'
        if case.find('error') is not None:
            status='ERROR'; errors+=1
        elif case.find('failure') is not None:
            status='FAIL'; failed+=1
        elif case.find('skipped') is not None:
            status='SKIPPED'; skipped+=1
        else:
            passed+=1
        if status!='PASS':
            detail=next((c for c in children if c.tag in ('error','failure','skipped')),None)
            failures.append({'classname':case.get('classname'),'name':case.get('name'),
                             'status':status,'message':detail.get('message','') if detail is not None else '',
                             'detail':detail.text or '' if detail is not None else ''})
    return {'path':path.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'passed':passed,'failed':failed,'errors':errors,'skipped':skipped,'cases':len(cases),
            'nonpassing':failures}


def main():
    reports=[]
    for path in sorted(EVIDENCE.rglob('*.xml'))+sorted((ROOT/'local-ci').glob('*/tests.xml')):
        try:
            reports.append(junit(path))
        except ET.ParseError:
            reports.append({'path':str(path.relative_to(ROOT)),'status':'INCOMPLETE_NOT_ACCEPTANCE'})
    (EVIDENCE/'test-summary.json').write_text(json.dumps(reports,indent=2),encoding='utf-8')
    env={'python':sys.version,'executable':sys.executable,'platform':platform.platform(),
         'packages':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()}}
    (EVIDENCE/('environment-'+platform.python_version()+'.json')).write_text(json.dumps(env,indent=2),encoding='utf-8')
    for result in reports:
        print(result['path'], {key:result[key] for key in ('passed','failed','errors','skipped') if key in result})


if __name__=='__main__':
    main()
