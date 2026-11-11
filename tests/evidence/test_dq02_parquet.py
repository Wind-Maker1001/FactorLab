import importlib.util
import json
import zlib
from pathlib import Path
import sqlite3
import zipfile
import datetime as dt

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

MODULE = Path(__file__).resolve().parents[2] / 'tools/data/dq02_parquet.py'
spec = importlib.util.spec_from_file_location('dq02_parquet', MODULE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_full_shard_stats_and_exact_private_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, 'BATCH_SIZE', 2)
    table = pa.table({'date': pa.array([dt.datetime(2024, 1, 1), dt.datetime(2024, 2, 1), None]),
                      'instrument': ['secret-a', 'secret-b', 'secret-c'],
                      'amount': [-2.0, float('inf'), float('nan')],
                      'volume': [0, 3, None], 'type': ['private-enum', 'private-enum', None]})
    content = tmp_path / 'fixture.parquet'
    pq.write_table(table, content)
    archive = tmp_path / 'stock.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.write(content, 'stock/cn_stock_bar1d/a.parquet')
        z.writestr('stock/cn_stock_bar1d/schema.json', json.dumps({'fields': [{'name': f.name, 'type': str(f.type)} for f in table.schema], 'metadata_source': 'zero_row_query'}))
    jobs = audit.discover(tmp_path)
    assert len(jobs) == 1
    receipt = audit.scan(jobs[0], tmp_path / 'private')
    assert receipt['rows'] == 3
    assert receipt['fields']['amount']['nonfinite'] == 2
    assert receipt['fields']['amount']['negative'] == 1
    assert receipt['fields']['volume']['nulls'] == 1
    assert receipt['fields']['volume']['positive'] == 1
    assert receipt['date_stats']['date'] == {'2024-01': 1, '2024-02': 1}
    assert receipt['key_coverage']['null_key_rows'] == 1
    assert receipt['key_coverage']['fields'] == ['date', 'instrument']
    assert 'secret-a' not in json.dumps(receipt)
    assert 'private-enum' not in json.dumps(receipt)
    with sqlite3.connect(receipt['sqlite_path']) as db:
        rows = db.execute('SELECT key,payload,partition FROM rows').fetchall()
        assert len(rows) == 3
        assert json.loads(zlib.decompress(rows[0][1]))['instrument'] == 'secret-a'
        assert json.loads(zlib.decompress(rows[1][1]))['amount'] == {'$float': '+inf'}
        assert rows[2][0] is None
        assert db.execute('SELECT count FROM enums WHERE value=?', ('"private-enum"',)).fetchone() == (2,)
    with pytest.raises(FileExistsError):
        audit.scan(jobs[0], tmp_path / 'private')


def test_business_grain_and_unknown_events():
    assert audit.candidate_key('index/cn_stock_index_component', ['date', 'instrument', 'member_code']) == ['date', 'instrument', 'member_code']
    assert audit.candidate_key('stock/cn_stock_dividend', ['date', 'instrument']) == []
    assert audit.candidate_key('unknown', ['date', 'instrument']) == []
    assert audit.candidate_key('common/holidays', ['date', 'market_code']) == ['date', 'market_code']
