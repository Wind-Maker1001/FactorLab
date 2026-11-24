from datetime import datetime, timezone
import json

import pytest

from factorlab.integrations.alpaca.cloud_runtime import (
    ObservationHTTP, RuntimeStore, Supervisor, atomic_json, health, weekly_window,
)
from factorlab.integrations.alpaca.http import AlpacaConfig
from factorlab.integrations.alpaca.market_data.sessions import AlpacaSessionCalendar


def calendar():
    return AlpacaSessionCalendar([
        {'date': '2026-11-20', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-11-23', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-11-24', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-11-25', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-11-27', 'open': '09:30', 'close': '13:00'},
        {'date': '2026-11-30', 'open': '09:30', 'close': '16:00'},
        {'date': '2026-12-01', 'open': '09:30', 'close': '16:00'},
    ], coverage_start='2026-11-16', coverage_end='2026-12-06')


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_weekly_cutoff_and_no_daily_trading():
    window = weekly_window(calendar(), at('2026-11-23T15:00:00'))
    assert window['due']
    assert window['signal_cutoff'] == '2026-11-20'
    assert not weekly_window(calendar(), at('2026-11-24T15:00:00'))['due']
    assert weekly_window(calendar(), at('2026-11-30T15:00:00'))['signal_cutoff'] == '2026-11-27'


def test_holiday_and_early_close():
    assert not weekly_window(calendar(), at('2026-11-26T15:00:00'))['market_open']
    assert weekly_window(calendar(), at('2026-11-27T17:59:00'))['market_open']
    assert not weekly_window(calendar(), at('2026-11-27T18:00:00'))['market_open']


def test_observer_has_transport_write_barrier():
    calls = []
    client = ObservationHTTP(AlpacaConfig('key', 'secret'), transport=lambda *args: calls.append(args))
    for method in ('POST', 'DELETE', 'PATCH'):
        with pytest.raises(PermissionError):
            client.request(method, '/v2/orders')
    assert calls == []


def test_persistent_restart_and_daily_report(tmp_path):
    store = RuntimeStore(tmp_path)
    store.put('equity_baseline', {'equity': '100'})
    store.event('RECOVERED', {'reconciled': True})
    store.report(at('2026-11-24T15:00:00'), {'equity': '100', 'target_weights': {}})
    store.db.close()
    resumed = RuntimeStore(tmp_path)
    assert resumed.starts == 2
    assert resumed.get('equity_baseline') == {'equity': '100'}
    assert resumed.db.execute('SELECT count(*) FROM recovery').fetchone()[0] == 1
    assert json.loads((tmp_path / 'reports/2026-11-24.json').read_text())['target_weights'] == {}
    resumed.db.close()


def test_health_staleness_and_stopped(tmp_path):
    payload = {'healthy': True, 'heartbeat': '2026-11-24T15:00:00+00:00', 'trade_stream': 'CONNECTED'}
    atomic_json(tmp_path / 'health.json', payload)
    assert health(tmp_path, at=at('2026-11-24T15:00:30'))['healthy']
    assert not health(tmp_path, at=at('2026-11-24T15:02:00'))['healthy']
    payload['trade_stream'] = 'STOPPED'
    atomic_json(tmp_path / 'health.json', payload)
    assert not health(tmp_path, at=at('2026-11-24T15:00:30'))['healthy']


def test_acceptance_uses_same_owner_lock_and_systemd_configuration(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import factorlab.integrations.alpaca.cloud_runtime as module
    import factorlab.integrations.alpaca.portfolio_acceptance as acceptance
    config = AlpacaConfig('key', 'secret')
    calls = []
    monkeypatch.setattr(module, 'private_root', lambda _: tmp_path)
    monkeypatch.setattr(module, 'credential_config', lambda _: config)
    @contextmanager
    def locked(root):
        calls.append(('locked', root))
        yield
        calls.append('unlocked')
    monkeypatch.setattr(module, 'owner_lock', locked)
    def run(**kwargs):
        assert calls == [('locked', tmp_path)]
        assert kwargs == dict(state_dir=tmp_path, phase='recover', runtime_config=config,
                              campaign='ALP-E04 CLOUD TEST')
        calls.append('acceptance')
        return {'complete': True}
    monkeypatch.setattr(acceptance, 'run_acceptance', run)
    assert module.main(['acceptance', '--phase', 'recover']) == 0
    assert calls == [('locked', tmp_path), 'acceptance', 'unlocked']


def test_acceptance_requires_explicit_phase():
    from factorlab.integrations.alpaca.cloud_runtime import main
    with pytest.raises(SystemExit) as error:
        main(['acceptance'])
    assert error.value.code == 2


def test_incompatible_schema_refused(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / 'runtime.sqlite3') as db:
        db.execute('PRAGMA user_version=2')
    with pytest.raises(ValueError, match='incompatible'):
        RuntimeStore(tmp_path)


def test_disconnect_restarts_session_without_submitting(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import factorlab.integrations.alpaca.cloud_runtime as module
    runtime = Supervisor(tmp_path, AlpacaConfig('key', 'secret'))
    runtime.market.thread = SimpleNamespace(start=lambda: None, join=lambda **_: None)
    calls = []
    runtime.poll = lambda: calls.append('rest')
    def session(adapter, **kwargs):
        calls.append('session')
        if calls.count('session') == 1:
            raise ConnectionError('connection lost')
        runtime.stop.set()
    monkeypatch.setattr(module, 'trade_updates', session)
    def delay(seconds):
        calls.append(('backoff', seconds))
    runtime.stop.wait = delay
    runtime.run()
    assert calls == ['rest', 'session', ('backoff', 1), 'rest', 'session']
    result = json.loads((tmp_path / 'health.json').read_text())
    assert result['trade_stream'] == 'STOPPED'
    assert not result['execution']['enabled']
    assert not result['healthy']


def test_market_failure_preserves_account_recovery_and_revokes_old_quotes(tmp_path, monkeypatch):
    import factorlab.integrations.alpaca.cloud_runtime as module
    monkeypatch.setattr(module, 'now', lambda: at('2026-11-24T15:00:00'))
    from types import SimpleNamespace
    from factorlab.integrations.alpaca.market_data.client import MarketDataError
    runtime = Supervisor(tmp_path, AlpacaConfig('key', 'secret'))
    runtime.health['market_rest'] = {'missing_symbols': [], 'fresh_quotes': True}
    runtime.adapter.recover = lambda: SimpleNamespace(reconciled=True)
    runtime.adapter.read_account_details = lambda: SimpleNamespace()
    runtime.calendar_day = at('2026-11-24T15:00:00').date()
    runtime.service = SimpleNamespace(calendar=calendar())
    def unavailable(*args):
        raise MarketDataError(503, 'unavailable')
    runtime.client.latest = unavailable
    runtime.poll()
    result = json.loads((tmp_path / 'health.json').read_text())
    assert result['account_recovered']
    assert not result['market_available']
    assert not result['healthy']
    assert result['error'] == 'MarketDataError'
    runtime.adapter.store.db.close()
    runtime.store.db.close()


def test_failed_recovery_revokes_old_health(tmp_path):
    runtime = Supervisor(tmp_path, AlpacaConfig('key', 'secret'))
    runtime.health['account_recovered'] = True
    def fail():
        raise ConnectionError('transport failed')
    runtime.adapter.recover = fail
    runtime.poll()
    result = json.loads((tmp_path / 'health.json').read_text())
    assert not result['account_recovered']
    assert not result['healthy']
    assert result['error'] == 'ConnectionError'
    assert not result['execution']['enabled']
    runtime.adapter.store.db.close()
    runtime.store.db.close()
