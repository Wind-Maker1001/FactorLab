"""Diagnostic-only timers and explicitly named process memory measurements.

No instrumentation is installed in the production evaluator by default.
Inclusive kernel timings must not be added to their enclosing stage timings.
"""

from contextlib import contextmanager, ExitStack
import ctypes
import io
import os
from pathlib import Path
import platform
import threading
from time import perf_counter
from unittest.mock import patch


class MemoryProbe:
    def __init__(self):
        self.metric = "unsupported"
        self.error = None
        if os.name == "nt":
            class Counters(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
                    (name, ctypes.c_size_t) for name in (
                        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                        "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]
            self.counters = Counters
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
            self.kernel.GetCurrentProcess.restype = ctypes.c_void_p
            self.psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
            self.psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            self.handle = self.kernel.GetCurrentProcess()
            self.metric = "Windows Working Set; Peak Working Set since process start; Private Commit separate"
        elif Path("/proc/self/status").exists():
            self.metric = "Linux VmRSS; VmHWM since process start; bytes converted from KiB"

    def snapshot(self):
        if os.name == "nt":
            values = self.counters()
            values.cb = ctypes.sizeof(values)
            if not self.psapi.GetProcessMemoryInfo(self.handle, ctypes.byref(values), values.cb):
                raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
            return {"resident_bytes": int(values.WorkingSetSize),
                    "process_lifetime_peak_resident_bytes": int(values.PeakWorkingSetSize),
                    "private_commit_bytes": int(values.PrivateUsage)}
        if self.metric.startswith("Linux"):
            entries = {}
            for line in Path("/proc/self/status").read_text().splitlines():
                parts = line.split()
                if parts[0] in ("VmRSS:", "VmHWM:"):
                    entries[parts[0]] = int(parts[1]) * 1024
            return {"resident_bytes": entries["VmRSS:"],
                    "process_lifetime_peak_resident_bytes": entries["VmHWM:"]}
        return {}


class PerformanceRecorder:
    def __init__(self, measure_memory=False):
        self.timings = {}
        self.current_phase = "initialization"
        self.phase_memory = {}
        self.memory_lock = threading.Lock()
        self.sample_interval_seconds = 0.05
        self.probe = MemoryProbe() if measure_memory else None
        self.sampling_error = None
        self.stop = threading.Event()
        self.thread = None

    def _sample(self):
        try:
            values = self.probe.snapshot()
            if values:
                with self.memory_lock:
                    phase = self.phase_memory.setdefault(self.current_phase, {"samples": 0, "sampled_max_resident_bytes": 0})
                    phase["samples"] += 1
                    phase["sampled_max_resident_bytes"] = max(phase["sampled_max_resident_bytes"], values["resident_bytes"])
        except (OSError, ValueError, KeyError) as exc:
            self.sampling_error = "%s: %s" % (type(exc).__name__, exc)

    def _sampling_loop(self):
        while not self.stop.wait(self.sample_interval_seconds):
            self._sample()

    def __enter__(self):
        if self.probe is not None:
            self._sample()
            self.thread = threading.Thread(target=self._sampling_loop, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *unused):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
            self._sample()

    @contextmanager
    def phase(self, name, sample=True):
        previous = self.current_phase
        if sample:
            self.current_phase = name
        start = perf_counter()
        try:
            yield
        finally:
            record = self.timings.setdefault(name, {"seconds": 0.0, "calls": 0})
            record["seconds"] += perf_counter() - start
            record["calls"] += 1
            self.current_phase = previous

    def wrap(self, function, name, sample=True):
        def measured(*args, **kwargs):
            with self.phase(name, sample=sample):
                return function(*args, **kwargs)
        return measured

    @contextmanager
    def instrument_evaluator(self, module):
        with ExitStack() as stack:
            for name in ("_daily_ic", "_quantile_returns", "_autocorrelation", "_top_quantile_turnover",
                         "_factor_correlation", "_summarise_ic", "_select_scope"):
                if hasattr(module, name):
                    stack.enter_context(patch.object(module, name, self.wrap(getattr(module, name), name)))
            if hasattr(module, "_PreparedFactors"):
                cls = module._PreparedFactors
                stack.enter_context(patch.object(cls, "create", staticmethod(self.wrap(cls.create, "prepare_workspace"))))
                from qbot.research.metrics import _factor_arrays as kernels
                for name in ("_pearson_finite", "_rank_finite"):
                    original = getattr(kernels, name)
                    def coefficient(*args, _fn=original, _name=name, **kwargs):
                        phase = "pearson_ic" if _name == "_pearson_finite" else (
                            "rank_ic" if self.current_phase == "_daily_ic" else "autocorrelation_rank")
                        with self.phase(phase, sample=False):
                            return _fn(*args, **kwargs)
                    stack.enter_context(patch.object(kernels, name, coefficient))
            if hasattr(module, "_safe_correlation"):
                original = module._safe_correlation
                def correlation(*args, **kwargs):
                    method = kwargs.get("method", args[2] if len(args) > 2 else None)
                    phase = "pearson_ic" if method == "pearson" else (
                        "rank_ic" if self.current_phase == "_daily_ic" else "autocorrelation_rank")
                    with self.phase(phase, sample=False):
                        return original(*args, **kwargs)
                stack.enter_context(patch.object(module, "_safe_correlation", correlation))
            yield

    @contextmanager
    def instrument_report(self):
        import pandas as pd
        from matplotlib.figure import Figure
        from qbot.research.reports import generator
        with ExitStack() as stack:
            for name in ("_bar_plot", "_correlation_plot", "_quantile_plot", "_regime_plot"):
                stack.enter_context(patch.object(generator, name, self.wrap(getattr(generator, name), "report" + name)))
            csv, savefig, text = pd.DataFrame.to_csv, Figure.savefig, Path.write_text
            def write_csv(frame, destination, *args, **kwargs):
                with self.phase("report_csv_" + Path(destination).name):
                    return csv(frame, destination, *args, **kwargs)
            def save(figure, destination, *args, **kwargs):
                with self.phase("report_png_" + Path(destination).name):
                    return savefig(figure, destination, *args, **kwargs)
            def write_text(path, content, *args, **kwargs):
                with self.phase("report_markdown"):
                    return text(path, content, *args, **kwargs)
            stack.enter_context(patch.object(pd.DataFrame, "to_csv", write_csv))
            stack.enter_context(patch.object(Figure, "savefig", save))
            stack.enter_context(patch.object(Path, "write_text", write_text))
            yield

    def payload(self):
        with self.memory_lock:
            sampled = {name: dict(values) for name, values in self.phase_memory.items()}
        memory = {"measured": self.probe is not None and self.probe.metric != "unsupported",
                  "metric": self.probe.metric if self.probe is not None else "not measured",
                  "sample_interval_seconds": self.sample_interval_seconds,
                  "phase_sampled_maxima_not_exact_peaks": sampled,
                  "sampling_error": self.sampling_error}
        if self.probe is not None:
            memory["at_recording_end"] = self.probe.snapshot()
        return {"stage_timings_inclusive_seconds_do_not_add_nested_kernels": {
            name: dict(values) for name, values in self.timings.items()}, "process_memory": memory}


def environment_details():
    import numpy as np
    stream = io.StringIO()
    from contextlib import redirect_stdout
    with redirect_stdout(stream):
        np.show_config()
    return {"platform": platform.platform(), "processor": platform.processor(),
            "cpu_count": os.cpu_count(), "blas_configuration": stream.getvalue(),
            "numeric_thread_environment": {name: os.environ.get(name) for name in
                ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")}}
