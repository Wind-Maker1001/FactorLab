"""P8-S04 offline acceptance: S01 FACTORLAB_DECISION_V1 + S03 versioned deployment."""
from contextlib import ExitStack, closing, contextmanager
from datetime import timedelta
from decimal import Decimal as D
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_cloud_monitor import sanitize_sample
from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import GridConsumer
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, open_grid_session
from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.integrations.binance_futures_trading.demo_strategy_release import ReleaseConsumer
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.strategy_deployment import (
    DeploymentController, DeploymentStore, LEGACY,
)
from factorlab.integrations.binance_futures_trading.strategy_package import (
    DECISION_INTERFACE, INTERFACE, build_package, install_package, strategy_package_identity,
)
from factorlab.strategies.loader import LoadedStrategy, StrategyWorkerError
from factorlab.trading.paper import Quote
from factorlab.trading.strategy import ClosedBar

from test_binance_demo_grid_network import network_grid, writes, observe
from test_binance_demo_network import NetworkFixture, wired
from test_demo_grid_supervisor import Control


EXAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'protocol_strategy'
COMMIT = '5' * 40


def make_bar(minute, close, *, high=None, low=None, open_price=None, interval=60):
    opened = NOW + timedelta(seconds=(minute - 1) * interval)
    closed = NOW + timedelta(seconds=minute * interval)
    c = D(str(close))
    o = c if open_price is None else D(str(open_price))
    h = max(o, c) if high is None else D(str(high))
    lo = min(o, c) if low is None else D(str(low))
    return ClosedBar('NVDAUSDT', opened, closed, o, h, lo, c, D('10'), interval, 'BINANCE_USDM_DEMO_REST')


def build_decision_package(tmp_path, name, source_dir, *, strategy_id, strategy_version,
                           entrypoint, files, configuration, execution_mode=None,
                           package=False, interval_seconds=60, state_version=1,
                           dependencies=(), migration='wait_flat', reduction=None):
    manifest = dict(
        schema_version=1,
        interface=DECISION_INTERFACE,
        environment='BINANCE_USDM_DEMO',
        symbol='NVDAUSDT',
        code_commit=COMMIT,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        state_version=state_version,
        interval_seconds=interval_seconds,
        entrypoint=entrypoint,
        files=list(files),
        dependencies=list(dependencies),
        configuration=configuration,
        migration=migration,
        evidence=dict(kind='SOFTWARE_CONFORMANCE_ONLY', research_qualification=False, tag=name),
    )
    if execution_mode is not None:
        manifest['execution_mode'] = execution_mode
    if package:
        manifest['package'] = True
    if reduction is not None:
        manifest['reduction'] = reduction
    archive = tmp_path / f'{name}.zip'
    built = build_package(source_dir, manifest, archive)
    return archive, built


def promote_candidate(manifest, selection_id, *, expected_active=LEGACY):
    return dict(
        schema_version=1,
        decision='PROMOTE',
        qualified=True,
        strictly_better=True,
        selection_id=selection_id,
        expected_active=expected_active,
        package_id=manifest['package_id'],
        evidence=manifest['evidence'],
    )


