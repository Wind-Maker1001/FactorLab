"""Evidence follow-up experiments must not certify supplier contracts."""
import hashlib
import importlib.util
import json
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('p4d02', ROOT / 'tools/evidence/audit_p4_d02.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def sample():
    rows, native = [], []
    for date in audit.DATES:
        cs, bq = 123456789.0, math.nextafter(123456789.0, math.inf)
        rows.append({'date': date, 'csmar': f'np.float64({cs!r})',
                     'bigquant': f'np.float64({bq!r})',
                     'bigquant_minus_csmar_exact': str(audit.Decimal.from_float(bq) - audit.Decimal.from_float(cs))})
        native.append({'date': date, 'native_Dnvaltrd': '123456789'})
    return {'source_versions': dict(audit.VERSIONS), 'differences': rows, 'native_amounts': native}


def test_rounding_agreement_does_not_swallow_exact_difference_or_certify_unit():
    result = audit.analyze(json.dumps(sample()).encode())
    assert all(not row['exact_equal'] and row['adjacent_binary64'] for row in result['observations'])
    assert all(row['candidate_decimal_places_equal']['2'] for row in result['observations'])
    assert result['exact_cross_source_status'] == 'FAIL'
    assert result['economic_unit_bigquant'] == result['historical_known_at'] == 'UNKNOWN'
    assert result['supplier_algorithm'] == 'UNRESOLVED'
    assert not result['candidate_rule_certified']


@pytest.mark.parametrize('mutation', ['version', 'date', 'delta', 'expression'])
def test_invalid_private_sample_is_rejected(mutation):
    data = sample()
    if mutation == 'version':
        data['source_versions']['bigquant'] = 'other'
    elif mutation == 'date':
        data['differences'].pop()
    elif mutation == 'delta':
        data['differences'][0]['bigquant_minus_csmar_exact'] = '0'
    else:
        data['differences'][0]['csmar'] = '__import__("os")'
    with pytest.raises(ValueError):
        audit.analyze(json.dumps(data).encode())


def test_published_experiment_contains_no_absolute_commercial_values():
    result = json.loads((ROOT / 'docs/validation/p4-d02/AMOUNT_EXPERIMENT.json').read_bytes())
    assert len(result['observations']) == 4
    for row in result['observations']:
        assert 'csmar' not in row and 'bigquant' not in row and 'native_Dnvaltrd' not in row
        assert row['native_to_prepared_equal'] and row['native_decimal_exactly_representable_binary64']
        assert abs(row['signed_ulps']) == 1 and not row['exact_equal']


def test_followup_receipt_binds_baseline_and_artifacts():
    directory = ROOT / 'docs/validation/p4-d02'
    receipt = json.loads((directory / 'receipt.json').read_bytes())
    for name, digest in receipt['artifacts'].items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
    for name, digest in receipt['baseline_artifacts'].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
    assert receipt['matrix_status_changes'] == []
    assert receipt['boundary'] == 'NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED'
