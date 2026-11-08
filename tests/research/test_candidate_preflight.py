"""Deterministic restricted-input contracts; fixtures are not vendor evidence."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from factorlab.data.admission import assert_formal_market_admission
from factorlab.data.governance import diagnose_tables
from factorlab.data.hub import DataHub
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.diagnostics.candidate_preflight import CandidateQuery, preflight
from factorlab.errors import ProviderError


class FixtureAdapter:
    capabilities = frozenset({'daily_bars'})

    def __init__(self, frame, source='fixture', version='v1'):
        self.frame, self.source, self.version = frame, source, version

    def read(self, kind, **kwargs):
        return QueryResult(self.frame.copy(), SourceMetadata(self.source, self.version,
            provenance={'source_kind': 'SYNTHETIC', 'fixture': True}))


def fixture():
    dates = pd.bdate_range('2017-01-02', periods=100)
    price = np.arange(100, dtype=float) + 10
    frame = pd.DataFrame({'date': dates, 'symbol': '601857.SH', 'raw_open': price,
        'raw_high': price + 1, 'raw_low': price - 1, 'raw_close': price,
        'volume': price * 100, 'amount': price * 1000})
    query = CandidateQuery('fixture', 'v1', '601857.SH', str(dates[0].date()),
        str(dates[-1].date()), str(dates[65].date()), str(dates[-1].date()))
    return frame, dates, query


def run(frame, dates, query):
    return preflight(DataHub({'fixture': FixtureAdapter(frame)}), query,
        calendar=dates, calendar_id='FIXTURE', calendar_kind='SYNTHETIC',
        units={'raw_open': 'fixture currency'}, action_evidence={'count': 0, 'coverage': 'UNKNOWN'})


def test_raw_factors_provenance_and_maturity():
    frame, dates, query = fixture()
    result = run(frame, dates, query)
    assert result.checks['coverage']['unique_security_dates'] == 100
    assert result.checks['coverage']['warmup_rows'] == 65
    assert all(f['valid_samples'] == 35 for f in result.factors[:5])
    assert all(f['source'] == 'fixture' and f['version'] == 'v1' for f in result.factors)
    assert all(f['context']['admission_status'] == 'FORMAL_ADMISSION_BLOCKED' for f in result.factors[:5])
    assert result.labels['raw_future_coverage'] == 29
    assert result.labels['last_signal_with_observed_exit'] == str(dates[-7].date())
    assert result.labels['last_observed_exit'] == str(dates[-1].date())
    assert result.labels['historical_maturity'] == 'UNKNOWN'
    assert result.labels['label_generation'] == 'NOT_RUN'
    assert result.checks['corporate_actions']['status'] == 'UNKNOWN'
    assert result.checks['time_fields']['first_distribution'] == 'UNKNOWN'
    assert result.checks['identity']['status'] == 'UNKNOWN'
    assert all(f['status'] == 'NOT_AVAILABLE' for f in result.factors[5:])


def test_missing_is_not_zero_filled_or_dropped_from_denominator():
    frame, dates, query = fixture()
    frame.loc[70, 'raw_close'] = np.nan
    frame.loc[80, 'raw_open'] = np.nan
    frame = frame.drop(index=75)
    result = run(frame, dates, query)
    assert result.checks['missing']['counts']['raw_close'] == 1
    assert result.checks['coverage']['missing_sessions'] == 1
    assert result.factors[0]['denominator'] == 35
    assert result.factors[0]['missing_rate'] > 0
    assert result.labels['raw_future_coverage'] < 29


def test_duplicates_are_rejected_by_hub_and_reported():
    frame, dates, query = fixture()
    result = run(pd.concat([frame, frame.iloc[[0]]]), dates, query)
    assert result.checks['input']['status'] == 'FAIL'
    assert 'duplicate' in result.checks['input']['reason']
    assert result.labels['status'] == 'NOT_RUN'


@pytest.mark.parametrize('column,value', [('raw_high', 0), ('volume', -1), ('raw_close', np.inf)])
def test_invalid_market_data_stops_factors(column, value):
    frame, dates, query = fixture()
    frame.loc[70, column] = value
    result = run(frame, dates, query)
    assert all(f['status'] == 'NOT_RUN' for f in result.factors[:5])


def test_unused_adjusted_fields_cannot_affect_raw_computation():
    frame, dates, query = fixture()
    frame['adj_close'] = -1.0
    frame['adj_open'] = np.inf
    result = run(frame, dates, query)
    assert all(f['valid_samples'] == 35 for f in result.factors[:5])
    assert all('HFQ' not in f['definition']['price_basis'] for f in result.factors[:5])


def test_nonfinite_open_is_not_a_mature_observation():
    frame, dates, query = fixture()
    frame.loc[80, 'raw_open'] = np.inf
    result = run(frame, dates, query)
    assert result.labels['raw_future_coverage'] == 27
    assert result.labels['historical_maturity'] == 'UNKNOWN'


def test_pin_mismatch_is_not_hidden():
    frame, dates, query = fixture()
    with pytest.raises(ValueError, match='version'):
        run(frame, dates, replace(query, version='v2'))


def test_conflicts_remain_separate_and_include_missing():
    frame, _, _ = fixture()
    a = FixtureAdapter(frame).read('daily_bars')
    other = frame.copy()
    other.loc[70, 'amount'] += 0.001
    other = other.drop(index=80)
    b = FixtureAdapter(other, 'other', 'v2').read('daily_bars')
    conflicts = diagnose_tables(a, b, source='fixture', version='v1', other_source='other',
        other_version='v2', fields=['raw_close', 'amount'])
    assert len(conflicts) == 3
    assert conflicts[0]['left']['source'] == 'fixture'
    assert any(c['type'] == 'MISSING' for c in conflicts)
    assert len(a.data) == 100 and len(b.data) == 99


def test_inaccessible_source_is_not_run():
    frame, dates, query = fixture()
    class Absent(FixtureAdapter):
        def read(self, kind, **kwargs):
            raise FileNotFoundError('private file absent')
    result = preflight(DataHub({'fixture': Absent(frame)}), query, calendar=dates,
        calendar_id='FIXTURE', calendar_kind='SYNTHETIC', units={})
    assert result.accessibility == 'NOT_ACCESSIBLE'
    assert result.checks['input']['status'] == 'NOT_RUN'
    assert not result.factors


@pytest.mark.parametrize('horizon', [1, 5, 20])
def test_label_horizon_is_t_plus_h_plus_one(horizon):
    frame, dates, query = fixture()
    result = run(frame, dates, replace(query, holding_days=horizon))
    assert result.labels['incomplete_future_coverage'] == horizon + 1
    assert result.labels['last_signal_with_observed_exit'] == str(dates[-horizon - 2].date())


def test_governed_version_is_bound_without_replacing_native_version():
    frame, dates, query = fixture()
    class Governed(FixtureAdapter):
        def read(self, kind, **kwargs):
            return QueryResult(self.frame, SourceMetadata('fixture', 'native-v1',
                provenance={'governance_version': 'archive-v2'}))
    result = preflight(DataHub({'fixture': Governed(frame)}), replace(query, version='archive-v2'),
        calendar=dates, calendar_id='FIXTURE', calendar_kind='SYNTHETIC', units={})
    assert result.source['data_version'] == 'native-v1'
    assert result.factors[0]['version'] == 'archive-v2'
    with pytest.raises(ValueError, match='version'):
        preflight(DataHub({'fixture': Governed(frame)}), replace(query, version='native-v1'),
            calendar=dates, calendar_id='FIXTURE', calendar_kind='SYNTHETIC', units={})


def test_diagnostics_cannot_authorize_formal_consumption():
    frame, dates, query = fixture()
    result = run(frame, dates, query)
    for candidate in (None, result):
        with pytest.raises(ProviderError, match='QUALIFICATION REQUIRED'):
            assert_formal_market_admission(candidate, membership=pd.DataFrame(), calendar=dates,
                bars=frame, benchmark=pd.DataFrame(), research_start=query.research_start,
                research_end=query.research_end, require_execution=True)
