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


D06_COMMIT = 'f3cbdb54d51780eb394b1a30b326914a4965ad89'
D06_ARTIFACTS = {
    'diagnostics.json': '9740c93253454e2080ebf242635304840ed796c40b611d098988fae23f78fbdc',
    'walk-forward-windows.csv': 'f7ea6b6625ec1c5d86d6f5f80de5dd3221f9cd1e7f2e8567cebd4b5b8921e99d',
    'factors.csv': '6a7c721b95be1a776a0b0b8677c5abfd9ce0c744da931d2cd3bc39bf7e365f9f',
}


def d06_reference(revision, *, repository=None):
    """Read pinned D06 aggregates after proving ancestry of the exact checkout.

    Shallow checkouts fetch only the immutable checkout's ancestry from origin;
    no branch ref or caller-selected revision is fetched or trusted.
    """
    if revision != D06_COMMIT:
        raise ValueError('D06 reference must match the fixed merged commit')
    repo = Path(repository) if repository is not None else Path(__file__).resolve().parents[2]
    git = ['git', '-C', str(repo)]
    if subprocess.check_output(git + ['rev-parse', '--is-shallow-repository'], text=True).strip() == 'true':
        subject = subprocess.check_output(git + ['rev-parse', 'HEAD'], text=True).strip()
        subprocess.run(git + ['fetch', '--no-tags', '--no-write-fetch-head', '--unshallow', 'origin', subject], check=True)
    commit = subprocess.check_output(git + ['rev-parse', revision + '^{commit}'], text=True).strip()
    if commit != D06_COMMIT or subprocess.check_output(git + ['cat-file', '-t', commit], text=True).strip() != 'commit':
        raise ValueError('D06 object identity mismatch')
    subprocess.run(git + ['merge-base', '--is-ancestor', commit, 'HEAD'], check=True)
    payloads = {}
    for name in ('diagnostics.json', 'walk-forward-windows.csv', 'factors.csv'):
        payloads[name] = subprocess.check_output(['git', '-C', str(repo), 'show',
            commit + ':docs/validation/p3-d06/' + name])
    if {name: sha256(payload) for name, payload in payloads.items()} != D06_ARTIFACTS:
        raise ValueError('D06 aggregate content identity mismatch')
    summary = json.loads(payloads['diagnostics.json'])
    windows = list(csv.DictReader(StringIO(payloads['walk-forward-windows.csv'].decode('utf-8-sig'))))
    return dict(commit=commit, upstream_pr=41, integration='MERGED_MAIN_RECEIPT',
        artifact_sha256={n: sha256(b) for n, b in payloads.items()},
        observations_origin='REUSED_UPSTREAM_RUN_NOT_RECOMPUTED',
        source='P3D06_COMMITTED_AGGREGATES', version=commit,
        effective_date=summary['window'], known_at='UNKNOWN', source_kind='AGGREGATE_RECEIPT',
        window=summary['window'], membership=summary['membership'],
        factors=summary['factors'], source_quality=summary['source_quality'],
        label_endpoints=summary['label_endpoints'],
        minimum_candidate=[w for w in windows if w['candidate'] == 'minimum_12_3_3' and w['fold'] == '0'],
        financial_qualification='UNKNOWN', admitted_member_days=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock-zip', type=Path)
    parser.add_argument('--bigquant-root', type=Path)
    parser.add_argument('--membership-zip', type=Path)
    parser.add_argument('--csmar-candidate', type=Path)
    parser.add_argument('--symbols', nargs='+', required=True)
    for name in ('start', 'end', 'research-start', 'research-end'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--upstream-d06-ref', default=D06_COMMIT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    research_inputs = {}
    if args.bigquant_root:
        if not args.membership_zip or args.stock_zip:
            parser.error('--bigquant-root requires --membership-zip and excludes --stock-zip')
        from d06_readiness_adapter import load_research
        bq, research_inputs['bigquant'] = load_research(args.bigquant_root, args.membership_zip,
            symbols=args.symbols, start=args.start, end=args.end)
    else:
        if not args.stock_zip or args.membership_zip:
            parser.error('select --stock-zip or --bigquant-root with --membership-zip')
        bq = bigquant(args.stock_zip, args.start, args.end, symbols=tuple(args.symbols))
    adapters = {'bigquant': bq}
    if args.csmar_candidate:
        adapters['csmar'], _ = csmar(args.csmar_candidate)
    # This grid detects differences between selected observations, not missing
    # sessions common to every source; independent calendar evidence stays UNKNOWN.
    calendar = (pd.DatetimeIndex(research_inputs['bigquant']['inputs']['calendar'].data.date)
        if research_inputs else pd.DatetimeIndex(sorted(set().union(*(set(a.frame.date) for a in adapters.values())))))
    queries = tuple(CandidateQuery(name, adapter.metadata.data_version, symbol,
        args.start, args.end, args.research_start, args.research_end)
        for name, adapter in adapters.items() for symbol in args.symbols)
    report = batch_readiness(DataHub(adapters), queries, calendar=calendar,
        calendar_id='BIGQUANT_CN_SESSIONS' if research_inputs else 'EXPLICIT_BATCH_OBSERVATION_UNION',
        calendar_kind='VENDOR_CALENDAR_UNCERTIFIED' if research_inputs else 'OBSERVED_SECURITY_SESSIONS_ONLY',
        research_inputs=research_inputs,
        units={'prices': 'UNKNOWN_VENDOR_CONTRACT', 'volume': 'UNKNOWN_VENDOR_CONTRACT',
               'amount': 'UNKNOWN_VENDOR_CONTRACT'})
    report['run'] = dict(symbols=args.symbols, start=args.start, end=args.end,
        research_start=args.research_start, research_end=args.research_end,
        software_revision=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=Path(__file__).resolve().parents[2], text=True).strip(),
        implementation_sha256={name: sha256((Path(__file__).resolve().parents[2] / name).read_bytes())
            for name in ('tools/data/audit_p3_d07.py', 'tools/data/candidate_preflight.py',
                         'src/factorlab/diagnostics/candidate_preflight.py',
                         'src/factorlab/diagnostics/real_data_readiness.py',
                         'src/factorlab/diagnostics/research_coverage.py',
                         'src/factorlab/diagnostics/research_readiness.py',
                         'tools/data/d06_readiness_adapter.py')})
    if args.upstream_d06_ref:
        report['upstream_d06'] = d06_reference(args.upstream_d06_ref)
    with args.output.open('x', encoding='utf-8') as target:
        json.dump(report, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.write('\n')
    print(json.dumps(dict(status=report['status'], queries=len(queries),
        boundary=report['boundary'], comparisons=report['comparisons']), ensure_ascii=False))


if __name__ == '__main__':
    main()
