"""Synthetic perturbations exercise isolation, not supplier certification."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
import pytest

from factorlab.data.admission import assert_formal_market_admission
from factorlab.data.hub import DataHub
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.diagnostics.candidate_preflight import CandidateQuery
from factorlab.diagnostics.real_data_readiness import batch_readiness, STATES
from factorlab.errors import ProviderError


class Adapter:
    capabilities = frozenset({'daily_bars'})

    def __init__(self, frame, version='v1'):
        self.frame, self.version, self.reads = frame, version, 0

    def read(self, kind, *, symbol, start, end, **kwargs):
        self.reads += 1
        return QueryResult(self.frame.loc[self.frame.symbol.eq(symbol)
            & self.frame.date.between(start, end)].copy(),
            SourceMetadata('SYNTHETIC', self.version, acquired_at='2026-11-09T00:00:00+00:00'))


def inputs():
    dates = pd.bdate_range('2021-01-04', periods=100)
    pieces = [pd.DataFrame(dict(date=dates, symbol=symbol, raw_open=10.,
        raw_high=11., raw_low=9., raw_close=np.arange(100) / 100 + 10,
        volume=100., amount=1000.)) for symbol in ('600000.SH', '000001.SZ')]
    return pd.concat(pieces, ignore_index=True), dates


def run(adapters, dates):
    queries = tuple(CandidateQuery(name, 'v1', symbol, str(dates[0].date()),
        str(dates[-1].date()), str(dates[65].date()), str(dates[-1].date()))
        for name in adapters for symbol in ('600000.SH', '000001.SZ'))
    return batch_readiness(DataHub(adapters), queries, calendar=dates,
        calendar_id='SYNTHETIC_GRID', calendar_kind='SYNTHETIC', units={})


def test_batch_identity_and_formal_boundary():
    frame, dates = inputs()
    adapter = Adapter(frame)
    report = run({'a': adapter}, dates)
    assert adapter.reads == 2
    assert len(report['sources']) == 2
    assert len({r['source']['table_sha256'] for r in report['sources']}) == 2
    for row in report['sources']:
        assert row['gates']['RAW']['status'] == 'PASS'
        assert row['gates']['PIT']['status'] == 'UNKNOWN'
        assert row['gates']['FORMAL']['status'] == 'FAIL'
        assert all(c['status'] in STATES for c in row['checks'].values())
        assert row['checks']['time_fields']['first_distribution'] == 'UNKNOWN'
    with pytest.raises(ProviderError):
        assert_formal_market_admission(report, membership=pd.DataFrame(), calendar=dates,
            bars=frame, benchmark=pd.DataFrame(), research_start=dates[65],
            research_end=dates[-1], require_execution=True)


def test_missing_does_not_disappear_from_grid():
    frame, dates = inputs()
    frame = frame.drop(index=70)
    frame.loc[80, 'raw_open'] = np.nan
    report = run({'a': Adapter(frame)}, dates)
    first, second = report['sources']
    assert first['checks']['coverage']['missing_sessions'] == 1
    assert first['checks']['missing']['counts']['raw_open'] == 1
    assert first['gates']['RAW']['status'] == 'FAIL'
    assert second['gates']['RAW']['status'] == 'PASS'
    assert first['factors'][0]['denominator'] == 35
    assert report['research_window']['qualified_window'] is None
    raw_gap = next(g for g in report['gaps'] if g['condition'] == 'RAW')
    assert raw_gap['category'] == 4
    assert 'eligibility' in raw_gap['required_evidence']


def test_anomaly_is_local_and_not_repaired():
    frame, dates = inputs()
    frame.loc[70, 'volume'] = -1
    adapter = Adapter(frame)
    report = run({'a': adapter}, dates)
    assert report['sources'][0]['gates']['RAW']['status'] == 'FAIL'
    assert report['sources'][0]['factors'][0]['status'] == 'UNKNOWN'
    assert report['sources'][1]['gates']['RAW']['status'] == 'PASS'
    assert adapter.frame.loc[70, 'volume'] == -1


def test_conflict_uses_same_reads_and_never_fuses():
    frame, dates = inputs()
    other = frame.copy()
    other.loc[70, 'amount'] += 1
    left, right = Adapter(frame), Adapter(other)
    report = run({'a': left, 'b': right}, dates)
    assert left.reads == right.reads == 2
    assert report['comparisons'][0]['by_field'] == {'amount': 1}
    assert report['comparisons'][0]['status'] == 'FAIL'
    assert all(not row['merged'] for row in report['comparisons'])
    assert report['comparisons'][1]['status'] == 'PASS'
    assert left.frame.loc[70, 'amount'] == 1000
    assert right.frame.loc[70, 'amount'] == 1001


def test_revision_requires_explicit_new_version():
    frame, dates = inputs()
    with pytest.raises(ValueError, match='version differs'):
        run({'a': Adapter(frame, version='revision-2')}, dates)


def test_future_rows_cannot_change_prior_window():
    frame, dates = inputs()
    baseline = run({'a': Adapter(frame)}, dates)
    future = frame.iloc[[0]].copy()
    future['date'] = dates[-1] + pd.Timedelta(days=10)
    future['raw_close'] = 9999.
    perturbed = run({'a': Adapter(pd.concat([frame, future], ignore_index=True))}, dates)
    assert baseline == perturbed


def test_duplicate_security_is_reported_as_actual_failure():
    frame, dates = inputs()
    report = run({'a': Adapter(pd.concat([frame, frame.iloc[[0]]], ignore_index=True))}, dates)
    assert report['sources'][0]['gates']['RAW']['status'] == 'FAIL'
    assert report['sources'][1]['gates']['RAW']['status'] == 'PASS'


def test_missing_field_is_a_failure_not_a_batch_crash():
    frame, dates = inputs()
    report = run({'a': Adapter(frame.drop(columns='raw_close'))}, dates)
    assert all(r['gates']['RAW']['status'] == 'FAIL' for r in report['sources'])
    assert all(r['checks']['input']['missing_columns'] == ['raw_close'] for r in report['sources'])


@pytest.mark.parametrize('corrupt_receipt', [False, True])
def test_existing_bigquant_loader_multisecurity_and_revision_bytes(tmp_path, corrupt_receipt):
    from factorlab.data.evidence import sha256
    path = Path(__file__).resolve().parents[2] / 'tools/data/candidate_preflight.py'
    spec = importlib.util.spec_from_file_location('_test_preflight_loader', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    frame, dates = inputs()
    native = frame.rename(columns={'symbol': 'instrument', **{
        'raw_' + f: f for f in ('open', 'high', 'low', 'close')}})
    payload = native.to_parquet(index=False)
    name = 'stock/cn_stock_real_bar1d/2021-01-01_2021-05-31.parquet'
    archive = tmp_path / 'stock.zip'
    with zipfile.ZipFile(archive, 'w') as target:
        target.writestr(name, payload)
        target.writestr(name + '.receipt.json', json.dumps({
            'sha256': '0' * 64 if corrupt_receipt else sha256(payload)}))
        target.writestr('stock/cn_stock_real_bar1d/schema.json', '{}')
    if corrupt_receipt:
        with pytest.raises(ValueError, match='differs from its receipt'):
            module.bigquant(archive, dates[0], dates[-1], symbols=('600000.SH', '000001.SZ'))
    else:
        adapter = module.bigquant(archive, dates[0], dates[-1], symbols=('600000.SH', '000001.SZ'))
        assert set(adapter.frame.symbol) == {'600000.SH', '000001.SZ'}
        assert len(adapter.frame) == 200
        assert adapter.metadata.provenance['selected_archive_binding']['members'][0]['sha256'] == sha256(payload)


def test_batch_requires_one_window():
    frame, dates = inputs()
    query = CandidateQuery('a', 'v1', '600000.SH', str(dates[0].date()),
        str(dates[-1].date()), str(dates[65].date()), str(dates[-1].date()))
    with pytest.raises(ValueError, match='one explicit batch window'):
        batch_readiness(DataHub({'a': Adapter(frame)}),
            (query, replace(query, symbol='000001.SZ', holding_days=2)), calendar=dates,
            calendar_id='SYNTHETIC', calendar_kind='SYNTHETIC', units={})