@pytest.fixture
def harness(network_grid, monkeypatch):
    root, credentials, factory, _, fake, requests = network_grid
    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(credentials.read_config(), opening_equity=D('1000'),
                                 stop_epoch=runtime.stop_epoch())
    at = NOW
    control = Control()
    authority = SimpleNamespace(allowed=True)
    monkeypatch.setattr(DemoSession, '_now', lambda _: at)
    transport = NetworkFixture.__call__
    from time import monotonic as real_monotonic
    start_mono = real_monotonic()

    def market(self, request, timeout):
        response = transport(self, request, timeout)
        if request.path.endswith('/fapi/v1/time'):
            payload = json.loads(response.body)
            payload['serverTime'] = payload['serverTime'] + int((real_monotonic() - start_mono) * 1000)
            return Response(response.status, response.headers, json.dumps(payload).encode())
        if request.path.endswith(('bookTicker', 'premiumIndex')):
            payload = json.loads(response.body)
            payload['time'] = int(at.timestamp() * 1000)
            return Response(response.status, response.headers, json.dumps(payload).encode())
        return response

    monkeypatch.setattr(NetworkFixture, '__call__', market)

    @contextmanager
    def open_router():
        with ExitStack() as stack:
            bridge = stack.enter_context(open_grid_session(root, credentials, factory, continuous=True))
            base = GridConsumer(bridge, control, clock=lambda: at, authority=lambda: authority.allowed,
                                ensure_campaign=bridge.ensure_campaign)
            base.supervisor = SimpleNamespace(collection=None, feed=SimpleNamespace(seconds=60))
            router = ReleaseConsumer(base, root, stack)
            yield router

    def set_time(minute, second=0):
        nonlocal at
        at = NOW + timedelta(minutes=minute, seconds=second)

    def quote(router, price='150'):
        router.base.latest_book = router.bridge.session._fresh_book()
        return router.quote(Quote('NVDAUSDT', D(str(price)), at))

    def install(archive):
        return install_package(archive, root / 'strategy-releases')

    return SimpleNamespace(
        root=root, fake=fake, requests=requests, control=control, authority=authority,
        open=open_router, set_time=set_time, quote=quote, install=install,
        now=lambda: at,
    )


# --- A. Protocol compatibility, manifest validation & worker isolation ---

