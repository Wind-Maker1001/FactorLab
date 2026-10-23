"""Native RSS measurements and durable stage receipts without extra dependencies."""

from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback


def memory_bytes():
    """Current RSS and cumulative process high watermark, in bytes."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage",
                )]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.WorkingSetSize, counters.PeakWorkingSetSize
    if sys.platform.startswith("linux"):
        fields = {}
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                key, value, _ = line.split()
                fields[key] = int(value) * 1024
        return fields["VmRSS:"], fields["VmHWM:"]
    raise RuntimeError("RSS measurement supports Windows and Linux only")


class Recorder:
    """Sample RSS every 10 ms; nested spans are inclusive, never additive.

    sampled_peak_rss_bytes is a lower bound for each span, including native
    allocations. process_high_watermark_bytes is exact OS process lifetime
    evidence, not an independently reset stage peak. START is flushed before
    every operation so an OOM/kill leaves its last active stage on disk.
    """

    def __init__(self, output):
        self.path = Path(output)
        self.path.mkdir(parents=True, exist_ok=True)
        self.rows = []
        self.active = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.sampler_error = None
        memory_bytes()  # Fail explicitly on unsupported platforms.
        self.thread = threading.Thread(target=self._sample, daemon=True)
        self.thread.start()

    def _sample(self):
        try:
            while not self.stop.wait(.01):
                rss, _ = memory_bytes()
                with self.lock:
                    for row in self.active:
                        row["sampled_peak_rss_bytes"] = max(row["sampled_peak_rss_bytes"], rss)
        except Exception as exc:  # Never silently certify a dead sampler.
            self.sampler_error = repr(exc)

    def _write(self, payload):
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @contextmanager
    def span(self, stage, model=None, **metadata):
        rss, _ = memory_bytes()
        row = dict(stage=stage, model=model, **metadata, status="RUNNING",
                   sampled_peak_rss_bytes=rss)
        self._write({**row, "event": "START"})
        with self.lock:
            self.active.append(row)
        started = time.perf_counter()
        try:
            yield row
            row["status"] = "PASS"
        except BaseException as exc:
            row.update(status="FAIL", exception=type(exc).__name__, reason=str(exc),
                       traceback=traceback.format_exc())
            raise
        finally:
            row["seconds"] = time.perf_counter() - started
            rss, high = memory_bytes()
            with self.lock:
                self.active.remove(row)
            row["sampled_peak_rss_bytes"] = max(row["sampled_peak_rss_bytes"], rss)
            row["process_high_watermark_bytes"] = high
            if self.sampler_error:
                row.update(status="FAIL", sampler_error=self.sampler_error)
            self.rows.append(row)
            self._write({**row, "event": "END"})

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
