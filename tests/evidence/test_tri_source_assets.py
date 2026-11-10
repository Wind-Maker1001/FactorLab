"""Targeted diagnostic and public-evidence boundary tests."""
import importlib.util
import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('tri_source_assets_audit', ROOT / 'tools/data/tri_source_assets_audit.py')
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def test_duplicate_conflicts_null_dates_and_nonfinite_are_separate():
    frame = pd.DataFrame({'date': ['2020-01-02', '2020-01-02', '2020-01-02', 'bad'],
                          'instrument': ['000001.SZ'] * 3 + ['invalid'],
                          'amount': [1.0, 1.0, 2.0, float('inf')]})
    profile = AUDIT.quality_profile(frame, ['date', 'instrument'], 'date', 'instrument')
    assert profile['duplicate_excess'] == 2
    assert profile['conflicting_keys'] == 1
    assert profile['invalid_date_rows'] == 1
    assert profile['code_format_anomalies'] == 1
    assert profile['nonfinite_nonnull']['amount'] == 1
    assert profile['economic_anomalies'] == 'NOT_CERTIFIED'


def test_prices_missing_values_and_negative_volume_not_repaired():
    frame = pd.DataFrame({'open': [10., None], 'high': [9., None], 'low': [8., None],
                          'close': [10., None], 'volume': [-1., None]})
    profile = AUDIT.quality_profile(frame)
    assert profile['ohlc_order_rows'] == 1
    assert profile['volume_negative'] == 1
    assert profile['nulls']['open'] == 1
    assert pd.isna(frame.loc[1, 'open'])
    assert frame.loc[0, 'high'] == 9.


def test_name_history_key_includes_effective_start_not_only_code():
    frame = pd.DataFrame({'instrument': ['000001.SZ', '000001.SZ'],
                          'start_date': pd.to_datetime(['2000-01-01', '2010-01-01']),
                          'end_date': pd.to_datetime(['2009-12-31', '2020-01-01']),
                          'name': ['former', 'later']})
    result = AUDIT.quality_profile(frame, ['instrument', 'start_date'], 'start_date', 'instrument')
    assert result['duplicate_excess'] == 0
    assert result['conflicting_keys'] == 0
    assert result['date_fields']['start_date']['min'] == '2000-01-01 00:00:00'


def test_literal_vendor_markers_are_not_reported_as_native_nulls():
    frame = pd.DataFrame({'Scode': ['000001', '000002'], 'Trddt': ['2018-01-02'] * 2,
                          'Dtrdvol': ['--', '12'], 'Dtrdamt': ['--', 'inf']})
    result = AUDIT.quality_profile(frame, ['Scode', 'Trddt'], 'Trddt', 'Scode')
    assert result['nulls']['Dtrdvol'] == 0
    assert result['Dtrdvol_nonnumeric_nonnull'] == 1
    assert result['Dtrdvol_literal_dash_marker'] == 1
    assert result['Dtrdamt_numeric_nonfinite'] == 1
    assert frame.loc[0, 'Dtrdvol'] == '--'


def test_nested_query_windows_remain_overlap_not_record_conflicts():
    receipts = [{'window_start': '2020-01-01', 'window_end': '2020-12-31'},
                {'window_start': '2020-02-01', 'window_end': '2020-02-28'},
                {'window_start': '2020-03-01', 'window_end': '2020-03-31'}]
    assert AUDIT.overlaps(receipts) == 2


def test_evidence_preserves_unknowns_and_contains_no_private_absolute_paths():
    root = ROOT / 'docs/validation/tri-source/assets'
    summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
    assert summary['status'] == 'PARTIAL_READ_ONLY_AUDIT'
    assert set(summary['guards']) == set(AUDIT.GUARDS)
    assert summary['metrics']['source_files_by_source']['CNRDS'] > 0
    for path in root.iterdir():
        if path.suffix in ['.csv', '.json', '.md']:
            text = path.read_text(encoding='utf-8')
            assert not re.search(r'[A-Za-z]:[/\\]', text), path.name
    provenance = json.loads((root / 'provenance.json').read_text(encoding='utf-8'))
    assert all(v['historical_vintage'] == 'PIT_UNVERIFIED' for v in provenance['historical_identity'].values())
    actual = [i for i in provenance['identities'] if 'hash_matches_receipt' in i]
    assert actual
    assert all(i['hash_matches_receipt'] and i['receipt_rows_match'] for i in actual)
    assert all(i['available_at'] == 'UNKNOWN' for i in actual)
