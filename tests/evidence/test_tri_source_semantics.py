"""Targeted semantic regression tests; synthetic assertions grant no qualification."""
from __future__ import annotations

from io import BytesIO
import importlib.util
from pathlib import Path
import subprocess

import openpyxl
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('tri_source_semantics_audit',
    ROOT / 'tools/data/tri_source_semantics_audit.py')
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


@pytest.mark.parametrize('missing', [None, '', float('nan'), pd.NA])
def test_missing_entitlement_is_not_zero(missing):
    assert audit.compare_cash(missing, 0) == 'INSUFFICIENT_EVIDENCE'


def test_binary64_encoding_is_distinct_from_financial_rounding():
    assert audit.compare_cash('0.06074', 0.06074000000000001) == 'MATCHED_TERMS'
    assert audit.compare_cash('0.06074', '0.06') == 'SEMANTIC_CONFLICT'
    assert audit.compare_cash('0', '0') == 'MATCHED_TERMS'


def test_proposal_and_implementation_dates_are_not_forced_equal():
    assert audit.compare_announcement('2018-03-23', '2018-06-13', False) == 'INSUFFICIENT_EVIDENCE'
    assert audit.compare_announcement('2018-03-23', '2018-06-13', True) == 'SEMANTIC_CONFLICT'
    assert audit.compare_announcement(None, None, True) == 'INSUFFICIENT_EVIDENCE'


def test_archive_reader_selects_target_without_executing_members(monkeypatch):
    workbook = openpyxl.Workbook()
    workbook.active.append(['SCode', 'ImProgress', 'DRDt'])
    workbook.active.append(['股票代码', '方案实施进度', '除权除息日'])
    workbook.active.append(['601857', '停止实施', '2018-06-21'])
    workbook.active.append(['000001', '实施完毕', '2018-06-21'])
    buffer = BytesIO()
    workbook.save(buffer)
    calls = []

    def read_member(args):
        calls.append(args)
        if '-tf' in args:
            return '分红实施内容.xlsx\r\nmalicious.py\r\n'.encode('gb18030')
        return buffer.getvalue()

    monkeypatch.setattr(subprocess, 'check_output', read_member)
    selected, info = audit.archive_workbook(Path('input.7z'), Path('tar.exe'))
    assert len(selected) == 1
    assert selected[0]['ImProgress'] == '停止实施'
    assert selected[0]['_physical_row'] == 3
    assert info['physical_data_rows_streamed'] == 2
    assert calls[1][-1] == '分红实施内容.xlsx'
    assert '-xOf' in calls[1]


def test_output_refuses_overwrite(tmp_path):
    path = tmp_path / 'events.csv'
    path.write_text('preserved source', encoding='utf-8')
    with pytest.raises(FileExistsError):
        audit.write_csv(path, [dict(comparison_status='EVENT_NOT_OBSERVED')])
    assert path.read_text(encoding='utf-8') == 'preserved source'


def test_unknown_status_is_not_silently_promoted(tmp_path):
    for name in ('corporate-actions.csv', 'adjustment-contracts.csv',
                 'identity-membership.csv', 'pit-evidence.csv', 'financial-semantics.csv'):
        audit.write_csv(tmp_path / name, [dict(source='synthetic', subject='vintage',
            comparison_status='PASS')])
    with pytest.raises(ValueError, match='Unknown comparison status'):
        audit.summarize(tmp_path, 'test-baseline', {}, {})
    assert not (tmp_path / 'summary.json').exists()
