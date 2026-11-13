"""Measure one existing CLI history run without changing its execution code."""
import json
import os
from pathlib import Path
import platform
import runpy
import shutil
import sys
import threading
from time import perf_counter, process_time
import psutil

ROOT = Path('D:/Code/FactorLab-worktrees/p7-r04b-e01-grid-study')
OUTPUT = ROOT / 'results/p7-r04b-e04-parallel-history'


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'src'))
    from factorlab.experiments import grid_independent as study
    original = study.execute_matrix
    measurements = dict(python=sys.version, executable=sys.executable,
        platform=platform.platform(), logical_cpus=psutil.cpu_count(),
        physical_cpus=psutil.cpu_count(logical=False), memory_bytes=psutil.virtual_memory().total,
        disk_free_before=shutil.disk_usage(ROOT).free, sample_interval_seconds=0.2)
    stop = threading.Event()
    parent = psutil.Process()
    peak_rss = 0
    peak_children = 0

    def sample():
        nonlocal peak_rss, peak_children
        while not stop.is_set():
            children = parent.children(recursive=True)
            rss = parent.memory_info().rss
            live = 0
            for child in children:
                try:
                    rss += child.memory_info().rss
                    live += 1
                except psutil.NoSuchProcess:
                    pass
            peak_rss = max(peak_rss, rss)
            peak_children = max(peak_children, live)
            stop.wait(0.2)

    def measured(*args, **kwargs):
        start = process_time()
        result = original(*args, **kwargs)
        main_cpu = process_time() - start
        record = json.loads((OUTPUT / 'study.json').read_text())
        measurements.update(matrix_main_cpu_seconds=main_cpu,
            worker_account_cpu_seconds=record['process_cpu_seconds']-main_cpu,
            worker_cpu_semantics='Sum of per-account process_time deltas; excludes spawn/import overhead. Derived from executor aggregate minus measured main matrix CPU.',
            worker_cpu_boundary_uncertainty_seconds=0.02)
        return result

    study.execute_matrix = measured
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    start, cpu = perf_counter(), process_time()
    try:
        sys.argv = [str(ROOT/'examples/research/grid_independent_demo.py'),
            '--snapshot', 'D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6',
            '--output', str(OUTPUT)]
        runpy.run_path(sys.argv[0], run_name='__main__')
    finally:
        measurements.update(cli_wall_seconds=perf_counter()-start, cli_main_cpu_seconds=process_time()-cpu)
        stop.set()
        thread.join()
        measurements.update(peak_process_tree_rss_bytes=peak_rss,
            peak_observed_child_processes=peak_children,
            memory_semantics='Peak sampled sum of RSS; shared pages may be counted more than once.',
            output_bytes=sum(p.stat().st_size for p in OUTPUT.rglob('*') if p.is_file()))
        (OUTPUT/'resources.json').write_text(json.dumps(measurements,indent=2)+'\n')
        print(json.dumps(measurements), flush=True)


if __name__ == '__main__':
    main()
