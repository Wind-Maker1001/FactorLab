"""Synthetic tests for conservative duplicate handling."""
import importlib.util
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'tools/data/cnrds_duplicate_audit.py'
spec = importlib.util.spec_from_file_location('cnrds_duplicate_audit', SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.mark.parametrize('left,right,expected,fields', [
    ({'Scode': '123456', 'Dclsprc': '1'}, {'Scode': '123456', 'Dclsprc': '1'}, 'EXACT_DUPLICATE', []),
    ({'Dclsprc': '1', 'Adclsprc': '2'}, {'Dclsprc': '1', 'Adclsprc': ''}, 'CONFLICTING_VALUES', ['Adclsprc']),
    ({'Dclsprc': '1'}, {'Dclsprc': '2'}, 'CONFLICTING_VALUES', ['Dclsprc']),
    ({'Dclsprc': '1.00'}, {'Dclsprc': '1'}, 'CONFLICTING_VALUES', ['Dclsprc']),
    ({'Coname': 'A'}, {'Coname': 'B'}, 'IDENTITY_AMBIGUITY', ['Coname']),
    ({'Adclsprc': ''}, {}, 'CONFLICTING_VALUES', ['Adclsprc']),
])
def test_full_native_content_controls_classification(left, right, expected, fields):
    assert audit.classify([left, right]) == (expected, fields)
    assert audit.classify([right, left]) == (expected, fields)


def test_third_candidate_cannot_be_dropped():
    assert audit.classify([{'Dclsprc': '1'}, {'Dclsprc': '1'}, {'Dclsprc': '2'}]) == ('CONFLICTING_VALUES', ['Dclsprc'])


def test_numeric_conflict_separated_from_missing_and_formatting(tmp_path):
    rows = [
        dict(classification='CONFLICTING_VALUES', differing_fields='Adclsprc', candidates=[{'Scode': '123456', 'Adclsprc': '2'}, {'Scode': '123456', 'Adclsprc': ''}]),
        dict(classification='CONFLICTING_VALUES', differing_fields='Dclsprc', candidates=[{'Scode': '123456', 'Dclsprc': '1.00'}, {'Scode': '123456', 'Dclsprc': '1'}]),
        dict(classification='IDENTITY_AMBIGUITY', differing_fields='Coname;Dclsprc', candidates=[{'Scode': '123456', 'Coname': 'A', 'Dclsprc': '1'}, {'Scode': '123456', 'Coname': 'B', 'Dclsprc': '2'}]),
    ]
    for row in rows:
        row['locators'] = [{'member': 'fixture.xlsx', 'sheet': 'prices', 'row': str(n)} for n in [3, 4]]
    (tmp_path / 'candidate-lineage.jsonl').write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
    (tmp_path / 'impact-summary.json').write_text(json.dumps({'duplicate_keys': 3}), encoding='utf-8')
    (tmp_path / 'input-manifest.json').write_text('{}', encoding='utf-8')
    audit.summarize(SimpleNamespace(output=tmp_path, private_output=tmp_path, checkpoints=tmp_path))
    result = json.loads((tmp_path / 'impact-summary.json').read_text(encoding='utf-8'))
    assert result['keys_with_actual_nonempty_numeric_disagreement'] == 1
    assert result['keys_with_numeric_disagreement_and_identity_ambiguity'] == 1
    assert result['keys_with_only_adjustment_field_differences'] == 1
    assert result['keys_with_same_supplier_code'] == 3
