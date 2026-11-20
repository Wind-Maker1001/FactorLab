"""Linux Demo credentials provisioned externally, with a private local binding."""

from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import stat
import uuid

from .demo_credentials import DemoCredentialStoreError
from .protocol import Credentials, Environment


class DemoFileCredentialStore:
    def __init__(self, *, namespace='FactorLab'):
        self.reference = f'{namespace}/Binance/USD-M/Demo/credentials'
        try:
            self._secret = Path(os.environ['FACTORLAB_DEMO_SECRET_FILE'])
            self._root = Path(os.environ['FACTORLAB_DEMO_STATE_ROOT'])
            if not self._secret.is_absolute() or not self._root.is_absolute():
                raise ValueError
            if self._secret == self._root / 'binding.json':
                raise ValueError
        except (KeyError, ValueError):
            raise DemoCredentialStoreError('Demo storage requires explicit absolute secret and state paths') from None

    @staticmethod
    def _private(descriptor, *, directory=False):
        info = os.fstat(descriptor)
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or (not directory and info.st_nlink != 1)):
            raise ValueError

    @staticmethod
    def _open_directory(path):
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.parts[1:]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def _directory(self):
        descriptor = self._open_directory(self._root)
        try:
            self._private(descriptor, directory=True)
            yield descriptor
        finally:
            os.close(descriptor)

    def _json(self, path, *, directory=None):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
            self._private(stream.fileno())
            return json.load(stream)

    def read(self):
        try:
            directory = self._open_directory(self._secret.parent)
            try:
                payload = self._json(self._secret.name, directory=directory)
            finally:
                os.close(directory)
            if (not isinstance(payload, dict) or payload.get('environment') != 'DEMO'
                    or not isinstance(payload.get('key'), str) or not payload['key']
                    or not isinstance(payload.get('secret'), str) or not payload['secret']):
                raise ValueError
            return Credentials(payload['key'], payload['secret'], Environment.DEMO)
        except (OSError, ValueError, UnicodeError):
            raise DemoCredentialStoreError('Protected Demo credential file is unavailable or invalid') from None

    def write(self, credentials):
        mounted = self.read()
        if (credentials.environment is not Environment.DEMO
                or not hmac.compare_digest(credentials.key.encode(), mounted.key.encode())
                or not hmac.compare_digest(credentials.secret.encode(), mounted.secret.encode())):
            raise DemoCredentialStoreError('Demo credentials must match the externally provisioned secret')

    def delete(self):
        raise DemoCredentialStoreError('Demo secrets must be removed by the external provisioner')

    def read_config(self):
        try:
            with self._directory() as directory:
                try:
                    payload = self._json('binding.json', directory=directory)
                except FileNotFoundError:
                    return None
                if not isinstance(payload, dict):
                    raise ValueError
                return payload
        except (OSError, ValueError, UnicodeError):
            raise DemoCredentialStoreError('Private Demo binding is unavailable or invalid') from None

    def write_config(self, config):
        temporary = '.binding-' + uuid.uuid4().hex
        try:
            if not isinstance(config, dict):
                raise ValueError
            blob = json.dumps(config, separators=(',', ':'), allow_nan=False).encode('utf-8')
            with self._directory() as directory:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=directory)
                try:
                    with os.fdopen(descriptor, 'wb') as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(blob)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, 'binding.json', src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
        except (OSError, ValueError, TypeError, UnicodeError):
            raise DemoCredentialStoreError('Private Demo binding could not be written') from None

    def delete_config(self):
        try:
            with self._directory() as directory:
                try:
                    os.unlink('binding.json', dir_fd=directory)
                except FileNotFoundError:
                    return
                os.fsync(directory)
        except (OSError, ValueError):
            raise DemoCredentialStoreError('Private Demo binding could not be removed') from None