def test_a_manifest_validation_identity_and_isolation(tmp_path, monkeypatch):
    archive, manifest = build_decision_package(
        tmp_path, 'limit-once', EXAMPLES,
        strategy_id='limit_once', strategy_version='1.0.0',
        entrypoint='limit_once:LimitOnce', files=['limit_once.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, quantity='.04', offset='5'),
        execution_mode='orders',
    )
    release, installed = install_package(archive, tmp_path / 'releases')
    assert installed == manifest
    ident = strategy_package_identity(installed)
    assert ident['strategy_id'] == 'limit_once'
    assert ident['strategy_version'] == '1.0.0'
    assert ident['protocol_version'] == DECISION_INTERFACE
    assert ident['execution_mode'] == 'orders'
    assert ident['interval_seconds'] == 60
    assert ident['instrument'] == 'NVDAUSDT'

    with pytest.raises(ValueError, match='trusted=True'):
        LoadedStrategy.from_strategy_package(release, trusted=False)

    monkeypatch.setenv('BINANCE_DEMO_API_SECRET', 'sentinel-secret-do-not-leak')
    loaded = LoadedStrategy.from_strategy_package(release, trusted=True)
    assert loaded.spec.strategy_id == 'limit_once'
    assert loaded.binding['package_id'] == manifest['package_id']
    assert loaded.binding['protocol'] == DECISION_INTERFACE

    # Mismatched execution_mode or pinned dependency must fail validation before activation.
    bad_mode_archive, _ = build_decision_package(
        tmp_path, 'bad-mode', EXAMPLES,
        strategy_id='limit_once', strategy_version='1.0.0',
        entrypoint='limit_once:LimitOnce', files=['limit_once.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, quantity='.04', offset='5'),
        execution_mode='target',
    )
    bad_release, _ = install_package(bad_mode_archive, tmp_path / 'releases')
    with pytest.raises(ValueError, match='execution_mode'):
        LoadedStrategy.from_strategy_package(bad_release, trusted=True)

    bad_dep_archive, _ = build_decision_package(
        tmp_path, 'bad-dep', EXAMPLES,
        strategy_id='limit_once', strategy_version='1.0.0',
        entrypoint='limit_once:LimitOnce', files=['limit_once.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, quantity='.04', offset='5'),
        dependencies=['nonexistent-factorlab-pkg==9.9.9'],
    )
    bad_dep_release, _ = install_package(bad_dep_archive, tmp_path / 'releases')
    with pytest.raises(StrategyWorkerError):
        LoadedStrategy.from_strategy_package(bad_dep_release, trusted=True)

    # Probe secret isolation inside worker.
    probe_dir = tmp_path / 'probe_src'
    probe_dir.mkdir()
    (probe_dir / 'probe.py').write_text(
        'import os\n'
        'from factorlab.strategies.protocol import Decision, StrategySpec\n'
        'class Probe:\n'
        '    def describe(self):\n'
        '        assert "BINANCE_DEMO_API_SECRET" not in os.environ\n'
        '        return StrategySpec("probe", "1", "orders", ["NVDAUSDT"], [60], ["quote"])\n'
        '    def decide(self, event, state):\n'
        '        return Decision(state={"env_clean": "BINANCE_DEMO_API_SECRET" not in os.environ})\n',
        encoding='utf-8',
    )
    probe_archive, _ = build_decision_package(
        tmp_path, 'probe', probe_dir,
        strategy_id='probe', strategy_version='1',
        entrypoint='probe:Probe', files=['probe.py'], configuration={},
    )
    probe_release, _ = install_package(probe_archive, tmp_path / 'releases')
    probe_loaded = LoadedStrategy.from_strategy_package(probe_release, trusted=True)
    decision = probe_loaded.decide(dict(kind='quote', symbol='NVDAUSDT'), {})
    assert decision.state == {'env_clean': True}


def test_a_legacy_evaluate_v1_and_c06_sanitize_remain_compatible(tmp_path, harness):
    src = tmp_path / 'hold_src'
    src.mkdir()
    (src / 'hold.py').write_text(
        'def evaluate(context, state):\n'
        '    return {"state": {"n": state.get("n", 0) + 1}, "intents": []}\n',
        encoding='utf-8',
    )
    manifest = dict(
        schema_version=1, interface=INTERFACE, environment='BINANCE_USDM_DEMO', symbol='NVDAUSDT',
        code_commit=COMMIT, state_version=1, entrypoint='hold:evaluate', files=['hold.py'],
        dependencies=[], configuration={}, migration='wait_flat',
        evidence=dict(kind='SOFTWARE_CONFORMANCE_ONLY', research_qualification=False),
    )
    archive = tmp_path / 'hold.zip'
    built = build_package(src, manifest, archive)
    harness.install(archive)
    with harness.open() as router:
        router.store.request(built, promote_candidate(built, 'legacy-hold-1'))
        router.before_poll()
        assert router.store.snapshot()['active'] == built['package_id']
        status = router.status()
        assert status['strategy_release']['active_protocol'] == INTERFACE
        sample = sanitize_sample(dict(heartbeat_at=1.0, strategy_release=status['strategy_release']))
        assert sample['strategy_release']['active'] == built['package_id']


# --- B. S01 DualMA through StrategyPackage ---

def test_b_dual_ma_package_target_weight_and_execution(tmp_path, harness):
    archive, manifest = build_decision_package(
        tmp_path, 'dual-ma', EXAMPLES,
        strategy_id='dual_ma', strategy_version='1',
        entrypoint='dual_ma:DualMAAdapter', files=['dual_ma.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, source='BINANCE_USDM_DEMO_REST', max_weight='.01'),
        execution_mode='target',
    )
    harness.install(archive)
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'dual-ma-1'))
        router.before_poll()
        assert router.store.snapshot()['active'] == manifest['package_id']
        # Warm up 20 historical bars (DualMAStrategy.history_limit=21; must NOT create executable pending orders).
        harness.set_time(21, 1)
        for m in range(1, 21):
            router.historical_bar(make_bar(m, '150' if m < 16 else '148'))
        harness.quote(router, '150')
        assert writes(harness.requests) == []

        # Fresh closed bar #21 with strong uptrend triggers DualMA BUY target_weight.
        harness.set_time(21, 2)
        assert router.closed_bar(make_bar(21, '165'))
        harness.set_time(21, 3)
        harness.quote(router, '150')
        posts = writes(harness.requests)
        assert len(posts) == 1
        assert posts[0]['side'] == ['BUY']
        assert posts[0]['reduceOnly'] == ['false']
        client_id = posts[0]['newClientOrderId'][0]
        harness.fake.trade_update(client_id, posts[0]['quantity'][0], '150')
        router.before_poll()
        assert router.bridge.session.status()['orders'][client_id]['status'] == 'FILLED'
        assert router.bridge.runtime.consumption(router.bridge.runtime.continuous_authorization())['posts'] == 1
        sample = sanitize_sample(dict(heartbeat_at=1.0, strategy_release=router.status()['strategy_release']))
        assert sample['strategy_release']['active_protocol'] == DECISION_INTERFACE


# --- C. S01 CloseMomentum versioned package (relative import) full target lifecycle ---

def test_c_close_momentum_package_full_buy_and_sell_lifecycle(tmp_path, harness):
    archive, manifest = build_decision_package(
        tmp_path, 'close-momentum', EXAMPLES / 'close_momentum_v1',
        strategy_id='close_momentum', strategy_version='1.0.0',
        entrypoint='__init__:CloseMomentum', files=['__init__.py', 'decision.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, weight='.01'),
        execution_mode='target', package=True,
    )
    harness.install(archive)
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'cm-1'))
        router.before_poll()
        assert router.store.snapshot()['active'] == manifest['package_id']

        # Bar 1: baseline close 150 (history_limit=2 -> no signal yet)
        harness.set_time(1, 1)
        assert router.closed_bar(make_bar(1, '150'))
        harness.set_time(1, 2)
        harness.quote(router, '150')
        assert writes(harness.requests) == []

        # Bar 2: close rises 150 -> 152 => BUY target_weight=0.01
        harness.set_time(2, 1)
        assert router.closed_bar(make_bar(2, '152'))
        harness.set_time(2, 2)
        harness.quote(router, '150')
        posts = writes(harness.requests)
        assert len(posts) == 1
        buy_id = posts[0]['newClientOrderId'][0]
        buy_qty = posts[0]['quantity'][0]
        assert posts[0]['side'] == ['BUY']
        harness.fake.trade_update(buy_id, buy_qty, '150')
        router.before_poll()
        assert router.bridge.session.status()['positions'][0]['quantity'] == D(buy_qty)

        # Bar 3: close drops 152 -> 149 => target_weight=0 => reduce-only SELL closes position
        harness.set_time(3, 1)
        assert router.closed_bar(make_bar(3, '149'))
        harness.set_time(3, 2)
        harness.quote(router, '150')
        posts = writes(harness.requests)
        assert len(posts) == 2
        sell_id = posts[1]['newClientOrderId'][0]
        assert posts[1]['side'] == ['SELL'] and posts[1]['reduceOnly'] == ['true']
        assert posts[1]['quantity'] == [buy_qty]
        harness.fake.trade_update(sell_id, buy_qty, '150')
        router.before_poll()
        assert router.bridge.session.status()['positions'] == []
        assert router.bridge.session.status()['orders'][sell_id]['status'] == 'FILLED'
        assert router.bridge.runtime.consumption(router.bridge.runtime.continuous_authorization())['posts'] == 2


# --- D & G. S01 Grid & LimitCycle native order strategies: BUY, partial fill, SELL/cancel, SQLite restart ---

def test_d_grid_package_partial_fill_cancel_and_restart_recovery(tmp_path, harness):
    archive, manifest = build_decision_package(
        tmp_path, 'grid-s01', EXAMPLES,
        strategy_id='grid', strategy_version='1',
        entrypoint='grid:GridAdapter', files=['grid.py'],
        configuration=dict(
            config=dict(lower=148, upper=152, levels=1, initial_price=150,
                        initial_inventory=0, quantity=1, max_inventory=1,
                        max_cash_committed=7, breakout='pause'),
            rules=dict(quantity_unit='.04', step_size='.01', tick_size='.01',
                       min_quantity='.01', min_notional='5'),
            instrument_id='NVDAUSDT', initial_equity='1000', interval_seconds=60,
        ),
        execution_mode='orders',
    )
    harness.install(archive)
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'grid-s01-1'))
        router.before_poll()
        assert router.store.snapshot()['active'] == manifest['package_id']

        harness.set_time(1, 1)
        assert router.closed_bar(make_bar(1, '150', high='150', low='150'))
        harness.set_time(1, 2)
        harness.quote(router, '150')
        posts = writes(harness.requests)
        assert len(posts) == 1
        buy_id = posts[0]['newClientOrderId'][0]
        assert posts[0]['side'] == ['BUY']
        # Partial fill 0.02 of 0.04
        harness.fake.trade_update(buy_id, '.02', '148')

    # Full SQLite restart before bar 2 closes: reopen session, recover partial fill, cancel via fresh breakout bar.
    harness.set_time(1, 30)
    with harness.open() as router:
        router.before_poll()
        assert router.store.snapshot()['active'] == manifest['package_id']
        order = router.bridge.session.status()['orders'][buy_id]
        assert order['status'] == 'PARTIALLY_FILLED'
        assert order['filled_quantity'] == D('.02')
        assert router.bridge.session.status()['positions'][0]['quantity'] == D('.02')
        # Breakout bar (>152) closes after restart and requests cancellation of resting remainder without new POST.
        harness.set_time(2, 1)
        assert router.closed_bar(make_bar(2, '160', high='160', low='150'))
        harness.set_time(2, 2)
        harness.quote(router, '160')
        deletes = writes(harness.requests, 'DELETE')
        assert len(deletes) == 1 and deletes[0]['origClientOrderId'] == [buy_id]
        router.before_poll()
        assert router.bridge.session.status()['orders'][buy_id]['status'] == 'CANCELED'
        assert router.bridge.session.status()['orders'][buy_id]['filled_quantity'] == D('.02')
        assert len(writes(harness.requests)) == 1


