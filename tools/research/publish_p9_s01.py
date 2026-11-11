"""Publish aggregate scenario evidence after an independent reproducibility replay."""
import argparse
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_p9_d05 import dump, file_hash
import pandas as pd


def publish(args):
    args.output.mkdir(parents=True, exist_ok=False)
    primary = json.loads((args.private / 'public/scenario-consumption-results.json').read_bytes())
    repeated = json.loads((args.repeat / 'public/scenario-consumption-results.json').read_bytes())
    comparisons = []
    for first, second in zip(primary['scenarios'], repeated['scenarios'], strict=True):
        name = first['research_id']
        if name != second['research_id'] or first['assumption_sha256'] != second['assumption_sha256']:
            raise ValueError('SCENARIO_REPRODUCIBILITY_IDENTITY_MISMATCH')
        tables = ('nav', 'trades', 'positions', 'rejections', 'target_comparison', 'scenario-marks')
        for table in tables:
            filename = f'fold-0-{table}.parquet'
            pd.testing.assert_frame_equal(pd.read_parquet(args.private / name / filename),
                pd.read_parquet(args.repeat / name / filename), check_exact=True)
        for field in ('end_nav', 'end_cash', 'held_shares', 'conditional_return', 'first_blocker'):
            if first[field] != second[field]:
                raise ValueError('SCENARIO_REPRODUCIBILITY_METRIC_MISMATCH: ' + field)
        comparisons.append(dict(research_id=name, assumption_sha256=first['assumption_sha256'],
            six_tables_exact=list(tables), completed_interval_metrics_exact=True))
    for path in (args.private / 'public').glob('*.json'):
        payload = json.loads(path.read_bytes())
        if path.name == 'verification.json':
            root = ET.parse(args.tests).getroot()
            suites = list(root.iter('testsuite'))
            payload['tests'] = dict(tests=sum(int(s.get('tests', 0)) for s in suites),
                failures=sum(int(s.get('failures', 0)) for s in suites),
                errors=sum(int(s.get('errors', 0)) for s in suites), junit_sha256=file_hash(args.tests))
            if payload['tests']['failures'] or payload['tests']['errors']:
                raise ValueError('SCENARIO_TESTS_FAILED')
            payload['reproducibility'] = comparisons
            payload['publication_main'] = args.baseline
            payload['dependency_pr114_state'] = 'MERGED'
            payload['source_level_price_admission'] = 'P10_G04_BLOCKED'
            payload['return_basis'] = 'RAW_PRICE_RETURN_NET_OF_TRADING_COSTS'
            payload['total_return_status'] = 'TOTAL_RETURN_UNAVAILABLE'
            payload['repeat_resources'] = json.loads((args.repeat / 'public/verification.json').read_bytes())['resources']
            payload['development_recovery'] = ('Initial failure-isolation fixture reached the engine missing-close '
                'guard before the intended qualification step; corrected it to add to an existing holding. '
                'Final targeted tests passed.')
            directories = [args.private, args.repeat, *args.additional_replay]
            payload['resources']['all_retained_replay_bytes'] = sum(
                p.stat().st_size for directory in directories for p in directory.rglob('*') if p.is_file())
        dump(args.output / path.name, payload)
    print(json.dumps(dict(published=str(args.output), reproducible_scenarios=len(comparisons))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('private', 'repeat', 'tests', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--additional-replay', type=Path, nargs='*', default=[])
    publish(parser.parse_args())
