"""Regression tests for P10-02 execution lightweighting, isolation, concurrency & ARM64 readiness."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal as D
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / 'src'


def _probe_imports(module_name: str) -> set[str]:
    code = (
        'import json, sys; '
        f'sys.path.insert(0, {str(SRC_ROOT)!r}); '
        f'__import__({module_name!r}); '
        'print(json.dumps(sorted(m.split(".")[0] for m in sys.modules)))'
    )
    out = subprocess.check_output([sys.executable, '-I', '-c', code], text=True)
    return set(json.loads(out))


@pytest.mark.parametrize('module_name', [
    'factorlab.trading',
    'factorlab.strategies.protocol',
    'factorlab.strategies.package',
    'factorlab.strategies.loader',
    'factorlab.strategies.worker',
    'factorlab.strategies.decision_runtime',
    'factorlab.strategies.grid_execution',
    'factorlab.strategies.adaptive_grid_v2',
    'factorlab.integrations.binance_futures_trading.demo_grid_supervisor',
    'factorlab.integrations.binance_futures_trading.demo_instance_owner',
])
def test_binance_and_strategy_core_import_zero_heavy_research_libs(module_name):
    loaded = _probe_imports(module_name)
    forbidden = {'numpy', 'pandas', 'scipy', 'pyarrow', 'sklearn', 'lightgbm', 'xgboost', 'matplotlib'}
    assert not (loaded & forbidden), f'{module_name} unexpectedly imported {sorted(loaded & forbidden)}'


@pytest.mark.parametrize('module_name', [
    'factorlab.integrations.alpaca.cloud_runtime',
    'factorlab.integrations.alpaca.strategy_instances',
])
def test_alpaca_runtime_excludes_ml_and_scipy_imports(module_name):
    loaded = _probe_imports(module_name)
    forbidden = {'scipy', 'sklearn', 'lightgbm', 'xgboost', 'matplotlib'}
    assert not (loaded & forbidden), f'{module_name} unexpectedly imported {sorted(loaded & forbidden)}'


def test_execution_lockfiles_exclude_research_and_gpu_packages():
    forbidden_prefixes = (
        'nvidia-nccl', 'nvidia-cublas', 'nvidia-cuda', 'cupy',
        'xgboost', 'lightgbm', 'scikit-learn', 'scipy', 'matplotlib',
    )
    for rel in (
        'requirements/alpaca-paper-linux-py313.lock.txt',
        'requirements/execution-binance.txt',
        'requirements/execution-tradfi.txt',
        'requirements/execution-dual-linux-py313.lock.txt',
    ):
        lines = [
            line.strip().lower()
            for line in (REPO_ROOT / rel).read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.strip().startswith('#')
        ]
        joined = '\n'.join(lines)
        for bad in forbidden_prefixes:
            assert bad not in joined, f'{rel} must not contain research/GPU dependency {bad}'


def test_deployers_do_not_hardcode_x86_64_interpreter():
    for rel in (
        'tools/deploy_alpaca_paper_runtime.py',
        'tools/deploy_demo_strategy.py',
        'tools/install_alpaca_paper_runtime.sh',
        'tools/install_tradfi_observer.sh',
    ):
        text = (REPO_ROOT / rel).read_text(encoding='utf-8')
        assert 'x86_64' not in text, f'{rel} should detect host architecture instead of hardcoding x86_64'


def _write_stateful_package(pkg_dir: Path, idx: int = 0) -> Path:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    sub = pkg_dir / 'sub'
    sub.mkdir(exist_ok=True)
    (sub / '__init__.py').write_text('', encoding='utf-8')
    (sub / 'helper.py').write_text(
        'SUB_CALLS = 0\n'
        'SUB_ITEMS = []\n'
        'def bump(tag):\n'
        '    global SUB_CALLS\n'
        '    SUB_CALLS += 1\n'
        '    SUB_ITEMS.append(tag)\n'
        '    return SUB_CALLS, len(SUB_ITEMS)\n',
        encoding='utf-8',
    )
    (pkg_dir / 'strategy.py').write_text(
        'import decimal\n'
        'import os\n'
        'from decimal import Decimal as D\n'
        'from factorlab.strategies.protocol import Decision, StrategySpec\n'
        'from .sub import helper\n'
        '\n'
        'MODULE_COUNTER = 0\n'
        'MODULE_BUCKET = []\n'
        '\n'
        'class LeakyStrategy:\n'
        '    CLASS_COUNTER = 0\n'
        '    CLASS_HISTORY = []\n'
        '\n'
        '    def __init__(self, idx=0):\n'
        '        self.idx = idx\n'
        '        self.inst_counter = 0\n'
        '\n'
        '    def describe(self):\n'
        '        return StrategySpec(f"leaky_{self.idx}", "1", "target", ["NVDAUSDT"], [60], ["closed_bar"], 1)\n'
        '\n'
        '    def decide(self, event, state):\n'
        '        global MODULE_COUNTER\n'
        '        MODULE_COUNTER += 1\n'
        '        MODULE_BUCKET.append(event["event_id"])\n'
        '        type(self).CLASS_COUNTER += 1\n'
        '        type(self).CLASS_HISTORY.append(event["event_id"])\n'
        '        self.inst_counter += 1\n'
        '        sub_calls, sub_len = helper.bump(event["event_id"])\n'
        '        prec_before = decimal.getcontext().prec\n'
        '        decimal.getcontext().prec = 6\n'
        '        if event.get("raise_error"):\n'
        '            raise RuntimeError("boom after mutating globals")\n'
        '        weight = D(MODULE_COUNTER + type(self).CLASS_COUNTER + sub_calls) / D("30")\n'
        '        return Decision(\n'
        '            target_weight=weight,\n'
        '            state={\n'
        '                "pid": os.getpid(),\n'
        '                "module_counter": MODULE_COUNTER,\n'
        '                "module_len": len(MODULE_BUCKET),\n'
        '                "class_counter": type(self).CLASS_COUNTER,\n'
        '                "class_len": len(type(self).CLASS_HISTORY),\n'
        '                "inst_counter": self.inst_counter,\n'
        '                "sub_calls": sub_calls,\n'
        '                "sub_len": sub_len,\n'
        '                "prec_before": prec_before,\n'
        '                "checkpoint_n": int(state.get("checkpoint_n", 0)) + 1,\n'
        '            },\n'
        '        )\n',
        encoding='utf-8',
    )
    (pkg_dir / 'strategy.json').write_text(
        json.dumps({
            'file': 'strategy.py',
            'class': 'LeakyStrategy',
            'version': '1',
            'parameters': {'idx': idx},
            'package': True,
        }),
        encoding='utf-8',
    )
    return pkg_dir


def test_p1_strict_per_request_isolation_globals_class_submodules_errors_and_restarts(tmp_path, monkeypatch):
    """P1 regression: identical (event, state) must be immune to globals, class attrs, submodules,
    interpreter context mutations, prior exceptions, pool contention, multiple instances, and reloads.
    """
    from factorlab.strategies.loader import LoadedStrategy, StrategyWorkerError

    monkeypatch.setenv('FACTORLAB_STRATEGY_WORKER_POOL_MAX', '2')
    now = datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc)
    ev = {'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'e1', 'now': now}
    ev_fail = {'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'e_err', 'now': now, 'raise_error': True}

    pkg_a = _write_stateful_package(tmp_path / 'pkg_a', idx=0)
    pkg_b = _write_stateful_package(tmp_path / 'pkg_b', idx=1)

    strat_a = LoadedStrategy(pkg_a, trusted=True)
    strat_b = LoadedStrategy(pkg_b, trusted=True)
    try:
        d1 = strat_a.decide(ev, {})
        d2 = strat_a.decide(ev, {})
        # Exact same inputs must produce identical target weight and isolated state counters == 1
        assert d1.target_weight == d2.target_weight == D('0.1')
        for d in (d1, d2):
            assert d.state['module_counter'] == 1
            assert d.state['module_len'] == 1
            assert d.state['class_counter'] == 1
            assert d.state['class_len'] == 1
            assert d.state['inst_counter'] == 1
            assert d.state['sub_calls'] == 1
            assert d.state['sub_len'] == 1
            assert d.state['prec_before'] == 28
            assert d.state['checkpoint_n'] == 1

        # Exception after mutating globals/submodules/decimal context must not taint next call
        with pytest.raises(StrategyWorkerError, match='RuntimeError: boom after mutating globals'):
            strat_a.decide(ev_fail, {'checkpoint_n': 5})
        d_after_err = strat_a.decide(ev, {'checkpoint_n': 5})
        assert d_after_err.target_weight == d1.target_weight
        assert d_after_err.state['module_counter'] == 1
        assert d_after_err.state['class_counter'] == 1
        assert d_after_err.state['sub_calls'] == 1
        assert d_after_err.state['prec_before'] == 28
        assert d_after_err.state['checkpoint_n'] == 6

        # Multiple instances and pool contention yield identical results
        d_b1 = strat_b.decide(ev, {'checkpoint_n': 5})
        d_b2 = strat_b.decide(ev, {'checkpoint_n': 5})
        assert d_b1.target_weight == d_b2.target_weight == d_after_err.target_weight
        assert d_b1.state['sub_calls'] == d_b2.state['sub_calls'] == 1
    finally:
        strat_a.close()
        strat_b.close()

    # Restart / reload recovery must produce bit-for-bit identical decision
    reloaded = LoadedStrategy(pkg_a, trusted=True)
    try:
        d_reloaded = reloaded.decide(ev, {'checkpoint_n': 5})
        assert d_reloaded.target_weight == d_after_err.target_weight
        assert d_reloaded.state['module_counter'] == 1
        assert d_reloaded.state['class_counter'] == 1
        assert d_reloaded.state['sub_calls'] == 1
        assert d_reloaded.state['checkpoint_n'] == 6
    finally:
        reloaded.close()


def test_p1_lightweight_runtime_profile_and_calendar_timezone_compatibility(tmp_path):
    """Verify P10-11 lightweight runtime_profile skips heavy preimports while warming calendar tz."""
    from factorlab.strategies.loader import LoadedStrategy

    pkg = tmp_path / 'lw_pkg'
    pkg.mkdir()
    (pkg / 'strategy.py').write_text(
        'from decimal import Decimal as D\n'
        'import sys\n'
        'from zoneinfo import ZoneInfo\n'
        'from datetime import datetime\n'
        'from factorlab.strategies.protocol import Decision, StrategySpec\n'
        'class LightweightCalStrategy:\n'
        '    def describe(self):\n'
        '        return StrategySpec("lw_cal", "1", "target", ["NVDAUSDT"], [60], ["closed_bar"], 1)\n'
        '    def decide(self, event, state):\n'
        '        ny = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("America/New_York"))\n'
        '        heavy = sorted(m for m in ("numpy", "pandas", "scipy", "pyarrow") if m in sys.modules)\n'
        '        return Decision(target_weight=D("0.2"), state={"heavy": heavy, "tz": ny.tzname()})\n',
        encoding='utf-8',
    )
    (pkg / 'strategy.json').write_text(
        json.dumps({
            'file': 'strategy.py',
            'class': 'LightweightCalStrategy',
            'version': '1',
            'parameters': {},
            'runtime_profile': 'lightweight',
            'calendar_timezone': 'America/New_York',
        }),
        encoding='utf-8',
    )
    now = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
    ev = {'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'e1', 'now': now}
    strat = LoadedStrategy(pkg, trusted=True)
    try:
        dec = strat.decide(ev, {})
        assert dec.target_weight == D('0.2')
        assert dec.state['heavy'] == []
        assert dec.state['tz'] == 'EDT'
    finally:
        strat.close()


def _write_timed_strategy(pkg_dir: Path, idx: int = 0) -> Path:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / 's.py').write_text(
        'import os\n'
        'import time\n'
        'from decimal import Decimal as D\n'
        'from factorlab.strategies.protocol import Decision, StrategySpec\n'
        'class TimedStrategy:\n'
        '    def __init__(self, idx=0):\n'
        '        self.idx = idx\n'
        '    def describe(self):\n'
        '        return StrategySpec(f"timed_{self.idx}", "1", "target", ["NVDAUSDT"], [60], ["closed_bar"], 1)\n'
        '    def decide(self, event, state):\n'
        '        t0 = time.time_ns()\n'
        '        delay = float(event.get("sleep_sec", 0.0))\n'
        '        if delay > 0:\n'
        '            time.sleep(delay)\n'
        '        if event.get("attempt_io"):\n'
        '            open("/etc/passwd", "r")\n'
        '        if event.get("raise_err"):\n'
        '            raise ValueError("strategy error")\n'
        '        t1 = time.time_ns()\n'
        '        return Decision(target_weight=D("0.1"), state={"pid": os.getpid(), "start_ns": t0, "end_ns": t1, "idx": self.idx})\n',
        encoding='utf-8',
    )
    (pkg_dir / 'strategy.json').write_text(
        json.dumps({'file': 's.py', 'class': 'TimedStrategy', 'version': '1', 'parameters': {'idx': idx}}),
        encoding='utf-8',
    )
    return pkg_dir


@pytest.mark.parametrize('pool_max', [1, 2])
def test_p2_real_live_subprocess_concurrency_strictly_bounded(tmp_path, monkeypatch, pool_max):
    """P2 regression: directly observe real alive OS subprocesses (Popen.poll() is None)
    under POOL_MAX=1 and POOL_MAX=2; verify old process exits before next starts.
    """
    import factorlab.strategies.loader as loader_mod
    from factorlab.strategies.loader import LoadedStrategy, active_worker_count, live_worker_pids

    monkeypatch.setenv('FACTORLAB_STRATEGY_WORKER_POOL_MAX', str(pool_max))
    monkeypatch.delenv('FACTORLAB_STRATEGY_WORKER_VM_MAX', raising=False)
    monkeypatch.delenv('FACTORLAB_STRATEGY_WORKER_LOCK_DIR', raising=False)

    orig_popen = loader_mod.subprocess.Popen
    tracker_lock = threading.Lock()
    spawned: list[subprocess.Popen] = []
    max_observed_alive = 0
    max_observed_reserved = 0

    def _spy_popen(*args, **kwargs):
        nonlocal max_observed_alive, max_observed_reserved
        proc = orig_popen(*args, **kwargs)
        with tracker_lock:
            spawned.append(proc)
            alive = sum(1 for p in spawned if p.poll() is None)
            if alive > max_observed_alive:
                max_observed_alive = alive
            reserved = active_worker_count()
            if reserved > max_observed_reserved:
                max_observed_reserved = reserved
        return proc

    monkeypatch.setattr(loader_mod.subprocess, 'Popen', _spy_popen)

    strategies = [LoadedStrategy(_write_timed_strategy(tmp_path / f't_{i}', i), trusted=True) for i in range(4)]
    now = datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc)
    try:
        # Reset counters after describe() calls
        with tracker_lock:
            spawned.clear()
            max_observed_alive = 0
            max_observed_reserved = 0

        def _run_one(i: int):
            ev = {'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': f'e_{i}', 'now': now, 'sleep_sec': 0.18}
            return strategies[i % len(strategies)].decide(ev, {})

        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(_run_one, range(6)))

        assert len(results) == 6
        assert max_observed_alive <= pool_max, f'live OS subprocesses {max_observed_alive} exceeded POOL_MAX={pool_max}'
        assert max_observed_reserved <= pool_max
        assert all(p.poll() is not None for p in spawned), 'all spawned OS subprocesses must be reaped'
        assert active_worker_count() == 0
        assert live_worker_pids() == ()

        # Also verify via actual execution timestamps inside worker OS processes
        intervals = [(r.state['start_ns'], r.state['end_ns']) for r in results]
        events = []
        for s, e in intervals:
            events.append((s, 1))
            events.append((e, -1))
        events.sort(key=lambda item: (item[0], item[1]))
        cur = 0
        max_overlap = 0
        for _, delta in events:
            cur += delta
            if cur > max_overlap:
                max_overlap = cur
        assert max_overlap <= pool_max, f'real worker execution overlap {max_overlap} exceeded POOL_MAX={pool_max}'
    finally:
        for s in strategies:
            s.close()


def test_p2_close_race_before_spawn_and_during_execution_timeout_and_errors(tmp_path, monkeypatch):
    """P2 regression: cover post-reservation/pre-spawn close race, mid-execution close,
    timeout kill+reap, exception cleanup, and zero leaked capacity or live PIDs.
    """
    import factorlab.strategies.loader as loader_mod
    from factorlab.strategies.loader import LoadedStrategy, StrategyWorkerError, active_worker_count, live_worker_pids

    monkeypatch.setenv('FACTORLAB_STRATEGY_WORKER_POOL_MAX', '1')
    monkeypatch.delenv('FACTORLAB_STRATEGY_WORKER_VM_MAX', raising=False)
    monkeypatch.delenv('FACTORLAB_STRATEGY_WORKER_LOCK_DIR', raising=False)

    now = datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc)
    strat_a = LoadedStrategy(_write_timed_strategy(tmp_path / 'a', 0), trusted=True, timeout=10)
    strat_b = LoadedStrategy(_write_timed_strategy(tmp_path / 'b', 1), trusted=True, timeout=10)
    strat_fast = LoadedStrategy(_write_timed_strategy(tmp_path / 'c', 2), trusted=True, timeout=0.45)

    spawn_count = 0
    orig_popen = loader_mod.subprocess.Popen
    orig_tempdir = loader_mod.tempfile.TemporaryDirectory

    def _counting_popen(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return orig_popen(*args, **kwargs)

    monkeypatch.setattr(loader_mod.subprocess, 'Popen', _counting_popen)

    try:
        # 0. Exact post-reservation / pre-spawn race: slot is reserved, then close() fires before Popen
        strat_race = LoadedStrategy(_write_timed_strategy(tmp_path / 'race', 9), trusted=True, timeout=10)
        spawn_before_race = spawn_count
        reserved_evt = threading.Event()
        proceed_evt = threading.Event()

        def _paused_tempdir(*args, **kwargs):
            reserved_evt.set()
            proceed_evt.wait(timeout=5.0)
            return orig_tempdir(*args, **kwargs)

        monkeypatch.setattr(loader_mod.tempfile, 'TemporaryDirectory', _paused_tempdir)
        race_res: list = []

        def _run_race():
            try:
                strat_race.decide({'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'r1', 'now': now}, {})
                race_res.append('OK')
            except Exception as exc:
                race_res.append(exc)

        t_race = threading.Thread(target=_run_race)
        t_race.start()
        assert reserved_evt.wait(timeout=5.0)
        assert active_worker_count() == 1  # capacity is reserved, subprocess NOT spawned yet

        t_close = threading.Thread(target=strat_race.close)
        t_close.start()
        time.sleep(0.05)  # close() marks _closed and slot.cancelled, waiting for slot release
        proceed_evt.set()
        t_race.join(timeout=5.0)
        t_close.join(timeout=5.0)
        monkeypatch.setattr(loader_mod.tempfile, 'TemporaryDirectory', orig_tempdir)

        assert not t_race.is_alive() and not t_close.is_alive()
        assert len(race_res) == 1 and isinstance(race_res[0], StrategyWorkerError)
        assert spawn_count == spawn_before_race, 'closed strategy must NEVER spawn subprocess after reservation'
        assert active_worker_count() == 0
        assert live_worker_pids() == ()

        # 1. Race: strat_a holds the single slot; strat_b queues waiting, then strat_b.close() is called
        #    before strat_b gets a slot, AND we also verify closing strat_a kills its running subprocess.
        a_result: list = []
        b_result: list = []

        def _run_a():
            try:
                strat_a.decide({'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'a1', 'now': now, 'sleep_sec': 5.0}, {})
                a_result.append('OK')
            except Exception as exc:
                a_result.append(exc)

        def _run_b():
            try:
                strat_b.decide({'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'b1', 'now': now, 'sleep_sec': 0.1}, {})
                b_result.append('OK')
            except Exception as exc:
                b_result.append(exc)

        t_a = threading.Thread(target=_run_a)
        t_a.start()
        deadline = time.monotonic() + 5.0
        while not live_worker_pids() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(live_worker_pids()) == 1

        t_b = threading.Thread(target=_run_b)
        t_b.start()
        time.sleep(0.05)  # t_b is now blocked waiting for pool capacity

        # Close strat_b while waiting: must wake immediately and never spawn a process
        spawns_before_b_close = spawn_count
        strat_b.close()
        t_b.join(timeout=3.0)
        assert not t_b.is_alive()
        assert len(b_result) == 1 and isinstance(b_result[0], StrategyWorkerError)
        assert spawn_count == spawns_before_b_close

        # Calling decide on already-closed strat_b must fail without spawning
        with pytest.raises(StrategyWorkerError, match='closed'):
            strat_b.decide({'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'b2', 'now': now}, {})
        assert spawn_count == spawns_before_b_close

        # Close strat_a while its OS subprocess is sleeping: must terminate+reap and free slot
        strat_a.close()
        t_a.join(timeout=5.0)
        assert not t_a.is_alive()
        assert len(a_result) == 1 and isinstance(a_result[0], StrategyWorkerError)
        assert active_worker_count() == 0
        assert live_worker_pids() == ()

        # 2. Timeout must kill & reap child OS process and release capacity
        with pytest.raises(StrategyWorkerError, match='timed out'):
            strat_fast.decide({'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'to1', 'now': now, 'sleep_sec': 3.0}, {})
        assert active_worker_count() == 0
        assert live_worker_pids() == ()

        # Give normal startup its usual deadline after the forced timeout.
        strat_fast.timeout = strat_a.timeout
        # 3. Exception & sandbox violation must release capacity and leave next call healthy
        with pytest.raises(StrategyWorkerError, match='ValueError: strategy error'):
            strat_fast.decide({'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'er1', 'now': now, 'raise_err': True}, {})
        with pytest.raises(StrategyWorkerError, match='PermissionError'):
            strat_fast.decide({'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'io1', 'now': now, 'attempt_io': True}, {})
        assert active_worker_count() == 0
        assert live_worker_pids() == ()

        # Next normal decision succeeds immediately
        ok = strat_fast.decide({'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'ok1', 'now': now}, {})
        assert ok.target_weight == D('0.1')
        assert active_worker_count() == 0
        assert live_worker_pids() == ()
    finally:
        strat_a.close()
        strat_b.close()
        strat_fast.close()


def test_p2_atexit_shutdown_reaps_inflight_worker_process(tmp_path):
    """Verify _WORKER_POOL.shutdown_all() kills and reaps an in-flight worker subprocess cleanly."""
    pkg = _write_timed_strategy(tmp_path / 'exit_pkg', idx=0)
    driver = (
        'import json, os, sys, threading, time\n'
        'from datetime import datetime, timezone\n'
        f'sys.path.insert(0, {str(SRC_ROOT)!r})\n'
        'from factorlab.strategies.loader import LoadedStrategy, _WORKER_POOL, live_worker_pids, active_worker_count\n'
        f's = LoadedStrategy({str(pkg)!r}, trusted=True, timeout=15)\n'
        'now = datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc)\n'
        'ev = {"kind": "closed_bar", "symbol": "NVDAUSDT", "event_id": "hang", "now": now, "sleep_sec": 10.0}\n'
        't = threading.Thread(target=lambda: _safe_call(s, ev))\n'
        'def _safe_call(st, e):\n'
        '    try:\n'
        '        st.decide(e, {})\n'
        '    except Exception:\n'
        '        pass\n'
        't.start()\n'
        'deadline = time.monotonic() + 5.0\n'
        'while not live_worker_pids() and time.monotonic() < deadline:\n'
        '    time.sleep(0.02)\n'
        'pids_before = live_worker_pids()\n'
        '_WORKER_POOL.shutdown_all()\n'
        't.join(timeout=5.0)\n'
        'print(json.dumps({"pids_before": list(pids_before), "pids_after": list(live_worker_pids()), "active_after": active_worker_count()}))\n'
    )
    out = subprocess.check_output([sys.executable, '-I', '-c', driver], text=True, timeout=20)
    data = json.loads(out)
    assert len(data['pids_before']) == 1
    assert data['pids_after'] == []
    assert data['active_after'] == 0


def test_p2_vm_wide_cross_process_cap_limits_colocated_binance_and_alpaca_hosts(tmp_path):
    """P2 regression: two independent host OS processes (e.g. Binance + Alpaca) each with POOL_MAX=2
    sharing FACTORLAB_STRATEGY_WORKER_LOCK_DIR and FACTORLAB_STRATEGY_WORKER_VM_MAX=1 must never
    overlap live worker execution across the VM.
    """
    pkg = _write_timed_strategy(tmp_path / 'vm_pkg', idx=0)
    lock_dir = tmp_path / 'vm-slots'
    out_dir = tmp_path / 'out'
    out_dir.mkdir()

    host_script = (
        'import json, os, sys, time\n'
        'from concurrent.futures import ThreadPoolExecutor\n'
        'from datetime import datetime, timezone\n'
        'from pathlib import Path\n'
        f'sys.path.insert(0, {str(SRC_ROOT)!r})\n'
        'from factorlab.strategies.loader import LoadedStrategy\n'
        'host_name = sys.argv[1]\n'
        'ready_file = Path(sys.argv[2])\n'
        'go_file = Path(sys.argv[3])\n'
        'result_file = Path(sys.argv[4])\n'
        f'strat = LoadedStrategy({str(pkg)!r}, trusted=True, timeout=20)\n'
        'ready_file.write_text("ready", encoding="utf-8")\n'
        'while not go_file.exists():\n'
        '    time.sleep(0.01)\n'
        'now = datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc)\n'
        'def _call(i):\n'
        '    ev = {"kind": "closed_bar", "symbol": "NVDAUSDT", "event_id": f"{host_name}_{i}", "now": now, "sleep_sec": 0.18}\n'
        '    d = strat.decide(ev, {})\n'
        '    return {"host": host_name, "pid": d.state["pid"], "start_ns": d.state["start_ns"], "end_ns": d.state["end_ns"]}\n'
        'with ThreadPoolExecutor(max_workers=2) as ex:\n'
        '    rows = list(ex.map(_call, range(2)))\n'
        'strat.close()\n'
        'result_file.write_text(json.dumps(rows), encoding="utf-8")\n'
    )
    script_path = tmp_path / 'host_driver.py'
    script_path.write_text(host_script, encoding='utf-8')

    env = dict(os.environ)
    env['FACTORLAB_STRATEGY_WORKER_POOL_MAX'] = '2'
    env['FACTORLAB_STRATEGY_WORKER_VM_MAX'] = '1'
    env['FACTORLAB_STRATEGY_WORKER_LOCK_DIR'] = str(lock_dir)

    go_file = tmp_path / 'go.flag'
    ready_bin = tmp_path / 'ready_binance.flag'
    ready_alp = tmp_path / 'ready_alpaca.flag'
    res_bin = out_dir / 'binance.json'
    res_alp = out_dir / 'alpaca.json'

    p_bin = subprocess.Popen([sys.executable, str(script_path), 'binance', str(ready_bin), str(go_file), str(res_bin)], env=env)
    p_alp = subprocess.Popen([sys.executable, str(script_path), 'alpaca', str(ready_alp), str(go_file), str(res_alp)], env=env)
    try:
        deadline = time.monotonic() + 20.0
        while not (ready_bin.exists() and ready_alp.exists()) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_bin.exists() and ready_alp.exists()
        go_file.write_text('go', encoding='utf-8')
        assert p_bin.wait(timeout=30) == 0
        assert p_alp.wait(timeout=30) == 0
    finally:
        for p in (p_bin, p_alp):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)

    all_rows = json.loads(res_bin.read_text(encoding='utf-8')) + json.loads(res_alp.read_text(encoding='utf-8'))
    assert len(all_rows) == 4
    events = []
    for row in all_rows:
        events.append((row['start_ns'], 1))
        events.append((row['end_ns'], -1))
    events.sort(key=lambda item: (item[0], item[1]))
    cur = 0
    max_vm_overlap = 0
    for _, delta in events:
        cur += delta
        if cur > max_vm_overlap:
            max_vm_overlap = cur
    assert max_vm_overlap == 1, f'cross-process VM worker overlap {max_vm_overlap} exceeded VM_MAX=1'


@pytest.mark.parametrize('ignore_sigterm', [False, True], ids=['terminate', 'ignore_sigterm_kill_fallback'])
def test_p2_large_stdin_pipe_full_timeout_terminates_without_deadlock_and_releases_slots(
    tmp_path, monkeypatch, ignore_sigterm
):
    """P2 regression: payload > OS pipe buffer (> 256 KiB, < 4 MiB limit) sent to a real
    subprocess that never reads stdin and sleeps 15s (optionally ignoring SIGTERM) must
    time out promptly without deadlocking on buffered stdin.close(), reap the child,
    join the writer/reader threads, release host & VM slots, and let normal decisions continue.
    """
    import factorlab.strategies.loader as loader_mod
    from factorlab.strategies.loader import (
        LoadedStrategy,
        StrategyWorkerError,
        _try_acquire_vm_slot,
        active_worker_count,
        live_worker_pids,
    )

    lock_dir = tmp_path / 'vm-slots'
    monkeypatch.setenv('FACTORLAB_STRATEGY_WORKER_POOL_MAX', '1')
    monkeypatch.setenv('FACTORLAB_STRATEGY_WORKER_VM_MAX', '1')
    monkeypatch.setenv('FACTORLAB_STRATEGY_WORKER_LOCK_DIR', str(lock_dir))

    # First construct a valid LoadedStrategy using the real worker so describe() succeeds.
    pkg = _write_timed_strategy(tmp_path / f'pipe_pkg_{int(ignore_sigterm)}', idx=0)
    strat = LoadedStrategy(pkg, trusted=True, timeout=10.0)
    strat.timeout = 0.3

    # Stub worker script: optionally ignores SIGTERM, never reads stdin, records its PID, sleeps 15s.
    pid_file = tmp_path / f'child_{int(ignore_sigterm)}.pid'
    stub_worker = tmp_path / f'stalled_worker_{int(ignore_sigterm)}.py'
    stub_worker.write_text(
        'import os, signal, time\n'
        'from pathlib import Path\n'
        f'if {ignore_sigterm!r} and hasattr(signal, "SIGTERM"):\n'
        '    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        f'Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")\n'
        'time.sleep(15.0)\n',
        encoding='utf-8',
    )

    real_worker = loader_mod.WORKER
    # > 256 KiB payload exceeds both Linux (64 KiB) and Windows (4-64 KiB) OS pipe buffers,
    # while staying well within the 4 MiB protocol limit.
    large_blob = 'X' * (384 * 1024)
    now = datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc)
    stalled_event = {
        'kind': 'closed_bar',
        'symbol': 'NVDAUSDT',
        'event_id': 'pipe-full-timeout',
        'now': now,
        'blob': large_blob,
    }

    spawned_procs: list[subprocess.Popen] = []
    orig_popen = loader_mod.subprocess.Popen

    def _track_popen(*args, **kwargs):
        p = orig_popen(*args, **kwargs)
        spawned_procs.append(p)
        return p

    monkeypatch.setattr(loader_mod.subprocess, 'Popen', _track_popen)
    try:
        monkeypatch.setattr(loader_mod, 'WORKER', stub_worker)
        t0 = time.monotonic()
        with pytest.raises(StrategyWorkerError, match='timed out'):
            strat.decide(stalled_event, {})
        elapsed = time.monotonic() - t0

        # Must finish promptly (0.3s timeout + <= 0.5s SIGTERM grace + kill/reap),
        # nowhere near the 15s child sleep.
        max_allowed = 4.0 if ignore_sigterm else 3.0
        assert elapsed < max_allowed, (
            f'timeout cleanup stalled for {elapsed:.2f}s (ignore_sigterm={ignore_sigterm})'
        )
        assert len(spawned_procs) == 1
        assert spawned_procs[0].poll() is not None, 'child OS process must be confirmed exited'

        # Verify OS-level child PID is actually gone and repeated cleanup is idempotent.
        loader_mod._terminate_and_reap(spawned_procs[0])
        if pid_file.exists():
            child_pid = int(pid_file.read_text(encoding='utf-8').strip())
            if os.name != 'nt':
                with pytest.raises(ProcessLookupError):
                    os.kill(child_pid, 0)

        # Verify stdin writer and stdout/stderr reader threads have exited cleanly.
        worker_threads = [
            t for t in threading.enumerate()
            if t.name in {'factorlab-worker-stdin', 'factorlab-worker-stdout', 'factorlab-worker-stderr'}
            and t.is_alive()
        ]
        assert worker_threads == [], f'lingering worker IO threads: {[t.name for t in worker_threads]}'

        # Verify host pool slot and VM file-lock slot are immediately available again.
        assert active_worker_count() == 0
        assert live_worker_pids() == ()
        slot_lock_path = lock_dir / 'worker-slot-0.lock'
        lease = _try_acquire_vm_slot(slot_lock_path)
        assert lease is not None, 'VM file-lock slot was not released after timeout cleanup'
        lease.release()

        # Restore real worker and verify subsequent normal strategy decision succeeds immediately.
        monkeypatch.setattr(loader_mod, 'WORKER', real_worker)
        strat.timeout = 10
        normal_ev = {'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'after-timeout-ok', 'now': now}
        dec = strat.decide(normal_ev, {})
        assert dec.target_weight == D('0.1')
        assert active_worker_count() == 0
        assert live_worker_pids() == ()
    finally:
        monkeypatch.setattr(loader_mod, 'WORKER', real_worker)
        strat.close()


def test_p2_large_stdin_pipe_full_active_close_terminates_without_deadlock(tmp_path, monkeypatch):
    """Verify explicit close() while stdin writer is blocked on a full pipe also terminates
    cleanly (including SIGTERM-ignoring child), joins IO threads, and releases host & VM slots.
    """
    import factorlab.strategies.loader as loader_mod
    from factorlab.strategies.loader import (
        LoadedStrategy,
        StrategyWorkerError,
        _try_acquire_vm_slot,
        active_worker_count,
        live_worker_pids,
    )

    lock_dir = tmp_path / 'vm-slots-close'
    monkeypatch.setenv('FACTORLAB_STRATEGY_WORKER_POOL_MAX', '1')
    monkeypatch.setenv('FACTORLAB_STRATEGY_WORKER_VM_MAX', '1')
    monkeypatch.setenv('FACTORLAB_STRATEGY_WORKER_LOCK_DIR', str(lock_dir))

    pkg = _write_timed_strategy(tmp_path / 'pipe_close_pkg', idx=1)
    strat = LoadedStrategy(pkg, trusted=True, timeout=20.0)
    real_worker = loader_mod.WORKER
    stub_worker = tmp_path / 'stalled_close_worker.py'
    stub_worker.write_text(
        'import signal, time\n'
        'if hasattr(signal, "SIGTERM"):\n'
        '    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        'time.sleep(15.0)\n',
        encoding='utf-8',
    )

    now = datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc)
    stalled_event = {
        'kind': 'closed_bar',
        'symbol': 'NVDAUSDT',
        'event_id': 'pipe-full-close',
        'now': now,
        'blob': 'Y' * (384 * 1024),
    }

    res: list = []

    def _run():
        try:
            strat.decide(stalled_event, {})
            res.append('OK')
        except Exception as exc:
            res.append(exc)

    try:
        monkeypatch.setattr(loader_mod, 'WORKER', stub_worker)
        t = threading.Thread(target=_run)
        t.start()
        deadline = time.monotonic() + 5.0
        while not live_worker_pids() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(live_worker_pids()) == 1
        time.sleep(0.1)  # Allow _write_stdin to fill the OS pipe buffer and block

        t0 = time.monotonic()
        strat.close()
        t.join(timeout=5.0)
        elapsed = time.monotonic() - t0

        assert not t.is_alive()
        assert elapsed < 4.0, f'active close() stalled for {elapsed:.2f}s'
        assert len(res) == 1 and isinstance(res[0], StrategyWorkerError)
        assert active_worker_count() == 0
        assert live_worker_pids() == ()
        # Repeated close() must be idempotent.
        strat.close()

        worker_threads = [
            th for th in threading.enumerate()
            if th.name in {'factorlab-worker-stdin', 'factorlab-worker-stdout', 'factorlab-worker-stderr'}
            and th.is_alive()
        ]
        assert worker_threads == []

        lease = _try_acquire_vm_slot(lock_dir / 'worker-slot-0.lock')
        assert lease is not None
        lease.release()
    finally:
        monkeypatch.setattr(loader_mod, 'WORKER', real_worker)
        strat.close()


def test_market_cache_lru_still_detects_tampering(tmp_path):
    import pandas as pd
    from factorlab.integrations.alpaca.market_data.cache import MarketCache, Series

    cache = MarketCache(tmp_path / 'cache', max_cached=2)
    t0 = pd.Timestamp('2026-01-02T14:30:00Z')
    frame = pd.DataFrame([{
        'date': pd.Timestamp('2026-01-02'),
        'instrument_id': 'id-AAPL',
        'event_at': t0,
        'close': 180.0,
    }])
    series = Series('id-AAPL', 'AAPL', '1Day', 'iex', 'raw', '2026-01-02')
    meta = cache.publish(series, frame, start='2026-01-02', end='2026-01-02',
                         calendar_version='test', captured_at=t0)
    loaded1, _ = cache.load(series)
    loaded1.loc[0, 'close'] = 999.0  # Mutating returned copy must not poison cache
    loaded2, _ = cache.load(series)
    assert float(loaded2.loc[0, 'close']) == 180.0

    # Tampering with parquet bytes on disk must still fail SHA-256 check despite LRU hit
    parquet = tmp_path / 'cache' / meta['generation'] / 'bars.parquet'
    parquet.write_bytes(parquet.read_bytes() + b'tamper')
    with pytest.raises(ValueError, match='cache generation integrity failure'):
        cache.load(series)


def test_grid_timestamp_preserves_nanosecond_ordering_and_replay_detection():
    """Verify stdlib naive timestamp parser preserves nanosecond precision without pandas,
    rejecting out-of-order sub-microsecond observations, replay timestamp changes, and
    unsupported sub-nanosecond precision.
    """
    from factorlab.strategies.grid import GridConfig, _parse_naive_timestamp
    from factorlab.strategies.grid_execution import GridAccount, GridExecution, GridRules
    from factorlab.trading.contracts import Market

    ts_hi = _parse_naive_timestamp('2026-01-01T00:00:00.123456900')
    ts_lo = _parse_naive_timestamp('2026-01-01T00:00:00.123456100')
    assert ts_lo < ts_hi
    assert ts_hi.isoformat() == '2026-01-01T00:00:00.123456900'
    assert ts_lo.isoformat() == '2026-01-01T00:00:00.123456100'
    with pytest.raises(ValueError, match='sub-nanosecond'):
        _parse_naive_timestamp('2026-01-01T00:00:00.1234567891')

    cfg = GridConfig(
        mode='arithmetic', lower=95.0, upper=105.0, levels=4,
        initial_cash=5000.0, initial_inventory=0, initial_price=100.0,
        quantity=1, max_inventory=10, max_cash_committed=2000.0,
        breakout='pause', order_ttl=50,
    )
    rules = GridRules(D('0.1'), D('0.01'), D('0.01'), D('0.01'), D('5'))
    grid = GridExecution(
        cfg, rules, deployment_id='p10ns1', account_id='acct-ns',
        market=Market.BINANCE_USDM, instrument_id='NVDAUSDT', initial_equity=D('5000'),
    )
    acct = GridAccount(
        inventory=D('0'), available=D('5000'), equity=D('5000'),
        committed=D('0'), turnover=D('0'), account_id='acct-ns',
        instrument_id='NVDAUSDT', market=Market.BINANCE_USDM,
    )
    ev1 = {'id': 'obs-1', 'time': '2026-01-01T00:00:00.123456900', 'price': D('100'), 'volume': 1}
    grid.observe(ev1, acct, delivery=1)
    assert grid.timestamp.isoformat() == '2026-01-01T00:00:00.123456900'

    # Out-of-order observation differing only in nanoseconds must be rejected
    ev_out_of_order = {'id': 'obs-2', 'time': '2026-01-01T00:00:00.123456100', 'price': D('100'), 'volume': 1}
    with pytest.raises(ValueError, match='observation time regression'):
        grid.observe(ev_out_of_order, acct, delivery=2)

    # Delivery replay with nanosecond-only timestamp change must be rejected as conflict
    ev1_mutated = {'id': 'obs-1', 'time': '2026-01-01T00:00:00.123456800', 'price': D('100'), 'volume': 1}
    with pytest.raises(ValueError):
        grid.observe(ev1_mutated, acct, delivery=1)


@pytest.mark.parametrize('text,nanosecond', [
    ('2026-01-01T00:00:00.123456', 0),
    ('2026-01-01T00:00:00.123456789', 789),
    ('1969-12-31T23:59:59.999999999', 999),
])
def test_grid_timestamp_copy_and_pickle_preserve_value_and_hash(text, nanosecond):
    from copy import copy, deepcopy
    from datetime import datetime
    import pickle

    from factorlab.strategies.grid import _parse_naive_timestamp

    stamp = _parse_naive_timestamp(text)
    copies = [copy(stamp), deepcopy(stamp)]
    copies.extend(pickle.loads(pickle.dumps(stamp, protocol))
                  for protocol in range(pickle.HIGHEST_PROTOCOL + 1))
    by_timestamp = {stamp: 'original'}
    for restored in copies:
        assert restored.isoformat() == text and restored.nanosecond == nanosecond
        assert by_timestamp[restored] == 'original'
    if nanosecond == 0:
        standard = datetime.fromisoformat(text)
        assert stamp == standard and by_timestamp[standard] == 'original'
