"""Account for inherited testcase-function/parameter counts, including blocked collection."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
BASELINE=ROOT/'docs/migration/evidence/baseline/full-suite-r2.xml'
FINAL=ROOT/'local-ci/full314-final-r3/tests.xml'


def inventory(path):
    return Counter((c.get('classname'),c.get('name').split('[')[0])
                   for c in ET.parse(path).getroot().iter('testcase'))


def main():
    baseline,final=inventory(BASELINE),inventory(FINAL)
    deficit=[{'module':module,'function':function,'count':count}
             for (module,function),count in (baseline-final).items()]
    added=[{'module':module,'function':function,'count':count}
           for (module,function),count in (final-baseline).items()]
    assert sum(x['count'] for x in deficit)==27
    assert all(x['module']=='tests.market_admission.test_revision_contract' for x in deficit)
    report={'status':'PASS_INVENTORY_ACCOUNTED_WITH_BLOCKED_COLLECTION','baseline_cases':sum(baseline.values()),
            'final_junit_entries_including_collection_error':sum(final.values()),
            'baseline_not_collected':deficit,'added_or_expanded':added,
            'baseline_junit_sha256':hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
            'final_junit_sha256':hashlib.sha256(FINAL.read_bytes()).hexdigest(),
            'scope':'Functions and parameter counts, not an assertion that blocked security cases passed.'}
    (ROOT/'docs/migration/evidence/test-case-inventory-comparison.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(report['status'],'27 unavailable revision-contract cases; no other inherited function/count deficit')


if __name__=='__main__':
    main()
