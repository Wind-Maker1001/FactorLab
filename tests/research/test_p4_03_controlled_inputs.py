"""Controlled routing fixtures are software evidence, not market qualification."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pandas as pd
import pytest

from factorlab.application.research_pipeline import ResearchPipeline
from factorlab.data.hub.contracts import QueryResult, SourceMetadata, UnsupportedQueryError
from factorlab.diagnostics.pipeline_preflight import content_identity, pipeline_preflight

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location('p4_03_cli', ROOT / 'tools/data/audit_p4_03_controlled_inputs.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


@pytest.fixture
def controlled():
    dates = pd.bdate_range('2021-01-04', periods=12)
    symbols = ['600000.SH', '000001.SZ']
    selection = dict(symbols=symbols, routes={g: 'joinquant' if g == 'membership' else 'bigquant' for g in cli.GROUPS},
        window=dict(start=str(dates[0].date()), end=str(dates[-1].date()),
            research_start=str(dates[3].date()), research_end=str(dates[5].date()),
            warmup_sessions=3, holding_days=5, execution_tail_sessions=1))
    frame = pd.DataFrame([(day, symbol) for symbol in symbols for day in dates], columns=['date', 'symbol'])
    bars = frame.copy()
    for prefix in ('raw_', 'adj_'):
        for field, value in [('open', 10.), ('high', 11.), ('low', 9.), ('close', 10.5)]:
            bars[prefix + field] = value
    bars['volume'], bars['amount'], bars['turnover'] = 100., 1000., .01
    data = dict(bars=bars, membership=frame.copy(), status=frame.assign(trade_status=1, is_st=False),
        listings=pd.DataFrame(dict(symbol=symbols, listing_date=pd.to_datetime(['2010-01-01'] * 2), delist_date=[pd.NaT] * 2)),
        calendar=pd.DataFrame({'date': dates}), benchmark=pd.DataFrame(dict(date=dates, symbol='000905.SH', open=100.)))
    inputs = {g: QueryResult(f, SourceMetadata(cli.SOURCE_IDS[selection['routes'][g]], 'fixture-v1',
        acquired_at='2026-11-09T00:00:00Z', provenance=dict(source_kind='SYNTHETIC',
            research_table_sha256=content_identity(f), input_members=[]))) for g, f in data.items()}
    identities = [dict(archive='fixture.zip', member='fixture', content_sha256='a' * 64)]
    definition = cli.inventory(selection, inputs, identities)
    return definition, inputs, identities


def issues(report):
    return {issue['code'] for issue in report['unmet_contracts']}


def test_six_groups_route_to_two_native_sources_without_fusion(controlled):
    definition, inputs, identities = controlled
    report = cli.audit(definition, inputs, identities)
    assert report['actual_raw_consumption'] == 'RUN'
    assert all(report['query_completeness'].values())
    assert report['capabilities'] == dict(bigquant=['calendar', 'daily_bars', 'index_bars', 'listings', 'security_status'], joinquant=['membership'])
    assert len(report['receipts']) == len(report['input_matrix']) == 10
    assert all(r['status'] == 'PASS' for r in report['receipts'])
    assert report['engineering'] == 'FAIL'
    assert issues(report) == {'UNIT_UNDECLARED', 'ACTION_SECURITY_MAPPING_MISSING'}
    assert report['formal_request'] == 'REJECTED'
    assert report['formal_admission'] == 'BLOCKED'
    assert report['corporate_action_binding']['stable_security_mapping'] == 'MISSING'
    assert not report['action_receipts']
    assert all(r['known_at'] == 'UNKNOWN' for r in report['receipts'])


def test_synthetic_complete_fields_and_declared_units_are_directly_consumable(controlled):
    definition, inputs, _ = controlled
    for entries in definition['queries'].values():
        for entry in entries:
            entry['units'] = {name: 'SYNTHETIC_DIMENSION' for name in entry['units']}
    hub, bindings, _ = cli.register(definition, inputs)
    report = pipeline_preflight(hub, bindings, symbols=tuple(definition['symbols']), **definition['window'], require_actions=False)
    assert report['engineering'] == 'PASS', report['unmet_contracts']
    assert [report['layers'][p]['sessions'] for p in ('warmup', 'research', 'tail')] == [3, 3, 6]
    assert report['formal_admission'] == 'BLOCKED'


@pytest.mark.parametrize('damage', ['kind', 'source', 'window', 'duplicate', 'native_source'])
def test_ambiguous_or_false_source_registration_rejected(controlled, damage):
    definition, inputs, _ = controlled
    entry = definition['queries']['bars'][0]
    if damage == 'kind':
        entry['query']['kind'] = 'corporate_actions'
    elif damage == 'source':
        entry['query']['source'] = 'joinquant'
    elif damage == 'window':
        entry['query']['end'] = '2021-01-15'
    elif damage == 'duplicate':
        definition['queries']['bars'].append(deepcopy(entry))
    else:
        inputs['bars'] = replace(inputs['bars'], metadata=replace(inputs['bars'].metadata, source_id='invented'))
    with pytest.raises(ValueError):
        cli.register(definition, inputs)


@pytest.mark.parametrize('field', ['content_sha256', 'receipt_sha256', 'schema_sha256', 'manifest_sha256'])
def test_raw_pin_drift_rejected_without_repinning(controlled, field):
    definition, inputs, identities = controlled
    changed = deepcopy(identities)
    changed[0][field] = '0' * 64
    with pytest.raises(ValueError, match='identity drift'):
        cli.audit(definition, inputs, changed)


@pytest.mark.parametrize('damage', ['version', 'content', 'field'])
def test_response_identity_and_field_gaps_rejected(controlled, damage):
    definition, inputs, identities = controlled
    if damage == 'version':
        inputs['bars'] = replace(inputs['bars'], metadata=replace(inputs['bars'].metadata, data_version='fixture-v2'))
    elif damage == 'content':
        inputs['bars'] = replace(inputs['bars'], data=inputs['bars'].data.assign(volume=999.))
    else:
        inputs['bars'] = replace(inputs['bars'], data=inputs['bars'].data.drop(columns='turnover'))
        for entry in definition['queries']['bars']:
            entry['content_sha256'] = content_identity(cli.scoped(inputs['bars'], 'bars', entry['query']['symbol'], definition['window']).data)
    report = cli.audit(definition, inputs, identities)
    assert 'INPUT_REJECTED' in issues(report)
    assert report['engineering'] == 'FAIL'
    if damage == 'field':
        assert all('turnover' in row['missing_fields'] for row in report['input_matrix'] if row['group'] == 'bars')


def test_no_membership_observation_remains_unknown(controlled):
    definition, inputs, identities = controlled
    inputs['membership'] = replace(inputs['membership'], data=inputs['membership'].data.iloc[:0])
    for entry in definition['queries']['membership']:
        entry['content_sha256'] = content_identity(inputs['membership'].data)
    report = cli.audit(definition, inputs, identities)
    assert 'SECURITY_MEMBERSHIP_UNKNOWN' in issues(report)
    assert all(row['membership'] == 'UNKNOWN' for row in report['coverage_matrix'])
    assert all(row['historical_eligibility'] == 'UNKNOWN' for row in report['coverage_matrix'])


def test_boundaries_missing_status_and_suspension_never_certify_history(controlled):
    definition, inputs, identities = controlled
    symbol = definition['symbols'][0]
    listings = inputs['listings'].data.copy()
    listings.loc[listings.symbol.eq(symbol), 'listing_date'] = pd.Timestamp('2021-01-07')
    inputs['listings'] = replace(inputs['listings'], data=listings)
    for group in ('bars', 'status'):
        frame = inputs[group].data
        frame = frame.loc[~(frame.symbol.eq(symbol) & frame.date.lt('2021-01-07'))].copy()
        if group == 'status':
            frame.loc[frame.date.eq('2021-01-08'), 'trade_status'] = 0
            frame = frame.loc[~(frame.symbol.eq(symbol) & frame.date.eq('2021-01-11'))]
        inputs[group] = replace(inputs[group], data=frame)
    for group, entries in definition['queries'].items():
        for entry in entries:
            entry['content_sha256'] = content_identity(cli.scoped(inputs[group], group, entry['query']['symbol'], definition['window']).data)
    report = cli.audit(definition, inputs, identities)
    cells = [c for c in report['coverage_matrix'] if c['symbol'] == symbol]
    assert all(c['bars'] == 'NOT_EXISTING_OBSERVATION' for c in cells if c['date'] < '2021-01-07')
    assert next(c for c in cells if c['date'] == '2021-01-11')['status'] == 'MISSING'
    assert next(c for c in cells if c['date'] == '2021-01-08')['status'] == 'OBSERVED'
    assert 'SECURITY_WARMUP_INSUFFICIENT' in issues(report)
    assert all(c['historical_eligibility'] == 'UNKNOWN' for c in cells)


def test_future_outside_scope_does_not_change_historical_pins_or_time(controlled):
    definition, inputs, identities = controlled
    baseline = cli.audit(definition, inputs, identities)
    frame = inputs['bars'].data
    inputs['bars'] = replace(inputs['bars'], data=pd.concat([frame, frame.iloc[:1].assign(date=pd.Timestamp('2030-01-01'), raw_close=-999.)], ignore_index=True))
    after = cli.audit(definition, inputs, identities)
    assert after['receipts'] == baseline['receipts']
    assert after['coverage_matrix'] == baseline['coverage_matrix']
    assert after['historical_pit'] == after['hfq_qualification'] == 'UNKNOWN'


def test_exact_routes_reject_unregistered_window_symbol_and_pit_cutoff(controlled):
    definition, inputs, _ = controlled
    hub, _, _ = cli.register(definition, inputs)
    for kwargs in [dict(symbol='601857.SH'), dict(start='2021-01-05'), dict(knowledge_as_of='2021-01-20T00:00:00Z')]:
        options = dict(start=definition['window']['start'], end=definition['window']['end'], symbol=definition['symbols'][0])
        options.update(kwargs)
        with pytest.raises(UnsupportedQueryError):
            hub.read('bigquant', 'daily_bars', **options)
    with pytest.raises(UnsupportedQueryError):
        hub.read('joinquant', 'daily_bars', start='2021-01-04', end='2021-01-19', symbol='600000.SH')


@pytest.mark.parametrize('damage', [None, 'response_version', 'builder_version', 'missing_input', 'time'])
def test_real_candidate_controlled_routes_enter_exploratory_builder(controlled, tmp_path, damage):
    definition, inputs, _ = controlled
    if damage == 'response_version':
        inputs['bars'] = replace(inputs['bars'], metadata=replace(inputs['bars'].metadata, data_version='fixture-v2'))
    hub, bindings, _ = cli.register(definition, inputs)
    build_started = RuntimeError('controlled exploratory dataset build started')
    builder = SimpleNamespace(snapshot=object(), data_version='fixture-v1',
        build_chunked=Mock(side_effect=build_started))
    config = SimpleNamespace(source_kind='REAL_CANDIDATE', builder=builder,
        models={'predeclared': {}}, queries={g: tuple(p.query for p in entries) for g, entries in bindings.items()},
        research_start=definition['window']['research_start'], research_end=definition['window']['research_end'],
        output=tmp_path / 'exploration', chunked=object())
    if damage == 'builder_version':
        builder.data_version = 'fixture-v2'
    elif damage == 'missing_input':
        del config.queries['membership']
    elif damage == 'time':
        config.research_end = definition['window']['end']
    errors = {'response_version': 'DataHub version differs from pinned query',
        'builder_version': 'builder data version must bind the selected bars version',
        'missing_input': 'six explicit input query groups required',
        'time': 'research interval requires a subsequent execution/label calendar'}
    if damage is not None:
        with pytest.raises(ValueError, match=errors[damage]):
            ResearchPipeline(config).run(hub, tracker=None, repo_root=ROOT, command=[])
        builder.build_chunked.assert_not_called()
        assert not config.output.exists()
        return
    with pytest.raises(RuntimeError) as caught:
        ResearchPipeline(config).run(hub, tracker=None, repo_root=ROOT, command=[])
    assert caught.value is build_started
    builder.build_chunked.assert_called_once()
    bars, benchmark, membership, status, listings, calendar = builder.build_chunked.call_args.args
    assert set(bars.symbol) == set(status.symbol) == set(listings.symbol) == set(definition['symbols'])
    assert set(benchmark.symbol) == {'000905.SH'}
    assert membership.date.between(config.research_start, config.research_end).all()
    assert len(membership) == 6
    assert calendar.equals(pd.DatetimeIndex(inputs['calendar'].data.date))
    assert builder.build_chunked.call_args.kwargs == dict(output_dir=config.output / 'dataset', config=config.chunked)


def test_receipt_only_cli_never_claims_raw_consumption(controlled, tmp_path, monkeypatch):
    definition, _, _ = controlled
    manifest, output = tmp_path / 'definition.json', tmp_path / 'report.json'
    manifest.write_text(json.dumps(definition), encoding='utf-8')
    monkeypatch.setattr('sys.argv', ['audit', '--manifest', str(manifest), '--output', str(output)])
    assert cli.main() == 1
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['actual_raw_consumption'] == report['upstream_d07']['raw_consumption'] == 'NOT_RUN'
    assert report['engineering'] == 'FAIL'
    assert report['formal_request'] == 'REJECTED'
    assert len(report['receipts']) == 10
    assert all(r['status'] == 'FAIL' for r in report['receipts'])


def test_membership_manifest_must_match_consumed_bytes(tmp_path):
    path = tmp_path / 'members.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('manifest.json', json.dumps({'output_sha256': {'members_daily.csv': '0' * 64}}))
        archive.writestr('members_daily.csv', 'date,stock_code\n2021-01-04,600000.XSHG\n')
    with pytest.raises(ValueError, match='membership payload differs'):
        cli.source_identities({}, tmp_path, path)
