"""Offline F03 parallel resource measurements and complete-group admission."""
from contextlib import contextmanager
from math import ceil
from pathlib import Path
import shutil
import sys
from threading import Event, Lock, Thread
from time import perf_counter

from factorlab.dataset.partitioned import _require_psutil, directory_bytes


@contextmanager
def _cpu_accounting(psutil):
    if sys.platform != 'win32':
        import resource

        def seconds():
            # The parent reaps the pool before the final sample. Group samples also
            # include live workers whose CPU has yet to enter RUSAGE_CHILDREN.
            own = resource.getrusage(resource.RUSAGE_SELF)
            reaped = resource.getrusage(resource.RUSAGE_CHILDREN)
            active = 0.
            for child in psutil.Process().children():
                try:
                    usage = child.cpu_times()
                except psutil.NoSuchProcess:
                    continue
                active += usage.user + usage.system + usage.children_user + usage.children_system
            return own.ru_utime + own.ru_stime + reaped.ru_utime + reaped.ru_stime + active

        yield seconds, 'getrusage self and reaped children, with live children at group boundaries'
        return

    import ctypes
    from ctypes import wintypes

    class BasicAccounting(ctypes.Structure):
        _fields_ = [('user_time', ctypes.c_int64), ('kernel_time', ctypes.c_int64),
                    ('period_user_time', ctypes.c_int64), ('period_kernel_time', ctypes.c_int64),
                    ('page_faults', wintypes.DWORD), ('processes', wintypes.DWORD),
                    ('active_processes', wintypes.DWORD), ('terminated_processes', wintypes.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                                wintypes.DWORD, wintypes.LPVOID)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())

        def seconds():
            counters = BasicAccounting()
            if not kernel.QueryInformationJobObject(job, 1, ctypes.byref(counters), ctypes.sizeof(counters), None):
                raise ctypes.WinError(ctypes.get_last_error())
            return (counters.user_time + counters.kernel_time) / 10_000_000

        yield seconds, 'Windows Job user + kernel time, including exited descendants'
    finally:
        kernel.CloseHandle(job)


class ResourceMonitor:
    """Track study-wide and per-group resources around the existing replay scheduler."""

    def __init__(self):
        self.psutil = _require_psutil()
        self.root = self.psutil.Process()
        self.lock = Lock()
        self.temporary = None
        self.group = None
        self.groups = []

    def _sample(self):
        with self.lock:
            peak_rss = 0
            for process in [self.root, *self.root.children(recursive=True)]:
                try:
                    peak_rss += process.memory_info().rss
                except self.psutil.NoSuchProcess:
                    # A Git or worker process can exit between enumeration and sampling.
                    continue
            disk = directory_bytes(self.temporary) if self.temporary is not None else 0
            for measurement in (self.overall, self.group):
                if measurement is not None:
                    measurement['peak_tree_rss_bytes'] = max(measurement['peak_tree_rss_bytes'], peak_rss)
                    measurement['peak_temporary_disk_bytes'] = max(measurement['peak_temporary_disk_bytes'], disk)

    @contextmanager
    def measure(self):
        self.started = perf_counter()
        self.overall = dict(peak_tree_rss_bytes=0, peak_temporary_disk_bytes=0,
                            groups=self.groups, sample_interval_seconds=0.1,
                            startup_wall_seconds=None, startup_tree_cpu_seconds=None)
        stop = Event()

        def sample_until_stop():
            while not stop.wait(self.overall['sample_interval_seconds']):
                self._sample()

        with _cpu_accounting(self.psutil) as (self.cpu_seconds, method):
            self.cpu_start = self.cpu_seconds()
            self._sample()
            thread = Thread(target=sample_until_stop, daemon=True)
            thread.start()
            try:
                yield self.overall
            finally:
                stop.set()
                thread.join()
                self._sample()
                self.overall.update(wall_seconds=perf_counter()-self.started,
                                    tree_cpu_seconds=self.cpu_seconds()-self.cpu_start,
                                    cpu_measurement=method)

    @contextmanager
    def observe(self, model, size, temporary, output):
        started, cpu = perf_counter(), self.cpu_seconds()
        if not self.groups:
            self.overall.update(startup_wall_seconds=started-self.started,
                                startup_tree_cpu_seconds=cpu-self.cpu_start)
        initial_evidence = directory_bytes(output)
        group = dict(model=model, size=size, peak_tree_rss_bytes=0, peak_temporary_disk_bytes=0)
        with self.lock:
            self.temporary = temporary
            self.group = group
        self._sample()
        complete = False
        try:
            yield
            complete = True
        finally:
            self._sample()
            group.update(complete=complete, wall_seconds=perf_counter()-started,
                         tree_cpu_seconds=self.cpu_seconds()-cpu,
                         additional_evidence_bytes=max(0, directory_bytes(output)-initial_evidence))
            with self.lock:
                self.group = None
            self.groups.append(group)


