"""Offline verification for P8-C04: Execute control persistence, startup recovery, lock exclusivity, and health."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import sys
from time import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading import demo_cloud_runtime as cloud
from factorlab.integrations.binance_futures_trading import demo_cloud_handoff as handoff
from factorlab.integrations.binance_futures_trading import demo_grid_supervisor as grid_mod
from factorlab.integrations.binance_futures_trading import demo_strategy_supervisor as strat_mod
from factorlab.integrations.binance_futures_trading import network, protocol
from factorlab.integrations.binance_futures_trading.demo_cli import open_session, parse_args
from factorlab.integrations.binance_futures_trading.demo_cloud_storage import atomic_json
from factorlab.integrations.binance_futures_trading.demo_runtime import (
    DemoRuntime, durable_control_state, open_grid_session, operator_lock, saved_grid_deployment,
)
from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import StrategyControl
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, DataQualityError, Environment, SafetyBlocked,
)
from factorlab.trading.storage import decode
from test_binance_demo_grid_network import network_grid
from test_binance_demo_network import NetworkFixture, wired


SESSION_NOW = DemoSession._now


def _start_continuous(root, store, *, opening_equity=D('1000'), **limits):
    with closing(DemoRuntime(root)) as runtime:
        epoch = runtime.stop_epoch()
        with open_session(root, store, runtime, dict(GET=0, POST=0, DELETE=0)) as (session, _):
            session.recover()
            return runtime.start_continuous(
                store.config, opening_equity=opening_equity, stop_epoch=epoch, **limits
            )


@pytest.fixture
def execute_harness(network_grid, monkeypatch):
    """Deterministic HTTP-intercepted Execute Grid worker harness with injectable faults."""
    root, store, factory, enable, fake, requests = network_grid
    state = SimpleNamespace(
        seconds=0.2,
        wall_offset=0.0,
        quote_age=100,
        fail_endpoints={},
        stale_book_count=0,
        polls=0,
        stop_after_polls=None,
        on_report=None,
    )
    initial = NOW.timestamp()
    original_init = protocol.FuturesClient.__init__

    def initialize(client, *args, **kwargs):
        original_init(client, *args, **kwargs)
        client.clock = lambda: initial + state.seconds + state.wall_offset
        client.monotonic = lambda: state.seconds

    original_request = NetworkFixture.__call__

    def response(exchange, request, timeout):
        for suffix, failures in list(state.fail_endpoints.items()):
            if request.path.endswith(suffix) and failures:
                action = failures.pop(0)
                if isinstance(action, Exception):
                    raise action
                return action
        if request.path.endswith('/klines'):
            exchange.calls.append((request.method, request.path, parse_qs(request.query)))
            params = parse_qs(request.query)
            start, end = int(params['startTime'][0]), int(params['endTime'][0])
            payload = [[at, '150', '153', '147', '150', '10', at + 59999]
                       for at in range(start, end + 1, 60000)]
            return Response(200, {}, json.dumps(payload).encode())
        result = original_request(exchange, request, timeout)
        if request.path.endswith(('/time', '/bookTicker', '/premiumIndex')):
            payload = json.loads(result.body)
            key = 'serverTime' if request.path.endswith('/time') else 'time'
            payload[key] = round((initial + state.seconds) * 1000)
            if request.path.endswith('/bookTicker'):
                if state.stale_book_count > 0:
                    state.stale_book_count -= 1
                    payload[key] -= 10000
                else:
                    payload[key] -= state.quote_age
            return Response(200, {}, json.dumps(payload).encode())
        return result

    monkeypatch.setattr(protocol.FuturesClient, '__init__', initialize)
    monkeypatch.setattr(DemoSession, '_now', SESSION_NOW)
    monkeypatch.setattr(NetworkFixture, '__call__', response)
    monkeypatch.setattr(protocol, 'build_opener', network.build_opener)
    monkeypatch.setattr(grid_mod, 'grid_factory', lambda *args, **kwargs: factory)
    monkeypatch.setattr(grid_mod.signal, 'signal', lambda *args: None)

    original_supervisor_run = grid_mod.DemoSupervisor.run

    def controlled_run(self, *, seconds=None):
        orig_report = self.report

        def wrapped_report(raw):
            orig_report(raw)
            state.polls += 1
            if state.on_report is not None:
                state.on_report(self, json.loads(raw), state)
            if state.stop_after_polls is not None and state.polls >= state.stop_after_polls:
                self.stop()

        self.report = wrapped_report
        return original_supervisor_run(self, seconds=seconds)

    monkeypatch.setattr(grid_mod.DemoSupervisor, 'run', controlled_run)

    def start_execute(*extra, poll_seconds='0.01', seconds='5', use_cli=False):
        args = parse_args([
            'grid-supervisor', 'start', '--execute',
            '--poll-seconds', str(poll_seconds),
            '--seconds', str(seconds),
            '--min-free-bytes', '0',
            *extra,
        ])
        if use_cli:
            return grid_mod.run_cli(args, root=root, credentials=store)
        return grid_mod.run(args, root=root, credentials=store)

    return SimpleNamespace(
        root=root, store=store, factory=factory, enable=enable,
        fake=fake, requests=requests, state=state, start_execute=start_execute,
    )


def _writes(requests, method='POST'):
    return [r for r in requests if r.get_method() == method]


# ---------------------------------------------------------------------------
# C04-01: Persistent control state preservation
# ---------------------------------------------------------------------------

def test_start_preserves_persisted_worker_stop_without_network_or_credentials(tmp_path, monkeypatch):
    path = tmp_path / 'grid-supervisor.sqlite3'
    config = dict(mode='execute', interval='1m', source='BINANCE_USDM_DEMO_REST', max_backfill=120, settings=None)
    with closing(StrategyControl(path, config)) as control:
        control.request('STOPPED')
    monkeypatch.setattr(grid_mod, 'DemoCredentialStore', lambda: pytest.fail('STOP must not access credentials'))
    monkeypatch.setattr(grid_mod, 'open_grid_session', lambda *a, **k: pytest.fail('STOP must not open session'))
    args = parse_args(['grid-supervisor', 'start', '--execute', '--min-free-bytes', '0'])
    assert grid_mod.run(args, root=tmp_path) == 0
    with closing(StrategyControl(path)) as control:
        assert control.desired() == 'STOPPED'
        assert control.snapshot()['observed']['status'] == 'STOPPED'
    report = json.loads((tmp_path / 'cloud-health.json').read_text())
    assert report['mode'] == 'EXECUTE'
    assert report['status'] == 'STOPPED'
    assert report['desired'] == 'STOPPED'
    assert report['requests'] == dict(GET=0, POST=0, DELETE=0)


@pytest.mark.parametrize('kind', ['runtime_stop', 'runtime_halted', 'legacy_halted'])
def test_start_preserves_persisted_runtime_stop_or_halted_without_init_running(
    execute_harness, monkeypatch, kind
):
    h = execute_harness
    auth = _start_continuous(h.root, h.store)
    with closing(DemoRuntime(h.root)) as runtime:
        if kind == 'runtime_stop':
            runtime.stop()
            expected = 'STOPPED'
        elif kind == 'runtime_halted':
            runtime.halt('global Demo loss budget exhausted')
            expected = 'HALTED'
        else:
            runtime.halt('legacy risk halt')
            runtime._set('continuous', json.dumps(dict(auth, state='STOPPED')))
            runtime.db.commit()
            expected = 'HALTED'
        epoch = runtime.stop_epoch()
        before_auth = runtime.continuous_authorization()

    before_requests = len(h.requests)
    monkeypatch.setattr(grid_mod, 'open_grid_session', lambda *a, **k: pytest.fail('STOP/HALTED must not touch network'))
    assert h.start_execute() == 0

    # Must NOT have initialized a RUNNING strategy_control row.
    assert not (h.root / 'grid-supervisor.sqlite3').exists()
    with closing(DemoRuntime(h.root)) as runtime:
        assert runtime.stop_epoch() == epoch
        assert runtime.continuous_authorization() == before_auth
        assert durable_control_state(runtime) == expected
    assert len(h.requests) == before_requests
    report = json.loads((h.root / 'cloud-health.json').read_text())
    assert report['mode'] == 'EXECUTE'
    assert report['status'] == expected
    assert report['desired'] == expected
    assert report['requests'] == dict(GET=0, POST=0, DELETE=0)
    if expected == 'HALTED':
        assert report['diagnostic'] is not None


def test_start_preserves_persisted_paused_without_promoting_to_running(execute_harness):
    h = execute_harness
    _start_continuous(h.root, h.store)
    # First start creates the deployment and runs briefly.
    h.state.stop_after_polls = 1
    assert h.start_execute() == 0
    with closing(StrategyControl(h.root / 'grid-supervisor.sqlite3')) as control:
        assert control.desired() == 'RUNNING'
    # Operator pauses the worker.
    pause_args = parse_args(['grid-supervisor', 'pause', '--execute'])
    assert grid_mod.run(pause_args, root=h.root, credentials=h.store) == 0
    with closing(StrategyControl(h.root / 'grid-supervisor.sqlite3')) as control:
        assert control.desired() == 'PAUSED'

    # Restarting Execute MUST preserve PAUSED and submit zero orders.
    h.state.polls = 0
    h.state.stop_after_polls = 2
    h.state.seconds += 60.0
    before_posts = len(_writes(h.requests, 'POST'))
    assert h.start_execute() == 0
    with closing(StrategyControl(h.root / 'grid-supervisor.sqlite3')) as control:
        assert control.desired() == 'PAUSED'
    assert len(_writes(h.requests, 'POST')) == before_posts == 0
    report = json.loads((h.root / 'cloud-health.json').read_text())
    assert report['desired'] == 'PAUSED'
    assert report['mode'] == 'EXECUTE'

    # Explicit resume restores RUNNING.
    resume_args = parse_args(['grid-supervisor', 'resume', '--execute'])
    assert grid_mod.run(resume_args, root=h.root, credentials=h.store) == 0
    with closing(StrategyControl(h.root / 'grid-supervisor.sqlite3')) as control:
        assert control.desired() == 'RUNNING'


def test_runtime_stop_mid_run_exits_cleanly_without_spinning(execute_harness):
    h = execute_harness
    _start_continuous(h.root, h.store)
    h.state.stop_after_polls = 10

    def trigger_stop(supervisor, raw, state):
        if state.polls == 1:
            with closing(DemoRuntime(h.root)) as other:
                other.stop()

    h.state.on_report = trigger_stop
    assert h.start_execute() == 0
    assert h.state.polls < 10
    report = json.loads((h.root / 'cloud-health.json').read_text())
    assert report['status'] == 'STOPPED'
    assert report['desired'] == 'STOPPED'


def test_strategy_supervisor_preserves_persisted_stop_and_pause(tmp_path):
    state = tmp_path / 'strategy-execute.sqlite3'
    config = dict(mode='execute', interval='1m', source='BINANCE_USDM_DEMO_REST', max_backfill=120)
    with closing(StrategyControl(state, config)) as control:
        control.request('STOPPED')
    args = SimpleNamespace(execute=True, interval='1m', max_backfill=120, poll_seconds=0.01, seconds=1)
    with closing(DemoRuntime(tmp_path)) as runtime:
        strat_mod.run_worker(args, tmp_path, None, runtime, dict(GET=0, POST=0, DELETE=0))
    with closing(StrategyControl(state)) as control:
        assert control.desired() == 'STOPPED'


# ---------------------------------------------------------------------------
# C04-02: Startup network recovery vs fatal failures
# ---------------------------------------------------------------------------

def test_startup_network_outage_recovers_via_supervisor_without_duplicate_grid_or_campaign(execute_harness):
    h = execute_harness
    _start_continuous(h.root, h.store)
    # Fail first 2 /fapi/v1/time calls (simulating network down at systemd boot).
    h.state.fail_endpoints['/time'] = [OSError('network unreachable'), OSError('dns timeout')]
    observed_statuses = []

    def track(supervisor, raw, state):
        health_now = json.loads((h.root / 'cloud-health.json').read_text())
        observed_statuses.append((health_now['status'], health_now['account_recovered'], health_now['recovery']['state']))
        if health_now['account_recovered']:
            supervisor.stop()

    h.state.on_report = track
    h.state.stop_after_polls = 8
    assert h.start_execute(poll_seconds='0.01', seconds='10') == 0

    # Must have entered RECOVERING during outage, then transitioned cleanly to RUNNING/RECOVERED.
    assert any(status == 'RECOVERING' and not recovered for status, recovered, _ in observed_statuses)
    assert observed_statuses[-1][0] == 'RUNNING'
    assert observed_statuses[-1][1] is True
    assert observed_statuses[-1][2] == 'RECOVERED'
    assert len(_writes(h.requests, 'POST')) == 0
    paths = [path for method, path, _ in h.fake.calls if method == 'GET']
    kline = paths.index('/fapi/v1/klines')
    assert '/fapi/v3/positionRisk' in paths[kline + 1:]

    # Exactly ONE campaign window and ONE Grid deployment created!
    with closing(DemoRuntime(h.root)) as runtime:
        assert runtime.db.execute('SELECT count(*) FROM campaigns').fetchone()[0] == 1
    with open_grid_session(h.root, h.store, h.factory, continuous=True) as bridge:
        rows = bridge.controller.store.db.execute(
            "SELECT count(*) FROM events WHERE id LIKE 'demo-grid:deployment:%'"
        ).fetchone()[0]
        assert rows == 1


def test_startup_stale_quote_enters_recovery_and_does_not_duplicate_grid(execute_harness):
    h = execute_harness
    _start_continuous(h.root, h.store)
    # First bookTicker during grid_factory is >5s stale, second is fresh.
    h.state.stale_book_count = 1
    recovered_seen = []

    def track(supervisor, raw, state):
        health_now = json.loads((h.root / 'cloud-health.json').read_text())
        recovered_seen.append((health_now['status'], health_now['account_recovered'], health_now['last_exception']))
        if health_now['account_recovered']:
            supervisor.stop()

    h.state.on_report = track
    h.state.stop_after_polls = 6
    assert h.start_execute(poll_seconds='0.01', seconds='10') == 0
    assert any(not ok and 'STALE' in (exc or '') for _, ok, exc in recovered_seen)
    assert recovered_seen[-1][1] is True


def test_startup_auth_and_identity_errors_fail_explicitly_with_exit_78(execute_harness, capsys):
    h = execute_harness
    _start_continuous(h.root, h.store)

    # 1. Exchange -2015 AUTH failure during startup must NOT enter retry loop -> exit 78.
    h.state.fail_endpoints['/account'] = [Response(401, {}, b'{"code":-2015,"msg":"Invalid API-key"}')]
    capsys.readouterr()
    code = h.start_execute(poll_seconds='0.01', seconds='5', use_cli=True)
    assert code == 78
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out == dict(event='grid_start_refused', reason='EXCHANGE_AUTH_REJECTED')

    # 2. Local binding/key mismatch fails BEFORE any network call -> exit 78.
    before = len(h.requests)
    h.store.credential = Credentials('wrong-key', 'wrong-secret', Environment.DEMO)
    code = h.start_execute(poll_seconds='0.01', seconds='5', use_cli=True)
    assert code == 78
    assert len(h.requests) == before
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out == dict(event='grid_start_refused', reason='IDENTITY_OR_CREDENTIAL_ERROR')


def test_startup_corrupt_sqlite_fails_explicitly_with_exit_78(execute_harness, capsys):
    h = execute_harness
    (h.root / 'runtime.sqlite3').write_bytes(b'corrupted sqlite bytes')
    before = len(h.requests)
    capsys.readouterr()
    code = h.start_execute(use_cli=True)
    assert code == 78
    assert len(h.requests) == before
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out['event'] == 'grid_start_refused'
    assert 'SQLITE' in out['reason']


def test_startup_unknown_order_reconciles_via_get_never_resubmits(execute_harness):
    h = execute_harness
    _start_continuous(h.root, h.store)
    # Create an UNKNOWN order in the journal via timeout_next.
    with open_grid_session(h.root, h.store, h.factory, continuous=True) as bridge:
        bridge.recover()
        h.fake.timeout_next = True
        bridge.observe(dict(id='initial', time=NOW.replace(tzinfo=None).isoformat(), price='150', volume=0))
        unknown_key = bridge.grid.active[0].intent.client_order_id
        assert bridge.session.status()['unknown'] == [unknown_key]
    assert len(_writes(h.requests, 'POST')) == 1

    # On restart, first GET /fapi/v1/order fails transiently, then succeeds and resolves UNKNOWN.
    h.state.fail_endpoints['/order'] = [OSError('temporary order query drop')]
    seen = []

    def track(supervisor, raw, state):
        health_now = json.loads((h.root / 'cloud-health.json').read_text())
        seen.append((health_now['status'], health_now['account_recovered'], health_now['unknown_count']))
        if health_now['account_recovered'] and health_now['unknown_count'] == 0:
            supervisor.stop()

    h.state.on_report = track
    h.state.stop_after_polls = 6
    assert h.start_execute(poll_seconds='0.01', seconds='10') == 0

    assert any(not recovered for _, recovered, _ in seen)
    assert seen[-1][1] is True and seen[-1][2] == 0
    # Critically: ZERO duplicate POSTs!
    assert len(_writes(h.requests, 'POST')) == 1


# ---------------------------------------------------------------------------
# C04-03: Lock exclusivity, restore quarantine, budget preservation
# ---------------------------------------------------------------------------

def test_execute_and_get_only_share_lock_exclusivity_and_quarantine(execute_harness, capsys):
    h = execute_harness
    _start_continuous(h.root, h.store)

    # Holding operator_lock (like GET-only cloud runtime or another Execute worker) blocks Execute with exit 78.
    with operator_lock(h.root, allow_quarantined=True):
        capsys.readouterr()
        assert h.start_execute(use_cli=True) == 78
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out == dict(event='grid_start_refused', reason='SESSION_LOCK_CONTENDED')

    # Restore quarantine blocks Execute with exit 78.
    (h.root / 'restore-quarantine.json').write_text('{"version":1}', encoding='utf-8')
    capsys.readouterr()
    assert h.start_execute(use_cli=True) == 78
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out == dict(event='grid_start_refused', reason='RESTORE_QUARANTINE')


@pytest.fixture
def handoff_source(execute_harness, tmp_path_factory, monkeypatch):
    h = execute_harness
    h.store.reference = 'FactorLab/Binance/USD-M/Demo/credentials'
    h.store.config['credential_reference'] = h.store.reference
    _start_continuous(h.root, h.store, total_orders=10, total_notional=D('100'))
    with open_grid_session(h.root, h.store, h.factory, continuous=True) as bridge:
        bridge.recover()
        h.fake.timeout_next = True
        bridge.observe(dict(id='initial', time=NOW.replace(tzinfo=None).isoformat(), price='150', volume=0))
        assert bridge.session.status()['unknown']
    with closing(StrategyControl(h.root / 'grid-supervisor.sqlite3',
                                 dict(mode='execute', settings=None))) as control:
        control.request('PAUSED')
    for path in (h.root, *h.root.rglob('*')):
        path.chmod(0o700 if path.is_dir() else 0o600)
    transfer = tmp_path_factory.mktemp('transfer')
    transfer.chmod(0o700)
    monkeypatch.setattr(handoff, 'DemoCredentialStore', lambda: h.store)
    return h, transfer


def test_handoff_seals_before_release_and_retries_the_same_snapshot(handoff_source, monkeypatch):
    h, transfer = handoff_source
    package = transfer / 'package'
    target = dict(machine_id='1' * 32, state_root='/var/lib/factorlab-demo/execute-state')
    original = handoff.storage.atomic_json

    def fail_release(path, payload, **kwargs):
        if path.name == handoff._RELEASE:
            raise OSError('release publication interrupted')
        original(path, payload, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(handoff.storage, 'atomic_json', fail_release)
        with pytest.raises(OSError, match='publication interrupted'):
            handoff.export_handoff(h.root, package, **target)
    assert not (package / handoff._RELEASE).exists()
    with pytest.raises(SafetyBlocked, match='GET-only'):
        with operator_lock(h.root):
            pytest.fail('Windows cannot reacquire the Execute lock after the handoff seal')
    with closing(DemoRuntime(h.root)) as runtime:
        with pytest.raises(SafetyBlocked, match='current owner'):
            runtime.stop()
    with pytest.raises(SafetyBlocked, match='different handoff'):
        handoff.export_handoff(h.root, transfer / 'another-package', **target)
    release_id = handoff.export_handoff(h.root, package, **target)
    assert json.loads((package / handoff._RELEASE).read_text())['id'] == release_id


if sys.platform == 'linux':
    @pytest.mark.parametrize('desired', ['PAUSED', 'STOPPED', 'HALTED'])
    def test_handoff_installs_only_after_get_recovers_unknown(handoff_source, monkeypatch, desired):
        h, transfer = handoff_source
        with closing(DemoRuntime(h.root)) as runtime:
            if desired == 'STOPPED':
                runtime.stop()
            if desired == 'HALTED':
                runtime.halt('test halted state')
        destination = transfer / 'execute-state'
        package = transfer / 'package'
        machine = Path('/etc/machine-id').read_text().strip()
        handoff.export_handoff(h.root, package, machine_id=machine, state_root=destination.as_posix())
        credentials = h.store.read()
        secret = transfer / 'secret.json'
        atomic_json(secret, dict(environment='DEMO', key=credentials.key, secret=credentials.secret))
        monkeypatch.setenv('FACTORLAB_DEMO_SECRET_FILE', str(secret))
        h.state.fail_endpoints['/time'] = [OSError('account GET unavailable')]
        with pytest.raises(DataQualityError, match='account read failed'):
            handoff.import_handoff(package, destination, min_free_bytes=0)
        assert not destination.exists()
        result = handoff.import_handoff(package, destination, min_free_bytes=0)
        assert result['snapshot_verified'] and result['desired'] == desired
        assert result['unknown'] == 0 and result['requests']['GET'] > 0
        assert result['consumed']['posts'] == 1
        assert len(_writes(h.requests, 'POST')) == 1 and not _writes(h.requests, 'DELETE')
        with pytest.raises(FileExistsError):
            handoff.import_handoff(package, destination, min_free_bytes=0)

    def test_handoff_refuses_another_path_and_a_modified_valid_database(handoff_source):
        h, transfer = handoff_source
        destination = transfer / 'execute-state'
        package = transfer / 'package'
        machine = Path('/etc/machine-id').read_text().strip()
        handoff.export_handoff(h.root, package, machine_id=machine, state_root=destination.as_posix())
        reads_before_import = len(h.requests)
        with pytest.raises(SafetyBlocked, match='another Linux host or state path'):
            handoff.import_handoff(package, transfer / 'different-state', min_free_bytes=0)
        with closing(DemoRuntime(package / 'databases')) as runtime:
            runtime.stop()
        with pytest.raises(SafetyBlocked, match='differs from the sealed snapshot'):
            handoff.import_handoff(package, destination, min_free_bytes=0)
        assert len(h.requests) == reads_before_import and not destination.exists()


def test_restart_preserves_budget_consumption_and_deployment(execute_harness):
    h = execute_harness
    _start_continuous(h.root, h.store, total_orders=10, total_notional=D('100'))
    with open_grid_session(h.root, h.store, h.factory, continuous=True) as bridge:
        bridge.recover()
        bridge.observe(dict(id='initial', time=NOW.replace(tzinfo=None).isoformat(), price='150', volume=0))
        deployment_id = bridge.grid.deployment_id

    h.state.stop_after_polls = 1
    assert h.start_execute() == 0
    first = json.loads((h.root / 'cloud-health.json').read_text())
    assert first['global_budget']['consumed']['posts'] == 1
    assert first['global_budget']['remaining_posts'] == 9
    assert first['grid']['deployment_id'] == deployment_id
    assert len(first['recent_orders']) == 1

    # Second restart keeps exact same deployment, order history, and remaining budget.
    h.state.polls = 0
    h.state.stop_after_polls = 1
    assert h.start_execute() == 0
    second = json.loads((h.root / 'cloud-health.json').read_text())
    assert second['starts'] == first['starts'] + 1
    assert second['restart_count'] == first['restart_count'] + 1
    assert second['global_budget']['consumed'] == first['global_budget']['consumed']
    assert second['global_budget']['remaining_posts'] == 9
    assert second['grid']['deployment_id'] == deployment_id
    assert len(_writes(h.requests, 'POST')) == 1


def test_execute_rotates_after_breakout_and_preserves_feed_budget_and_ledger(execute_harness, capsys, monkeypatch):
    # The Actions runner can itself be a systemd service, separate from this Execute worker.
    monkeypatch.setattr(grid_mod, 'detect_systemd_unit', lambda: None)
    h = execute_harness
    policy = _start_continuous(h.root, h.store, orders=2, total_orders=3)
    with open_grid_session(h.root, h.store, h.factory, continuous=True) as old:
        old.recover()
        old.observe(dict(id='initial', time=NOW.replace(tzinfo=None).isoformat(), price='150', volume=0))
        buy = old.grid.active[0].intent
        h.fake.trade_update(buy.client_order_id, '.04', '146')
        old.recover()
        old.dispatch()
        sell = old.grid.active[0].intent
        old_id = old.grid.deployment_id
        original_window = old.runtime.active_campaign()['id']
        old.observe(dict(id='breakout', time=NOW.replace(tzinfo=None).isoformat(), price='155', volume=0),
                    dispatch=False)
        assert old.grid.closed_by_breakout

    trace = []

    def track(supervisor, raw, state):
        worker = supervisor.feed
        health = worker.health_snapshot()
        trace.append((worker.bridge.grid.deployment_id, worker.watermarks()[0],
                      worker.consumer.last_bar, health['grid'], health['global_budget']))
        assert worker.bridge is worker.consumer.bridge
        assert health['grid']['deployment_id'] == worker.bridge.grid.deployment_id
        if state.polls <= 2:
            assert worker.bridge.grid.closed_by_breakout
            assert worker.bridge.session.status()['open_orders']
            assert len(_writes(h.requests, 'POST')) == 2
            if state.polls == 2:
                h.fake.trade_update(sell.client_order_id, '.04', '154')
        elif state.polls == 3:
            assert worker.bridge.generation == 1
            assert not worker.consumer.live_bar
            assert worker.bridge.runtime.active_campaign()['id'] != original_window
            assert worker.bridge.runtime.continuous_authorization() == policy
            state.seconds += 1.2
        elif state.polls == 4:
            capsys.readouterr()
            assert cloud.health(h.root) == 1
            observed = json.loads(capsys.readouterr().out)
            assert observed['alive'] and observed['market_fresh'] and observed['account_recovered']
            assert not observed['dispatch_eligible'] and not observed['demo_execution_ready']

    h.state.seconds = 59.2
    h.state.on_report = track
    h.state.stop_after_polls = 4
    # The scenario terminates after four reports. Slow filesystem runners must
    # not truncate that safety/recovery scenario at the default five-second wall deadline.
    assert h.start_execute(seconds='20') == 0
    new_id = old_id + '-00001'
    assert [row[0] for row in trace] == [old_id, old_id, new_id, new_id]
    assert trace[1][1] == trace[2][1]
    assert trace[2][2] is None
    assert trace[3][1] == trace[2][1] + 60000
    assert trace[3][3]['live_bar']
    assert trace[2][4]['consumed']['posts'] == 2
    assert D(trace[2][4]['consumed']['submitted_notional']) == D('12')
    assert trace[3][4]['consumed']['posts'] == 3
    assert trace[3][4]['remaining_posts'] == 0

    h.state.polls = 0
    h.state.stop_after_polls = 1

    def resumed(supervisor, raw, state):
        worker = supervisor.feed
        assert worker.bridge.grid.deployment_id == new_id
        assert worker.bridge.feed_deployment_id == old_id
        assert worker.watermarks()[0] == trace[3][1]
        orders = worker.bridge.session.status()['orders']
        assert orders[buy.client_order_id]['fee_usdt'] == D('.00584')
        assert orders[sell.client_order_id]['fee_usdt'] == D('.00616')
        assert [order['client_order_id'] for order in worker.bridge.session.status()['open_orders']] == [
            new_id + '-00000001']
        assert worker.health_snapshot()['global_budget'] == trace[3][4]
        journal = worker.bridge.controller.store
        assert journal.event_payload(f'demo-grid:deployment:{old_id}') is not None
        assert decode(json.loads(saved_grid_deployment(journal)))['replaces'] == old_id

    h.state.on_report = resumed
    assert h.start_execute() == 0
    assert len(_writes(h.requests, 'POST')) == 3
    assert not _writes(h.requests, 'DELETE')


def test_execute_user_pause_blocks_breakout_rotation_and_preflight_rejects_unknown_identity(
        execute_harness, capsys):
    h = execute_harness
    _start_continuous(h.root, h.store)
    with open_grid_session(h.root, h.store, h.factory, continuous=True) as old:
        old.recover()
        old.observe(dict(id='breakout', time=NOW.replace(tzinfo=None).isoformat(), price='155', volume=0),
                    dispatch=False)
        old_id = old.grid.deployment_id

    path = h.root / 'grid-supervisor.sqlite3'
    config = dict(mode='execute', interval='1m', source=grid_mod.SOURCE, max_backfill=120, settings=None)
    with closing(StrategyControl(path, config)) as control:
        control.request('PAUSED')
    h.state.stop_after_polls = 1
    assert h.start_execute() == 0
    snapshot = json.loads((h.root / 'cloud-health.json').read_text())
    assert snapshot['desired'] == 'PAUSED'
    assert snapshot['grid']['deployment_id'] == old_id
    assert snapshot['grid']['grid_reason'] == 'BREAKOUT'

    with open_grid_session(h.root, h.store, read_only=True) as inspect:
        journal = inspect.controller.store
        forged = decode(json.loads(saved_grid_deployment(journal)))
        forged['deployment']['deployment_id'] = old_id + '-00001'
        forged.update(generation=1, feed_deployment_id=old_id, replaces=old_id,
                      terminal_reason='BREAKOUT', activated_at=NOW)
        with journal.transaction():
            journal.record_metadata('demo-grid:deployment:' + forged['deployment']['deployment_id'], forged)
    before_requests = len(h.requests)
    capsys.readouterr()
    assert h.start_execute(use_cli=True) == 78
    assert json.loads(capsys.readouterr().out)['event'] == 'grid_start_refused'
    assert len(h.requests) == before_requests
    assert not _writes(h.requests)


# ---------------------------------------------------------------------------
# C04-04: Health wiring & honest readiness
# ---------------------------------------------------------------------------

@pytest.fixture
def execute_health_snapshot(monkeypatch):
    now_ts = time()
    now_iso = datetime.fromtimestamp(now_ts, timezone.utc).isoformat()
    active_systemd = dict(
        unit='factorlab-demo-execute.service',
        available=True,
        load_state='loaded',
        active_state='active',
        sub_state='running',
        main_pid=4242,
        n_restarts=0,
        result='success',
        exec_main_status=0,
        exec_main_start_timestamp='2026-11-22 00:00:00 UTC',
    )
    monkeypatch.setattr(cloud, 'time', lambda: now_ts)
    monkeypatch.setattr(cloud, 'systemd_unit_state', lambda unit: active_systemd if unit else None)

    return dict(
        version=1,
        pid=4242,
        heartbeat_at=now_ts,
        started_at=now_ts - 30,
        starts=1,
        restart_count=0,
        systemd_unit='factorlab-demo-execute.service',
        mode='EXECUTE',
        status='RUNNING',
        reason=None,
        desired='RUNNING',
        last_valid_market_at=now_iso,
        last_reconciled_at=now_iso,
        account_recovered=True,
        account_age_seconds=1.0,
        market_batch_age_seconds=1.0,
        requests=dict(GET=12, POST=1, DELETE=0),
        unknown_count=0,
        unknown_orders=[],
        recent_orders=[dict(client_order_id='fl-1-g-1', status='SUBMITTED')],
        open_orders=[dict(client_order_id='fl-1-g-1', symbol='NVDAUSDT', side='BUY')],
        positions=[dict(symbol='NVDAUSDT', quantity='0.04')],
        equity=dict(walletBalance='1000', availableBalance='990', unrealizedProfit='0.5'),
        nav_usdt='1000.5',
        grid=dict(deployment_id='fl-1-g', state='RUNNING', grid_reason=None, effective='RUNNING',
                  reason=None, running=True, live_bar=True, last_bar=now_iso, inventory='0.04',
                  reservations_count=0),
        campaign=dict(enabled=True, post_budget=4, posts_consumed=1, remaining_posts=3),
        global_budget=dict(state='RUNNING', total_orders=100, remaining_posts=99, remaining_notional='694'),
        recovery=dict(state='RECOVERED', consecutive_failures=0, recovery_attempts=1,
                      last_failure_at=None, last_failure_reason=None, last_recovered_at=now_iso,
                      startup_recovered=True),
        last_exception=None,
    ), active_systemd


def test_health_execute_reports_real_facts_and_never_trusts_systemd_active_alone(
        tmp_path, monkeypatch, capsys, execute_health_snapshot):
    base, active_systemd = execute_health_snapshot

    # 1. Truly healthy Execute -> exit 0, demo_execution_ready=True, all fields present.
    atomic_json(tmp_path / 'cloud-health.json', base)
    assert cloud.health(tmp_path) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['ready'] is True
    assert out['execute_healthy'] is True
    assert out['demo_execution_ready'] is True
    assert out['dispatch_eligible'] is True
    assert out['systemd']['active_state'] == 'active'
    assert out['nav_usdt'] == '1000.5'
    assert out['global_budget']['remaining_posts'] == 99

    # 2. Even with systemd active, ANY unready condition forces ready=False & demo_execution_ready=False (exit 1)!
    unready_cases = [
        dict(status='RECOVERING', account_recovered=False),
        dict(status='PAUSED', desired='PAUSED'),
        dict(status='STOPPED', desired='STOPPED'),
        dict(status='HALTED', desired='HALTED'),
        dict(unknown_count=1, unknown_orders=['fl-1-g-2']),
        dict(market_batch_age_seconds=6.0),
        dict(account_age_seconds=6.0),
        dict(account_recovered=False),
        dict(campaign=dict(enabled=False)),
        dict(grid=dict(base['grid'], state='PAUSED', running=False)),
    ]
    for override in unready_cases:
        atomic_json(tmp_path / 'cloud-health.json', dict(base, **override))
        assert cloud.health(tmp_path) == 1, override
        out = json.loads(capsys.readouterr().out)
        assert out['ready'] is False, override
        assert out['execute_healthy'] is False, override
        assert out['demo_execution_ready'] is False, override
        if override.get('account_recovered') is False:
            assert out['positions'] is None and out['nav_usdt'] is None and out['open_orders'] is None

    # 3. If systemd reports unit inactive or different PID, alive=False and ready=False even with fresh snapshot.
    monkeypatch.setattr(cloud, 'systemd_unit_state', lambda unit: dict(active_systemd, active_state='inactive', main_pid=0))
    atomic_json(tmp_path / 'cloud-health.json', base)
    assert cloud.health(tmp_path) == 1
    out = json.loads(capsys.readouterr().out)
    assert out['alive'] is False
    assert out['ready'] is False
    assert out['demo_execution_ready'] is False


@pytest.mark.parametrize(('budget', 'expected'), [
    pytest.param(dict(state='RUNNING', remaining_posts=1, remaining_notional='0.01'), True, id='available'),
    pytest.param(dict(state='RUNNING', remaining_posts=0, remaining_notional='1'), False, id='no-posts'),
    pytest.param(dict(state='RUNNING', remaining_posts=1, remaining_notional='0'), False, id='no-notional'),
    pytest.param(dict(state='HALTED', remaining_posts=1, remaining_notional='1'), False, id='halted'),
    pytest.param(None, False, id='no-authorization'),
    pytest.param(dict(state='RUNNING', remaining_posts=1), False, id='missing-notional'),
    pytest.param(dict(state='RUNNING', remaining_posts=True, remaining_notional='1'), False, id='invalid-posts'),
    pytest.param(dict(state='RUNNING', remaining_posts=1, remaining_notional='invalid'), False, id='invalid-notional'),
    pytest.param(dict(state='RUNNING', remaining_posts=1, remaining_notional='Infinity'), False, id='nonfinite-notional'),
])
def test_execute_health_requires_remaining_global_budget(tmp_path, capsys, execute_health_snapshot, budget, expected):
    base, _ = execute_health_snapshot
    base['global_budget'] = budget
    base['campaign']['remaining_posts'] = 0
    atomic_json(tmp_path / 'cloud-health.json', base)

    assert cloud.health(tmp_path) == (0 if expected else 1)
    out = json.loads(capsys.readouterr().out)
    assert out['alive'] is True
    for flag in ('execute_healthy', 'demo_execution_ready', 'dispatch_eligible', 'ready'):
        assert out[flag] is expected


def test_no_credentials_leaked_in_logs_health_or_control(execute_harness, capsys):
    h = execute_harness
    key_sentinel = 'DEMO_KEY_SENTINEL_99887766'
    secret_sentinel = 'DEMO_SECRET_SENTINEL_55443322'
    h.store.credential = Credentials(key_sentinel, secret_sentinel, Environment.DEMO)
    account_id = protocol.credential_account_id(Environment.DEMO, key_sentinel)
    h.store.config['account_id'] = account_id
    h.store.config['journal_id'] = h.store.config['journal_id']

    _start_continuous(h.root, h.store)
    h.state.fail_endpoints['/time'] = [OSError('transient blip')]
    h.state.stop_after_polls = 2
    capsys.readouterr()
    assert h.start_execute(poll_seconds='0.01', seconds='5') == 0
    captured = capsys.readouterr()

    artifacts = [
        captured.out,
        captured.err,
        (h.root / 'cloud-health.json').read_text(encoding='utf-8'),
    ]
    with closing(StrategyControl(h.root / 'grid-supervisor.sqlite3')) as control:
        artifacts.append(json.dumps(control.snapshot(), default=str))

    for blob in artifacts:
        assert key_sentinel not in blob
        assert secret_sentinel not in blob
