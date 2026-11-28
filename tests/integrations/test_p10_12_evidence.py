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


def test_typed_capital_evidence_decodes_and_redacts_nested_account_identity(tmp_path):
    from contextlib import closing
    from decimal import Decimal
    import json
    from factorlab.trading.portfolio_execution import PortfolioStore
    from factorlab.trading.storage import dumps
    tool = importlib.util.spec_from_file_location('p10_12_capital_evidence',
        Path(__file__).resolve().parents[2] / 'tools/p10_12_capital_evidence.py')
    module = importlib.util.module_from_spec(tool)
    tool.loader.exec_module(module)
    (tmp_path / 'health.json').write_text(json.dumps({'healthy': True,
        'strategy': {'account_id': 'private-account', 'state': 'RUNNING'}}))
    with closing(PortfolioStore(tmp_path / 'portfolio.sqlite3')) as store:
        store.db.execute('INSERT INTO capital_commands VALUES (?,?,?,?)',
            ('private-account', 'withdraw', dumps({'action': 'snapshot'}),
             dumps({'status': 'COMPLETE', 'data': {'account_id': 'private-account',
                                                  'equity': Decimal('49')}})))
    result = module.capture(tmp_path)
    assert result['capital_commands'][0]['result']['data'] == {'equity': Decimal('49')}
    assert 'private-account' not in json.dumps(result, default=str)
    assert 'map' not in result['capital_commands'][0]['result']
