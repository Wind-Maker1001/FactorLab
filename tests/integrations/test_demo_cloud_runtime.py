"""Offline subprocess acceptance of the cloud runtime safety boundary."""
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Thread

import pytest

from factorlab.integrations.binance_futures_trading.demo_cloud_runtime import (
    CloudFeed, durable_state, health, run,
)
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, operator_lock, state_root
from factorlab.integrations.binance_futures_trading.observability import RequestCounts
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked

MODULE = 'factorlab.integrations.binance_futures_trading.demo_cloud_runtime'


@pytest.fixture(autouse=True)
def private_creation_mask():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def start(root):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'src'))
    return subprocess.Popen([sys.executable, '-m', MODULE, 'run', '--state-root', str(root),
        '--offline', '--poll-seconds', '.05', '--min-free-bytes', '0'],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_configured_root_and_windows_default(tmp_path, monkeypatch):
    monkeypatch.setenv('FACTORLAB_DEMO_STATE_ROOT', str(tmp_path))
    assert state_root() == tmp_path.resolve()
    monkeypatch.setenv('FACTORLAB_DEMO_STATE_ROOT', 'relative')
    with pytest.raises(SafetyBlocked):
        state_root()
    monkeypatch.delenv('FACTORLAB_DEMO_STATE_ROOT')
    if os.name == 'nt':
        monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
        assert state_root() == tmp_path / 'FactorLab' / 'BinanceDemo'
    else:
        with pytest.raises(SafetyBlocked):
            state_root()


def test_thread_exit_does_not_release_worker_lock(tmp_path):
    with operator_lock(tmp_path):
        thread = Thread(target=lambda: None)
        thread.start()
        thread.join()
        with pytest.raises(SafetyBlocked):
            with operator_lock(tmp_path):
                pass
    with operator_lock(tmp_path):
        pass


def test_restore_quarantine_blocks_ordinary_sessions(tmp_path):
    (tmp_path / 'restore-quarantine.json').write_text('{}')
    with pytest.raises(SafetyBlocked):
        with operator_lock(tmp_path):
            pass
    with operator_lock(tmp_path, allow_quarantined=True):
        pass


def test_two_workers_and_kill_release_lock(tmp_path):
    worker = start(tmp_path)
    try:
        assert json.loads(worker.stdout.readline())['event'] == 'cloud_health'
        with pytest.raises(SafetyBlocked):
            with operator_lock(tmp_path):
                pass
        other = start(tmp_path)
        out, err = other.communicate(timeout=15)
        assert other.returncode == 78, (out, err)
        worker.kill()
        worker.communicate(timeout=15)
        with operator_lock(tmp_path):
            pass
        with closing(DemoRuntime(tmp_path)) as runtime:
            assert runtime.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert run(tmp_path, offline=True, seconds=.05, poll_seconds=.01, min_free_bytes=0) == 0
        report = json.loads((tmp_path / 'cloud-health.json').read_text())
        assert report['restart_count'] == 1
        assert report['requests'] == dict(GET=0, POST=0, DELETE=0)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=15)


if os.name != 'nt':
    def test_sigterm_closes_without_operator_stop(tmp_path):
        worker = start(tmp_path)
        try:
            worker.stdout.readline()
            worker.send_signal(signal.SIGTERM)
            out, err = worker.communicate(timeout=15)
            assert worker.returncode == 0, (out, err)
            with closing(DemoRuntime(tmp_path)) as runtime:
                assert durable_state(tmp_path, runtime) == 'READ_ONLY'
            assert health(tmp_path) == 1
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=15)


def test_stop_persists_no_network_or_credential_access(tmp_path, monkeypatch):
    with closing(DemoRuntime(tmp_path)) as runtime:
        runtime.stop()
        epoch = runtime._get('stop_epoch')
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_cloud_runtime.DemoCredentialStore',
                        lambda: pytest.fail('STOP must precede credentials'))
    assert run(tmp_path, seconds=.1, poll_seconds=.01, min_free_bytes=0) == 0
    with closing(DemoRuntime(tmp_path)) as runtime:
        assert runtime._get('stop_epoch') == epoch
    report = json.loads((tmp_path / 'cloud-health.json').read_text())
    assert report['desired'] == 'STOPPED'
    assert report['requests'] == dict(GET=0, POST=0, DELETE=0)


