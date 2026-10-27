"""Synthetic fault injection and cross-process publication; no financial admission."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import ctypes as c
from ctypes import wintypes as w
from datetime import date, timedelta
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from factorlab.forward import _windows_lock as native
from factorlab.forward import signal_recorder as module
from factorlab.forward.signal_recorder import SignalRecorder, SignalLockError, SignalLockTimeoutError, load_signal_record
from tests.research.test_forward_full_predictions import DAY, _predictions, _signals, _kwargs


def publish(task):
    root, index, offset = task
    day = DAY + timedelta(days=offset)
    recorder = SignalRecorder(root, activation_date=DAY, lock_timeout=15, lock_poll_interval=.001)
    record = recorder.record(_signals(), predictions=_predictions(), cross_section_size=6,
                             **_kwargs(signal_date=day, data_asof=day,
                                       generated_at=day.isoformat()+'T16:10:00+08:00',
                                       run_id='synthetic-lock-%d' % index))
    loaded = load_signal_record(record.directory, require_completion=False)
    assert loaded.predictions_sha256 == hashlib.sha256(record.predictions_path.read_bytes()).hexdigest()
    return day.isoformat(), record.version, loaded.predictions_sha256


@pytest.mark.parametrize('executor', ['thread', 'process'])
@pytest.mark.parametrize('different_dates', [False, True])
def test_four_writers_are_exclusive_and_hash_complete(tmp_path, executor, different_dates):
    pool_type = ThreadPoolExecutor if executor == 'thread' else ProcessPoolExecutor
    options = {} if executor == 'thread' else {'mp_context': multiprocessing.get_context('spawn')}
    root = tmp_path / 'synthetic'
    tasks = [(root, i, i if different_dates else 0) for i in range(4)]
    with pool_type(max_workers=4, **options) as pool:
        results = list(pool.map(publish, tasks))
    assert len(set((day, version) for day, version, _ in results)) == 4
    assert sorted(v for _, v, _ in results) == (['v000001']*4 if different_dates else
                                                ['v000001', 'v000002', 'v000003', 'v000004'])
    assert len(list(root.rglob('predictions.csv'))) == 4
    assert len([p for p in root.rglob('*') if p.is_file()]) == 16
    assert not list(root.glob('*.lock'))
    assert not list(root.rglob('*.tmp-*'))


def test_single_thread_recreation_keeps_prior_bytes(tmp_path):
    root = tmp_path / 'synthetic'
    publish((root, 0, 0))
    before = {p.name: p.read_bytes() for p in (root/DAY.isoformat()/'v000001').iterdir()}
    publish((root, 1, 0))
    assert before == {p.name: p.read_bytes() for p in (root/DAY.isoformat()/'v000001').iterdir()}
    assert not list(root.glob('*.lock'))


@pytest.mark.parametrize('payload', ['[]', 'null', '{broken', '{"token":"foreign-owner"}'])
def test_corrupt_or_foreign_lock_cannot_be_released(tmp_path, payload):
    recorder = SignalRecorder(tmp_path/'synthetic', activation_date=DAY)
    with pytest.raises(SignalLockError):
        with recorder._exclusive_date_lock(DAY):
            recorder.lock_path(DAY).write_text(payload, encoding='utf-8')
    assert recorder.lock_path(DAY).read_text(encoding='utf-8') == payload


def test_body_exception_cleans_owned_lock(tmp_path):
    recorder = SignalRecorder(tmp_path/'synthetic', activation_date=DAY)
    with pytest.raises(RuntimeError, match='injected'):
        with recorder._exclusive_date_lock(DAY):
            raise RuntimeError('injected body failure')
    assert not recorder.lock_path(DAY).exists()


def test_crashed_owner_is_not_broken_or_admitted(tmp_path):
    root = tmp_path/'synthetic'
    source = Path(module.__file__).resolve().parents[2]
    program = ('import os; from datetime import date; '
               'from factorlab.forward.signal_recorder import SignalRecorder; '
               'r=SignalRecorder(os.environ["SYNTHETIC_LOCK_ROOT"], activation_date="2026-09-24"); '
               'c=r._exclusive_date_lock(date(2026,9,24)); c.__enter__(); os._exit(23)')
    env = dict(os.environ, PYTHONPATH=str(source), SYNTHETIC_LOCK_ROOT=str(root))
    process = subprocess.run([sys.executable, '-B', '-c', program], env=env, capture_output=True, timeout=30)
    assert process.returncode == 23, process.stderr
    recorder = SignalRecorder(root, activation_date=DAY, lock_timeout=.03, lock_poll_interval=.001)
    before = recorder.lock_path(DAY).read_bytes()
    with pytest.raises(SignalLockTimeoutError):
        publish_recorder = recorder._exclusive_date_lock(DAY)
        with publish_recorder:
            pytest.fail('crashed owner authorized a writer')
    assert recorder.lock_path(DAY).read_bytes() == before
    assert not list(root.rglob('predictions.csv'))


def test_unclassified_denial_fails_without_sleep_or_publication(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(13, 'injected actual permission denial')
    def no_sleep(*args):
        pytest.fail('a generic permission denial must not be retried')
    monkeypatch.setattr(module.os, 'open', denied)
    monkeypatch.setattr(module, '_windows_create_exclusive', denied)
    monkeypatch.setattr(module.time_module, 'sleep', no_sleep)
    with pytest.raises(SignalLockError):
        publish((tmp_path/'synthetic', 0, 0))
    assert not list(tmp_path.rglob('predictions.csv'))


@pytest.fixture
def windows_api():
    if os.name != 'nt':
        pytest.skip('native Windows handle semantics')
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.SetFileInformationByHandle.restype = w.BOOL
    kernel.DeleteFileW.argtypes = [w.LPCWSTR]
    kernel.DeleteFileW.restype = w.BOOL
    return kernel


def pending_handle(kernel, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'synthetic-delete-pending-only\n')
    handle = kernel.CreateFileW(str(path), 0x80010000, 7, None, 3, 0x80, None)
    assert handle != c.c_void_p(-1).value
    disposition = w.BOOL(True)
    if not kernel.SetFileInformationByHandle(handle, 4, c.byref(disposition), c.sizeof(disposition)):
        kernel.CloseHandle(handle)
        pytest.fail('cannot construct explicit delete-pending state')
    return handle


def test_native_status_is_delete_pending_not_acl(tmp_path, windows_api):
    path = tmp_path/'pending.signal.lock'
    handle = pending_handle(windows_api, path)
    try:
        with pytest.raises(native.DeletePendingError) as error:
            native.create_exclusive(str(path))
        assert error.value.ntstatus == 0xC0000056
    finally:
        assert windows_api.CloseHandle(handle)
    descriptor = native.create_exclusive(str(path))
    os.write(descriptor, b'synthetic')
    os.fsync(descriptor)
    os.close(descriptor)
    assert path.read_bytes() == b'synthetic'
    with pytest.raises(FileExistsError):
        native.create_exclusive(str(path))


def test_pending_deletion_recovers_only_after_handle_closes(tmp_path, windows_api):
    recorder = SignalRecorder(tmp_path/'synthetic', activation_date=DAY, lock_timeout=2, lock_poll_interval=.001)
    handle = pending_handle(windows_api, recorder.lock_path(DAY))
    observed, failures = threading.Event(), []
    original = module._windows_create_exclusive

    def observed_create(path):
        try:
            return original(path)
        except native.DeletePendingError:
            observed.set()
            raise

    def release():
        if not observed.wait(1):
            failures.append('never observed pending deletion')
        windows_api.CloseHandle(handle)

    from unittest.mock import patch
    releaser = threading.Thread(target=release)
    releaser.start()
    try:
        with patch.object(module, '_windows_create_exclusive', observed_create):
            result = publish((recorder.root, 0, 0))
    finally:
        releaser.join(3)
    assert observed.is_set() and not failures and not releaser.is_alive()
    assert result[1] == 'v000001'
    assert not recorder.lock_path(DAY).exists()


def test_permanent_pending_deletion_has_bounded_timeout(tmp_path, windows_api):
    recorder = SignalRecorder(tmp_path/'synthetic', activation_date=DAY, lock_timeout=.04, lock_poll_interval=.001)
    handle = pending_handle(windows_api, recorder.lock_path(DAY))
    start = time.monotonic()
    try:
        with pytest.raises(SignalLockTimeoutError) as error:
            with recorder._exclusive_date_lock(DAY):
                pytest.fail('pending deletion authorized a writer')
        assert isinstance(error.value.__cause__, native.DeletePendingError)
        assert time.monotonic()-start < 2
        assert not list(recorder.root.rglob('predictions.csv'))
    finally:
        windows_api.CloseHandle(handle)


def test_real_acl_denial_is_not_retried(tmp_path, windows_api, monkeypatch):
    root = tmp_path/'synthetic'
    root.mkdir()
    command = subprocess.run(['icacls', str(root), '/deny', '*S-1-1-0:(W)'], capture_output=True)
    assert command.returncode == 0, 'cannot construct ACL denial fixture'
    try:
        with pytest.raises(PermissionError) as direct:
            native.create_exclusive(str(root/'direct.signal.lock'))
        assert direct.value.ntstatus == 0xC0000022
        def no_sleep(*args):
            pytest.fail('ACL denial retried')
        monkeypatch.setattr(module.time_module, 'sleep', no_sleep)
        with pytest.raises(SignalLockError) as error:
            publish((root, 0, 0))
        assert error.value.__cause__.ntstatus == 0xC0000022
        assert not list(root.rglob('predictions.csv'))
    finally:
        restore = subprocess.run(['icacls', str(root), '/remove:d', '*S-1-1-0'], capture_output=True)
        assert restore.returncode == 0, 'ACL fixture restoration failed'


def test_native_descriptor_conversion_failure_stays_closed(tmp_path, windows_api, monkeypatch):
    import msvcrt
    def fail(*args):
        raise OSError('injected descriptor conversion failure')
    monkeypatch.setattr(msvcrt, 'open_osfhandle', fail)
    path = tmp_path/'conversion.signal.lock'
    with pytest.raises(OSError, match='conversion failure'):
        native.create_exclusive(str(path))
    assert path.exists()  # Refuse and preserve a stale lock for inspection.
    path.unlink()  # No leaked native handle may prevent controlled cleanup.


def test_no_share_delete_blocks_removal(tmp_path, windows_api):
    path = tmp_path/'sharing.signal.lock'
    path.write_bytes(b'synthetic')
    handle = windows_api.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != c.c_void_p(-1).value
    try:
        assert not windows_api.DeleteFileW(str(path))
        assert c.get_last_error() == 32
        with pytest.raises(FileExistsError):
            native.create_exclusive(str(path))
    finally:
        windows_api.CloseHandle(handle)
    path.unlink()
