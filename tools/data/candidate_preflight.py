"""Selected-window candidate diagnostics. Run with python -I -X utf8.

Only aggregate reports leave private source storage. No archive extraction.
"""
from __future__ import annotations

import argparse
from collections import Counter
from io import BytesIO
import json
from pathlib import Path
import sys
import zipfile

# Bind this checkout when invoked as a standalone script under isolated Python.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

import pandas as pd
import pyarrow.parquet as pq

from factorlab.data.admission import assert_formal_market_admission
from factorlab.data.evidence import sha256
from factorlab.data.governance import diagnose_tables
from factorlab.data.hub import DataHub
from factorlab.data.hub.contracts import QueryResult, SourceMetadata, UnsupportedQueryError
from factorlab.diagnostics.candidate_preflight import BOUNDARY, CandidateQuery, preflight
from factorlab.errors import ProviderError


class SelectedTableAdapter:
    """Retrospective, content-pinned table; no historical availability contract."""
    capabilities = frozenset({'daily_bars'})

    def __init__(self, frame, metadata):
        self.frame, self.metadata = frame, metadata

    def read(self, kind, *, symbol, start, end, knowledge_as_of, **kwargs):
        if knowledge_as_of is not None:
            raise UnsupportedQueryError('historical availability UNKNOWN')
        selected = self.frame.loc[self.frame.symbol.eq(symbol) & self.frame.date.between(start, end)]
        return QueryResult(selected.copy(), self.metadata)