def test_missing_credentials_refuses_before_network(tmp_path, monkeypatch):
    from factorlab.integrations.binance_futures_trading.demo_cloud_runtime import main
    monkeypatch.delenv('FACTORLAB_DEMO_SECRET_FILE', raising=False)
    if os.name == 'nt':
        # Never consult the operator's real Credential Manager during acceptance.
        from factorlab.integrations.binance_futures_trading.demo_credentials import DemoCredentialStore
        monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_cloud_runtime.DemoCredentialStore',
                            lambda: DemoCredentialStore(namespace='FactorLab-cloud-missing-test'))
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_cloud_runtime.open_session',
                        lambda *a, **k: pytest.fail('missing credentials must not reach session'))
    assert main(['run', '--state-root', str(tmp_path), '--min-free-bytes', '0']) == 78


def test_health_distinguishes_offline_from_get_only_and_execution(tmp_path, capsys):
    from datetime import datetime, timezone
    from time import time
    from factorlab.integrations.binance_futures_trading.demo_cloud_storage import atomic_json
    for mode, status in [('OFFLINE', 'OFFLINE'), ('GET_ONLY', 'READ_ONLY')]:
        atomic_json(tmp_path / 'cloud-health.json', dict(heartbeat_at=time(), started_at=time() - 5,
            mode=mode, status=status, market_batch_age_seconds=0 if mode == 'GET_ONLY' else None,
            last_reconciled_at=datetime.now(timezone.utc).isoformat() if mode == 'GET_ONLY' else None,
            account_recovered=mode == 'GET_ONLY', equity=None, positions=None, nav_usdt=None))
        assert health(tmp_path) == 0
        report = json.loads(capsys.readouterr().out)
        assert report['offline_heartbeat_healthy'] == (mode == 'OFFLINE')
        assert report['get_only_healthy'] == (mode == 'GET_ONLY')
        assert report['cloud_get_only_accepted'] is False
        assert report['demo_execution_ready'] is False
        assert report['account_recovered'] == report['market_fresh'] == (mode == 'GET_ONLY')


def test_cloud_health_retains_request_diagnostics(tmp_path):
    from contextlib import ExitStack
    requests = RequestCounts()
    requests.record('session', 'GET', '/fapi/v3/account', .75, 'CLOCK_SKEW', {})
    with closing(DemoRuntime(tmp_path)) as runtime, ExitStack() as stack:
        feed = CloudFeed(tmp_path, runtime, None, requests, stack, offline=True)
        feed.pause('STALE_POLL')
        feed.pause('R13_QUOTE_GAP')
        feed.pause('STALE_ACCOUNT')
        report = feed.snapshot()
    assert report['safety_events'] == dict(STALE_ACCOUNT=1, STALE_POLL=1, R13_QUOTE_GAP=1, CLOCK_SKEW=1)
    assert report['last_exception'] == 'STALE_ACCOUNT'
    endpoint = report['api']['endpoints'][0]
    assert endpoint['errors'] == {'CLOCK_SKEW': 1}
    assert endpoint['max_seconds'] == .75
    assert report['request_failure_rate'] == 1
    assert report['last_recovery_seconds'] is None


def test_low_space_refuses_before_session(tmp_path, monkeypatch):
    from factorlab.integrations.binance_futures_trading.demo_cloud_runtime import main
    assert main(['run', '--offline', '--state-root', str(tmp_path),
                 '--min-free-bytes', str(2**63)]) == 78


def test_unknown_and_reconciliation_failure_never_create_market_feed(tmp_path):
    from contextlib import ExitStack
    class Session:
        def __init__(self):
            self.result = dict(reconciled=False, unknown=[])
        def recover(self):
            return self.result
    with closing(DemoRuntime(tmp_path)) as runtime, ExitStack() as stack:
        feed = CloudFeed(tmp_path, runtime, None, RequestCounts(), stack, min_free_bytes=0)
        feed.session = Session()
        with pytest.raises(Exception, match='RECONCILIATION_FAILED'):
            feed.poll()
        feed.session.result = dict(reconciled=True, unknown=['reserved-order'])
        with pytest.raises(Exception, match='ORDER_UNKNOWN'):
            feed.poll()
        assert feed.feed is None
        assert dict(feed.requests) == dict(GET=0, POST=0, DELETE=0)


