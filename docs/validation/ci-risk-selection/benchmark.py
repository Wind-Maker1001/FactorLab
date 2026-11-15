"""Replay PR 175's immutable risk subject and compare trusted plan/identity bytes."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time

BASE = 'ac3505faa13f2cc9533fb13ba16551c4d521cd36'
PR_HEAD = 'e1af57acd48cb27738d5b8315df129c5156bcaa5'
SUBJECT = '336d64f80fceb90e6973b340fe049bf9363ca63a'
TREE = 'b8b856ac9c33ea95027a90cdf770439fd1673ee0'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    repository = args.repository.resolve()
    identity = load('identity', repository / 'tools/ci/pr_identity.py')
    records, plans = [], []
    with tempfile.TemporaryDirectory(prefix='factorlab-risk-benchmark-') as directory:
        root = Path(directory)
        original = root / 'original.py'
        original.write_bytes(subprocess.check_output(
            ['git', '-C', str(repository), 'show', BASE + ':tools/ci/pr_risk.py']))
        for label, source in [('before', original), ('after', repository / 'tools/ci/pr_risk.py')]:
            risk = load(label, source)
            stages = {}

            def instrument(name):
                function = getattr(risk, name)

                def measured(*arguments, **keywords):
                    start = time.perf_counter()
                    try:
                        return function(*arguments, **keywords)
                    finally:
                        stages[name] = stages.get(name, 0) + time.perf_counter() - start
                setattr(risk, name, measured)

            for name in ('git', 'tree', 'contents', 'graph', 'output_path_literals', 'closure', 'classify'):
                instrument(name)
            start = time.perf_counter()
            result = risk.select(repository, BASE, SUBJECT)
            elapsed = time.perf_counter() - start
            start = time.perf_counter()
            raw = (json.dumps(result, indent=2) + '\n').encode()
            serialization = time.perf_counter() - start
            plans.append(raw)
            records.append(dict(label=label, selection_seconds=elapsed,
                                serialization_seconds=serialization,
                                stages_inclusive_seconds=stages, bytes=len(raw),
                                risk_sha256=hashlib.sha256(raw).hexdigest()))
            print(json.dumps(records[-1]), flush=True)
        assert plans[0] == plans[1], 'risk plan bytes changed'
        candidate = root / 'candidate.git'
        subprocess.run(['git', 'clone', '--bare', '--shared', str(repository), str(candidate)],
                       check=True, capture_output=True)
        subprocess.run(['git', '-C', str(candidate), 'update-ref', 'HEAD', SUBJECT], check=True)
        expected = dict(repository='Wind-Maker1001/FactorLab', run_id='43728021733',
                        run_attempt='1', event='pull_request_target', event_sha=BASE,
                        pr=175, base=BASE, pr_head=PR_HEAD)
        sealed = []
        for raw in plans:
            receipt = identity.issue(candidate, expected)
            receipt['risk_sha256'] = hashlib.sha256(raw).hexdigest()
            identity.validate(candidate, expected, receipt, raw, SUBJECT, receipt['risk_sha256'])
            sealed.append((json.dumps(receipt, indent=2) + '\n').encode())
        assert sealed[0] == sealed[1], 'sealed identity bytes changed'
    report = dict(subject=SUBJECT, tree=TREE, base=BASE, pr_head=PR_HEAD, measurements=records,
                  risk_plan_bytes_identical=True, sealed_identity_bytes_identical=True,
                  excluded_fields=[])
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
