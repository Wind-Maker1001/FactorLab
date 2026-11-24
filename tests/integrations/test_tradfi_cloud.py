from datetime import datetime, timezone
import json

from factorlab.integrations.binance_futures_trading.tradfi_cloud import ObservationLedger, account_projection, health


def sample(at, error=None):
    return {'observed_at': at, 'market_error': error, 'quotes': {},
            'execution_status': 'AWAITING_SHARED_ACCOUNT_EXECUTION_INTERFACE'}


def test_ledger_restarts_and_disconnect_recovery_are_durable(tmp_path):
    ledger = ObservationLedger(tmp_path)
    ledger.record(sample('2026-11-24T15:00:00+00:00'))
    ledger.record(sample('2026-11-24T15:00:30+00:00', 'TimeoutError'))
    ledger.close()
    restarted = ObservationLedger(tmp_path)
    restarted.record(sample('2026-11-24T15:01:00+00:00', 'TimeoutError'))
    restarted.record(sample('2026-11-24T15:01:30+00:00'))
    restarted.close()
    health = json.loads((tmp_path / 'tradfi-health.json').read_text())
    assert (health['starts'], health['disconnects'], health['recoveries']) == (2, 1, 1)
    report = json.loads((tmp_path / 'daily-2026-11-24.json').read_text())
    assert report['samples'] == 4
    assert report['market_failure_samples'] == 2
    assert report['stock_reference_return'] is None
    assert report['execution_cost'] is None


def test_account_projection_suppresses_stale_financials(tmp_path):
    now = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)
    path = tmp_path / 'cloud-health.json'
    health = {'heartbeat_at': now.timestamp() - 1, 'account_recovered': True,
              'account_age_seconds': 0, 'status': 'RUNNING', 'mode': 'EXECUTE',
              'equity': '5000', 'nav_usdt': '5000', 'positions': []}
    path.write_text(json.dumps(health))
    assert account_projection(path, now)['nav_usdt'] == '5000'
    health['account_age_seconds'] = 100
    path.write_text(json.dumps(health))
    result = account_projection(path, now)
    assert not result['fresh']
    assert result['nav_usdt'] is None
    assert result['positions'] is None


def test_monitor_does_not_trust_cached_quote_fresh_flag(tmp_path):
    now = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)
    snapshot = {'observed_at': now.isoformat(), 'market_error': None,
                'quotes': {'QQQUSDT': {'time': (now.timestamp() - 100) * 1000, 'fresh': True}},
                'mode': 'READ_ONLY_SIGNAL_OBSERVER', 'execution_status': 'AWAITING_SHARED_ACCOUNT_EXECUTION_INTERFACE',
                'target_status': 'NO_STRATEGY_CONFIGURED'}
    (tmp_path / 'tradfi-health.json').write_text(json.dumps(snapshot))
    assert not health(tmp_path, now)['healthy']


def test_missing_owner_health_is_explicit(tmp_path):
    result = account_projection(tmp_path / 'absent', datetime.now(timezone.utc))
    assert result == {'fresh': False, 'reason': 'EXECUTE_HEALTH_MISSING'}
