"""Bounded, observational Windows lock diagnosis; never retries a failed test."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import platform
import runpy
import subprocess
import sys
import threading
import time
import traceback
from unittest.mock import patch


def win_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def native_observation(path):
    if os.name != "nt":
        return {"status": "NOT_WINDOWS"}
    kernel = win_api()
    # Metadata-only OPEN_EXISTING with all sharing modes; no creation/deletion.
    handle = kernel.CreateFileW(str(path), 0, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return {"open_existing_winerror": ctypes.get_last_error()}
    kernel.CloseHandle(handle)
    return {"open_existing_winerror": 0}


def exception_data(exc):
    result = {"type": type(exc).__name__, "message": str(exc),
              "errno": getattr(exc, "errno", None), "winerror": getattr(exc, "winerror", None)}
    if os.name == "nt":
        value = ctypes.c_ulong()
        crt = ctypes.CDLL("ucrtbase")
        crt._get_doserrno(ctypes.byref(value))
        result["crt_doserrno"] = value.value
    return result


class Observer:
    def __init__(self):
        self.events = []
        self.handles = {}
        self.guard = threading.Lock()

    def event(self, operation, path, **fields):
        with self.guard:
            self.events.append(dict(sequence=len(self.events), monotonic_ns=time.monotonic_ns(),
                                    thread_id=threading.get_ident(), operation=operation,
                                    path=str(path), observed_open_descriptors=dict(self.handles), **fields))

    def install(self):
        original_open, original_close, original_unlink = os.open, os.close, Path.unlink

        def observed_open(path, flags, mode=0o777, *, dir_fd=None):
            tracked = str(path).endswith(".signal.lock")
            if tracked:
                self.event("create_attempt", path, flags=flags)
            try:
                descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            except OSError as exc:
                if tracked:
                    error = exception_data(exc)
                    self.event("create_error", path, error=error, native=native_observation(path))
                raise
            if tracked:
                with self.guard:
                    self.handles[descriptor] = dict(path=str(path), thread_id=threading.get_ident())
                self.event("create_success", path, descriptor=descriptor)
            return descriptor

        def observed_close(descriptor):
            with self.guard:
                metadata = self.handles.get(descriptor)
            try:
                return original_close(descriptor)
            finally:
                if metadata:
                    with self.guard:
                        self.handles.pop(descriptor, None)
                    self.event("close_return", metadata["path"], descriptor=descriptor)

        def observed_unlink(path, *args, **kwargs):
            tracked = str(path).endswith(".signal.lock")
            if tracked:
                self.event("release_attempt", path)
            try:
                result = original_unlink(path, *args, **kwargs)
            except OSError as exc:
                if tracked:
                    self.event("release_error", path, error=exception_data(exc))
                raise
            if tracked:
                self.event("release_return", path)
            return result

        return patch.multiple(os, open=observed_open, close=observed_close), patch.object(Path, "unlink", observed_unlink)


def pending_delete_example(root):
    """Keep a known share-delete handle open and observe the actual OS result."""
    if os.name != "nt":
        return {"status": "NOT_WINDOWS"}
    root.mkdir()
    path = root / "pending.signal.lock"
    path.write_bytes(b"synthetic diagnostic only")
    kernel = win_api()
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return {"status": "SETUP_ERROR", "winerror": ctypes.get_last_error()}
    result = {"held_handle": int(handle), "share_mode": 7, "sequence": []}
    try:
        kernel.DeleteFileW.argtypes = [wintypes.LPCWSTR]
        kernel.DeleteFileW.restype = wintypes.BOOL
        if not kernel.DeleteFileW(str(path)):
            return {"status": "DELETE_ERROR", "winerror": ctypes.get_last_error()}
        result["sequence"].append("unlink_return_with_known_handle_still_open")
        try:
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            result["exclusive_create_while_pending"] = exception_data(exc)
        else:
            os.close(descriptor)
            result["exclusive_create_while_pending"] = {"unexpected_success": True}
            path.unlink()
        result["native_while_pending"] = native_observation(path)
    finally:
        kernel.CloseHandle(handle)
    result["sequence"].append("known_handle_closed")
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    path.unlink()
    result["exclusive_create_after_close"] = "PASS"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=40)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100:
        parser.error("iterations must be between 1 and 100")
    source, output = args.source.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(source / "src"))
    namespace = runpy.run_path(str(source / "tests/research/test_forward_full_predictions.py"))
    test = namespace["test_concurrent_full_prediction_writers_publish_distinct_complete_versions"]
    before = subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True)
    observer, runs = Observer(), []
    patched_os, patched_path = observer.install()
    with patched_os, patched_path:
        for index in range(args.iterations):
            directory = output / ("iteration-%03d" % index)
            directory.mkdir()
            try:
                test(directory)
            except Exception as exc:
                runs.append(dict(iteration=index, status="FAIL", error=exception_data(exc), traceback=traceback.format_exc()))
            else:
                runs.append(dict(iteration=index, status="PASS"))
    pending = pending_delete_example(output / "minimal-pending-delete")
    acl = subprocess.run(["icacls", str(output)], capture_output=True, text=True) if os.name == "nt" else None
    after = subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True)
    result = dict(schema="FACTORLAB_WINDOWS_LOCK_DIAGNOSTIC_V1",
                  subject_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip(),
                  source=str(source), python=sys.version, platform=platform.platform(),
                  max_workers=4, writers_per_iteration=4, iterations=args.iterations,
                  runs=runs, events=observer.events, minimal_pending_delete=pending,
                  source_status_unchanged=before == after,
                  temp_acl={"exit_code": acl.returncode, "output": acl.stdout} if acl else None,
                  security_software_handle_attribution="UNKNOWN; observer tracks only Python-created lock descriptors",
                  original_remote_failure_attribution="UNKNOWN",
                  retry_policy="No test retries or exception suppression; each independent iteration retains its result",
                  remaining_observed_descriptors=observer.handles)
    (output / "diagnostic.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(subject=result["subject_sha"], passed=sum(r["status"] == "PASS" for r in runs),
                          failed=sum(r["status"] == "FAIL" for r in runs), source_status_unchanged=before == after)))
    return int(any(r["status"] != "PASS" for r in runs) or before != after)


if __name__ == "__main__":
    raise SystemExit(main())