def test_d_g_limit_cycle_partial_fill_sell_cancel_and_restart(tmp_path, harness):
    archive, manifest = build_decision_package(
        tmp_path, 'limit-cycle-sell', EXAMPLES,
        strategy_id='limit_cycle', strategy_version='1.0.0',
        entrypoint='limit_cycle:LimitCycle', files=['limit_cycle.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, quantity='.04',
                           buy_offset='.00', sell_offset='.00', mode='buy_then_sell'),
        execution_mode='orders',
    )
    harness.install(archive)
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'lc-sell-1'))
        router.before_poll()
        assert router.store.snapshot()['active'] == manifest['package_id']
        # LimitCycle declares ['quote', 'order_event'] (no closed_bar); fresh quote executes directly.
        harness.set_time(1, 1)
        harness.quote(router, '150')
        posts = writes(harness.requests)
        assert len(posts) == 1
        entry_id = posts[0]['newClientOrderId'][0]
        assert entry_id.endswith('-entry')
        # Partial fill first, then restart SQLite before final fill.
        harness.fake.trade_update(entry_id, '.02', '150')

    harness.set_time(1, 5)
    with harness.open() as router:
        router.before_poll()
        assert router.bridge.session.status()['orders'][entry_id]['status'] == 'PARTIALLY_FILLED'
        # Replaying quote while partially filled must NOT issue duplicate entry POST or premature exit.
        harness.quote(router, '150')
        assert len(writes(harness.requests)) == 1
        # Complete fill of entry -> next before_poll delivers order_event(FILLED) -> quote issues reduce-only SELL.
        harness.fake.trade_update(entry_id, '.02', '150')
        harness.set_time(1, 10)
        router.before_poll()
        harness.set_time(1, 11)
        harness.quote(router, '150')
        posts = writes(harness.requests)
        assert len(posts) == 2
        exit_id = posts[1]['newClientOrderId'][0]
        assert exit_id.endswith('-exit')
        assert posts[1]['side'] == ['SELL'] and posts[1]['reduceOnly'] == ['true']
        assert posts[1]['quantity'] == ['0.04']
        harness.fake.trade_update(exit_id, '.04', '150')
        harness.set_time(1, 15)
        router.before_poll()
        assert router.bridge.session.status()['positions'] == []
        assert router.bridge.session.status()['orders'][exit_id]['status'] == 'FILLED'


