"""Small input-contract fixtures; no financial qualification is manufactured."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from factorlab.application.research_pipeline import HubQuery, ResearchPipeline
from factorlab.data.hub import DataHub
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.diagnostics.pipeline_preflight import (
    ActionBinding, FIELDS, InputBinding, content_identity, pipeline_preflight,
)
from factorlab.errors import ProviderError

ROOT = Path(__file__).parents[2]
SYMBOLS = ('600000.SH', '000001.SZ')


class FixtureAdapter:
    def __init__(self, result, kind):
        self.result = result
        self.capabilities = frozenset({kind})

    def read(self, kind, **kwargs):
        return self.result


def fixture():
    dates = pd.bdate_range('2021-01-04', periods=12)
    lo, hi = str(dates[0].date()), str(dates[-1].date())
    sources, pins, actions = {}, {}, []

    def bind(group, symbol, frame):
        route = group + ':' + (symbol or 'calendar')
        metadata = SourceMetadata(route, 'v1', acquired_at='2026-11-09T00:00:00Z',
            provenance={'source_kind': 'SYNTHETIC'})
        result = QueryResult(frame, metadata)
        sources[route] = FixtureAdapter(result, group)
        units = {name: 'fixture_dimension' for name in FIELDS[group] if name not in {'date', 'symbol', 'listing_date', 'delist_date'}}
        pins.setdefault(group, []).append(InputBinding(HubQuery(route, 'v1', group, lo, hi, symbol),
            route, content_identity(frame), units, {name: 'synthetic_native:' + name for name in FIELDS[group]}))

    for number, symbol in enumerate(SYMBOLS):
        bars = pd.DataFrame(dict(date=dates, symbol=symbol))
        for prefix in ('raw_', 'adj_'):
            for name in ('open', 'high', 'low', 'close'):
                bars[prefix + name] = 10. + {'open': 0, 'high': 1, 'low': -1, 'close': .5}[name]
        bars['volume'], bars['amount'], bars['turnover'] = 100., 1000., .01
        bind('bars', symbol, bars)
        bind('membership', symbol, bars.loc[bars.date.between(dates[3], dates[5]), ['date', 'symbol']].copy())
        bind('status', symbol, bars[['date', 'symbol']].assign(trade_status=1, is_st=False))
        bind('listings', symbol, pd.DataFrame(dict(symbol=[symbol], listing_date=[pd.Timestamp('2010-01-01')],
            delist_date=[pd.NaT], security_id=['stable:' + symbol])))
        route = 'actions:' + symbol
        sources[route] = FixtureAdapter(QueryResult([], SourceMetadata(route, 'v1', coverage='COMPLETE_QUERY_EVENT_COVERAGE', provenance={'source_kind': 'SYNTHETIC'})), 'corporate_actions')
        actions.append(ActionBinding(route, 'v1', route, 'stable:' + symbol, lo, hi, content_identity([])))
    bind('calendar', None, pd.DatetimeIndex(dates))
    bind('benchmark', '000905.SH', pd.DataFrame(dict(date=dates, symbol='000905.SH', open=100.)))
    return sources, {g: tuple(v) for g, v in pins.items()}, dict(symbols=SYMBOLS, start=lo, end=hi,
        research_start=str(dates[3].date()), research_end=str(dates[5].date()),
        warmup_sessions=3, holding_days=5, execution_tail_sessions=1, actions=tuple(actions))


def run(sources, pins, options):
    return pipeline_preflight(DataHub(sources), pins, **options)


def change(sources, pins, group, transform, index=0):
    pin = pins[group][index]
    result = sources[pin.query.source].result
    sources[pin.query.source].result = replace(result, data=transform(result.data.copy()))
    changed = list(pins[group])
    changed[index] = replace(pin, content_sha256=content_identity(sources[pin.query.source].result.data))
    pins[group] = tuple(changed)


def codes(report):
    return {issue['code'] for issue in report['unmet_contracts']}


def test_complete_six_inputs_independent_sources_and_explicit_layers():
    sources, pins, options = fixture()
    report = run(sources, pins, options)
    assert report['engineering'] == 'PASS', report['unmet_contracts']
    assert all(report['query_completeness'].values())
    assert [report['layers'][p]['sessions'] for p in ('warmup', 'research', 'tail')] == [3, 3, 6]
    assert len(report['coverage_matrix']) == 24
    assert len({r['query']['source'] for r in report['receipts'] if r['group'] == 'bars'}) == 2
    assert all(r['known_at'] == 'UNKNOWN' for r in report['receipts'])
    assert report['formal_admission'] == 'BLOCKED'
    assert all(r['label_endpoints_available'] for r in report['label_endpoints'])


@pytest.mark.parametrize('group', ['membership', 'status'])
def test_missing_required_query(group):
    sources, pins, options = fixture()
    del pins[group]
    report = run(sources, pins, options)
    assert report['engineering'] == 'FAIL'
    assert not report['query_completeness'][group]
    assert 'MISSING_QUERY' in codes(report)


@pytest.mark.parametrize('damage', ['source', 'version', 'hash', 'provider_hash', 'missing_source'])
def test_identity_rejection(damage):
    sources, pins, options = fixture()
    pin = pins['bars'][0]
    adapter = sources[pin.query.source]
    if damage == 'hash':
        pins['bars'] = (replace(pin, content_sha256='0' * 64), pins['bars'][1])
    elif damage == 'missing_source':
        del sources[pin.query.source]
    else:
        metadata = adapter.result.metadata
        metadata = replace(metadata, **({'source_id': 'other'} if damage == 'source' else
            {'data_version': 'v2'} if damage == 'version' else {'provenance': {'research_table_sha256': '0' * 64}}))
        adapter.result = replace(adapter.result, metadata=metadata)
    assert 'INPUT_REJECTED' in codes(run(sources, pins, options))


@pytest.mark.parametrize('field,code', [('warmup_sessions', 'WARMUP_INSUFFICIENT'),
    ('holding_days', 'TAIL_INSUFFICIENT'), ('execution_tail_sessions', 'TAIL_INSUFFICIENT')])
def test_horizon_requirements(field, code):
    sources, pins, options = fixture()
    options[field] = 20
    assert code in codes(run(sources, pins, options))


@pytest.mark.parametrize('group', ['bars', 'status', 'listings', 'membership'])
def test_security_identity_conflict(group):
    sources, pins, options = fixture()
    change(sources, pins, group, lambda f: f.assign(symbol='601857.SH'))
    assert 'INPUT_REJECTED' in codes(run(sources, pins, options))


@pytest.mark.parametrize('damage', ['field', 'null', 'duplicate', 'outside_date'])
def test_field_and_relationship_rejection(damage):
    sources, pins, options = fixture()
    transform = {
        'field': lambda f: f.drop(columns='raw_open'),
        'null': lambda f: f.assign(raw_open=float('nan')),
        'duplicate': lambda f: pd.concat([f, f.iloc[[0]]]),
        'outside_date': lambda f: f.assign(date=pd.Timestamp('2030-01-01')),
    }[damage]
    change(sources, pins, 'bars', transform)
    assert 'INPUT_REJECTED' in codes(run(sources, pins, options))


def test_nonexistent_observations_not_supplier_missing_and_warmup_not_inferred():
    sources, pins, options = fixture()
    change(sources, pins, 'listings', lambda f: f.assign(listing_date=pd.Timestamp('2021-01-07')))
    for group in ('bars', 'status'):
        change(sources, pins, group, lambda f: f.loc[f.date.ge('2021-01-07')])
    report = run(sources, pins, options)
    before = [r for r in report['coverage_matrix'] if r['symbol'] == SYMBOLS[0] and r['phase'] == 'warmup']
    assert all(r['bars'] == 'NOT_EXISTING_OBSERVATION' for r in before)
    assert not any(i['code'] == 'COVERAGE_MISSING' and i.get('symbol') == SYMBOLS[0] and i.get('date', '') < '2021-01-07' for i in report['unmet_contracts'])
    assert all(r['historical_eligibility'] == 'UNKNOWN' for r in before)
    assert 'SECURITY_WARMUP_INSUFFICIENT' in codes(report)


def test_future_prices_do_not_reconstruct_historical_membership_or_knowledge():
    sources, pins, options = fixture()
    baseline = run(sources, pins, options)
    change(sources, pins, 'bars', lambda f: f.assign(raw_close=f.raw_close.where(f.date.le('2021-01-11'), 10.6)))
    after = run(sources, pins, options)
    assert after['engineering'] == baseline['engineering']
    assert after['coverage_matrix'] == baseline['coverage_matrix']
    assert after['historical_pit'] == 'UNKNOWN'
    assert after['label_endpoints'] == baseline['label_endpoints']
    assert after['receipts'][0]['known_at'] == 'UNKNOWN'


@pytest.mark.parametrize('missing', ['unit', 'origin', 'unknown_origin', 'actions', 'mapping'])
def test_price_sources_units_and_company_action_requirements(missing):
    sources, pins, options = fixture()
    if missing in {'unit', 'origin'}:
        key = 'units' if missing == 'unit' else 'field_origins'
        pins['bars'] = (replace(pins['bars'][0], **{key: {}}), pins['bars'][1])
    elif missing == 'unknown_origin':
        pins['bars'] = (replace(pins['bars'][0], field_origins={name: 'UNKNOWN' for name in FIELDS['bars']}), pins['bars'][1])
    elif missing == 'actions':
        options['actions'] = ()
    else:
        change(sources, pins, 'listings', lambda f: f.drop(columns='security_id'))
    assert run(sources, pins, options)['engineering'] == 'FAIL'


def test_pit_hfq_unverified_formal_request_rejected():
    sources, pins, options = fixture()
    options['requested_formal'] = True
    report = run(sources, pins, options)
    assert report['engineering'] == 'PASS'
    assert report['formal_request'] == 'REJECTED'
    assert report['formal_admission'] == 'BLOCKED'
    assert report['historical_pit'] == report['hfq_qualification'] == 'UNKNOWN'


def test_real_candidate_pipeline_hits_original_admission_gate(tmp_path):
    sources, pins, options = fixture()
    config = SimpleNamespace(source_kind='REAL_CANDIDATE', builder=SimpleNamespace(snapshot=object()),
        models={'predeclared': {}}, queries={g: tuple(p.query for p in v) for g, v in pins.items()},
        research_start=options['research_start'], research_end=options['research_end'])
    with pytest.raises(ProviderError, match='replayable v2 qualification'):
        ResearchPipeline(config).run(DataHub(sources), tracker=None, repo_root=ROOT, command=[])
    assert not list(tmp_path.iterdir())


def load_cli():
    spec = importlib.util.spec_from_file_location('p4_01_cli', ROOT / 'tools/data/audit_p4_01_pipeline_inputs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_aggregate_identity_and_tampering_rejection(tmp_path):
    module = load_cli()
    path = ROOT / 'docs/validation/p3-d07/real-multisource.json'
    report = module.d07_reference(path)
    assert report['raw_consumption'] == 'NOT_RUN'
    assert len(report['sources']) == 4
    changed = tmp_path / 'receipt.json'
    changed.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='identity mismatch'):
        module.d07_reference(changed)


@pytest.mark.parametrize('damage', ['hash', 'coverage', 'security_mapping'])
def test_action_evidence_rejections(damage):
    sources, pins, options = fixture()
    action = options['actions'][0]
    if damage == 'hash':
        options['actions'] = (replace(action, content_sha256='0' * 64), options['actions'][1])
    elif damage == 'coverage':
        adapter = sources[action.source]
        adapter.result = replace(adapter.result, metadata=replace(adapter.result.metadata, coverage='UNKNOWN'))
    else:
        change(sources, pins, 'listings', lambda f: f.assign(security_id='wrong-security'))
    assert run(sources, pins, options)['engineering'] == 'FAIL'


@pytest.mark.parametrize('damage', ['dataframe', 'unordered'])
def test_pipeline_calendar_representation_required(damage):
    sources, pins, options = fixture()
    change(sources, pins, 'calendar', lambda dates: pd.DataFrame({'date': dates}) if damage == 'dataframe' else dates[::-1])
    assert 'INPUT_REJECTED' in codes(run(sources, pins, options))


def test_d07_scoped_view_preserves_parent_hash_and_pipeline_calendar():
    module = load_cli()
    sources, pins, options = fixture()
    original = sources[pins['bars'][0].query.source].result
    original = replace(original, metadata=replace(original.metadata,
        provenance={'research_table_sha256': content_identity(original.data)}))
    calendar = pd.DataFrame({'date': original.data.date})
    calendar_result = QueryResult(calendar, replace(original.metadata,
        provenance={'research_table_sha256': content_identity(calendar)}))
    hub = DataHub({'d07': module._D07ResultAdapter({'daily_bars': original, 'calendar': calendar_result})})
    result = hub.read('d07', 'daily_bars', start=options['research_start'],
        end=options['research_end'], symbol=SYMBOLS[0])
    assert len(result.data) == 3
    assert result.metadata.provenance['parent_research_table_sha256'] == content_identity(original.data)
    assert result.metadata.provenance['research_table_sha256'] == content_identity(result.data)
    assert content_identity(result.data) != content_identity(original.data)
    response = hub.read('d07', 'calendar', start=options['start'], end=options['end'])
    assert isinstance(response.data, pd.DatetimeIndex)
    assert response.metadata.provenance['research_table_sha256'] == content_identity(response.data)


def test_cli_emits_actual_gap_json_and_chinese_summary(tmp_path):
    import subprocess
    import sys
    output = tmp_path / 'gaps.json'
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8',
        str(ROOT / 'tools/data/audit_p4_01_pipeline_inputs.py'),
        '--manifest', str(ROOT / 'docs/validation/p4-01/input-definition.json'),
        '--output', str(output)], capture_output=True, encoding='utf-8')
    assert result.returncode == 1, result.stderr
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['actual_raw_consumption'] == 'NOT_RUN'
    assert report['engineering'] == 'FAIL'
    assert '工程输入预检' in result.stdout
    assert report['formal_admission'] == 'BLOCKED'
