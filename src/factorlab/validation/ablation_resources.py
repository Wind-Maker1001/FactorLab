"""Small best-effort resident-memory sampler, including native allocations."""

import ctypes
from pathlib import Path
import sys
import threading


def _resident_reader():
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

        kernel, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]

        def read():
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return None
            return int(counters.WorkingSetSize)

        return read
    if sys.platform.startswith("linux"):
        def read():
            try:
                for line in Path("/proc/self/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
            except OSError:
                pass
            return None
        return read
    return lambda: None


class ResidentMemorySample:
    """20ms samples are a lower bound on true peak, not an isolated benchmark."""

    def __enter__(self):
        self.read = _resident_reader()
        self.baseline = self.peak = self.read()
        self.stop = threading.Event()

        def sample():
            while not self.stop.wait(0.02):
                value = self.read()
                if value is not None:
                    self.peak = max(self.peak or 0, value)

        self.thread = threading.Thread(target=sample, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        value = self.read()
        if value is not None:
            self.peak = max(self.peak or 0, value)
