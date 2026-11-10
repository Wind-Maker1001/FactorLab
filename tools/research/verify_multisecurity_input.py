"""Replay qualification and consume every qualified interval with flat targets."""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import pandas as pd
from factorlab.backtest.target import run_target_positions
from factorlab.data.hub import DataHub
from factorlab.data.research_input import ResearchInputAdapter, SAFETY
from factorlab.dataset.identity import file_hash

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root','source-root','manifest-sha256','output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    clock = time.perf_counter()
    adapter = ResearchInputAdapter(args.root, args.source_root, args.manifest_sha256)
    hub = DataHub({'research': adapter})
    rows = intervals = 0
    for record in adapter.quality['securities']:
        for interval in record['intervals']:
            query = dict(symbol=record['symbol'], start=interval['start'], end=interval['end'])
            bars = hub.read('research', 'daily_bars', **query).data
            dates = hub.read('research', 'observed_sessions', **query).data
            targets = bars[['date', 'symbol']].copy()
            targets['target_weight'] = float('nan')
            result = run_target_positions(targets, bars, dates)
            assert len(result['nav']) == interval['sessions']
            assert result['trades'].empty
            rows += len(bars)
            intervals += 1
    metrics = dict(quality_replay='PASS', execution_flat_target_rows=rows, intervals=intervals,
        manifest_sha256=args.manifest_sha256, safety=SAFETY,
        verifier_sha256=file_hash(Path(__file__)), elapsed_seconds=time.perf_counter()-clock)
    if sys.platform == 'win32':
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage',
                'QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        metrics['peak_working_set_bytes'] = counters.PeakWorkingSetSize
    with Path(args.output).open('x', encoding='utf-8') as handle:
        json.dump(metrics, handle, indent=2)
    print(json.dumps(metrics, indent=2))
