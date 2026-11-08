"""Aggregation must preserve defects instead of cleaning source observations."""
import runpy
from pathlib import Path

import pandas as pd


PROFILE = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tools/data/audit_p3_d01.py'))['profile']


def test_profile_keeps_duplicate_null_and_price_defects():
    frame = pd.DataFrame({
        'date': pd.to_datetime(['2024-01-02', '2024-01-02', '2024-01-03']),
        'instrument': ['600000.SH'] * 3,
        'open': [10.0, 10.0, None], 'high': [9.0, 11.0, None],
        'low': [8.0, 9.0, None], 'close': [10.0, 0.0, None],
        'volume': [-1, 1, 0], 'amount': [float('inf'), -2.0, None],
    })
    result = PROFILE(frame, ['date', 'instrument'])
    assert result['rows'] == 3
    assert result['unique_keys'] == 2
    assert result['duplicate_excess'] == 1
    assert result['nulls']['open'] == 1
    assert result['nonfinite_nonnull']['amount'] == 1
    assert result['ohlc_order_rows'] == 2
    assert result['nonpositive_price_rows'] == 1
    assert result['negative_volume_rows'] == 1
    assert result['negative_amount_rows'] == 1
    assert len(frame) == 3


def test_composite_event_key_counts_securities_not_methods():
    frame = pd.DataFrame({
        'date': pd.to_datetime(['2024-01-02'] * 3),
        'instrument': ['600000.SH', '600000.SH', '600001.SH'],
        'type': ['cash', 'cash', 'cash'], 'method': ['a', 'b', 'a'],
    })
    result = PROFILE(frame, ['date', 'instrument', 'type', 'method'])
    assert result['unique_keys'] == 3
    assert result['securities'] == 2
    assert result['observed_dates'] == 1
