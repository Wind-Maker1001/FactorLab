from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

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
