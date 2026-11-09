"""Source-separated batch preparation; publish aggregates only, never vendor rows."""
from __future__ import annotations

import argparse
import csv
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from candidate_preflight import bigquant, csmar
from factorlab.data.evidence import sha256
from factorlab.data.hub import DataHub
from factorlab.diagnostics.candidate_preflight import CandidateQuery
from factorlab.diagnostics.real_data_readiness import batch_readiness


def d06_reference(revision):
    """Read committed aggregate evidence, not executable unmerged source."""
    repo = Path(__file__).resolve().parents[2]
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', revision + '^{commit}'], text=True).strip()
    payloads = {}
    for name in ('diagnostics.json', 'walk-forward-windows.csv', 'factors.csv'):
        payloads[name] = subprocess.check_output(['git', '-C', str(repo), 'show',
            commit + ':docs/validation/p3-d06/' + name])
    summary = json.loads(payloads['diagnostics.json'])
    windows = list(csv.DictReader(StringIO(payloads['walk-forward-windows.csv'].decode('utf-8-sig'))))
    return dict(commit=commit, upstream_pr=41, integration='UNMERGED_REFERENCE_ONLY',
        artifact_sha256={n: sha256(b) for n, b in payloads.items()},
        observations_origin='REUSED_UPSTREAM_RUN_NOT_RECOMPUTED',
        window=summary['window'], membership=summary['membership'],
        factors=summary['factors'], source_quality=summary['source_quality'],
        label_endpoints=summary['label_endpoints'],
        minimum_candidate=[w for w in windows if w['candidate'] == 'minimum_12_3_3' and w['fold'] == '0'],
        financial_qualification='UNKNOWN', admitted_member_days=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock-zip', type=Path, required=True)
    parser.add_argument('--csmar-candidate', type=Path)
    parser.add_argument('--symbols', nargs='+', required=True)
    for name in ('start', 'end', 'research-start', 'research-end'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--upstream-d06-ref')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    bq = bigquant(args.stock_zip, args.start, args.end, symbols=tuple(args.symbols))
    adapters = {'bigquant': bq}
    if args.csmar_candidate:
        adapters['csmar'], _ = csmar(args.csmar_candidate)
    # This grid detects differences between selected observations, not missing
    # sessions common to every source; independent calendar evidence stays UNKNOWN.
    calendar = pd.DatetimeIndex(sorted(set().union(*(set(a.frame.date) for a in adapters.values()))))
    queries = tuple(CandidateQuery(name, adapter.metadata.data_version, symbol,
        args.start, args.end, args.research_start, args.research_end)
        for name, adapter in adapters.items() for symbol in args.symbols)
    report = batch_readiness(DataHub(adapters), queries, calendar=calendar,
        calendar_id='EXPLICIT_BATCH_OBSERVATION_UNION',
        calendar_kind='OBSERVED_SECURITY_SESSIONS_ONLY',
        units={'prices': 'UNKNOWN_VENDOR_CONTRACT', 'volume': 'UNKNOWN_VENDOR_CONTRACT',
               'amount': 'UNKNOWN_VENDOR_CONTRACT'})
    report['run'] = dict(symbols=args.symbols, start=args.start, end=args.end,
        research_start=args.research_start, research_end=args.research_end,
        software_revision=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=Path(__file__).resolve().parents[2], text=True).strip(),
        implementation_sha256={name: sha256((Path(__file__).resolve().parents[2] / name).read_bytes())
            for name in ('tools/data/audit_p3_d07.py', 'tools/data/candidate_preflight.py',
                         'src/factorlab/diagnostics/candidate_preflight.py',
                         'src/factorlab/diagnostics/real_data_readiness.py')})
    if args.upstream_d06_ref:
        report['upstream_d06'] = d06_reference(args.upstream_d06_ref)
    with args.output.open('x', encoding='utf-8') as target:
        json.dump(report, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.write('\n')
    print(json.dumps(dict(status=report['status'], queries=len(queries),
        boundary=report['boundary'], comparisons=report['comparisons']), ensure_ascii=False))


if __name__ == '__main__':
    main()
