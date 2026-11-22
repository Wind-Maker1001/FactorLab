"""Protected Linux secrets preserve existing private Demo binding semantics."""
import json
import os
import sys

import pytest

from factorlab.integrations.binance_futures_trading.demo_credentials import DemoCredentialStore, DemoCredentialStoreError
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment



if sys.platform == 'linux':
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


if sys.platform == 'linux':
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


if sys.platform == 'linux':
    @pytest.mark.parametrize('payload', [dict(environment='PRODUCTION', key='key', secret='secret'),
        dict(environment='DEMO', key='', secret='secret'), dict(environment='DEMO', key=3, secret='secret'), []])
    def test_invalid_secret_rejected_without_material(provisioned, payload):
        _, secret, store = provisioned
        secret.write_text(json.dumps(payload))
        with pytest.raises(DemoCredentialStoreError) as failure:
            store.read()
        assert 'synthetic' not in str(failure.value)


if sys.platform == 'linux':
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


if sys.platform == 'linux':
    def test_secret_symlink_refused(provisioned, tmp_path):
        _, secret, store = provisioned
        actual = tmp_path / 'actual'
        secret.rename(actual)
        secret.symlink_to(actual)
        with pytest.raises(DemoCredentialStoreError):
            store.read()


if sys.platform == 'linux':
    def test_environment_does_not_replace_secret_path(provisioned, monkeypatch):
        _, _, _ = provisioned
        monkeypatch.delenv('FACTORLAB_DEMO_SECRET_FILE')
        monkeypatch.setenv('BINANCE_DEMO_API_KEY', 'not-authority')
        monkeypatch.setenv('BINANCE_DEMO_API_SECRET', 'not-authority')
        with pytest.raises(DemoCredentialStoreError):
            DemoCredentialStore()


if sys.platform == 'linux':
    def test_systemd_load_credential_acl_validation(provisioned, monkeypatch):
        import stat
        import struct
        from types import SimpleNamespace
        from factorlab.integrations.binance_futures_trading import demo_file_credentials

        root, secret, _ = provisioned
        uid = os.getuid()
        monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(secret.parent))
        store = demo_file_credentials.DemoFileCredentialStore()

        def pack_acl(entries):
            return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *item) for item in entries)

        valid_file_acl = pack_acl([(0x01, 4, 0xffffffff), (0x02, 4, uid), (0x04, 0, 0xffffffff),
                                   (0x10, 4, 0xffffffff), (0x20, 0, 0xffffffff)])
        valid_dir_acl = pack_acl([(0x01, 5, 0xffffffff), (0x02, 5, uid), (0x04, 0, 0xffffffff),
                                  (0x10, 5, 0xffffffff), (0x20, 0, 0xffffffff)])
        extra_user_acl = pack_acl([(0x01, 4, 0xffffffff), (0x02, 4, uid), (0x02, 4, uid + 1),
                                   (0x04, 0, 0xffffffff), (0x10, 4, 0xffffffff), (0x20, 0, 0xffffffff)])
        group_acl = pack_acl([(0x01, 4, 0xffffffff), (0x02, 4, uid), (0x04, 4, 0xffffffff),
                              (0x10, 4, 0xffffffff), (0x20, 0, 0xffffffff)])

        # Valid root-owned systemd LoadCredential directory (0550) + file (0440) with strict single-uid ACL.
        def credential_fstat(fd):
            info = os.fstat(fd)
            fields, extra_fields = info.__reduce__()[1]
            fields = list(fields)
            directory = stat.S_ISDIR(info.st_mode)
            fields[stat.ST_MODE] = stat.S_IFMT(info.st_mode) | (0o550 if directory else 0o440)
            fields[stat.ST_UID] = 0
            fields[stat.ST_NLINK] = 2 if directory else 1
            return os.stat_result(fields, extra_fields)

        file_acl = valid_file_acl

        def credential_getxattr(fd, name):
            return valid_dir_acl if stat.S_ISDIR(os.fstat(fd).st_mode) else file_acl

        # Patch only the credential module's OS reference so fdopen and pytest see real metadata.
        credential_os = SimpleNamespace(**{**vars(os), 'fstat': credential_fstat, 'getxattr': credential_getxattr})
        monkeypatch.setattr(demo_file_credentials, 'os', credential_os)
        assert store.read() == Credentials('synthetic-demo-key', 'synthetic-secret', Environment.DEMO)

        # Extra user or group permission in ACL must be rejected.
        for file_acl in (extra_user_acl, group_acl):
            with pytest.raises(DemoCredentialStoreError):
                store.read()

        # Outside CREDENTIALS_DIRECTORY, root-owned 0440 must still be rejected.
        monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(root))
        file_acl = valid_file_acl
        with pytest.raises(DemoCredentialStoreError):
            store.read()


if os.name == 'nt':
    def test_windows_credentials_and_binding_survive_reopen():
        from uuid import uuid4
        namespace = f'FactorLab-cloud-test-{uuid4()}'
        store = DemoCredentialStore(namespace=namespace)
        credentials = Credentials('synthetic-demo-key', 'synthetic-secret', Environment.DEMO)
        binding = dict(account_id='synthetic-account', journal_id='unchanged', configuration={})
        try:
            assert store.read() is None
            assert store.read_config() is None
            store.write(credentials)
            store.write_config(binding)
            reopened = DemoCredentialStore(namespace=namespace)
            assert reopened.reference == store.reference
            assert reopened.read() == credentials
            assert reopened.read_config() == binding
            reopened.write(Credentials(credentials.key, 'rotated-secret', Environment.DEMO))
            assert store.read().secret == 'rotated-secret'
            assert store.read_config() == binding
            store.delete()
            assert reopened.read() is None
            assert reopened.read_config() == binding
            store.delete_config()
            assert reopened.read_config() is None
        finally:
            store.delete()
            store.delete_config()