# --- E & F. Same-PID hot switching and automatic rollback on timeout/crash/invalid output ---

def test_e_same_pid_hot_switch_chain_preserves_budget_and_isolates_signals(tmp_path, harness):
    a_arc, a_man = build_decision_package(
        tmp_path, 'lc-a', EXAMPLES,
        strategy_id='limit_cycle', strategy_version='1.0.0',
        entrypoint='limit_cycle:LimitCycle', files=['limit_cycle.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, quantity='.04',
                           buy_offset='-2.00', mode='buy_then_cancel', cancel_after_quotes=5),
        execution_mode='orders',
    )
    b_arc, b_man = build_decision_package(
        tmp_path, 'cm-b', EXAMPLES / 'close_momentum_v1',
        strategy_id='close_momentum', strategy_version='1.0.0',
        entrypoint='__init__:CloseMomentum', files=['__init__.py', 'decision.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, weight='.01'),
        execution_mode='target', package=True,
    )
    harness.install(a_arc)
    harness.install(b_arc)
    with harness.open() as router:
        pid = os.getpid()
        assert router.adapter.boot_id == str(pid)
        # 1. legacy-grid -> S01 LimitCycle (hot)
        router.store.request(a_man, promote_candidate(a_man, 'hot-1', expected_active=LEGACY))
        router.before_poll()
        assert router.store.snapshot()['active'] == a_man['package_id']
        harness.set_time(1, 1)
        harness.quote(router, '150')
        assert len(writes(harness.requests)) == 1
        order_a = writes(harness.requests)[0]['newClientOrderId'][0]

        # 2. S01 LimitCycle (with open BUY) -> S01 CloseMomentum (same PID hot switch cancels old order first)
        router.store.request(b_man, promote_candidate(b_man, 'hot-2', expected_active=a_man['package_id']))
        harness.set_time(1, 5)
        router.before_poll()
        assert router.store.snapshot()['active'] == a_man['package_id']
        assert writes(harness.requests, 'DELETE')[0]['origClientOrderId'] == [order_a]
        harness.set_time(1, 6)
        router.before_poll()
        assert router.store.snapshot()['active'] == b_man['package_id']
        assert router.adapter.boot_id == str(pid)
        # Budget preserved (not reset)
        assert router.bridge.runtime.consumption(router.bridge.runtime.continuous_authorization())['posts'] == 1

        # 3. Roll back to legacy-grid on same PID
        router.store.rollback('restore-a')
        router.before_poll()
        assert router.store.snapshot()['active'] == a_man['package_id']
        router.store.rollback('restore-legacy')
        router.before_poll()
        assert router.store.snapshot()['active'] == LEGACY
        assert router.adapter.boot_id == str(pid)


@pytest.mark.parametrize('fault_kind', ['crash', 'timeout', 'invalid_output'])
def test_f_automatic_rollback_on_worker_crash_timeout_and_invalid_output(tmp_path, harness, monkeypatch, fault_kind):
    stable_arc, stable_man = build_decision_package(
        tmp_path, 'stable-hold', EXAMPLES,
        strategy_id='limit_cycle', strategy_version='1.0.0',
        entrypoint='limit_cycle:LimitCycle', files=['limit_cycle.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, mode='hold'),
        execution_mode='orders',
    )
    bad_dir = tmp_path / f'bad_{fault_kind}'
    bad_dir.mkdir()
    body = {
        'crash': 'import os; os._exit(42)',
        'timeout': 'import time; time.sleep(6)',
        'invalid_output': 'return "not-a-decision"',
    }[fault_kind]
    (bad_dir / 'bad.py').write_text(
        'from factorlab.strategies.protocol import Decision, StrategySpec\n'
        'class BadStrategy:\n'
        '    def describe(self):\n'
        '        return StrategySpec("bad", "1", "orders", ["NVDAUSDT"], [60], ["quote"])\n'
        '    def decide(self, event, state):\n'
        f'        {body}\n',
        encoding='utf-8',
    )
    bad_arc, bad_man = build_decision_package(
        tmp_path, f'bad-{fault_kind}', bad_dir,
        strategy_id='bad', strategy_version='1',
        entrypoint='bad:BadStrategy', files=['bad.py'], configuration={},
        execution_mode='orders',
    )
    harness.install(stable_arc)
    harness.install(bad_arc)
    orig_from_pkg = LoadedStrategy.from_strategy_package
    monkeypatch.setattr(
        LoadedStrategy, 'from_strategy_package',
        classmethod(lambda cls, release_dir, manifest=None, *, trusted=False, timeout=15:
                    orig_from_pkg(release_dir, manifest=manifest, trusted=trusted,
                                  timeout=2.0 if Path(release_dir).name == bad_man['package_id'] else timeout)),
    )
    with harness.open() as router:
        router.store.request(stable_man, promote_candidate(stable_man, f'stable-{fault_kind}'))
        router.before_poll()
        assert router.store.snapshot()['active'] == stable_man['package_id']

        router.store.request(bad_man, promote_candidate(bad_man, f'bad-{fault_kind}',
                                                        expected_active=stable_man['package_id']))
        router.before_poll()
        assert router.store.snapshot()['active'] == bad_man['package_id']

        harness.set_time(1, 1)
        harness.quote(router, '150')
        assert router.store.pending()['phase'] == 'ROLLBACK'
        assert router.store.pending()['reason'].startswith('STRATEGY_EXCEPTION:')
        harness.set_time(1, 2)
        router.before_poll()
        assert router.store.snapshot()['active'] == stable_man['package_id']
        assert router.store.snapshot()['switch']['phase'] == 'ROLLED_BACK'
        assert writes(harness.requests) == []


# --- H & I. Risk-exposure guards during switch + HTTP 200 unconfirmed DELETE retry & #236/#237 regressions ---

def test_h_i_switch_retains_unconfirmed_http200_cancel_retries_after_get_and_blocks_residual_exposure(
    tmp_path, harness, monkeypatch,
):
    a_arc, a_man = build_decision_package(
        tmp_path, 'lc-cancel', EXAMPLES,
        strategy_id='limit_cycle', strategy_version='1.0.0',
        entrypoint='limit_cycle:LimitCycle', files=['limit_cycle.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, quantity='.04',
                           buy_offset='-2.00', mode='buy_then_cancel', cancel_after_quotes=1),
        execution_mode='orders',
    )
    b_arc, b_man = build_decision_package(
        tmp_path, 'lc-next', EXAMPLES,
        strategy_id='limit_cycle', strategy_version='1.0.1',
        entrypoint='limit_cycle:LimitCycle', files=['limit_cycle.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, mode='hold', version='1.0.1'),
        execution_mode='orders',
    )
    harness.install(a_arc)
    harness.install(b_arc)

    # Inject one malformed HTTP 200 DELETE response (PR #237 review regression inside S03 switch).
    corrupt_delete = {'armed': False}
    orig_call = NetworkFixture.__call__

    def corrupting_call(self, request, timeout):
        if request.method == 'DELETE' and corrupt_delete['armed']:
            corrupt_delete['armed'] = False
            # Exchange did NOT actually cancel the order; response is HTTP 200 invalid JSON.
            return Response(200, {}, b'{corrupted')
        return orig_call(self, request, timeout)

    monkeypatch.setattr(NetworkFixture, '__call__', corrupting_call)

    with harness.open() as router:
        router.store.request(a_man, promote_candidate(a_man, 'h-a'))
        router.before_poll()
        harness.set_time(1, 1)
        harness.quote(router, '150')
        posts = writes(harness.requests)
        assert len(posts) == 1
        order_id = posts[0]['newClientOrderId'][0]

        # Request switch while order is open, with corrupt HTTP 200 DELETE on first attempt.
        corrupt_delete['armed'] = True
        router.store.request(b_man, promote_candidate(b_man, 'h-b', expected_active=a_man['package_id']))
        harness.set_time(1, 5)
        router.before_poll()
        # First DELETE returned corrupt HTTP 200 -> write_uncertain=True, order still active, switch blocked.
        assert router.store.snapshot()['active'] == a_man['package_id']
        assert router.bridge.session.write_uncertain is True
        assert len(writes(harness.requests, 'DELETE')) == 1

        # Next poll: GET recovery confirms order still NEW -> clears write_uncertain -> retries DELETE with same ID!
        harness.set_time(1, 10)
        router.before_poll()
        assert router.bridge.session.write_uncertain is False
        deletes = writes(harness.requests, 'DELETE')
        assert len(deletes) == 2
        assert deletes[0]['origClientOrderId'] == deletes[1]['origClientOrderId'] == [order_id]
        # Verify retry history archived in runtime SQLite.
        history = router.bridge.runtime.db.execute(
            "SELECT count(*) FROM dispatch_retry_history WHERE method='DELETE' AND client_id=?",
            (order_id,),
        ).fetchone()[0]
        assert history == 1

        # Next poll: GET confirms CANCELED -> switch completes safely!
        harness.set_time(1, 15)
        router.before_poll()
        assert router.store.snapshot()['active'] == b_man['package_id']
        assert router.store.snapshot()['switch']['phase'] == 'SUCCEEDED'
        assert len(writes(harness.requests)) == 1


def test_h_i_residual_position_and_paused_bar_never_leak_across_switch_or_resume(tmp_path, harness):
    cm_arc, cm_man = build_decision_package(
        tmp_path, 'cm-pause', EXAMPLES / 'close_momentum_v1',
        strategy_id='close_momentum', strategy_version='1.0.0',
        entrypoint='__init__:CloseMomentum', files=['__init__.py', 'decision.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, weight='.01'),
        execution_mode='target', package=True,
    )
    hold_arc, hold_man = build_decision_package(
        tmp_path, 'hold-next', EXAMPLES,
        strategy_id='limit_cycle', strategy_version='1.0.0',
        entrypoint='limit_cycle:LimitCycle', files=['limit_cycle.py'],
        configuration=dict(symbol='NVDAUSDT', interval_seconds=60, mode='hold'),
        execution_mode='orders',
    )
    harness.install(cm_arc)
    harness.install(hold_arc)
    with harness.open() as router:
        router.store.request(cm_man, promote_candidate(cm_man, 'pause-cm'))
        router.before_poll()
        harness.set_time(1, 1)
        assert router.closed_bar(make_bar(1, '150'))
        # Pause during bar 2 (PR #236 P2 regression for S01 target strategy).
        harness.control.state = 'PAUSED'
        router.before_poll()
        harness.set_time(2, 1)
        assert not router.closed_bar(make_bar(2, '155'))
        harness.set_time(2, 2)
        harness.control.state = 'RUNNING'
        router.before_poll()
        assert not router.closed_bar(make_bar(2, '155'))
        harness.quote(router, '150')
        assert writes(harness.requests) == []
        # Historical catch-up fills bar sequence continuity without arming a trade signal.
        assert router.historical_bar(make_bar(2, '155'))
        harness.quote(router, '150')
        assert writes(harness.requests) == []

        # Fresh bar 3 triggers BUY; fill creates residual position -> wait_flat blocks switch until flat.
        harness.set_time(3, 1)
        assert router.closed_bar(make_bar(3, '158'))
        harness.set_time(3, 2)
        harness.quote(router, '150')
        posts = writes(harness.requests)
        assert len(posts) == 1
        buy_id = posts[0]['newClientOrderId'][0]
        harness.fake.trade_update(buy_id, posts[0]['quantity'][0], '150')
        router.before_poll()

        router.store.request(hold_man, promote_candidate(hold_man, 'wait-flat-hold',
                                                         expected_active=cm_man['package_id']))
        harness.set_time(3, 5)
        router.before_poll()
        assert router.store.snapshot()['active'] == cm_man['package_id']
        assert router.store.pending()['reason'] == 'WAIT_OLD_POSITIONS'
