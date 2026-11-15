"""Offline engineering receipts are not forward financial samples."""
from copy import deepcopy
import json

import pytest

from factorlab.experiments.grid_forward import engineering_protocol, freeze, evaluate

FROZEN = '2026-11-15T00:00:00+00:00'
NOW = '2026-11-16T00:00:00+00:00'


def evidence():
    return dict(kind='FORWARD_PAPER', observations=[dict(
        opened_at=FROZEN, closed_at='2026-11-15T01:00:00+00:00',
        known_at='2026-11-15T01:00:01+00:00',
        decision_at='2026-11-15T01:00:02+00:00',
        receipt='SYNTHETIC_ENGINEERING_ONLY', completed_bar=True)])


def setup(tmp_path):
    protocol = engineering_protocol('offline-v1', FROZEN)
    record = freeze(tmp_path, protocol)
    return record['id']


def test_immutable_and_versioned(tmp_path):
    identity = setup(tmp_path)
    with pytest.raises(FileExistsError):
        setup(tmp_path)
    other = freeze(tmp_path, engineering_protocol('offline-v2', FROZEN))
    assert other['id'] != identity


def test_tampering_rejected(tmp_path):
    identity = setup(tmp_path)
    path = tmp_path / 'experiments' / (identity + '.json')
    record = json.loads(path.read_text())
    record['payload']['parameters']['version'] = 'altered'
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='checksum'):
        evaluate(tmp_path, identity, evidence(), evaluated_at=NOW)


@pytest.mark.parametrize('field,value', [
    ('opened_at', '2026-08-12T00:00:00+00:00'),
    ('closed_at', '2026-11-17T01:00:00+00:00'),
    ('known_at', '2026-11-15T00:59:00+00:00'),
    ('decision_at', '2026-11-15T01:00:00+00:00'),
    ('completed_bar', False), ('receipt', ''),
])
def test_inadmissible_information(tmp_path, field, value):
    identity = setup(tmp_path)
    sample = evidence()
    sample['observations'][0][field] = value
    with pytest.raises(ValueError):
        evaluate(tmp_path, identity, sample, evaluated_at=NOW)


def test_historical_shadow_and_missing_bars_rejected(tmp_path):
    identity = setup(tmp_path)
    for kind in ('HISTORICAL_REFERENCE', 'SHADOW_TOUCH', 'DEMO_CONFIRMED'):
        sample = evidence()
        sample['kind'] = kind
        with pytest.raises(ValueError):
            evaluate(tmp_path, identity, sample, evaluated_at=NOW)
    sample = evidence()
    sample['observations'].append(deepcopy(sample['observations'][0]))
    with pytest.raises(ValueError, match='unordered'):
        evaluate(tmp_path, identity, sample, evaluated_at=NOW)


def test_reproducible_blocked_result(tmp_path):
    identity = setup(tmp_path)
    for sample in (evidence(), dict(kind='FORWARD_PAPER', observations=[])):
        first = evaluate(tmp_path, identity, sample, evaluated_at=NOW)
        assert first == evaluate(tmp_path, identity, sample, evaluated_at=NOW)
        assert first['status'] == 'BLOCKED'
        assert first['sample_status'] == ('NOT_ASSESSED' if sample['observations']
                                          else 'INSUFFICIENT_FORWARD_SAMPLE')
        assert first['financial_conclusion'] == 'NOT_ESTABLISHED'
        assert first['metrics'] is None
        assert first['controls'] == ['grid_original', 'fixed_hold', 'lagged_exposure', 'cash']


def test_shared_comparison_contract_is_frozen(tmp_path):
    protocol = engineering_protocol('offline-v1', FROZEN)
    contract = protocol['comparison_contract']
    assert 'all accounts' in contract['costs']
    assert 'identical UTC half-open' in contract['window']
    protocol['comparison_contract']['costs'] = 'choose cost per account after seeing results'
    with pytest.raises(ValueError, match='unsupported'):
        freeze(tmp_path, protocol)


def test_controls_cannot_mutate_canonical_protocol(tmp_path):
    protocol = engineering_protocol('offline-v1', FROZEN)
    protocol['controls'].append('future_best_control')
    with pytest.raises(ValueError, match='unsupported'):
        freeze(tmp_path, protocol)


def test_naive_timestamps_and_future_freeze_rejected(tmp_path):
    with pytest.raises(ValueError, match='UTC'):
        engineering_protocol('offline-v1', '2026-11-15T00:00:00')
    identity = setup(tmp_path)
    with pytest.raises(ValueError, match='freeze lies'):
        evaluate(tmp_path, identity, evidence(), evaluated_at='2026-11-14T00:00:00+00:00')


def test_cannot_silently_activate_financial_protocol(tmp_path):
    protocol = engineering_protocol('offline-v1', FROZEN)
    protocol['financial_status'] = 'ACTIVE'
    with pytest.raises(ValueError, match='cannot activate'):
        freeze(tmp_path, protocol)
