"""Deterministic missing-key, null-field and physical partition fixtures."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location('p3d02', Path(__file__).parents[2] / 'tools/data/audit_p3_d02.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_identity_candidates_do_not_match_implicitly():
    members = pd.DataFrame({'date': ['2025-01-02'], 'instrument': ['300114.SZ']})
    bars = pd.DataFrame({'date': ['2025-01-02'], 'instrument': ['302132.SZ'], **{f: [1.] for f in audit.FIELDS}})
    row = audit.match(members, bars).iloc[0]
    assert audit.classify(row) == ('MISSING_BAR_KEY', 'UNKNOWN')


def test_present_null_row_not_missing_key_or_proven_suspension():
    members = pd.DataFrame({'date': ['2025-01-02'], 'instrument': ['000001.SZ']})
    bars = members.assign(**{f: float('nan') for f in audit.FIELDS}, suspended=1)
    joined = audit.match(members, bars)
    assert len(joined) == 1
    assert audit.classify(joined.iloc[0]) == ('PRESENT_BAR_NULL_FIELDS', 'UNKNOWN')
    assert joined[audit.FIELDS].isna().sum().sum() == 6


def test_duplicate_source_keys_are_not_multiplied():
    members = pd.DataFrame({'date': ['2025-01-02'], 'instrument': ['000001.SZ']})
    with pytest.raises(pd.errors.MergeError):
        audit.match(members, pd.concat([members, members]))


def test_lexical_suffix_conversion_only():
    assert audit.normalize(pd.Series(['600068.XSHG', '300114.XSHE', '302132.SZ'])).tolist() == ['600068.SH', '300114.SZ', '302132.SZ']


def test_observed_date_runs_split_at_member_date_gaps():
    spec = importlib.util.spec_from_file_location('p3d02_sensitivity', Path(__file__).parents[2] / 'tools/data/p3_d02_sensitivity.py')
    sensitivity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sensitivity)
    frame = pd.DataFrame({'instrument': ['A', 'A', 'A'], 'date': ['2025-01-03', '2025-01-06', '2025-01-08']})
    result = sensitivity.runs(frame, ['2025-01-03', '2025-01-06', '2025-01-07', '2025-01-08'])
    assert [r['member_dates'] for r in result] == [2, 1]
    assert result[0]['end'] == '2025-01-06'


def test_partition_row_coordinates_survive_filtering():
    import io
    import pyarrow as pa
    import pyarrow.parquet as pq
    buffer = io.BytesIO()
    pq.write_table(pa.table({'instrument': ['OTHER', 'TARGET'], 'open': [2., None]}), buffer, row_group_size=1)
    class Archive:
        def read(self, _):
            return buffer.getvalue()
    result = audit.read_part(Archive(), 'stock/test/part.parquet')
    selected = result.loc[result.instrument.eq('TARGET')].iloc[0]
    assert selected.source_row_zero_based == 1
    assert selected.source_member == 'stock/test/part.parquet'
    assert selected.source_receipt == 'stock/test/part.parquet.receipt.json'


def test_native_records_absence_and_conflict_remain_distinct():
    spec = importlib.util.spec_from_file_location('p3d02_finalize', Path(__file__).parents[2] / 'tools/data/finalize_p3_d02.py')
    finalizer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(finalizer)
    a = {'values': {'Stkcd': '300114', 'Filling': '0'}}
    b = {'values': {'Stkcd': '300114', 'Filling': '1'}}
    assert finalizer.csmar_decision([]) == 'NOT_AVAILABLE'
    assert finalizer.csmar_decision([a, a]) == 'CONFIRMED'
    assert finalizer.csmar_decision([a, b]) == 'CONFLICT'


def test_snapshot_and_overlapping_partitions():
    class Archive:
        def namelist(self):
            return ['stock/info/unpartitioned_snapshot.parquet', 'stock/info/2020-01-01_2020-12-31.parquet', 'stock/info/2021-01-01_2021-12-31.parquet']
    assert list(audit.partitions(Archive(), 'info', pd.Timestamp('2021-09-13'), pd.Timestamp('2021-09-30'))) == ['stock/info/unpartitioned_snapshot.parquet', 'stock/info/2021-01-01_2021-12-31.parquet']
