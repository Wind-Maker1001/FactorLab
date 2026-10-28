"""Read-only source diagnosis with controlled synthetic Win32 handle lifetimes.

No test retries. Paths in the public receipt are relative to the synthetic root.
RtlGetLastNtStatus is observational only, never an authorization decision.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes as c
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import subprocess
import sys
import threading
import time
from unittest.mock import patch

NT = c.WinDLL('ntdll') if os.name == 'nt' else None
CRT = c.CDLL('ucrtbase') if os.name == 'nt' else None
if NT is not None:
    NT.RtlGetLastNtStatus.restype = w.LONG
    CRT._get_doserrno.argtypes = [c.POINTER(c.c_ulong)]


def api():
    k = c.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL
    k.DeleteFileW.argtypes = [w.LPCWSTR]
    k.DeleteFileW.restype = w.BOOL
    k.GetFileInformationByHandleEx.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    k.GetFileInformationByHandleEx.restype = w.BOOL
    k.SetFileInformationByHandle.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    k.SetFileInformationByHandle.restype = w.BOOL
    return k


class StandardInfo(c.Structure):
    _fields_ = [('allocation', c.c_longlong), ('size', c.c_longlong),
                ('links', w.DWORD), ('delete_pending', c.c_ubyte), ('directory', c.c_ubyte)]


def snapshot(k, handle):
    info = StandardInfo()
    if not k.GetFileInformationByHandleEx(handle, 1, c.byref(info), c.sizeof(info)):
        return {'query_error': c.get_last_error()}
    return {'delete_pending': bool(info.delete_pending), 'links': info.links, 'size': info.size}


def error(exc):
    # Capture the NT thread status and CRT errno before any other OS operation.
    status = NT.RtlGetLastNtStatus() & 0xffffffff if NT is not None else None
    dos = c.c_ulong()
    if os.name == 'nt':
        CRT._get_doserrno(c.byref(dos))
    return dict(exception=type(exc).__name__, errno=getattr(exc, 'errno', None),
                winerror=getattr(exc, 'winerror', None), crt_doserrno=dos.value,
                observed_ntstatus=None if status is None else '0x%08X' % status)


def create_error(path):
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        return error(exc)
    os.close(fd)
    return {'created': True}


def controlled(root, recorder_class):
    k = api()
    root.mkdir()
    path = root / 'pending.signal.lock'
    path.write_bytes(b'synthetic only\n')
    handle = k.CreateFileW(str(path), 0x80010000, 7, None, 3, 0x80, None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    result = {'share_delete': {'access': 'GENERIC_READ | DELETE', 'share_mode': 7,
                              'before': snapshot(k, handle)}}
    try:
        disposition = w.BOOL(True)
        result['share_delete']['SetFileInformationByHandle_FileDispositionInfo'] = bool(
            k.SetFileInformationByHandle(handle, 4, c.byref(disposition), c.sizeof(disposition)))
        result['share_delete']['after'] = snapshot(k, handle)
        result['share_delete']['exclusive_create'] = create_error(path)
        # An actual recorder sees precisely the same controlled state.
        recorder = recorder_class(root / 'recorder', activation_date='2026-09-24', lock_timeout=.05)
        recorder.root.mkdir()
        lock = recorder.lock_path('2026-09-24')
        lock.write_bytes(b'synthetic only\n')
        owned = k.CreateFileW(str(lock), 0x80010000, 7, None, 3, 0x80, None)
        if owned == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            assert k.SetFileInformationByHandle(owned, 4, c.byref(disposition), c.sizeof(disposition))
            result['recorder_delete_pending'] = snapshot(k, owned)
            from datetime import date
            try:
                with recorder._exclusive_date_lock(date(2026, 9, 24)):
                    result['recorder_delete_pending']['entered'] = True
            except Exception as exc:
                result['recorder_delete_pending']['error'] = error(exc)
                result['recorder_delete_pending']['cause'] = error(exc.__cause__) if exc.__cause__ else None
        finally:
            k.CloseHandle(owned)
    finally:
        k.CloseHandle(handle)
    # Preserve observations even on a system using POSIX removal semantics.
    if path.exists():
        path.unlink()
    result['after_final_handle_close'] = create_error(path)
    path.unlink()
    path.write_bytes(b'synthetic only\n')
    handle = k.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        success = bool(k.DeleteFileW(str(path)))
        result['without_share_delete'] = {'share_mode': 3, 'DeleteFileW': success,
                                         'winerror': 0 if success else c.get_last_error(),
                                         'state': snapshot(k, handle), 'exclusive_create': create_error(path)}
    finally:
        k.CloseHandle(handle)
    path.unlink()
    denied = root / 'denied'
    denied.mkdir()
    deny = subprocess.run(['icacls', str(denied), '/deny', '*S-1-1-0:(W)'], capture_output=True)
    try:
        result['acl_denial'] = {'fixture_exit': deny.returncode,
                                'exclusive_create': create_error(denied / 'denied.signal.lock')}
    finally:
        restore = subprocess.run(['icacls', str(denied), '/remove:d', '*S-1-1-0'], capture_output=True)
        result['acl_denial']['restore_exit'] = restore.returncode
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--iterations', type=int, default=100)
    a = p.parse_args()
    if os.name != 'nt' or not 1 <= a.iterations <= 300:
        p.error('Windows required; iterations must be 1..300')
    source, output = a.source.resolve(), a.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(source / 'src'))
    ns = runpy.run_path(str(source / 'tests/research/test_forward_full_predictions.py'))
    events, rounds, guard = [], [], threading.Lock()
    original_open, original_unlink = os.open, Path.unlink

    def event(op, path, **fields):
        with guard:
            events.append(dict(seq=len(events), ns=time.perf_counter_ns(), pid=os.getpid(),
                               tid=threading.get_native_id(), op=op,
                               path=Path(path).relative_to(output).as_posix(), **fields))

    def observed_open(path, flags, mode=0o777, **kwargs):
        tracked = str(path).endswith('.signal.lock')
        if tracked:
            event('exclusive_create_call', path, flags=flags)
        try:
            fd = original_open(path, flags, mode, **kwargs)
        except OSError as exc:
            details = error(exc)
            if tracked:
                event('exclusive_create_error', path, **details)
            raise
        if tracked:
            event('exclusive_create_return', path, descriptor=fd)
        return fd

    def observed_unlink(path, *args, **kwargs):
        tracked = str(path).endswith('.signal.lock')
        if tracked:
            event('unlink_call', path)
        try:
            value = original_unlink(path, *args, **kwargs)
        except OSError as exc:
            details = error(exc)
            if tracked:
                event('unlink_error', path, **details)
            raise
        if tracked:
            event('unlink_return', path)
        return value

    for index in range(a.iterations):
        directory = output / ('round-%03d' % index)
        directory.mkdir()
        start = time.perf_counter()
        with patch.object(os, 'open', observed_open), patch.object(Path, 'unlink', observed_unlink):
            try:
                ns['test_concurrent_full_prediction_writers_publish_distinct_complete_versions'](directory)
            except Exception as exc:
                outcome = {'status': 'FAIL', 'error': error(exc),
                           'cause': error(exc.__cause__) if exc.__cause__ else None}
            else:
                outcome = {'status': 'PASS'}
        files = {f.relative_to(directory).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
                 for f in directory.rglob('*') if f.is_file()}
        rounds.append(dict(iteration=index, seconds=time.perf_counter()-start,
                           files=files, file_count=len(files),
                           locks=[n for n in files if n.endswith('.lock')], **outcome))
    states = controlled(output/'controlled', ns['SignalRecorder'])
    setup_ok = (states['share_delete']['after'].get('delete_pending') is True and
                states['share_delete']['exclusive_create'].get('observed_ntstatus') == '0xC0000056' and
                states['acl_denial']['fixture_exit'] == states['acl_denial']['restore_exit'] == 0 and
                states['acl_denial']['exclusive_create'].get('observed_ntstatus') == '0xC0000022' and
                not states['recorder_delete_pending'].get('entered', False))
    result = dict(schema='FACTORLAB_NATIVE_LOCK_PROBE_V2',
                  subject_sha=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip(),
                  lock_source_sha256=hashlib.sha256((source/'src/factorlab/forward/signal_recorder.py').read_bytes()).hexdigest(),
                  python=sys.version, platform=platform.platform(), workers=4,
                  iterations=a.iterations, passed=sum(r['status']=='PASS' for r in rounds),
                  failed=sum(r['status']=='FAIL' for r in rounds), rounds=rounds, events=events,
                  controlled=states, controlled_setup_valid=setup_ok,
                  external_handle_attribution='UNKNOWN: no system-wide ETW/ProcMon handle trace',
                  no_test_retries=True)
    (output/'native-diagnostic.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('subject_sha','iterations','passed','failed','controlled')}))
    # This is an observation of original controls; actual FAIL remains in the receipt.
    return int(bool(result['failed']) or not setup_ok)


if __name__ == '__main__':
    raise SystemExit(main())
