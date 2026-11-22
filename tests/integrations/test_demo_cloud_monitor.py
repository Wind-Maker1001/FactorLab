"""Tests for P8-C06 lightweight Azure Demo monitoring, alerting, and daily reporting."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from factorlab.integrations.binance_futures_trading.demo_cloud_monitor import (
    AlertDeliveryConfig,
    MonitorThresholds,
    append_bounded_jsonl,
    deliver_webhook,
    evaluate_alerts,
    generate_daily_report,
    load_alert_config,
    load_jsonl,
    render_daily_report_markdown,
    run_alert_self_test,
    run_monitor_cycle,
    sanitize_sample,
    summarize_all_windows,
)
from factorlab.integrations.binance_futures_trading.demo_cloud_runtime import (
    CONFIG_ERROR,
    main as cloud_main,
)
from factorlab.integrations.binance_futures_trading.demo_cloud_storage import atomic_json
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked


def _make_health_payload(
    *,
    now: float = 1_800_000_000.0,
    pid: int = 3792,
    started_at: float | None = None,
    starts: int = 2,
    n_restarts: int = 0,
    status: str = 'RUNNING',
    desired: str = 'RUNNING',
    alive: bool = True,
    ready: bool = True,
    execute_healthy: bool = True,
    dispatch_eligible: bool = True,
    account_recovered: bool = True,
    account_age_seconds: float = 0.6,
    last_recovery_seconds: float = 0.8,
    market_fresh: bool = True,
    market_batch_age_seconds: float = 1.2,
    unknown_count: int = 0,
    unknown_orders: list | None = None,
    requests_get: int = 1000,
    requests_post: int = 0,
    requests_delete: int = 0,
    api_count: int = 1000,
    api_errors: int = 0,
    api_total_seconds: float = 120.0,
    api_max_seconds: float = 1.5,
    consumed_posts: int = 7,
    total_orders: int = 100,
    consumed_notional: str = '41.33',
    total_notional: str = '700',
    global_budget_state: str = 'RUNNING',
    disk_free_bytes: int = 20 * 1024 ** 3,
    disk_bytes: int = 60 * 1024 ** 3,
    recovery_attempts: int = 0,
    worker_consecutive_failures: int = 0,
    last_exception: str | None = None,
    stale_account: int = 0,
    stale_poll: int = 0,
    r13_gap: int = 0,
    clock_skew: int = 0,
    open_orders_count: int = 1,
    positions_count: int = 0,
) -> dict:
    started = now - 600 if started_at is None else started_at
    rem_posts = max(0, total_orders - consumed_posts)
    rem_notional = str(max(0.0, float(total_notional) - float(consumed_notional)))
    return dict(
        version=1,
        pid=pid,
        heartbeat_at=now,
        started_at=started,
        starts=starts,
        restart_count=max(0, starts - 1),
        systemd_unit='factorlab-demo-execute.service',
        systemd=dict(
            unit='factorlab-demo-execute.service',
            available=True,
            load_state='loaded',
            active_state='active' if alive else 'inactive',
            sub_state='running' if alive else 'dead',
            main_pid=pid if alive else 0,
            n_restarts=n_restarts,
            result='success' if alive else 'exit-code',
            exec_main_status=0 if alive else 1,
        ),
        mode='EXECUTE',
        status=status,
        desired=desired,
        reason=None if ready else 'NOT_READY',
        alive=alive,
        ready=ready,
        execute_healthy=execute_healthy,
        dispatch_eligible=dispatch_eligible,
        demo_execution_ready=execute_healthy,
        uptime_seconds=now - started if alive else None,
        account_recovered=account_recovered,
        account_age_seconds=account_age_seconds,
        last_reconciled_at=datetime.fromtimestamp(now - 1, timezone.utc).isoformat(),
        last_recovery_seconds=last_recovery_seconds,
        market_fresh=market_fresh,
        market_batch_age_seconds=market_batch_age_seconds,
        last_valid_market_at=datetime.fromtimestamp(now - 1, timezone.utc).isoformat(),
        unknown_count=unknown_count,
        unknown_orders=list(unknown_orders or []),
        open_orders=[{'client_order_id': f'oid-{i}'} for i in range(open_orders_count)],
        positions=[{'symbol': 'NVDAUSDT', 'quantity': '0.04'} for _ in range(positions_count)],
        recent_orders=[
            {'client_order_id': 'fl-1', 'status': 'FILLED', 'exchange_updated_at': '2026-11-22T16:00:00+00:00'},
            {'client_order_id': 'fl-2', 'status': 'NEW', 'exchange_updated_at': '2026-11-22T16:20:00+00:00'},
        ],
        grid=dict(
            deployment_id='fl-demo-g-00002',
            state='RUNNING' if execute_healthy else 'PAUSED',
            effective='RUNNING' if dispatch_eligible else 'PAUSED',
            running=execute_healthy,
            live_bar=dispatch_eligible,
            reason=None,
            grid_reason=None,
            last_bar='2026-11-22 16:40:00+00:00',
            reservations_count=0,
        ),
        requests=dict(GET=requests_get, POST=requests_post, DELETE=requests_delete),
        api=dict(
            scope='PROCESS_SINCE_START',
            round_loop=dict(count=10, p50_seconds=0.3, p95_seconds=0.75, max_seconds=1.1),
            endpoints=[
                dict(
                    phase='live', source='session', method='GET', endpoint='/fapi/v3/account',
                    count=api_count,
                    errors={'TIMEOUT': api_errors} if api_errors else {},
                    total_seconds=api_total_seconds,
                    last_seconds=0.12,
                    max_seconds=api_max_seconds,
                ),
            ],
        ),
        recovery=dict(
            state='RECOVERING' if worker_consecutive_failures else 'RECOVERED',
            consecutive_failures=worker_consecutive_failures,
            recovery_attempts=recovery_attempts,
            last_failure_at=None,
            last_failure_reason=last_exception,
            last_recovered_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
            startup_recovered=True,
        ),
        safety_events=dict(
            STALE_ACCOUNT=stale_account,
            STALE_POLL=stale_poll,
            R13_QUOTE_GAP=r13_gap,
            CLOCK_SKEW=clock_skew,
        ),
        last_exception=last_exception,
        campaign=dict(
            enabled=True,
            expires_at='2026-11-23T00:00:00+00:00',
            post_budget=4,
            posts_consumed=2,
            remaining_posts=2,
        ),
        global_budget=dict(
            state=global_budget_state,
            total_orders=total_orders,
            remaining_posts=rem_posts,
            total_notional=total_notional,
            remaining_notional=rem_notional,
            consumed=dict(posts=consumed_posts, submitted_notional=consumed_notional),
        ),
        disk_free_bytes=disk_free_bytes,
        disk_bytes=disk_bytes,
        state_bytes=16_760_000,
        database_bytes=16_740_000,
        sidecar_bytes=0,
    )


def test_sanitize_sample_redacts_secrets_and_identity():
    raw = _make_health_payload()
    raw['api_key'] = 'SHOULD_NEVER_APPEAR'
    raw['secret'] = 'SUPER_SECRET_SENTINEL'
    raw['signature'] = 'HMAC_SIGNATURE_SENTINEL'
    raw['account_id'] = 'DEMO:FactorLab-Demo:full_hash_identity_sentinel'
    raw['equity'] = {'walletBalance': '5000.00'}

    sample = sanitize_sample(raw, observed_at=1_800_000_000.0)
    serialized = json.dumps(sample)
    for sentinel in (
        'SHOULD_NEVER_APPEAR',
        'SUPER_SECRET_SENTINEL',
        'HMAC_SIGNATURE_SENTINEL',
        'full_hash_identity_sentinel',
        'walletBalance',
    ):
        assert sentinel not in serialized
    assert sample['alive'] is True
    assert sample['execute_healthy'] is True
    assert sample['global_budget']['remaining_posts'] == 93
    assert sample['open_orders_count'] == 1
    assert sample['recent_order_status_counts'] == {'FILLED': 1, 'NEW': 1}


def test_bounded_jsonl_enforces_retention_and_byte_cap(tmp_path):
    log_path = tmp_path / 'samples.jsonl'
    now = 1_800_000_000.0

    # Write an expired record and several fresh records exceeding max_bytes
    append_bounded_jsonl(
        log_path, dict(observed_at=now - 10 * 86400, payload='expired'),
        max_bytes=500, retention_seconds=8 * 86400, now=now,
    )
    for i in range(15):
        append_bounded_jsonl(
            log_path, dict(observed_at=now - 600 + i * 30, payload='x' * 60, seq=i),
            max_bytes=500, retention_seconds=8 * 86400, now=now,
        )

    assert log_path.stat().st_size <= 500
    rows = load_jsonl(log_path)
    assert len(rows) >= 2
    assert all(r.get('payload') != 'expired' for r in rows)
    assert rows[-1]['seq'] == 14


def test_window_summary_24h_72h_7d_restarts_coverage_latency_and_budget():
    now = 1_800_000_000.0
    samples = []

    # 3 days ago (inside 7d only)
    samples.append(sanitize_sample(
        _make_health_payload(
            now=now - 4 * 86400, pid=100, starts=1, n_restarts=0,
            api_count=100, api_total_seconds=20.0, api_max_seconds=0.8,
            consumed_posts=2, consumed_notional='10.0', requests_post=0,
        ),
        observed_at=now - 4 * 86400,
    ))
    # 48h ago (inside 72h & 7d)
    samples.append(sanitize_sample(
        _make_health_payload(
            now=now - 48 * 3600, pid=100, starts=1, n_restarts=0,
            api_count=400, api_total_seconds=80.0, api_max_seconds=1.2,
            consumed_posts=4, consumed_notional='20.0', requests_post=2,
        ),
        observed_at=now - 48 * 3600,
    ))
    # Inside 24h: process segment 1 (pid=200) -> unexpected restart -> process segment 2 (pid=201)
    samples.append(sanitize_sample(
        _make_health_payload(
            now=now - 6 * 3600, pid=200, started_at=now - 10 * 3600, starts=2, n_restarts=0,
            api_count=500, api_errors=2, api_total_seconds=100.0, api_max_seconds=1.1,
            consumed_posts=5, consumed_notional='25.0', requests_post=1,
            recovery_attempts=1, stale_account=1, market_batch_age_seconds=1.0,
        ),
        observed_at=now - 6 * 3600,
    ))
    samples.append(sanitize_sample(
        _make_health_payload(
            now=now - 3 * 3600, pid=200, started_at=now - 10 * 3600, starts=2, n_restarts=0,
            api_count=800, api_errors=5, api_total_seconds=190.0, api_max_seconds=2.4,
            consumed_posts=7, consumed_notional='37.5', requests_post=3,
            recovery_attempts=3, stale_account=2, market_batch_age_seconds=2.5,
            unknown_count=1, unknown_orders=[{'client_order_id': 'fl-unk-9'}],
            ready=False, execute_healthy=False, dispatch_eligible=False, account_recovered=False,
        ),
        observed_at=now - 3 * 3600,
    ))
    # Process restarted (pid=201, systemd n_restarts=1, starts=3), counters reset
    samples.append(sanitize_sample(
        _make_health_payload(
            now=now, pid=201, started_at=now - 1800, starts=3, n_restarts=1,
            api_count=200, api_errors=1, api_total_seconds=40.0, api_max_seconds=0.9,
            consumed_posts=8, consumed_notional='43.0', requests_post=1,
            recovery_attempts=1, stale_account=0, market_batch_age_seconds=0.8,
            unknown_count=0,
        ),
        observed_at=now,
    ))

    windows = summarize_all_windows(samples, now=now)
    assert set(windows.keys()) == {'24h', '72h', '7d'}

    w24 = windows['24h']
    assert w24['sample_count'] == 3
    assert w24['window_complete'] is False  # only 6h span inside 24h
    assert w24['service_availability']['alive_ratio'] == 1.0
    assert pytest.approx(w24['service_availability']['ready_ratio'], abs=1e-4) == 2 / 3
    assert w24['sample_coverage']['missing_intervals'] > 0
    assert w24['restarts']['systemd_n_restarts_delta'] == 1
    assert w24['restarts']['pid_changes'] == 1
    assert w24['restarts']['unexpected_restarts'] == 1
    assert w24['restarts']['process_segments'] == 2
    # API segmented delta: seg1 (800-500=300) + seg2 (200) = 500 requests; errors: (5-2)+1 = 4
    assert w24['api_latency']['requests_delta'] == 500
    assert w24['api_latency']['errors_delta'] == 4
    assert w24['api_latency']['observed_max_request_seconds'] == 2.4
    assert w24['api_latency']['abrupt_segment_losses_possible'] is True
    # UNKNOWN
    assert w24['unknown_orders']['samples_with_unknown'] == 1
    assert w24['unknown_orders']['max_unknown_count'] == 1
    assert w24['unknown_orders']['unknown_order_ids'] == ['fl-unk-9']
    # Budget consumption over 6 hours: 8 - 5 = 3 POSTs -> 0.5 POST/h -> 12 POST/day -> 92 / 0.5 = 184h left
    bc24 = w24['budget_consumption']
    assert bc24['posts_consumed_delta'] == 3
    assert pytest.approx(bc24['posts_per_hour'], abs=1e-4) == 0.5
    assert pytest.approx(bc24['posts_per_day'], abs=1e-3) == 12.0
    assert pytest.approx(bc24['estimated_hours_until_exhausted'], abs=1e-2) == 184.0
    assert bc24['budget_monotonic_anomaly'] is False
    assert bc24['policy_note'] == 'READ_ONLY_ACCOUNTING_NO_RESET_OR_EXPANSION'
    assert w24['order_execution']['observed_real_order_activity'] is True

    # 72h includes 48h sample (4 -> 8 = 4 POSTs), 7d includes 4d sample (2 -> 8 = 6 POSTs)
    assert windows['72h']['budget_consumption']['posts_consumed_delta'] == 4
    assert windows['7d']['budget_consumption']['posts_consumed_delta'] == 6


def test_all_seven_alert_classes_dedupe_escalation_ratelimit_and_recovery():
    now = 1_800_000_000.0
    thresholds = MonitorThresholds(
        health_failure_streak=2,
        account_failure_streak=2,
        restart_recovery_healthy_samples=2,
        renotify_seconds=600,
    )
    cfg = AlertDeliveryConfig(
        renotify_seconds=600,
        rate_limit_window_seconds=300,
        rate_limit_max_deliveries=10,
    )

    state = dict(
        version=1, updated_at=None, last_sample=None,
        consecutive_unhealthy=0, consecutive_account_failures=0,
        consecutive_healthy_since_restart=0, restart_timestamps=[],
        active_alerts={}, delivery_timestamps=[],
    )

    # 0. Healthy baseline
    s0 = sanitize_sample(_make_health_payload(now=now), observed_at=now)
    state, ev0 = evaluate_alerts(s0, state, thresholds=thresholds, alert_config=cfg, now=now)
    assert ev0 == []
    assert state['active_alerts'] == {}

    # 1. Fault tick #1: process_exited + order_unknown + disk_space_low (WARN) + budget_low (WARN)
    s1 = sanitize_sample(_make_health_payload(
        now=now + 60, alive=False, status='EXITED', ready=False, execute_healthy=False,
        account_recovered=False, unknown_count=1, unknown_orders=[{'client_order_id': 'fl-u1'}],
        disk_free_bytes=800 * 1024 ** 2, disk_bytes=10 * 1024 ** 3,  # WARN: 800 MiB (8% of 10 GiB)
        consumed_posts=85, total_orders=100,  # WARN: 15 remaining (15%)
    ), observed_at=now + 60)
    state, ev1 = evaluate_alerts(s1, state, thresholds=thresholds, alert_config=cfg, now=now + 60)
    keys1 = {e['alert_key']: e['severity'] for e in ev1}
    assert keys1 == {
        'process_exited': 'CRITICAL',
        'order_unknown': 'CRITICAL',
        'disk_space_low': 'WARNING',
        'budget_low': 'WARNING',
    }

    # 2. Fault tick #2: streak triggers sustained_health_failure + account_recovery_abnormal;
    # disk & budget escalate to CRITICAL; process_exited & order_unknown are deduped!
    s2 = sanitize_sample(_make_health_payload(
        now=now + 120, alive=False, status='EXITED', ready=False, execute_healthy=False,
        account_recovered=False, unknown_count=1, unknown_orders=[{'client_order_id': 'fl-u1'}],
        disk_free_bytes=300 * 1024 ** 2, disk_bytes=10 * 1024 ** 3,  # CRITICAL (< 512 MiB and 3%)
        consumed_posts=95, total_orders=100,  # CRITICAL (5 remaining)
    ), observed_at=now + 120)
    state, ev2 = evaluate_alerts(s2, state, thresholds=thresholds, alert_config=cfg, now=now + 120)
    by_key2 = {e['alert_key']: e['event'] for e in ev2}
    assert by_key2 == {
        'sustained_health_failure': 'ALERT_FIRING',
        'account_recovery_abnormal': 'ALERT_FIRING',
        'disk_space_low': 'ALERT_ESCALATED',
        'budget_low': 'ALERT_ESCALATED',
    }

    # 3. Fault tick #3 (60s later): all active alerts deduped (0 new events)
    state, ev3 = evaluate_alerts(s2, state, thresholds=thresholds, alert_config=cfg, now=now + 180)
    assert ev3 == []

    # 4. After cooldown (600s later), sustained_health_failure escalates (streak=4 >= 2*2) and the other 5 emit reminders
    state, ev4 = evaluate_alerts(s2, state, thresholds=thresholds, alert_config=cfg, now=now + 800)
    assert len(ev4) == 6
    by_key4 = {e['alert_key']: e['event'] for e in ev4}
    assert by_key4['sustained_health_failure'] == 'ALERT_ESCALATED'
    assert {k: v for k, v in by_key4.items() if k != 'sustained_health_failure'} == {
        'process_exited': 'ALERT_REMINDER',
        'order_unknown': 'ALERT_REMINDER',
        'disk_space_low': 'ALERT_REMINDER',
        'budget_low': 'ALERT_REMINDER',
        'account_recovery_abnormal': 'ALERT_REMINDER',
    }

    # 5. Service comes back on a new PID with systemd NRestarts=1 -> unexpected_restart fires, 6 previous alerts resolve
    s5 = sanitize_sample(_make_health_payload(
        now=now + 860, pid=4001, starts=3, n_restarts=1,
    ), observed_at=now + 860)
    state, ev5 = evaluate_alerts(s5, state, thresholds=thresholds, alert_config=cfg, now=now + 860)
    resolved5 = {e['alert_key'] for e in ev5 if e['event'] == 'ALERT_RESOLVED'}
    fired5 = {e['alert_key'] for e in ev5 if e['event'] == 'ALERT_FIRING'}
    assert fired5 == {'unexpected_restart'}
    assert resolved5 == {
        'process_exited', 'order_unknown', 'disk_space_low', 'budget_low',
        'sustained_health_failure', 'account_recovery_abnormal',
    }
    assert list(state['active_alerts'].keys()) == ['unexpected_restart']

    # 6. First healthy tick after restart: still stabilizing (1 < 2)
    s6 = sanitize_sample(_make_health_payload(now=now + 920, pid=4001, starts=3, n_restarts=1), observed_at=now + 920)
    state, ev6 = evaluate_alerts(s6, state, thresholds=thresholds, alert_config=cfg, now=now + 920)
    assert ev6 == []
    assert 'unexpected_restart' in state['active_alerts']

    # 7. Second healthy tick after restart: unexpected_restart resolves!
    s7 = sanitize_sample(_make_health_payload(now=now + 980, pid=4001, starts=3, n_restarts=1), observed_at=now + 980)
    state, ev7 = evaluate_alerts(s7, state, thresholds=thresholds, alert_config=cfg, now=now + 980)
    assert len(ev7) == 1
    assert ev7[0]['event'] == 'ALERT_RESOLVED'
    assert ev7[0]['alert_key'] == 'unexpected_restart'
    assert state['active_alerts'] == {}


def test_webhook_https_enforcement_and_unconfigured_honesty(tmp_path):
    # Unconfigured delivery must honestly report NOT_CONFIGURED and mobile_push_configured=False
    unconfigured = deliver_webhook(
        dict(event='ALERT_FIRING', alert_key='order_unknown', severity='CRITICAL', summary='x'),
        AlertDeliveryConfig(),
    )
    assert unconfigured['configured'] is False
    assert unconfigured['mobile_push_configured'] is False
    assert unconfigured['status'] == 'NOT_CONFIGURED'
    assert unconfigured['delivered'] is False

    # Non-HTTPS public URL must be rejected
    bad_cfg = tmp_path / 'bad-alert.json'
    bad_cfg.write_text(json.dumps({'webhook_url': 'http://alerts.example.com/hook'}), encoding='utf-8')
    with pytest.raises(ValueError, match='https://'):
        load_alert_config(bad_cfg)

    # Loopback HTTP without explicit opt-in is also rejected
    loopback_no_opt = tmp_path / 'loopback-no-opt.json'
    loopback_no_opt.write_text(json.dumps({'webhook_url': 'http://127.0.0.1:8080/hook'}), encoding='utf-8')
    with pytest.raises(ValueError, match='https://'):
        load_alert_config(loopback_no_opt)

    # Valid HTTPS config loads cleanly
    good_cfg = tmp_path / 'good-alert.json'
    good_cfg.write_text(json.dumps({'webhook_url': 'https://hooks.example.invalid/demo'}), encoding='utf-8')
    loaded = load_alert_config(good_cfg)
    assert loaded.webhook_url == 'https://hooks.example.invalid/demo'


def test_alert_self_test_end_to_end_loopback_delivery():
    result = run_alert_self_test()
    assert result['passed'] is True, result
    assert result['dedupe_verified'] is True
    assert result['recovery_verified'] is True
    assert result['sensitive_key_leak_detected'] is False
    assert result['total_webhook_deliveries'] == 12  # 6 firing + 6 resolved
    assert result['unconfigured_behavior']['status'] == 'NOT_CONFIGURED'
    assert result['unconfigured_behavior']['mobile_push_configured'] is False


def test_daily_report_separates_liveness_readiness_and_real_orders(tmp_path):
    monitor_dir = tmp_path / 'monitoring'
    now = 1_800_000_000.0

    # Alive and trading-ready, but ZERO new POST orders in window
    h_ready_no_post = _make_health_payload(
        now=now - 300, alive=True, ready=True, execute_healthy=True, dispatch_eligible=True,
        consumed_posts=7, requests_post=0,
    )
    run_monitor_cycle(h_ready_no_post, monitor_dir, now=now - 300)
    h_latest = _make_health_payload(
        now=now, alive=True, ready=True, execute_healthy=True, dispatch_eligible=True,
        consumed_posts=7, requests_post=0,
    )
    run_monitor_cycle(h_latest, monitor_dir, now=now)

    report = generate_daily_report(monitor_dir, current_health=h_latest, now=now, write_files=True)
    assert report['service_liveness']['classification'] == 'SERVICE_ALIVE'
    assert report['trading_readiness']['classification'] == 'DISPATCH_ELIGIBLE'
    assert report['real_order_execution']['classification'] == 'NO_NEW_ORDER_SUBMISSION_IN_24H_WINDOW'
    assert report['real_order_execution']['observed_real_order_activity_24h'] is False
    assert report['alerts']['external_webhook_configured'] is False
    assert report['alerts']['mobile_push_configured'] is False

    md = render_daily_report_markdown(report)
    assert 'Service Liveness' in md
    assert 'Trading Readiness' in md
    assert 'Real Order Execution' in md
    assert 'NO_NEW_ORDER_SUBMISSION_IN_24H_WINDOW' in md
    assert (monitor_dir / 'daily-reports' / 'daily-report-latest.json').exists()
    assert (monitor_dir / 'daily-reports' / 'daily-report-latest.md').exists()

    # Now check Alive + PAUSED -> SERVICE_ALIVE but NOT_TRADING_READY
    h_paused = _make_health_payload(
        now=now + 60, alive=True, status='PAUSED', desired='PAUSED',
        ready=False, execute_healthy=False, dispatch_eligible=False,
    )
    report_paused = generate_daily_report(monitor_dir, current_health=h_paused, now=now + 60)
    assert report_paused['service_liveness']['classification'] == 'SERVICE_ALIVE'
    assert report_paused['trading_readiness']['classification'] == 'NOT_TRADING_READY'


def test_cli_monitor_report_and_state_root_isolation(tmp_path, capsys):
    state_dir = tmp_path / 'execute-state'
    state_dir.mkdir(mode=0o700)
    monitor_dir = tmp_path / 'monitoring'

    # 1. Monitor dir inside state_root must be refused (never pollute trading state root)
    rc_bad = cloud_main(['monitor', '--state-root', str(state_dir), '--monitor-dir', str(state_dir / 'mon')])
    assert rc_bad == CONFIG_ERROR
    capsys.readouterr()

    # 2. Missing cloud-health.json in monitor mode emits process_exited alert and returns 1
    rc_missing = cloud_main(['monitor', '--state-root', str(state_dir), '--monitor-dir', str(monitor_dir)])
    assert rc_missing == 1
    out_missing = json.loads(capsys.readouterr().out)
    assert out_missing['alive'] is False
    assert 'process_exited' in out_missing['monitoring']['active_alert_keys']

    # 3. Valid cloud-health.json -> monitor returns 0, resolves process_exited, keeps state_dir untouched
    from time import time as now_time
    now = now_time()
    snap = _make_health_payload(now=now)
    # Remove computed fields that evaluate_health computes itself
    raw_health = dict(snap)
    raw_health.pop('systemd', None)
    raw_health['systemd_unit'] = None
    atomic_json(state_dir / 'cloud-health.json', raw_health)
    before_files = sorted(p.name for p in state_dir.iterdir())

    rc_ok = cloud_main(['monitor', '--state-root', str(state_dir), '--monitor-dir', str(monitor_dir)])
    assert rc_ok == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok['ready'] is True
    assert out_ok['monitoring']['active_alert_count'] == 0
    assert any(e['event'] == 'ALERT_RESOLVED' and e['alert_key'] == 'process_exited'
               for e in out_ok['monitoring']['events'])
    assert sorted(p.name for p in state_dir.iterdir()) == before_files

    # 4. Daily report CLI works and outputs Markdown
    rc_rep = cloud_main(['report', '--state-root', str(state_dir), '--monitor-dir', str(monitor_dir),
                         '--write-report', '--markdown'])
    assert rc_rep == 0
    md_out = capsys.readouterr().out
    assert '# FactorLab Azure Demo Daily Operations Report' in md_out

    # 5. Built-in test-alert CLI passes
    rc_self = cloud_main(['test-alert'])
    assert rc_self == 0
    self_out = json.loads(capsys.readouterr().out)
    assert self_out['passed'] is True


def test_rate_limiting_suppresses_excess_warning_deliveries():
    now = 1_800_000_000.0
    cfg = AlertDeliveryConfig(
        webhook_url='https://hooks.example.invalid/demo',
        rate_limit_window_seconds=300,
        rate_limit_max_deliveries=1,
    )
    state = dict(
        version=1, updated_at=None, last_sample=None,
        consecutive_unhealthy=0, consecutive_account_failures=0,
        consecutive_healthy_since_restart=0, restart_timestamps=[],
        active_alerts={}, delivery_timestamps=[now - 10],  # quota already full
    )
    sample = sanitize_sample(_make_health_payload(
        now=now,
        disk_free_bytes=800 * 1024 ** 2, disk_bytes=10 * 1024 ** 3,  # WARNING
        consumed_posts=85, total_orders=100,  # WARNING
    ), observed_at=now)
    _, events = evaluate_alerts(sample, state, alert_config=cfg, now=now)
    assert len(events) == 2
    assert all(e['suppressed_by_rate_limit'] is True for e in events)
    delivery = deliver_webhook(events[0], cfg)
    assert delivery['status'] == 'RATE_LIMITED'
    assert delivery['delivered'] is False

