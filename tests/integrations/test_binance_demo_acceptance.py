"""Read-only M17 evidence must not imply execution admission or actual fills."""

import json

import pytest

from factorlab.integrations.binance_futures_trading import demo_acceptance as acceptance
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment, SafetyBlocked


@pytest.fixture
def bound(monkeypatch):
    credential = Credentials('synthetic-key', 'synthetic-secret', Environment.DEMO)
    monkeypatch.setattr(acceptance, 'load_bound_credential',
                        lambda _: ({'account_id': 'demo-account'}, credential))
    return credential


@pytest.mark.parametrize('account,verified', [('demo-account', True), ('UNKNOWN', False)])
def test_signed_read_is_not_trade_or_write_authorization(monkeypatch, bound, account, verified):
    calls = []

    def inspect(credential):
        calls.append(credential)
        return dict(checks={'account_id': dict(value=account, eligible=True if verified else None)},
                    endpoints=[dict(method='GET', path='/fapi/v3/account')], network_writes=0)

    monkeypatch.setattr(acceptance, 'inspect_demo', inspect)
    report = acceptance.preflight(object())
    assert calls == [bound]
    assert report['credential_binding_verified'] is verified
    assert ('DEMO_ENVIRONMENT_VERIFIED' in report['statuses']) is verified
    assert report['execution_admission'] == 'BLOCKED'
    assert report['statuses'][-2:] == ['BLOCKED_AUTHORIZATION', 'REAL_DEMO_FILL_NOT_VERIFIED']
    assert 'DEMO_ORDER_ACK_VERIFIED' not in report['statuses']
    assert 'DEMO_TRADE_RECEIPT_VERIFIED' not in report['statuses']
    assert bound.key not in json.dumps(report, default=str)
    assert bound.secret not in json.dumps(report, default=str)


def test_signed_account_mismatch_is_rejected(monkeypatch, bound):
    monkeypatch.setattr(acceptance, 'inspect_demo', lambda _: {
        'checks': {'account_id': {'eligible': True, 'value': 'another-account'}}})
    with pytest.raises(SafetyBlocked, match='account binding mismatch'):
        acceptance.preflight(object())


def test_production_credential_never_reaches_network(monkeypatch):
    credential = Credentials('synthetic-key', 'synthetic-secret', Environment.PRODUCTION)
    monkeypatch.setattr(acceptance, 'load_bound_credential', lambda _: ({}, credential))
    monkeypatch.setattr(acceptance, 'inspect_demo', lambda _: pytest.fail('network invoked'))
    with pytest.raises(SafetyBlocked, match='Demo credentials required'):
        acceptance.preflight(object())


def test_missing_binding_records_block_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, 'DemoCredentialStore', object)

    def missing(_):
        raise SafetyBlocked('Demo account binding missing; run setup')

    monkeypatch.setattr(acceptance, 'load_bound_credential', missing)
    monkeypatch.setattr(acceptance, 'inspect_demo', lambda _: pytest.fail('network invoked'))
    target = tmp_path / 'preflight.json'
    acceptance.main(['--output', str(target)])
    report = json.loads(target.read_text())
    assert report['statuses'] == ['BLOCKED_AUTHORIZATION', 'REAL_DEMO_FILL_NOT_VERIFIED']
    assert report['network_writes'] == 0


def test_existing_evidence_not_overwritten_or_queried(tmp_path, monkeypatch):
    target = tmp_path / 'preflight.json'
    target.write_text('retained evidence')
    monkeypatch.setattr(acceptance, 'DemoCredentialStore', lambda: pytest.fail('credentials loaded'))
    with pytest.raises(FileExistsError):
        acceptance.main(['--output', str(target)])
    assert target.read_text() == 'retained evidence'