class ResourcePlanner:
    """Estimate a whole model's next tier from one fixed eight-worker pilot batch."""

    def __init__(self, calibration, definition):
        self.calibration = calibration
        self.policy = definition['resources']
        self.planning = definition.get('resource_planning', {})
        self.psutil = _require_psutil()

    def admit(self, *, model, size, begin, accounts, elapsed, temporary, output, settings):
        measured = self.calibration['models'].get(model)
        if measured is None:
            return dict(admitted=False, status='RESOURCE_NOT_ADMITTED', reasons=['PILOT_NOT_COMPLETED'])
        safety = settings['cost_safety_factor']
        batch_count = ceil(accounts/self.policy['pilot_paths_per_model'])
        seconds = measured['wall_seconds']*batch_count*safety
        cpu_seconds = measured['tree_cpu_seconds']*batch_count*safety
        startup = self.calibration['performance']['startup_wall_seconds']
        output_reserve = self.policy['output_reserve_seconds']
        remaining_seconds = settings['budget_seconds'] - elapsed
        rss = ceil(self.calibration['performance']['peak_tree_rss_bytes']*safety)
        scratch = max(ceil(measured['peak_temporary_disk_bytes']*safety),
                      self.policy['max_workers']*self.planning.get('expected_temporary_disk_bytes', 0))
        evidence = ceil(self.calibration['evidence_bytes_per_account']*accounts*safety)
        scratch_free = shutil.disk_usage(temporary).free
        output_free = shutil.disk_usage(output).free
        memory_free = self.psutil.virtual_memory().available
        reserve = self.policy['minimum_free_after_bytes']
        same_volume = Path(temporary).stat().st_dev == Path(output).stat().st_dev
        scratch_required = scratch + reserve + (evidence if same_volume else 0)
        output_required = evidence + reserve + (scratch if same_volume else 0)
        reasons = []
        if seconds + output_reserve > remaining_seconds:
            reasons.append('WALL_BUDGET')
        if rss > memory_free:
            reasons.append('AVAILABLE_MEMORY')
        if scratch_required > scratch_free:
            reasons.append('SCRATCH_DISK_FREE')
        if output_required > output_free:
            reasons.append('EVIDENCE_DISK_FREE')
        return dict(admitted=not reasons, status='RESOURCE_NOT_ADMITTED' if reasons else 'RESOURCE_ADMITTED',
                    reasons=reasons, projected_additional_seconds=seconds,
                    projected_tree_cpu_seconds=cpu_seconds, measured_startup_wall_seconds=startup,
                    output_reserve_seconds=output_reserve, remaining_wall_seconds=remaining_seconds,
                    projected_peak_tree_rss_bytes=rss, available_memory_bytes=memory_free,
                    projected_scratch_bytes=scratch, minimum_scratch_free_bytes=scratch_required,
                    scratch_free_bytes=scratch_free, projected_additional_evidence_bytes=evidence,
                    minimum_output_free_bytes=output_required, output_free_bytes=output_free,
                    cost_safety_factor=safety, same_scratch_and_output_volume=same_volume)


def calibrate(output, *, inputs, receipt, config, rules, definition, diagnostic=None):
    from factorlab.experiments.mc_grid_tail_convergence import run_study

    policy = definition['resources']
    paths = policy['pilot_paths_per_model']
    seed = policy['pilot_seed']
    pilot_settings = definition['experiment'] | dict(seeds=[seed], sizes=[paths],
                                                     budget_seconds=policy['pilot_budget_seconds'])
    pilot_definition = definition | dict(research_id='MC-05-F03-PILOT', experiment=pilot_settings)
    monitor = ResourceMonitor()
    with monitor.measure() as performance:
        result = run_study(output, inputs=inputs, receipt=receipt, config=config, rules=rules,
                           definition=pilot_definition, max_workers=policy['max_workers'],
                           group_observer=monitor.observe, diagnostic=diagnostic)
    models = {group['model']: dict(group, accounts=paths, accounts_per_second=paths/group['wall_seconds'])
              for group in monitor.groups if group['complete']}
    accounts = sum(info['accounts'] for info in models.values())
    planning = definition.get('resource_planning', {})
    evidence_floor = planning.get('expected_evidence_bytes', 0)/planning.get('projected_accounts', 1)
    evidence_per_account = max(directory_bytes(output)/max(accounts, 1), evidence_floor)
    return dict(purpose='FIXED_PRECALIBRATION_ONLY', seed=seed, paths_per_model=paths,
                included_in_formal_statistics=False, admission=result['decisions'], accounts=accounts,
                max_workers=policy['max_workers'], models=models, performance=performance,
                evidence_bytes_per_account=evidence_per_account)
