"""Protected Linux secrets preserve existing private Demo binding semantics."""
import json
import os
import sys

import pytest

from factorlab.integrations.binance_futures_trading.demo_credentials import DemoCredentialStore, DemoCredentialStoreError
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux protected Secret backend')


@pytest.fixture
def provisioned(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    root.mkdir(mode=0o700)
    secret = tmp_path / 'secret.json'
    secret.write_text(json.dumps(dict(environment='DEMO', key='synthetic-demo-key', secret='synthetic-secret')))
    secret.chmod(0o600)
    monkeypatch.setenv('FACTORLAB_DEMO_SECRET_FILE', str(secret))
    monkeypatch.setenv('FACTORLAB_DEMO_STATE_ROOT', str(root))
    return root, secret, DemoCredentialStore()


def test_secret_and_binding_survive_reopen_and_secret_refresh(provisioned):
    root, secret, store = provisioned
    credentials = store.read()
    assert credentials == Credentials('synthetic-demo-key', 'synthetic-secret', Environment.DEMO)
    store.write(credentials)
    binding = dict(account_id='fixed-digest-bound-account', journal_id='unchanged', configuration={})
    store.write_config(binding)
    assert (root / 'binding.json').stat().st_mode & 0o077 == 0
    (root / 'order-history').write_text('do-not-reset')
    replacement = secret.with_suffix('.replacement')
    replacement.write_text(json.dumps(dict(environment='DEMO', key='synthetic-demo-key', secret='rotated-secret')))
    replacement.chmod(0o600)
    os.replace(replacement, secret)
    reopened = DemoCredentialStore()
    assert reopened.read().secret == 'rotated-secret'
    assert reopened.read_config() == binding
    assert (root / 'order-history').read_text() == 'do-not-reset'
    with pytest.raises(DemoCredentialStoreError):
        reopened.write(credentials)
    with pytest.raises(DemoCredentialStoreError):
        reopened.delete()
    assert reopened.read_config() == binding


@pytest.mark.parametrize('payload', [dict(environment='PRODUCTION', key='key', secret='secret'),
    dict(environment='DEMO', key='', secret='secret'), dict(environment='DEMO', key=3, secret='secret'), []])
def test_invalid_secret_rejected_without_material(provisioned, payload):
    _, secret, store = provisioned
    secret.write_text(json.dumps(payload))
    with pytest.raises(DemoCredentialStoreError) as failure:
        store.read()
    assert 'synthetic' not in str(failure.value)


def test_missing_secret_and_insecure_modes(provisioned):
    root, secret, store = provisioned
    secret.chmod(0o644)
    with pytest.raises(DemoCredentialStoreError):
        store.read()
    secret.chmod(0o600)
    root.chmod(0o755)
    with pytest.raises(DemoCredentialStoreError):
        store.write_config({})
    root.chmod(0o700)
    secret.unlink()
    with pytest.raises(DemoCredentialStoreError):
        store.read()


def test_secret_symlink_refused(provisioned, tmp_path):
    _, secret, store = provisioned
    actual = tmp_path / 'actual'
    secret.rename(actual)
    secret.symlink_to(actual)
    with pytest.raises(DemoCredentialStoreError):
        store.read()


def test_environment_does_not_replace_secret_path(provisioned, monkeypatch):
    _, _, _ = provisioned
    monkeypatch.delenv('FACTORLAB_DEMO_SECRET_FILE')
    monkeypatch.setenv('BINANCE_DEMO_API_KEY', 'not-authority')
    monkeypatch.setenv('BINANCE_DEMO_API_SECRET', 'not-authority')
    with pytest.raises(DemoCredentialStoreError):
        DemoCredentialStore()