def test_identity_and_disk_refusals_do_not_enter_network_backoff(tmp_path, monkeypatch):
    from contextlib import ExitStack, contextmanager
    @contextmanager
    def refused(*args):
        raise SafetyBlocked('synthetic binding mismatch')
        yield
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_cloud_runtime.open_session', refused)
    with closing(DemoRuntime(tmp_path)) as runtime, ExitStack() as stack:
        feed = CloudFeed(tmp_path, runtime, None, RequestCounts(), stack, min_free_bytes=0)
        with pytest.raises(ValueError, match='BOUND_ACCOUNT'):
            feed.poll()
        feed.min_free_bytes = 2**63
        with pytest.raises(ValueError, match='DISK_SPACE_LOW'):
            feed.poll()


def test_network_failure_then_get_recovery(tmp_path):
    from contextlib import ExitStack
    from factorlab.integrations.binance_futures_trading.adapter import AccountReadExpired
    from factorlab.integrations.binance_futures_trading.demo_supervisor import DemoSupervisor

    class Session:
        failures = iter((AccountReadExpired('expired read set'), OSError('synthetic disconnection')))

        def recover(self):
            failure = next(self.failures, None)
            if failure:
                raise failure
            return dict(reconciled=True, unknown=[])

    class Market:
        def poll(self):
            return dict(bars=1, quotes=1)

        def watermarks(self):
            return None, None

        def pause(self, reason):
            pass

    with closing(DemoRuntime(tmp_path)) as runtime, ExitStack() as stack:
        feed = CloudFeed(tmp_path, runtime, None, RequestCounts(), stack, min_free_bytes=0)
        feed.session, feed.feed = Session(), Market()
        observed = []

        def report(_):
            observed.append((feed.status, feed.account_recovered, feed.last_exception))
            if feed.account_recovered:
                supervisor.stop()

        supervisor = DemoSupervisor(feed, poll_seconds=.01, report=report)
        supervisor.run(seconds=1)
        assert observed == [('PAUSED', False, 'STALE_ACCOUNT'), ('PAUSED', False, 'OSError'),
                            ('READ_ONLY', True, 'OSError')]
        assert feed.refusals['STALE_ACCOUNT'] == 1
        assert feed.requests['POST'] == feed.requests['DELETE'] == 0


@pytest.mark.parametrize('method', ['POST', 'DELETE', 'PUT', 'PATCH'])
def test_cloud_session_transport_has_no_write_capability(method, monkeypatch):
    from factorlab.integrations.binance_futures_trading.network import DemoNetworkTransport
    from factorlab.integrations.binance_futures_trading.protocol import Environment, SignedRequest
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.network.build_opener',
                        lambda *a: pytest.fail('write reached network'))
    with pytest.raises(SafetyBlocked, match='writes disabled'):
        DemoNetworkTransport()(SignedRequest(method, '/fapi/v1/order', '', {}, Environment.DEMO), 1)


