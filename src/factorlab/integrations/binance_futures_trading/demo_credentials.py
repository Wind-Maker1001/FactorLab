"""Current-user Windows Credential Manager storage for FactorLab Demo."""

import ctypes
from ctypes import wintypes
import json
import os

from .protocol import Credentials, Environment


_GENERIC = 1
# Keep the entry for the same Windows user across logons on this machine.
_PERSIST_LOCAL_MACHINE = 2
_NOT_FOUND = 1168
_BYTE_POINTER = ctypes.POINTER(ctypes.c_ubyte)


class _Credential(ctypes.Structure):
    _fields_ = [
        ('Flags', wintypes.DWORD),
        ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPWSTR),
        ('Comment', wintypes.LPWSTR),
        ('LastWritten', wintypes.FILETIME),
        ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', _BYTE_POINTER),
        ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD),
        ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wintypes.LPWSTR),
        ('UserName', wintypes.LPWSTR),
    ]


_CREDENTIAL_POINTER = ctypes.POINTER(_Credential)


class DemoCredentialStoreError(RuntimeError):
    """A sanitized Windows storage or stored-data error."""


class DemoCredentialStore:
    """Store the Demo key pair and the private runtime configuration separately."""

    def __init__(self, *, namespace='FactorLab'):
        if os.name != 'nt':
            raise DemoCredentialStoreError('Demo credential storage requires Windows Credential Manager')
        root = f'{namespace}/Binance/USD-M/Demo'
        self.reference = root + '/credentials'
        self._config_target = root + '/runtime-config'
        self._api = ctypes.WinDLL('advapi32.dll', use_last_error=True)
        self._api.CredReadW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_CREDENTIAL_POINTER),
        )
        self._api.CredReadW.restype = wintypes.BOOL
        self._api.CredWriteW.argtypes = (_CREDENTIAL_POINTER, wintypes.DWORD)
        self._api.CredWriteW.restype = wintypes.BOOL
        self._api.CredDeleteW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD)
        self._api.CredDeleteW.restype = wintypes.BOOL
        self._api.CredFree.argtypes = (ctypes.c_void_p,)
        self._api.CredFree.restype = None

    def _read(self, target):
        credential = _CREDENTIAL_POINTER()
        if not self._api.CredReadW(target, _GENERIC, 0, ctypes.byref(credential)):
            code = ctypes.get_last_error()
            if code == _NOT_FOUND:
                return None
            raise DemoCredentialStoreError(f'Windows Credential Manager read failed (Windows error {code})')
        try:
            entry = credential.contents
            return ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize)
        finally:
            self._api.CredFree(credential)

    def _write(self, target, blob):
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        entry = _Credential(
            Type=_GENERIC, TargetName=target, CredentialBlobSize=len(blob),
            CredentialBlob=buffer, Persist=_PERSIST_LOCAL_MACHINE, UserName='FactorLab Demo',
        )
        if not self._api.CredWriteW(ctypes.byref(entry), 0):
            code = ctypes.get_last_error()
            raise DemoCredentialStoreError(f'Windows Credential Manager write failed (Windows error {code})')

    def _delete(self, target):
        if not self._api.CredDeleteW(target, _GENERIC, 0):
            code = ctypes.get_last_error()
            if code != _NOT_FOUND:
                raise DemoCredentialStoreError(f'Windows Credential Manager deletion failed (Windows error {code})')

    def read(self):
        blob = self._read(self.reference)
        if blob is None:
            return None
        try:
            key, secret = blob.decode('utf-8').split('\0', 1)
            return Credentials(key, secret, Environment.DEMO)
        except ValueError:
            raise DemoCredentialStoreError('Stored Demo credential is invalid') from None

    def write(self, credentials):
        if credentials.environment is not Environment.DEMO:
            raise DemoCredentialStoreError('Demo credential storage requires explicitly Demo credentials')
        blob = f'{credentials.key}\0{credentials.secret}'.encode('utf-8')
        self._write(self.reference, blob)

    def delete(self):
        self._delete(self.reference)

    def read_config(self):
        blob = self._read(self._config_target)
        if blob is None:
            return None
        try:
            return json.loads(blob)
        except ValueError:
            raise DemoCredentialStoreError('Stored Demo runtime configuration is invalid') from None

    def write_config(self, config):
        blob = json.dumps(config, separators=(',', ':'), allow_nan=False).encode('utf-8')
        self._write(self._config_target, blob)

    def delete_config(self):
        self._delete(self._config_target)
