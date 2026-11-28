import importlib.util
from pathlib import Path
import sqlite3


spec = importlib.util.spec_from_file_location('p10_12_online_acceptance',
    Path(__file__).resolve().parents[2] / 'tools' / 'p10_12_online_acceptance.py')
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


def test_database_summary_reads_live_wal_without_exporting_account_or_receipts(tmp_path):
    path = tmp_path / 'portfolio.sqlite3'
    db = sqlite3.connect(path)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE capital_events(account TEXT, event TEXT)')
        db.execute("INSERT INTO capital_events VALUES ('private-account', 'receipt')")
        db.commit()
        assert acceptance.database_summary(path) == {'capital_events': 1}
        assert db.execute('SELECT * FROM capital_events').fetchall() == [('private-account', 'receipt')]
    finally:
        db.close()


def test_database_summary_does_not_create_missing_journal(tmp_path):
    import pytest
    path = tmp_path / 'missing.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        acceptance.database_summary(path)
    assert not path.exists()
