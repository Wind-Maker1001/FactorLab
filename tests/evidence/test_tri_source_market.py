"""Targeted audit failures; fixtures contain no supplier observations."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import zipfile

import numpy as np
import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'tools/data/tri_source_market_audit.py'
spec = importlib.util.spec_from_file_location('tri_source_market_audit', SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_native_empty_strings_count_as_missing(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    columns = ['date','code','symbol','sheet','row',*audit.FIELDS,'filling','trade_code','name']
    record = {k:'' for k in columns}
    record.update(date='2020-01-02',code='123456',symbol='123456.SH',sheet='sheet1',row='4')
    pq.write_table(pa.Table.from_pylist([record]),tmp_path/'CSMAR-fixture.parquet')
    (tmp_path/'CSMAR-fixture.json').write_text(json.dumps({'archive':'fixture.zip','member':'prices.xlsx','sha256':'fixture'}),encoding='utf-8')
    result = audit.native_frame(tmp_path,'CSMAR','2020-01-01','2020-01-31',{})
    assert result[audit.FIELDS[:4]].isna().all(axis=1).tolist() == [True]


def test_native_fill_prices_do_not_imply_positive_volume(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    columns = ['date','code','symbol','sheet','row',*audit.FIELDS,'filling','trade_code','name']
    record = {k:'' for k in columns}
    record.update(date='2020-01-02',code='123456',symbol='123456.SH',sheet='prices',row='4',
                  open='12.34',high='12.34',low='12.34',close='12.34',filling='1')
    pq.write_table(pa.Table.from_pylist([record]),tmp_path/'CNRDS-fixture.parquet')
    (tmp_path/'CNRDS-fixture.json').write_text(json.dumps({'archive':'fixture.zip','member':'prices.xlsx','sha256':'fixture'}),encoding='utf-8')
    result = audit.native_frame(tmp_path,'CNRDS','2020-01-01','2020-01-31',{})
    assert result[audit.FIELDS[:4]].iloc[0].tolist() == ['12.34'] * 4
    assert pd.isna(result.volume.iloc[0])
    assert not pd.to_numeric(result.volume).gt(0).any()


def test_missing_date_rejected():
    frame = pd.DataFrame({'date': [None], 'symbol': ['123456.SH']}).set_index(['date', 'symbol'])
    with pytest.raises(ValueError, match='missing date'):
        audit.validate_keys(frame, 'fixture')


def test_duplicate_keys_rejected_even_identical():
    frame = pd.DataFrame({'date': ['2020-01-02'] * 2, 'symbol': ['123456.SH'] * 2}).set_index(['date', 'symbol'])
    with pytest.raises(ValueError, match='duplicate'):
        audit.validate_keys(frame, 'fixture')


def test_missing_source_is_not_missing_price():
    assert audit.classify('CSMAR', 'CNRDS', 'close', '1', None, present_b=False) == 'SOURCE_MISSING'
    assert audit.classify('CSMAR', 'CNRDS', 'close', '1', None) == 'INSUFFICIENT_EVIDENCE'
    assert audit.classify('CSMAR', 'CNRDS', 'close', None, None) == 'INSUFFICIENT_EVIDENCE'


def test_units_not_inferred_from_equal_values():
    assert audit.classify('CSMAR', 'BigQuant', 'close', '1.00', '1.00') == 'SEMANTICALLY_INCOMPARABLE'
    assert audit.classify('CSMAR', 'CNRDS', 'pre_close', '1.00', '1.00') == 'SEMANTICALLY_INCOMPARABLE'
    assert audit.classify('CSMAR', 'CNRDS', 'adj_close', '1.00', '1.00') == 'SEMANTICALLY_INCOMPARABLE'


def test_native_decimal_and_one_ulp_are_not_rounded():
    a = 100000000.0
    b = np.nextafter(a, np.inf)
    assert audit.classify('CSMAR', 'CNRDS', 'amount', str(a), str(b)) == 'MISMATCH'
    assert audit.classify('CSMAR', 'CNRDS', 'amount', '1.00000000000000001', '1.00000000000000002') == 'MISMATCH'
    assert audit.classify('CSMAR', 'CNRDS', 'amount', '1E+2', '100.00') == 'MATCHED'


def test_fill_cannot_masquerade_as_execution():
    assert audit.filled_execution('1', '100')
    assert audit.filled_execution('2', '100')
    assert not audit.filled_execution('1', '0')
    assert not audit.filled_execution('0', '100')


def test_month_partition_preserves_nullable_string_schema(tmp_path):
    from argparse import Namespace
    import pyarrow as pa
    import pyarrow.parquet as pq
    schema = pa.schema([('date',pa.string()),('limit_up',pa.string())])
    table = pa.Table.from_pylist([{'date':'2020-01-02','limit_up':None}, {'date':'2020-01-03','limit_up':'11.1'}],schema=schema)
    pq.write_table(table,tmp_path/'CSMAR-fixture.parquet',row_group_size=1)
    (tmp_path/'CSMAR-fixture.json').write_text(json.dumps({'source':'CSMAR','sha256':'fixture','selected_rows':2}),encoding='utf-8')
    audit.partition_native(Namespace(private_root=tmp_path))
    replay = pq.read_table(tmp_path/'monthly/CSMAR-fixture/2020-01.parquet')
    assert replay.schema.field('limit_up').type == pa.string()
    assert replay.to_pylist() == table.to_pylist()


def test_native_string_table_and_physical_coordinates():
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('xl/workbook.xml', '<workbook xmlns="'+ns+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="prices" sheetId="1" r:id="r1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/sharedStrings.xml', '<sst xmlns="'+ns+'"><si><t>123456</t></si><si><t>1.00000000000000001</t></si></sst>')
        archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="'+ns+'"><sheetData><row r="7"><c r="A7" t="s"><v>0</v></c><c r="B7" t="s"><v>1</v></c></row></sheetData></worksheet>')
    decoded = list(audit.workbook_rows(buffer.getvalue()))
    assert decoded == [('prices', 7, ['123456', '1.00000000000000001'])]


def test_source_mismatch_rejected(tmp_path):
    path = tmp_path / 'fixture.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('stock/cn_stock_real_bar1d/schema.json', json.dumps({'datasource_id': 'cn_stock_bar1d'}))
    reader = audit.BQReader(path, 'stock', [])
    with pytest.raises(ValueError, match='source/table mismatch'):
        reader.read('cn_stock_real_bar1d', '2020-01-01', '2020-01-31')


def test_bigquant_receipt_mismatch_rejected(tmp_path):
    path = tmp_path / 'fixture.zip'
    prefix = 'stock/cn_stock_real_bar1d/'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(prefix+'schema.json', json.dumps({'datasource_id': 'cn_stock_real_bar1d'}))
        archive.writestr(prefix+'2020.parquet', b'not parquet')
        archive.writestr(prefix+'2020.parquet.receipt.json', json.dumps({'sha256': '0'*64, 'window_start': '2020-01-01', 'window_end': '2020-12-31'}))
    reader = audit.BQReader(path, 'stock', [])
    with pytest.raises(ValueError, match='receipt identity mismatch'):
        reader.read('cn_stock_real_bar1d', '2020-01-01', '2020-01-31')
