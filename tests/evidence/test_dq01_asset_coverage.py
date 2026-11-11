"""Coverage semantics against tiny, independently generated containers."""
import importlib.util
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

MODULE = Path(__file__).resolve().parents[2] / 'tools/data/dq01_asset_coverage.py'
spec = importlib.util.spec_from_file_location('dq01', MODULE)
dq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dq)


def test_zip_disguised_as_7z_is_enumerated_without_reading_data(tmp_path):
    path = tmp_path / '财务.7z'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('利润表-2020_1.xlsx', b'not-opened-as-workbook')
        archive.writestr('字段说明.txt', 'value[数值] 单位：元'.encode())
    result = dq.inspect('CNRDS', path.name, path)
    assert result['file']['actual_format'] == 'ZIP'
    assert result['file']['extension_matches'] is False
    assert result['file']['status'] == 'ACCESSIBLE'
    data, dictionary = result['members']
    assert data['inspection'] == 'DIRECTORY_VERIFIED'
    assert data['content_evidence'] == 'NOT_RUN'
    assert dictionary['inspection'] == 'PAYLOAD_VERIFIED'
    assert dictionary['payload_sha256']


def test_truncated_zip_is_corrupted_not_merely_present(tmp_path):
    path = tmp_path / 'broken.zip'
    path.write_bytes(b'PK\x03\x04' + b'\0' * 30)
    result = dq.inspect('CSMAR', path.name, path)
    assert result['file']['status'] == 'CORRUPTED'
    assert result['file']['sha256']
    assert not result['members']


def test_missing_and_permission_denied_have_distinct_states(tmp_path, monkeypatch):
    path = tmp_path / 'absent.zip'
    assert dq.inspect('CSMAR', path.name, path)['file']['status'] == 'MISSING'
    path.write_bytes(b'PK\x03\x04')
    original = Path.open

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError('fixture')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', denied)
    assert dq.inspect('CSMAR', path.name, path)['file']['status'] == 'UNREADABLE'


def test_pdf_without_text_is_not_claimed_text_verified(tmp_path):
    path = tmp_path / 'blank.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    result = dq.inspect('CNRDS', path.name, path)['file']
    assert result['status'] == 'ACCESSIBLE'
    assert result['pages'] == 1
    assert result['text_status'] == 'NOT_RUN_NO_TEXT_LAYER'


def test_encrypted_pdf_is_explicit(tmp_path):
    path = tmp_path / 'secret.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt('secret')
    writer.write(path)
    assert dq.inspect('CNRDS', path.name, path)['file']['status'] == 'ENCRYPTED'


def test_reconcile_includes_missing_baseline_and_new_assets(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'new.zip').write_bytes(b'')
    result = dq.reconcile({'CSMAR': root}, {('CSMAR', 'missing.zip')})
    assert set(result) == {('CSMAR', 'missing.zip'), ('CSMAR', 'new.zip')}


def part(member):
    return {'source': 'CNRDS', 'asset': 'CNSP.zip', 'member': member, 'format': '.xlsx', 'date_hint': '2020'}


def test_partial_member_history_does_not_certify_whole_table():
    first, second = part('2020/个股日回报率-2020_1.xlsx'), part('2020/个股日回报率-2020_2.xlsx')
    inherited = {('CNRDS', 'CNSP.zip', first['member']): {'full_rows': True, 'reference': 'PR85'}}
    tables = dq.aggregate_tables([first, second], {}, {}, inherited)
    assert len(tables) == 1
    assert tables[0]['partitions'] == 2
    assert tables[0]['remaining_content_partitions'] == 1
    assert tables[0]['full_table_row_scan'] == 'NOT_RUN'


def test_schema_only_table_has_no_fake_partition_or_row_scan():
    tables = dq.aggregate_tables([], {('BigQuant', 'stock.zip', 'stock/empty/schema.json'): {'fields': []}}, {}, {})
    assert tables[0]['logical_table'] == 'stock/empty'
    assert tables[0]['partitions'] == 0
    assert tables[0]['full_table_row_scan'] == 'NOT_RUN'
    assert tables[0]['evidence'] == 'METADATA_ONLY'


def test_corrupt_metadata_payload_does_not_get_verified(tmp_path):
    path = tmp_path / 'corrupt.zip'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('field.txt', b'abc')
    raw = bytearray(path.read_bytes())
    raw[raw.index(b'abc')] ^= 1
    path.write_bytes(raw)
    result = dq.inspect('CNRDS', path.name, path)
    assert result['file']['status'] == 'CORRUPTED'
    assert result['members'][0]['content_evidence'] == 'FAILED'


def test_actual_7z_directory_only(tmp_path):
    py7zr = pytest.importorskip('py7zr')
    path = tmp_path / 'actual.7z'
    with py7zr.SevenZipFile(path, 'w') as archive:
        archive.writestr(b'payload', 'table.csv')
    result = dq.inspect('CNRDS', path.name, path)
    assert result['file']['status'] == 'ACCESSIBLE'
    assert result['file']['actual_format'] == '7Z'
    assert result['members'][0]['inspection'] == 'DIRECTORY_VERIFIED'
    assert result['members'][0]['content_evidence'] == 'NOT_RUN'


@pytest.mark.parametrize('member', ['基金重仓流通股_2020-1.xlsx', '基金重仓流通股_2020-2.xlsx', '基金重仓流通股-2019_1.xlsx'])
def test_mixed_year_partition_separators_do_not_inflate_table_count(member):
    assert dq.table_name('CNRDS', 'IORD.zip', member) == '基金重仓流通股'