def csmar(root):
    manifest_bytes = (root / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    payloads = {}
    for name in ('market/bars.parquet', 'corporate_actions/events.json', 'calendar/observed_sessions.parquet'):
        payload = (root / name).read_bytes()
        if sha256(payload) != manifest['files'][name]:
            raise ValueError('pinned candidate hash mismatch: ' + name)
        payloads[name] = payload
    frame = pq.read_table(BytesIO(payloads['market/bars.parquet'])).to_pandas()
    frame = frame[['date', 'symbol', 'raw_open', 'raw_high', 'raw_low', 'raw_close', 'volume', 'amount']]
    events = json.loads(payloads['corporate_actions/events.json'])
    evidence = dict(coverage=manifest['coverage']['action_coverage'], count=len(events),
        event_types=dict(Counter(row['event']['event_type'] for row in events)),
        missing_record_dates=sum(row['event']['record_date'] is None for row in events),
        execution_status=dict(Counter(row['execution_status'] for row in events)),
        available_at_semantics='2026 candidate preparation, NOT historical first distribution',
        event_sha256=manifest['files']['corporate_actions/events.json'])
    metadata = SourceMetadata('CSMAR', 'sha256:' + sha256(manifest_bytes),
        calendar_kind=manifest['coverage']['calendar'], coverage='PARTIAL',
        provenance={'manifest_sha256': sha256(manifest_bytes),
            'selected_files': {name: manifest['files'][name] for name in payloads},
            'input_catalog_sha256': manifest['input_catalog_sha256'],
            'source_kind': 'REAL_CANDIDATE', 'identity': manifest['coverage']['identity'],
            'original_vendor_replay': 'REUSED_PREPARED_V4_NOT_RERUN'})
    return SelectedTableAdapter(frame, metadata), evidence


def bigquant(path, start, end):
    pieces, members, schemas = [], [], {}
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.startswith('stock/cn_stock_real_bar1d/') or not name.endswith('.parquet'):
                continue
            lo, hi = Path(name).stem.split('_')
            if pd.Timestamp(lo) > pd.Timestamp(end) or pd.Timestamp(hi) < pd.Timestamp(start):
                continue
            payload = archive.read(name)
            receipt = archive.read(name + '.receipt.json')
            table = pq.read_table(BytesIO(payload), columns=['date', 'instrument', 'open', 'high', 'low', 'close', 'volume', 'amount'])
            frame = table.to_pandas()
            frame = frame.loc[frame.instrument.eq('601857.SH') & frame.date.between(start, end)]
            pieces.append(frame)
            members.append(dict(member=name, sha256=sha256(payload), receipt_sha256=sha256(receipt)))
        schema_name = 'stock/cn_stock_real_bar1d/schema.json'
        schema = archive.read(schema_name)
        schemas[schema_name] = sha256(schema)
    if not pieces:
        raise FileNotFoundError('no overlapping BigQuant partitions')
    frame = pd.concat(pieces, ignore_index=True).rename(columns={
        'instrument': 'symbol', **{name: 'raw_' + name for name in ('open', 'high', 'low', 'close')}})
    binding = dict(members=sorted(members, key=lambda x: x['member']), schemas=schemas)
    version = 'sha256:' + sha256(json.dumps(binding, sort_keys=True).encode())
    metadata = SourceMetadata('BigQuant', version, calendar_kind='OBSERVED_SECURITY_SESSIONS_ONLY',
        provenance={'selected_archive_binding': binding, 'source_kind': 'REAL_CANDIDATE',
                    'unit_contract': 'UNKNOWN', 'archive': 'BQ/stock.zip'})
    return SelectedTableAdapter(frame, metadata)


def fixture():
    dates = pd.bdate_range('2017-01-02', periods=100)
    frame = pd.DataFrame(dict(date=dates, symbol='601857.SH', raw_open=10., raw_high=11.,
        raw_low=9., raw_close=10., volume=100., amount=1000.))
    return SelectedTableAdapter(frame, SourceMetadata('SYNTHETIC_FIXTURE', 'fixture-v1',
        provenance={'source_kind': 'SYNTHETIC'}))


def run(args):
    adapters, actions, unavailable = {}, {}, {}
    if args.fixture:
        adapters['fixture'] = fixture()
        start, end = '2017-01-02', '2017-05-19'
        research_start, research_end = '2017-04-03', end
    else:
        start, end = '2017-01-03', '2018-12-28'
        research_start, research_end = '2018-01-01', end
        for name, loader in (('csmar', lambda: csmar(args.csmar_candidate)),
                             ('bigquant', lambda: bigquant(args.stock_zip, start, end))):
            try:
                loaded = loader()
                if name == 'csmar':
                    adapters[name], actions[name] = loaded
                else:
                    adapters[name] = loaded
            except (FileNotFoundError, PermissionError) as exc:
                unavailable[name] = dict(accessibility='NOT_ACCESSIBLE', acceptance='NOT_RUN',
                                         reason=type(exc).__name__)
    report = dict(boundary=BOUNDARY, purpose='RETROSPECTIVE_TECHNICAL_DIAGNOSTICS',
                  unavailable=unavailable, sources={}, cross_source={'status': 'NOT_RUN'})
    hub = DataHub(adapters)
    for name, adapter in adapters.items():
        dates = pd.DatetimeIndex(sorted(adapter.frame.date.unique()))
        query = CandidateQuery(name, adapter.metadata.data_version, '601857.SH',
                               start, end, research_start, research_end)
        diagnostic = preflight(hub, query, calendar=dates, calendar_id=name + '-observed-dates',
            calendar_kind='SYNTHETIC' if args.fixture else 'OBSERVED_SECURITY_SESSIONS_ONLY',
            units={'prices': 'CNY candidate mapping', 'volume': 'candidate shares; vendor contract unverified',
                   'amount': 'candidate CNY; vendor precision contract unverified'},
            identity_evidence='CURRENT_NATIVE_COMPANY_RECORD' if name == 'csmar' else None,
            action_evidence=actions.get(name))
        source = diagnostic.to_dict()
        source['formal_gate'] = formal_refusal(diagnostic, adapter.frame, dates, query)
        if args.pipeline_ref:
            source['research_pipeline'] = pipeline_refusal(adapter, query,
                revision=args.pipeline_ref, repo=Path(__file__).resolve().parents[2])
        report['sources'][name] = source
    if {'csmar', 'bigquant'}.issubset(adapters):
        left = hub.read('csmar', 'daily_bars', symbol='601857.SH', start=start, end=end)
        right = hub.read('bigquant', 'daily_bars', symbol='601857.SH', start=start, end=end)
        conflicts = diagnose_tables(left, right, source='csmar', version=left.metadata.data_version,
            other_source='bigquant', other_version=right.metadata.data_version,
            fields=['raw_open', 'raw_high', 'raw_low', 'raw_close', 'volume', 'amount'])
        report['cross_source'] = dict(status='FAIL' if conflicts else 'PASS',
            comparison='EXACT_CANDIDATE_VALUES_ONLY_NOT_CERTIFIED_UNITS',
            semantic_qualification='UNKNOWN', conflicts=len(conflicts),
            by_field=dict(Counter(c['field'] for c in conflicts)),
            by_type=dict(Counter(c['type'] for c in conflicts)),
            left_version=left.metadata.data_version, right_version=right.metadata.data_version,
            date_keys_equal=set(left.data.date) == set(right.data.date),
            merged=False, selection='NONE')
    return report


def formal_refusal(diagnostic, frame, dates, query):
    try:
        assert_formal_market_admission(diagnostic, membership=pd.DataFrame(), calendar=dates,
            bars=frame, benchmark=pd.DataFrame(), research_start=query.research_start,
            research_end=query.research_end, require_execution=True)
    except ProviderError as exc:
        return dict(status='PASS', outcome='REJECTED', reason=str(exc))
    raise AssertionError('uncertified diagnostic entered formal admission')


def pipeline_refusal(adapter, query, *, revision, repo):
    """Execute only the refusal path of a pinned, read-only pipeline revision."""
    import subprocess
    import types
    from types import SimpleNamespace

    code = subprocess.check_output(['git', '-C', str(repo), 'show',
        revision + ':src/factorlab/application/research_pipeline.py'], text=True, encoding='utf-8')
    module = types.ModuleType('_p308_readonly_pipeline')
    sys.modules[module.__name__] = module
    exec(compile(code, 'git:' + revision + ':research_pipeline.py', 'exec'), module.__dict__)
    dates = pd.DatetimeIndex(sorted(adapter.frame.date.unique()))
    class BoundaryInputs:
        capabilities = frozenset({'daily_bars', 'calendar', 'membership'})

        def read(self, kind, **kwargs):
            if kind == 'calendar':
                data = dates
            elif kind == 'membership':
                # Schema-only negative-test placeholder, not a membership claim.
                data = pd.DataFrame({'date': pd.Series([], dtype='datetime64[ns]')})
            else:
                data = adapter.frame.copy()
            return QueryResult(data, adapter.metadata)

    class NoBuild:
        snapshot = object()

        def build_chunked(self, *args, **kwargs):
            raise AssertionError('formal pipeline reached dataset construction')

    queries = {}
    for name in ('bars', 'benchmark', 'membership', 'status', 'listings', 'calendar'):
        kind = name if name in ('membership', 'calendar') else 'daily_bars'
        queries[name] = (module.HubQuery('boundary', query.version, kind, query.start, query.end,
                                       None if kind == 'calendar' else query.symbol),)
    config = SimpleNamespace(source_kind='REAL_CANDIDATE', builder=NoBuild(),
        queries=queries, models={'must_not_fit': {}}, research_start=query.research_start,
        research_end=str(dates[-2].date()))
    try:
        module.ResearchPipeline(config).run(DataHub({'boundary': BoundaryInputs()}),
            tracker=None, repo_root=repo, command=['p308-negative-boundary'])
    except ProviderError as exc:
        return dict(status='PASS', outcome='REJECTED_BEFORE_DATASET_BUILD',
            pipeline_revision=revision, reason=str(exc),
            input_scope='ACTUAL_CANDIDATE_BARS_WITH_SCHEMA_ONLY_AUXILIARY_PLACEHOLDERS',
            formal_test_research_end=config.research_end)
    raise AssertionError('formal pipeline did not reject uncertified candidate')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', action='store_true')
    parser.add_argument('--pipeline-ref', help='optional trusted git revision for read-only pipeline refusal test')
    parser.add_argument('--csmar-candidate', type=Path)
    parser.add_argument('--stock-zip', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.fixture and (args.csmar_candidate is None or args.stock_zip is None):
        parser.error('both source paths are required for real diagnostics')
    report = run(args)
    with args.output.open('x', encoding='utf-8') as target:
        json.dump(report, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.write('\n')
    print(json.dumps({'sources': list(report['sources']), 'unavailable': report['unavailable'],
                      'cross_source': report['cross_source'], 'boundary': BOUNDARY}, ensure_ascii=False))


if __name__ == '__main__':
    main()
