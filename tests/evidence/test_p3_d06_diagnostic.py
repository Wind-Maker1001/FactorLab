"""Coverage accounting and calendar-boundary fixtures for aggregate diagnostics."""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location('p3d06', Path(__file__).parents[2] / 'tools/data/audit_p3_d06.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_missing_key_is_not_a_present_null_or_identity_successor():
    members = pd.DataFrame({'date': pd.to_datetime(['2025-01-02'] * 2), 'symbol': ['300114.SZ', 'A']})
    bars = pd.DataFrame({'date': pd.to_datetime(['2025-01-02'] * 2), 'symbol': ['302132.SZ', 'A'], 'close': [2., np.nan]})
    row = audit.field_profile(audit.join_members(members, bars), ['close'], 'fixture')[0]
    assert (row['missing_keys'], row['null_on_present_keys'], row['unavailable_member_days']) == (1, 1, 2)
    assert row['finite_member_days'] == 0


def test_duplicate_observations_are_rejected_not_fused():
    members = pd.DataFrame({'date': [pd.Timestamp('2025-01-02')], 'symbol': ['A']})
    with pytest.raises(pd.errors.MergeError):
        audit.join_members(members, pd.concat([members, members]))


def test_label_endpoints_follow_sessions_not_observed_security_rows():
    dates = pd.bdate_range('2025-01-02', periods=10)
    members = pd.DataFrame({'date': dates[:3], 'symbol': 'A'})
    bars = pd.DataFrame({'date': dates, 'symbol': 'A', 'adj_open': 10.})
    bars = bars.loc[bars.date.ne(dates[1])]
    result = audit.label_availability(bars, members, dates, dates)
    assert result.entry_date.tolist() == dates[1:4].tolist()
    assert result.exit_date.tolist() == dates[6:9].tolist()
    assert result.label_endpoints_available.tolist() == [False, True, True]
    result = audit.label_availability(bars, members, dates, dates.delete(7))
    assert result.label_endpoints_available.tolist() == [False, False, True]


def test_full_history_warmup_precedes_membership_and_missing_sessions_propagate():
    dates = pd.bdate_range('2024-01-02', periods=90)
    bars = pd.DataFrame({'date': dates, 'symbol': '600000.SH'})
    for field in ['raw_open', 'raw_high', 'raw_low', 'raw_close',
                  'adj_open', 'adj_high', 'adj_low', 'adj_close',
                  'volume', 'amount', 'turnover']:
        bars[field] = np.arange(90, dtype=float) + 100
    bars[['raw_high', 'adj_high']] += 1
    bars[['raw_low', 'adj_low']] -= 1
    members = bars.loc[70:, ['date', 'symbol']].reset_index(drop=True)
    factors, common, _, _ = audit.factor_diagnostics(bars, members, dates)
    assert len(factors) == 78
    assert common.all78.all()
    assert factors.finite.eq(20).all()
    sparse = bars.loc[bars.date.ne(dates[69])]
    factors, common, _, _ = audit.factor_diagnostics(sparse, members, dates)
    mom = factors.loc[factors.factor_id.eq('MOM60')].iloc[0]
    vol = factors.loc[factors.factor_id.eq('VOL60')].iloc[0]
    assert mom.finite == 20  # Endpoint-only momentum does not need every intervening price.
    assert vol.finite == 0  # Return volatility does need all observations in the window.
    assert not common.all78.any()


def test_invalid_warmup_blocks_security_without_changing_source():
    dates = pd.bdate_range('2024-01-02', periods=70)
    bars = pd.DataFrame({'date': dates, 'symbol': '600000.SH'})
    for field in ['raw_open', 'raw_high', 'raw_low', 'raw_close',
                  'adj_open', 'adj_high', 'adj_low', 'adj_close', 'volume', 'amount', 'turnover']:
        bars[field] = 10.
    bars.loc[0, 'raw_high'] = 9.
    before = bars.copy(deep=True)
    members = bars.loc[65:, ['date', 'symbol']].reset_index(drop=True)
    factors, common, _, quality = audit.factor_diagnostics(bars, members, dates)
    assert quality['invalid_input_rows'] == 1
    assert quality['invalid_rows_outside_research_dates'] == 1
    assert quality['quarantined_member_days'] == 5
    assert factors.finite.eq(0).all()
    assert not common.all78.any()
    pd.testing.assert_frame_equal(bars, before)


def test_fold_uses_real_exit_boundary_in_addition_to_five_day_purge():
    dates = pd.bdate_range('2021-01-04', '2023-01-03')
    frame = pd.DataFrame({'date': dates, 'symbol': 'A',
                          'entry_date': pd.Series(dates).shift(-1),
                          'exit_date': pd.Series(dates).shift(-6),
                          'all14': True, 'all64': True, 'all78': True,
                          'label_endpoints_available': True, 'structural_eligible': True})
    # Use only the minimum candidate here; the expanded one requires 30 months.
    splitter = audit.ExpandingWalkForwardSplitter(initial_train_months=12)
    fold = next(splitter.split(frame, calendar=dates))
    assert fold.audit.train_max_exit_date < fold.audit.validation_first_date
    assert fold.audit.validation_max_exit_date < fold.audit.test_first_date
    assert fold.audit.train_overlap_rows_removed == 1
    assert fold.audit.validation_overlap_rows_removed == 1
