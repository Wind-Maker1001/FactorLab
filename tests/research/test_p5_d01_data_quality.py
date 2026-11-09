"""Observation quality is not a financial admission credential."""
from dataclasses import replace

import pandas as pd
import pytest

from factorlab.data.units import bigquant_field_semantics
from tests.research.test_p4_01_pipeline_inputs import fixture, change, run, codes


def first_cell(report):
    return report['research_quality']['observations'][0]


@pytest.mark.parametrize('damage,flag', [
    ('missing', 'RAW_FIELD_MISSING'),
    ('ohlc', 'RAW_OHLC_RELATIONSHIP_INVALID'),
    ('infinite', 'NONFINITE_OR_NONNUMERIC_FIELD'),
    ('negative', 'NEGATIVE_MARKET_QUANTITY'),
])
def test_numeric_rejection_retains_observed_rows(damage, flag):
    sources, pins, options = fixture()
    field, value = {'missing': ('raw_open', float('nan')), 'ohlc': ('raw_high', 1.),
                    'infinite': ('amount', float('inf')), 'negative': ('volume', -1.)}[damage]
    change(sources, pins, 'bars', lambda f: f.assign(**{field: value}))
    report = run(sources, pins, options)
    assert 'INPUT_REJECTED' in codes(report)
    cell = first_cell(report)
    assert flag in cell['observation_flags']
    assert 'EXPECTED_BAR_NOT_OBSERVED' not in cell['observation_flags']
    assert not cell['numeric_calculable']
    # A rejected query does not block valid observations of another security.
    assert report['research_quality']['numeric_calculable_rows'] == 12
    assert report['formal_admission'] == 'BLOCKED'


def test_listing_missing_suspension_and_membership_are_separate():
    sources, pins, options = fixture()
    change(sources, pins, 'bars', lambda f: f.iloc[2:].copy())
    change(sources, pins, 'listings', lambda f: f.assign(listing_date=pd.Timestamp('2021-01-05'),
                                                      delist_date=pd.Timestamp('2021-01-14')))
    change(sources, pins, 'status', lambda f: f.assign(trade_status=0))
    report = run(sources, pins, options)
    rows = report['research_quality']['observations'][:12]
    assert 'PRE_LISTING' in rows[0]['observation_flags']
    assert 'EXPECTED_BAR_NOT_OBSERVED' not in rows[0]['observation_flags']
    assert 'EXPECTED_BAR_NOT_OBSERVED' in rows[1]['observation_flags']
    assert 'HISTORICAL_MEMBERSHIP_UNKNOWN' in rows[0]['observation_flags']
    assert rows[3]['membership_observation'] == 'POSITIVE_OBSERVATION'
    assert 'SUSPENDED_OBSERVATION' in rows[3]['observation_flags']
    assert rows[3]['numeric_calculable']
    assert rows[-1]['observed_listing_boundary'] == 'POST_DELISTING'
    assert 'LIFETIME_CONFLICT' in rows[-1]['observation_flags']
    assert all(r['historical_identity_and_timing'] == 'UNKNOWN' for r in rows)


def test_identity_rejection_does_not_publish_untrusted_observations():
    sources, pins, options = fixture()
    pin = pins['bars'][0]
    pins['bars'] = (replace(pin, content_sha256='0' * 64), pins['bars'][1])
    cell = first_cell(run(sources, pins, options))
    assert not cell['numeric_calculable']
    assert 'EXPECTED_BAR_NOT_OBSERVED' in cell['observation_flags']


def test_current_vendor_documentation_does_not_clear_historical_units():
    semantics = bigquant_field_semantics('bars')
    assert semantics['volume']['vendor_documented_unit'] == 'shares'
    assert semantics['amount']['vendor_documented_unit'] == 'yuan; currency unspecified'
    assert semantics['turnover']['vendor_documented_unit'] == 'fraction'
    assert semantics['turnover']['native_field'] == 'cn_stock_real_bar1d.turn'
    assert semantics['turnover']['transformation'] == 'rename_only'
    assert all(s['normalized_unit'] == 'UNKNOWN_VENDOR_CONTRACT' for s in semantics.values())
    assert all(s['multiplier'] is None for s in semantics.values())
    assert bigquant_field_semantics('benchmark')['open']['evidence_level'] == 'UNKNOWN'
    sources, pins, options = fixture()
    for index, pin in enumerate(pins['bars']):
        adapter = sources[pin.query.source]
        adapter.result = replace(adapter.result, metadata=replace(adapter.result.metadata,
            field_semantics=semantics))
        entries = list(pins['bars'])
        entries[index] = replace(pin, units={f: 'UNKNOWN_VENDOR_CONTRACT' for f in pin.units})
        pins['bars'] = tuple(entries)
    report = run(sources, pins, options)
    assert sum(i['code'] == 'UNIT_UNDECLARED' for i in report['unmet_contracts']) == 22
    fields = [f for f in report['research_quality']['fields'] if f['group'] == 'bars']
    assert all(not f['source_and_unit_explained'] for f in fields)
    assert report['research_quality']['numeric_calculable_rows'] == 24


def test_late_anomaly_does_not_quarantine_earlier_observations():
    sources, pins, options = fixture()
    def damage(frame):
        frame.loc[frame.index[-1], 'adj_low'] = 100.
        return frame
    change(sources, pins, 'bars', damage)
    rows = run(sources, pins, options)['research_quality']['observations'][:12]
    assert all(row['numeric_calculable'] for row in rows[:-1])
    assert not rows[-1]['numeric_calculable']
    assert 'ADJ_OHLC_RELATIONSHIP_INVALID' in rows[-1]['observation_flags']


def test_post_delisting_absence_is_not_supplier_missing():
    sources, pins, options = fixture()
    change(sources, pins, 'listings', lambda f: f.assign(delist_date=pd.Timestamp('2021-01-14')))
    change(sources, pins, 'bars', lambda f: f.loc[f.date.le('2021-01-14')].copy())
    rows = run(sources, pins, options)['research_quality']['observations'][:12]
    assert rows[-1]['observation_flags'][0] == 'POST_DELISTING'
    assert 'EXPECTED_BAR_NOT_OBSERVED' not in rows[-1]['observation_flags']
    assert not rows[-1]['numeric_calculable']


def test_known_units_and_numeric_failure_are_independent():
    sources, pins, options = fixture()
    pin = pins['bars'][0]
    adapter = sources[pin.query.source]
    adapter.result = replace(adapter.result, metadata=replace(adapter.result.metadata,
        field_semantics={'raw_open': dict(normalized_unit='CNY/share',
            evidence_level='REPLAY_VALIDATED_DICTIONARY_AND_CONVERSION')}))
    change(sources, pins, 'bars', lambda f: f.assign(raw_open=float('nan')))
    report = run(sources, pins, options)
    field = next(f for f in report['research_quality']['fields'] if f['group'] == 'bars')
    assert field['source_and_unit_explained']
    assert not first_cell(report)['numeric_calculable']
    assert report['formal_admission'] == 'BLOCKED'


def test_unknown_status_not_normal_trading():
    sources, pins, options = fixture()
    change(sources, pins, 'status', lambda f: f.assign(trade_status=float('nan')))
    flags = first_cell(run(sources, pins, options))['observation_flags']
    assert 'STATUS_UNKNOWN' in flags
    assert 'NORMAL_TRADING_OBSERVATION' not in flags