def test_sigkill_sqlite_hot_journal_keeps_committed_budget(tmp_path):
    import sqlite3
    path = tmp_path / 'runtime.sqlite3'
    with closing(DemoRuntime(tmp_path)) as runtime:
        with runtime.db:
            runtime._set('spent_budget', '7')
            runtime.db.execute('CREATE TABLE crash_payload (payload BLOB)')
            runtime.db.executemany('INSERT INTO crash_payload VALUES (?)', [(b'x' * 4096,)] * 100)
    path.chmod(0o600)
    child = subprocess.Popen([sys.executable, '-c', '''
import sqlite3, sys
from threading import Event
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA cache_size=5')
db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE runtime SET value='0' WHERE key='spent_budget'")
db.execute('UPDATE crash_payload SET payload=zeroblob(4096)')
print('uncommitted', flush=True)
Event().wait(60)
''', str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'uncommitted'
        child.kill()
        child.communicate(timeout=15)
        # The service, not a pre-test SQLite read, must recover the hot journal.
        assert run(tmp_path, offline=True, seconds=.02, poll_seconds=.01, min_free_bytes=0) == 0
        with closing(DemoRuntime(tmp_path)) as runtime:
            assert runtime._get('spent_budget') == '7'
            assert runtime.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=15)


def test_health_market_age_advances_since_snapshot(tmp_path, monkeypatch, capsys):
    from factorlab.integrations.binance_futures_trading.demo_cloud_storage import atomic_json
    from datetime import datetime, timezone
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_cloud_runtime.time', lambda: 1000)
    atomic_json(tmp_path / 'cloud-health.json', dict(heartbeat_at=920, started_at=900,
        status='PAUSED', mode='GET_ONLY', market_batch_age_seconds=85,
        last_reconciled_at=datetime.fromtimestamp(920, timezone.utc).isoformat(),
        account_recovered=False, positions=[{'quantity': '1'}], equity={'walletBalance': '100'},
        nav_usdt='120'))
    assert health(tmp_path, max_age=90) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['alive'] is True
    assert report['uptime_seconds'] == 100
    assert report['market_batch_age_seconds'] == 165
    assert report['market_fresh'] is False
    assert report['account_recovered'] is False
    assert report['positions'] is report['nav_usdt'] is None


def test_execute_strategy_stop_is_preserved_and_beats_grid_pause(tmp_path):
    from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import StrategyControl
    with closing(DemoRuntime(tmp_path)) as runtime:
        for name, desired in [('grid-supervisor.sqlite3', 'PAUSED'), ('strategy-execute.sqlite3', 'STOPPED')]:
            with closing(StrategyControl(tmp_path / name, dict(mode='execute'))) as control:
                control.request(desired)
        assert durable_state(tmp_path, runtime) == 'STOPPED'


def test_cloud_market_cursor_recovers_after_long_downtime(tmp_path, monkeypatch):
    from contextlib import ExitStack
    from datetime import datetime, timezone
    from urllib.parse import parse_qs
    from test_demo_market_feed import Transport, bar
    from factorlab.integrations.binance_futures_trading.protocol import FuturesClient, Environment
    from factorlab.integrations.binance_futures_trading.demo_market_feed import DemoMarketFeed
    class Paged(Transport):
        def __call__(self, request, timeout):
            if request.path.endswith('/klines'):
                params = parse_qs(request.query)
                self.rows = [bar(t) for t in range(int(params['startTime'][0]), int(params['endTime'][0]) + 1, 60000)]
            return super().__call__(request, timeout)
    class Session:
        def recover(self):
            return dict(reconciled=True, unknown=[])
    transport = Paged()
    client = FuturesClient(environment=Environment.DEMO, transport=transport,
        clock=transport.clock, monotonic=transport.monotonic)
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_cloud_runtime.MarketClient', lambda _: client)
    def market(*args, **kwargs):
        return DemoMarketFeed(*args, **kwargs,
            clock=lambda: datetime.fromtimestamp(transport.time / 1000, timezone.utc))
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_cloud_runtime.DemoMarketFeed', market)
    with closing(DemoRuntime(tmp_path)) as runtime:
        with ExitStack() as stack:
            cloud = CloudFeed(tmp_path, runtime, None, RequestCounts(), stack, min_free_bytes=0)
            cloud.session = Session()
            cloud.poll()
            previous = cloud.watermarks()[0]
        transport.time += 200 * 60000
        with ExitStack() as stack:
            restarted = CloudFeed(tmp_path, runtime, None, RequestCounts(), stack, min_free_bytes=0)
            restarted.session = Session()
            restarted.poll()
            assert restarted.watermarks()[0] > previous
            assert restarted.feed.reason != 'BACKFILL_LIMIT'
            assert all(request.method == 'GET' for request in transport.requests)


def test_health_does_not_validate_or_initialize_database(tmp_path, monkeypatch, capsys):
    from factorlab.integrations.binance_futures_trading.demo_cloud_runtime import main
    from factorlab.integrations.binance_futures_trading.demo_cloud_storage import atomic_json
    from time import time
    (tmp_path / 'runtime.sqlite3').write_bytes(b'corrupt')
    atomic_json(tmp_path / 'cloud-health.json', dict(heartbeat_at=time(), started_at=time() - 1,
        status='OFFLINE', mode='OFFLINE', market_batch_age_seconds=None,
        last_reconciled_at=None, account_recovered=False))
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_cloud_runtime.state_root',
                        lambda: pytest.fail('health must not validate/open SQLite'))
    assert main(['health', '--state-root', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)['ready'] is True
