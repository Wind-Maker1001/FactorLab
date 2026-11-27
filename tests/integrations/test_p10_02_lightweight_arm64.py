"""Regression tests for P10-02 execution lightweighting & cross-architecture readiness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
import os
from pathlib import Path
import subprocess
import sys

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


def test_persistent_worker_reuses_process_enforces_sandbox_and_respects_pool_limit(tmp_path, monkeypatch):
    from factorlab.strategies.loader import LoadedStrategy, StrategyWorkerError

    monkeypatch.setenv('FACTORLAB_STRATEGY_WORKER_POOL_MAX', '2')
    code = '''import os
from decimal import Decimal as D
from factorlab.strategies.protocol import Decision, StrategySpec

class EchoPidStrategy:
    def __init__(self, idx=0, attempt_io=False):
        self.idx = idx
        self.attempt_io = attempt_io
        self.calls = 0

    def describe(self):
        return StrategySpec(f"echo_{self.idx}", "1", "target", ["NVDAUSDT"], [60], ["closed_bar"], 1)

    def decide(self, event, state):
        self.calls += 1
        if self.attempt_io:
            open("/etc/passwd", "r")
        return Decision(target_weight=D("0.1"), state={"pid": os.getpid(), "instance_calls": self.calls, "n": int(state.get("n", 0)) + 1})
'''
    strategies = []
    try:
        for i in range(3):
            sdir = tmp_path / f'strat_{i}'
            sdir.mkdir()
            (sdir / 's.py').write_text(code, encoding='utf-8')
            (sdir / 'strategy.json').write_text(json.dumps({
                'file': 's.py', 'class': 'EchoPidStrategy', 'version': '1', 'parameters': {'idx': i},
            }), encoding='utf-8')
            strategies.append(LoadedStrategy(sdir, trusted=True))

        now = datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc)
        ev = {'kind': 'closed_bar', 'symbol': 'NVDAUSDT', 'event_id': 'e1', 'now': now}
        d1 = strategies[0].decide(ev, {})
        d2 = strategies[0].decide(ev, d1.state)
        # Same isolated worker reused across calls, yet fresh strategy object per call (no hidden instance leakage)
        assert d1.state['pid'] == d2.state['pid']
        assert d1.state['instance_calls'] == 1
        assert d2.state['instance_calls'] == 1
        assert d2.state['n'] == 2

        # Triggering strategies 1 and 2 evicts LRU worker 0 under pool limit=2; subsequent call transparently restarts
        strategies[1].decide(ev, {})
        strategies[2].decide(ev, {})
        d3 = strategies[0].decide(ev, d2.state)
        assert d3.state['n'] == 3
        assert d3.state['pid'] != d1.state['pid']

        # Sandbox still blocks unauthorized filesystem access
        bad_dir = tmp_path / 'bad'
        bad_dir.mkdir()
        (bad_dir / 's.py').write_text(code, encoding='utf-8')
        (bad_dir / 'strategy.json').write_text(json.dumps({
            'file': 's.py', 'class': 'EchoPidStrategy', 'version': '1', 'parameters': {'idx': 9, 'attempt_io': True},
        }), encoding='utf-8')
        bad = LoadedStrategy(bad_dir, trusted=True)
        strategies.append(bad)
        with pytest.raises(StrategyWorkerError, match='PermissionError'):
            bad.decide(ev, {})
    finally:
        for s in strategies:
            s.close()


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
