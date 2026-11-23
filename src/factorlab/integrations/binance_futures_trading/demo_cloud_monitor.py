"""NO LIVE MONEY: Lightweight monitoring, alerting, and daily reporting for Linux Demo runtime.

Reuses existing ``cloud-health.json``, ``demo_cloud_runtime.evaluate_health``, systemd health
timer, and journald without introducing a new trading database or heavy monitoring stack.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from threading import Lock, Thread
from time import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .demo_cloud_storage import atomic_json
from .protocol import SafetyBlocked


WINDOWS = (
    ('24h', 24 * 3600),
    ('72h', 72 * 3600),
    ('7d', 7 * 24 * 3600),
)

DEFAULT_SAMPLE_INTERVAL_SECONDS = 60
DEFAULT_SAMPLE_RETENTION_DAYS = 8
# 8 days @ 1/min = 11,520 samples; at ~2.1-2.6 KiB/sample this needs ~24-29 MiB.
# 48 MiB provides ~1.6x-2x headroom so byte capping never truncates the 7-day window early.
DEFAULT_MAX_SAMPLES_BYTES = 48 * 1024 * 1024
DEFAULT_ALERT_EVENT_RETENTION_DAYS = 14
DEFAULT_MAX_ALERT_EVENTS_BYTES = 2 * 1024 * 1024  # 2 MiB cap
DEFAULT_DAILY_REPORT_RETENTION_DAYS = 14
DEFAULT_BACKUP_ROOT = '/var/backups/factorlab-demo'
DEFAULT_MAINTENANCE_MAX_DOWNTIME_SECONDS = 120.0

_SENSITIVE_KEY_FRAGMENTS = (
    'secret',
    'password',
    'passwd',
    'token',
    'apikey',
    'privatekey',
    'signature',
    'authorization',
    'cookie',
    'credential',
    'accountid',
)


@dataclass(frozen=True)
class MonitorThresholds:
    """Alerting thresholds tuned for low-noise operation on a 1 GiB Azure VM."""
    health_failure_streak: int = 3
    account_failure_streak: int = 2
    account_recovery_consecutive_failures: int = 3
    disk_warn_free_bytes: int = 1024 * 1024 * 1024  # 1 GiB
    disk_critical_free_bytes: int = 512 * 1024 * 1024  # 512 MiB
    disk_warn_free_ratio: float = 0.10
    disk_critical_free_ratio: float = 0.05
    budget_warn_remaining_posts: int = 20
    budget_critical_remaining_posts: int = 10
    budget_warn_remaining_ratio: float = 0.20
    budget_critical_remaining_ratio: float = 0.10
    restart_burst_window_seconds: int = 1800
    restart_burst_threshold: int = 2
    restart_recovery_healthy_samples: int = 5
    maintenance_max_downtime_seconds: float = DEFAULT_MAINTENANCE_MAX_DOWNTIME_SECONDS
    renotify_seconds: int = 3600
    rate_limit_window_seconds: int = 300
    rate_limit_max_deliveries: int = 10


@dataclass(frozen=True)
class AlertDeliveryConfig:
    """External private alert delivery configuration (never stored in repository)."""
    webhook_url: str | None = None
    timeout_seconds: float = 10.0
    bearer_token: str | None = None
    custom_headers: dict[str, str] | None = None
    renotify_seconds: int = 3600
    rate_limit_window_seconds: int = 300
    rate_limit_max_deliveries: int = 10
    allow_insecure_loopback_http: bool = False
    send_daily_report: bool = False
    source_path: str | None = None


def _iso_utc(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def _parse_iso_ts(value) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith('Z'):
        raw = raw[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_c07_backup_records(backup_root: Path | str | None = None) -> dict | None:
    """Read-only inspection of C07 backup-status.json and backup-history.jsonl metadata."""
    raw = backup_root if backup_root is not None else os.environ.get('FACTORLAB_DEMO_BACKUP_ROOT')
    if raw:
        root = Path(raw)
    elif os.name != 'nt':
        root = Path(DEFAULT_BACKUP_ROOT)
    else:
        return None
    try:
        if not root.exists() or not root.is_dir():
            return None
    except OSError:
        return None

    latest_status = None
    status_path = root / 'backup-status.json'
    try:
        if status_path.exists() and status_path.is_file() and not status_path.is_symlink():
            parsed = json.loads(status_path.read_text(encoding='utf-8'))
            if isinstance(parsed, dict):
                latest_status = parsed
    except (OSError, ValueError):
        latest_status = None

    history: list[dict] = []
    history_path = root / 'backup-history.jsonl'
    try:
        if history_path.exists() and history_path.is_file() and not history_path.is_symlink():
            history = load_jsonl(history_path)
    except OSError:
        history = []

    if latest_status is None and not history:
        return None
    return dict(backup_root=str(root), latest_status=latest_status, history=history)


def _match_planned_maintenance(
    backup_records: dict | None,
    *,
    unit: str | None,
    prev_pid: int | None,
    curr_pid: int | None,
    prev_observed_at: float | None,
    curr_observed_at: float | None,
    n_restarts_increased: bool,
    starts_delta: int,
    service_recovered: bool,
    max_downtime_seconds: float = DEFAULT_MAINTENANCE_MAX_DOWNTIME_SECONDS,
    allow_history_fallback: bool = False,
) -> dict | None:
    """Return verified C07 maintenance info iff this restart strictly matches a succeeded backup window."""
    if backup_records is None or n_restarts_increased or not service_recovered or starts_delta > 1:
        return None
    if prev_observed_at is None or curr_observed_at is None:
        return None

    if unit is None or prev_pid is None or curr_pid is None or prev_pid == curr_pid:
        return None
    latest = backup_records.get('latest_status')
    candidates = []
    if isinstance(latest, dict):
        window = latest.get('maintenance_window')
        if isinstance(window, dict) and window.get('service_control_enabled') is True:
            pre_health = window.get('pre_health')
            post_health = window.get('post_health')
            candidates.append(('c07_backup_status', dict(
                latest, **window,
                pre_pid=pre_health.get('pid') if isinstance(pre_health, dict) else None,
                post_pid=post_health.get('pid') if isinstance(post_health, dict) else None,
            )))
    if allow_history_fallback:
        candidates.extend(('c07_backup_history', entry) for entry in reversed(backup_records['history']))

    for source, entry in candidates:
        downtime = _safe_float(entry.get('downtime_seconds'))
        restarted_ts = _parse_iso_ts(entry.get('restarted_at'))
        if (
            entry.get('status') == 'SUCCEEDED'
            and entry.get('error_category') is None
            and entry.get('restart_succeeded') is True
            and entry.get('reconcile_verified') is True
            and downtime is not None
            and 0.0 <= downtime <= max_downtime_seconds
            and restarted_ts is not None
            and prev_observed_at - 30.0 <= restarted_ts <= curr_observed_at + 30.0
            and entry.get('interrupted_unit') == unit
            and _safe_int(entry.get('pre_pid')) == prev_pid
            and _safe_int(entry.get('post_pid')) == curr_pid
        ):
            return dict(
                source=source,
                backup_name=entry.get('backup_name'),
                started_at=entry.get('started_at'),
                completed_at=entry.get('completed_at'),
                stopped_at=entry.get('stopped_at'),
                restarted_at=entry.get('restarted_at'),
                downtime_seconds=downtime,
                interrupted_unit=unit,
                pre_pid=prev_pid,
                post_pid=curr_pid,
                reconcile_verified=True,
            )
    return None


def _maintenance_service_recovered(sample: dict) -> bool:
    if sample.get('alive') is not True or sample.get('snapshot_error') is not None:
        return False
    status = sample.get('status')
    return (
        status == 'RUNNING'
        or (status == 'READ_ONLY' and sample.get('ready') is True)
        or (
            status == 'PAUSED'
            and sample.get('desired') == 'PAUSED'
            and sample.get('account_recovered') is True
        )
    )


def _evaluate_maintenance_failure(
    backup_records: dict | None,
    *,
    sample: dict,
    maintenance_alert_active: bool,
    max_downtime_seconds: float,
    now_ts: float,
) -> tuple[dict | None, bool]:
    """Detect failed, timed-out, unreconciled, or unrecovered C07 backup maintenance windows."""
    if not isinstance(backup_records, dict):
        return None, False
    latest = backup_records.get('latest_status')
    if not isinstance(latest, dict):
        return None, False
    completed_ts = _parse_iso_ts(latest.get('completed_at')) or _parse_iso_ts(latest.get('started_at'))
    if completed_ts is not None and (now_ts - completed_ts) > 48 * 3600:
        return None, False

    mw = latest.get('maintenance_window') if isinstance(latest.get('maintenance_window'), dict) else {}
    downtime = _safe_float(mw.get('downtime_seconds'))
    restarted_ts = _parse_iso_ts(mw.get('restarted_at')) or completed_ts
    service_running = _maintenance_service_recovered(sample)
    reasons: list[str] = []
    if latest.get('status') != 'SUCCEEDED':
        reasons.append(f"backup_status={latest.get('status')}")
    if latest.get('error_category') is not None:
        reasons.append(f"error_category={latest.get('error_category')}")
    if mw.get('service_control_enabled') is True and mw.get('interrupted_unit'):
        if mw.get('restart_succeeded') is False:
            reasons.append('restart_succeeded=False')
        if mw.get('reconcile_verified') is False:
            reasons.append(f"reconcile_verified=False({mw.get('reconcile_reason')})")
        if downtime is not None and downtime > max_downtime_seconds:
            reasons.append(f"downtime_exceeded={downtime:.2f}s>{max_downtime_seconds:.1f}s")
        recent_maint = restarted_ts is not None and -30.0 <= (now_ts - restarted_ts) <= 600.0
        if not service_running and (recent_maint or maintenance_alert_active):
            reasons.append(f"service_not_recovered(alive={sample.get('alive')},status={sample.get('status')})")
    if not reasons:
        verified_window = mw.get('service_control_enabled') is False or (
            mw.get('service_control_enabled') is True
            and mw.get('restart_succeeded') is True
            and mw.get('reconcile_verified') is True
            and downtime is not None
            and 0.0 <= downtime <= max_downtime_seconds
        )
        return None, completed_ts is not None and service_running and verified_window
    return dict(
        severity='CRITICAL',
        summary=(
            f"C07 backup maintenance failed or exceeded bounds "
            f"(backup={latest.get('backup_name')}, {', '.join(reasons)})"
        ),
        details=dict(
            backup_name=latest.get('backup_name'),
            status=latest.get('status'),
            error_category=latest.get('error_category'),
            started_at=latest.get('started_at'),
            completed_at=latest.get('completed_at'),
            downtime_seconds=downtime,
            max_downtime_seconds=max_downtime_seconds,
            restart_succeeded=mw.get('restart_succeeded'),
            reconcile_verified=mw.get('reconcile_verified'),
            reconcile_reason=mw.get('reconcile_reason'),
            reasons=reasons,
        ),
    ), False


def _parse_decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return dec if dec.is_finite() else None


def _safe_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _safe_int(value) -> int | None:
    if type(value) is int:
        return value
    return None


def _contains_sensitive_key(obj) -> str | None:
    """Recursively verify outbound payloads never include sensitive credential/identity keys."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            normalized = str(key).lower().replace('-', '').replace('_', '')
            if any(frag in normalized for frag in _SENSITIVE_KEY_FRAGMENTS):
                return str(key)
            nested = _contains_sensitive_key(value)
            if nested is not None:
                return nested
    elif isinstance(obj, list):
        for item in obj:
            nested = _contains_sensitive_key(item)
            if nested is not None:
                return nested
    return None


def _validate_webhook_url(url: str, *, allow_insecure_loopback_http: bool = False) -> str:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or '').lower()
    if not host:
        raise ValueError('webhook_url must include a host')
    if scheme == 'https':
        return url.strip()
    if scheme == 'http' and allow_insecure_loopback_http and host in {'127.0.0.1', 'localhost', '::1'}:
        return url.strip()
    raise ValueError('webhook_url must use https:// (http:// is only allowed for explicit loopback testing)')


def load_alert_config(config_path: Path | str | None = None) -> AlertDeliveryConfig:
    """Load optional private external webhook config; missing/unset returns unconfigured."""
    raw_path = config_path if config_path is not None else os.environ.get('FACTORLAB_DEMO_ALERT_CONFIG')
    if not raw_path:
        return AlertDeliveryConfig()
    path = Path(raw_path)
    if not path.exists():
        return AlertDeliveryConfig(source_path=str(path))
    if path.is_symlink() or not path.is_file():
        raise SafetyBlocked(f'alert config must be a regular non-symlink file: {path}')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('alert config must be a JSON object')
    webhook_url = data.get('webhook_url')
    allow_loopback = bool(data.get('allow_insecure_loopback_http', False))
    if webhook_url is not None:
        webhook_url = str(webhook_url).strip() or None
    if webhook_url is not None:
        webhook_url = _validate_webhook_url(webhook_url, allow_insecure_loopback_http=allow_loopback)
    headers = data.get('custom_headers')
    if headers is not None:
        if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise ValueError('custom_headers must be a mapping of string keys to string values')
    bearer = data.get('bearer_token')
    if bearer is not None:
        bearer = str(bearer).strip() or None
    return AlertDeliveryConfig(
        webhook_url=webhook_url,
        timeout_seconds=max(1.0, min(30.0, float(data.get('timeout_seconds', 10.0)))),
        bearer_token=bearer,
        custom_headers=dict(headers) if headers else None,
        renotify_seconds=max(60, int(data.get('renotify_seconds', 3600))),
        rate_limit_window_seconds=max(30, int(data.get('rate_limit_window_seconds', 300))),
        rate_limit_max_deliveries=max(1, int(data.get('rate_limit_max_deliveries', 10))),
        allow_insecure_loopback_http=allow_loopback,
        send_daily_report=bool(data.get('send_daily_report', False)),
        source_path=str(path),
    )


def ensure_monitor_dir(monitor_dir: Path | str) -> Path:
    """Ensure private monitoring directory exists (separate from trading state root)."""
    path = Path(os.path.abspath(monitor_dir))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SafetyBlocked(f'monitor directory cannot traverse symlinks: {part}')
    if not path.exists():
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    elif not path.is_dir():
        raise SafetyBlocked(f'monitor directory path is not a directory: {path}')
    return path


def sanitize_sample(health_payload: dict, *, observed_at: float | None = None) -> dict:
    """Extract a bounded, redacted monitoring sample from a health evaluation payload.

    Explicitly excludes secrets, signatures, full account identifiers, and unbounded order lists.
    """
    now_ts = float(time() if observed_at is None else observed_at)
    systemd_raw = health_payload.get('systemd') if isinstance(health_payload.get('systemd'), dict) else None
    systemd = None
    if systemd_raw is not None:
        systemd = dict(
            unit=systemd_raw.get('unit'),
            available=bool(systemd_raw.get('available')),
            active_state=systemd_raw.get('active_state'),
            sub_state=systemd_raw.get('sub_state'),
            main_pid=_safe_int(systemd_raw.get('main_pid')),
            n_restarts=_safe_int(systemd_raw.get('n_restarts')),
            result=systemd_raw.get('result'),
            exec_main_status=_safe_int(systemd_raw.get('exec_main_status')),
        )

    api = health_payload.get('api') if isinstance(health_payload.get('api'), dict) else {}
    endpoints = api.get('endpoints') if isinstance(api.get('endpoints'), list) else []
    api_total_requests = 0
    api_total_errors = 0
    api_total_seconds = 0.0
    api_max_seconds = None
    for row in endpoints:
        if not isinstance(row, dict):
            continue
        cnt = _safe_int(row.get('count')) or 0
        api_total_requests += cnt
        errors = row.get('errors') if isinstance(row.get('errors'), dict) else {}
        api_total_errors += sum(v for v in errors.values() if type(v) is int)
        tot = _safe_float(row.get('total_seconds'))
        if tot is not None:
            api_total_seconds += tot
        mx = _safe_float(row.get('max_seconds'))
        if mx is not None:
            api_max_seconds = mx if api_max_seconds is None else max(api_max_seconds, mx)

    round_loop = api.get('round_loop') if isinstance(api.get('round_loop'), dict) else {}
    requests_raw = health_payload.get('requests') if isinstance(health_payload.get('requests'), dict) else {}
    requests = {
        'GET': _safe_int(requests_raw.get('GET')) or 0,
        'POST': _safe_int(requests_raw.get('POST')) or 0,
        'DELETE': _safe_int(requests_raw.get('DELETE')) or 0,
    }

    unknown_orders_raw = health_payload.get('unknown_orders')
    unknown_ids: list[str] = []
    if isinstance(unknown_orders_raw, list):
        for item in unknown_orders_raw[:20]:
            if isinstance(item, dict) and item.get('client_order_id'):
                unknown_ids.append(str(item['client_order_id']))
            elif isinstance(item, str):
                unknown_ids.append(item)

    recent_orders = health_payload.get('recent_orders')
    recent_status_counts: dict[str, int] = {}
    last_order_updated_at = None
    if isinstance(recent_orders, list):
        for order in recent_orders:
            if not isinstance(order, dict):
                continue
            st = str(order.get('status') or 'OTHER')
            recent_status_counts[st] = recent_status_counts.get(st, 0) + 1
            upd = order.get('exchange_updated_at')
            if upd is not None and (last_order_updated_at is None or str(upd) > str(last_order_updated_at)):
                last_order_updated_at = str(upd)

    grid_raw = health_payload.get('grid') if isinstance(health_payload.get('grid'), dict) else None
    grid_summary = None
    if grid_raw is not None:
        breakout = grid_raw.get('breakout_exit') if isinstance(grid_raw.get('breakout_exit'), dict) else None
        grid_summary = dict(
            deployment_id=grid_raw.get('deployment_id'),
            state=grid_raw.get('state'),
            effective=grid_raw.get('effective'),
            running=bool(grid_raw.get('running')),
            live_bar=bool(grid_raw.get('live_bar')),
            reason=grid_raw.get('reason'),
            breakout_phase=breakout.get('phase') if breakout else None,
        )

    recovery_raw = health_payload.get('recovery') if isinstance(health_payload.get('recovery'), dict) else {}
    recovery = dict(
        state=recovery_raw.get('state'),
        consecutive_failures=_safe_int(recovery_raw.get('consecutive_failures')),
        recovery_attempts=_safe_int(recovery_raw.get('recovery_attempts')) or 0,
        last_failure_reason=recovery_raw.get('last_failure_reason'),
        startup_recovered=bool(recovery_raw.get('startup_recovered', False)),
    )

    safety_raw = health_payload.get('safety_events') if isinstance(health_payload.get('safety_events'), dict) else {}
    safety_events = {
        key: (_safe_int(safety_raw.get(key)) or 0)
        for key in ('STALE_ACCOUNT', 'STALE_POLL', 'R13_QUOTE_GAP', 'CLOCK_SKEW')
    }

    campaign_raw = health_payload.get('campaign') if isinstance(health_payload.get('campaign'), dict) else {}
    campaign = dict(
        enabled=bool(campaign_raw.get('enabled')) if 'enabled' in campaign_raw else None,
        expires_at=campaign_raw.get('expires_at'),
        post_budget=_safe_int(campaign_raw.get('post_budget')),
        posts_consumed=_safe_int(campaign_raw.get('posts_consumed')),
        remaining_posts=_safe_int(campaign_raw.get('remaining_posts')),
    )

    gb_raw = health_payload.get('global_budget') if isinstance(health_payload.get('global_budget'), dict) else None
    global_budget = None
    if gb_raw is not None:
        consumed = gb_raw.get('consumed') if isinstance(gb_raw.get('consumed'), dict) else {}
        global_budget = dict(
            state=gb_raw.get('state'),
            total_orders=_safe_int(gb_raw.get('total_orders')),
            consumed_posts=_safe_int(consumed.get('posts')),
            remaining_posts=_safe_int(gb_raw.get('remaining_posts')),
            total_notional=str(gb_raw.get('total_notional')) if gb_raw.get('total_notional') is not None else None,
            consumed_notional=str(consumed.get('submitted_notional')) if consumed.get('submitted_notional') is not None else None,
            remaining_notional=str(gb_raw.get('remaining_notional')) if gb_raw.get('remaining_notional') is not None else None,
        )

    open_orders = health_payload.get('open_orders')
    positions = health_payload.get('positions')

    sample = dict(
        version=1,
        observed_at=now_ts,
        observed_at_utc=_iso_utc(now_ts),
        heartbeat_at=_safe_float(health_payload.get('heartbeat_at')),
        started_at=_safe_float(health_payload.get('started_at')),
        pid=_safe_int(health_payload.get('pid')),
        starts=_safe_int(health_payload.get('starts')),
        restart_count=_safe_int(health_payload.get('restart_count')),
        systemd_unit=health_payload.get('systemd_unit'),
        systemd=systemd,
        mode=health_payload.get('mode'),
        status=health_payload.get('status'),
        desired=health_payload.get('desired'),
        reason=health_payload.get('reason'),
        alive=bool(health_payload.get('alive', False)),
        ready=bool(health_payload.get('ready', False)),
        execute_healthy=bool(health_payload.get('execute_healthy', False)),
        dispatch_eligible=bool(health_payload.get('dispatch_eligible', False)),
        demo_execution_ready=bool(health_payload.get('demo_execution_ready', False)),
        uptime_seconds=_safe_float(health_payload.get('uptime_seconds')),
        account_recovered=bool(health_payload.get('account_recovered', False)),
        account_age_seconds=_safe_float(health_payload.get('account_age_seconds')),
        last_reconciled_at=health_payload.get('last_reconciled_at'),
        last_recovery_seconds=_safe_float(health_payload.get('last_recovery_seconds')),
        market_fresh=bool(health_payload.get('market_fresh', False)),
        market_batch_age_seconds=_safe_float(health_payload.get('market_batch_age_seconds')),
        last_valid_market_at=health_payload.get('last_valid_market_at'),
        unknown_count=_safe_int(health_payload.get('unknown_count')),
        unknown_order_ids=unknown_ids,
        open_orders_count=len(open_orders) if isinstance(open_orders, list) else None,
        positions_count=len(positions) if isinstance(positions, list) else None,
        recent_order_status_counts=recent_status_counts,
        last_order_updated_at=last_order_updated_at,
        grid=grid_summary,
        strategy_release=health_payload.get('strategy_release'),
        requests=requests,
        request_failure_rate=_safe_float(health_payload.get('request_failure_rate')),
        api_total_requests=api_total_requests,
        api_total_errors=api_total_errors,
        api_total_seconds=round(api_total_seconds, 6),
        api_max_seconds=round(api_max_seconds, 6) if api_max_seconds is not None else None,
        api_round_p95_seconds=_safe_float(round_loop.get('p95_seconds')),
        api_round_max_seconds=_safe_float(round_loop.get('max_seconds')),
        recovery=recovery,
        safety_events=safety_events,
        last_exception=health_payload.get('last_exception'),
        campaign=campaign,
        global_budget=global_budget,
        disk_free_bytes=_safe_int(health_payload.get('disk_free_bytes')),
        disk_bytes=_safe_int(health_payload.get('disk_bytes')),
        state_bytes=_safe_int(health_payload.get('state_bytes')),
        database_bytes=_safe_int(health_payload.get('database_bytes')),
        sidecar_bytes=_safe_int(health_payload.get('sidecar_bytes')),
        snapshot_error=health_payload.get('snapshot_error'),
    )
    leaked = _contains_sensitive_key(sample)
    if leaked is not None:
        raise SafetyBlocked(f'sanitized monitoring sample contained disallowed key: {leaked}')
    return sample


def _atomic_write_lines(path: Path, lines: list[bytes]) -> None:
    path = Path(os.path.abspath(path))
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            for line in lines:
                stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open('r', encoding='utf-8', errors='replace') as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def append_bounded_jsonl(
    path: Path,
    record: dict,
    *,
    max_bytes: int,
    retention_seconds: int,
    timestamp_key: str = 'observed_at',
    now: float | None = None,
) -> dict:
    """Append a JSONL record and enforce time-based retention + strict byte cap."""
    now_ts = float(time() if now is None else now)
    encoded = (json.dumps(record, allow_nan=False, sort_keys=True, separators=(',', ':'), default=str) + '\n').encode('utf-8')
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)

    # Fast path: if file does not exceed 90% of max_bytes and oldest entry is inside retention, append directly.
    needs_compact = not path.exists() or (path.stat().st_size + len(encoded) > max_bytes)
    cutoff = now_ts - retention_seconds
    if not needs_compact:
        try:
            with path.open('rb') as stream:
                first_line = stream.readline()
            if first_line:
                first_obj = json.loads(first_line.decode('utf-8', errors='replace'))
                first_ts = _safe_float(first_obj.get(timestamp_key))
                if first_ts is not None and first_ts < cutoff - 3600:
                    needs_compact = True
        except (OSError, ValueError):
            needs_compact = True

    if not needs_compact:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(encoded)
            stream.flush()
        return dict(compacted=False, size_bytes=path.stat().st_size)

    kept: list[bytes] = []
    if path.exists():
        with path.open('rb') as stream:
            for raw in stream:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped.decode('utf-8', errors='replace'))
                except ValueError:
                    continue
                ts = _safe_float(obj.get(timestamp_key))
                if ts is not None and ts < cutoff:
                    continue
                kept.append(stripped + b'\n')
    kept.append(encoded)

    # Enforce strict byte ceiling by dropping oldest lines first.
    total = sum(len(line) for line in kept)
    start_idx = 0
    while total > max_bytes and start_idx < len(kept) - 1:
        total -= len(kept[start_idx])
        start_idx += 1
    kept = kept[start_idx:]
    _atomic_write_lines(path, kept)
    return dict(compacted=True, retained_records=len(kept), size_bytes=sum(len(line) for line in kept))


def backfill_from_journald(
    monitor_dir: Path,
    *,
    health_unit: str = 'factorlab-demo-health.service',
    since_days: int = 7,
    max_bytes: int = DEFAULT_MAX_SAMPLES_BYTES,
    retention_days: int = DEFAULT_SAMPLE_RETENTION_DAYS,
    _runner=None,
) -> dict:
    """Import existing health timer JSON snapshots from journald into samples.jsonl without duplicates."""
    monitor_dir = ensure_monitor_dir(monitor_dir)
    samples_path = monitor_dir / 'samples.jsonl'
    existing = load_jsonl(samples_path)
    seen_observations = {(row['observed_at_utc'], row['pid']) for row in existing}

    if _runner is None and os.name == 'nt':
        return dict(imported=0, total_samples=len(existing), source='journald_unavailable_on_windows')

    run_cmd = subprocess.run if _runner is None else _runner
    try:
        proc = run_cmd(
            ['journalctl', '--no-pager', '--all', '-o', 'json', '--since', f'{max(1, int(since_days))} days ago',
             '-u', health_unit],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return dict(imported=0, total_samples=len(existing), error=type(exc).__name__)
    if proc.returncode != 0:
        return dict(imported=0, total_samples=len(existing), error=f'journalctl_rc_{proc.returncode}')

    imported = 0
    merged = list(existing)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get('MESSAGE')
        if isinstance(msg, list) and all(type(b) is int and 0 <= b <= 255 for b in msg):
            msg = bytes(msg).decode('utf-8', errors='replace')
        if not isinstance(msg, str) or not msg.startswith('{'):
            continue
        try:
            payload = json.loads(msg)
        except ValueError:
            continue
        if not isinstance(payload, dict) or 'heartbeat_at' not in payload:
            continue
        # Monitor stdout identifies the sample already written locally. Older health-only
        # invocations have no observation time apart from the journal entry timestamp.
        monitoring = payload.get('monitoring') or {}
        obs_ts = _parse_iso_ts(monitoring.get('observed_at_utc'))
        if obs_ts is None:
            ts_usec = entry.get('__REALTIME_TIMESTAMP')
            if not isinstance(ts_usec, str) or not ts_usec.isdigit():
                continue
            obs_ts = int(ts_usec) / 1_000_000.0
        observation = (_iso_utc(obs_ts), _safe_int(payload.get('pid')))
        if observation in seen_observations:
            continue
        sample = sanitize_sample(payload, observed_at=obs_ts)
        merged.append(sample)
        seen_observations.add(observation)
        imported += 1

    merged.sort(key=lambda row: _safe_float(row.get('observed_at')) or 0.0)
    now_ts = time()
    cutoff = now_ts - retention_days * 86400
    merged = [row for row in merged if (_safe_float(row.get('observed_at')) or 0.0) >= cutoff]
    encoded_lines = [
        (json.dumps(row, allow_nan=False, sort_keys=True, separators=(',', ':'), default=str) + '\n').encode('utf-8')
        for row in merged
    ]
    total = sum(len(x) for x in encoded_lines)
    idx = 0
    while total > max_bytes and idx < len(encoded_lines) - 1:
        total -= len(encoded_lines[idx])
        idx += 1
    _atomic_write_lines(samples_path, encoded_lines[idx:])
    return dict(imported=imported, total_samples=len(encoded_lines) - idx, size_bytes=total)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * pct) - 1))
    return ordered[index]


def _segment_counter_delta(samples: list[dict], extractor) -> tuple[int, int]:
    """Sum monotonic counter deltas segmented by (pid, started_at) so process restarts never go negative.

    Returns (total_delta, segment_count).
    """
    segments: list[tuple[tuple, list[int]]] = []
    current_key = None
    for row in samples:
        pid = row.get('pid')
        started_at = row.get('started_at')
        if pid is None and started_at is None:
            continue
        seg_key = (pid, round(float(started_at), 2) if started_at is not None else None)
        val = extractor(row)
        if val is None:
            continue
        if seg_key != current_key:
            segments.append((seg_key, [val]))
            current_key = seg_key
        else:
            segments[-1][1].append(val)

    total_delta = 0
    for idx, (_, values) in enumerate(segments):
        if not values:
            continue
        if idx == 0:
            total_delta += max(0, values[-1] - values[0])
        else:
            # New process segment started inside the window: its entire observed counter is within the window.
            total_delta += max(0, values[-1])
    return total_delta, len(segments)


def _segment_float_delta(samples: list[dict], extractor) -> float:
    segments: list[tuple[tuple, list[float]]] = []
    current_key = None
    for row in samples:
        pid = row.get('pid')
        started_at = row.get('started_at')
        if pid is None and started_at is None:
            continue
        seg_key = (pid, round(float(started_at), 2) if started_at is not None else None)
        val = extractor(row)
        if val is None:
            continue
        if seg_key != current_key:
            segments.append((seg_key, [val]))
            current_key = seg_key
        else:
            segments[-1][1].append(val)

    total = 0.0
    for idx, (_, values) in enumerate(segments):
        if not values:
            continue
        if idx == 0:
            total += max(0.0, values[-1] - values[0])
        else:
            total += max(0.0, values[-1])
    return total


def summarize_window(
    samples: list[dict],
    *,
    label: str,
    window_seconds: int,
    now: float | None = None,
    expected_interval_seconds: int = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    backup_records: dict | None = None,
    max_maintenance_downtime_seconds: float = DEFAULT_MAINTENANCE_MAX_DOWNTIME_SECONDS,
) -> dict:
    """Compute availability, coverage, restarts, recovery, market freshness, latency, UNKNOWN, and budget metrics."""
    now_ts = float(time() if now is None else now)
    start_ts = now_ts - window_seconds
    in_window = [
        row for row in samples
        if (_safe_float(row.get('observed_at')) or -1.0) >= start_ts
        and (_safe_float(row.get('observed_at')) or float('inf')) <= now_ts + 5.0
    ]
    in_window.sort(key=lambda row: _safe_float(row.get('observed_at')) or 0.0)
    count = len(in_window)

    if count == 0:
        return dict(
            label=label,
            window_seconds=window_seconds,
            window_start_utc=_iso_utc(start_ts),
            window_end_utc=_iso_utc(now_ts),
            sample_count=0,
            observed_span_seconds=0.0,
            window_complete=False,
            service_availability=dict(
                alive_samples=0, alive_ratio=None,
                ready_samples=0, ready_ratio=None,
                execute_healthy_samples=0, execute_healthy_ratio=None,
                dispatch_eligible_samples=0, dispatch_eligible_ratio=None,
            ),
            sample_coverage=dict(
                expected_interval_seconds=expected_interval_seconds,
                expected_samples_full_window=max(1, int(round(window_seconds / expected_interval_seconds))),
                expected_samples_observed_span=0,
                full_window_coverage_ratio=0.0,
                observed_span_coverage_ratio=None,
                missing_intervals=0,
                max_gap_seconds=None,
            ),
            restarts=dict(
                systemd_n_restarts_delta=0, starts_delta=0, pid_changes=0,
                unexpected_restarts=0, planned_maintenance_restarts=0, process_segments=0,
            ),
            account_recovery=dict(
                account_recovered_samples=0, account_recovered_ratio=None,
                account_recovery_failure_samples=0, recovery_attempts_delta=0,
                stale_account_delta=0, recovery_seconds_avg=None,
                recovery_seconds_p95=None, recovery_seconds_max=None,
            ),
            market_freshness=dict(
                market_fresh_samples=0, market_fresh_ratio=None,
                market_stale_samples=0, market_age_avg_seconds=None,
                market_age_p95_seconds=None, market_age_max_seconds=None,
                stale_poll_delta=0, r13_quote_gap_delta=0,
            ),
            api_latency=dict(
                requests_delta=0, errors_delta=0, window_error_rate=None,
                avg_request_seconds=None, observed_max_request_seconds=None,
                round_p95_max_seconds=None, clock_skew_delta=0,
                latency_boundary='OBSERVED_TAIL_BOUND_NOT_FULL_P99',
                abrupt_segment_losses_possible=False,
            ),
            unknown_orders=dict(
                samples_with_unknown=0, max_unknown_count=None,
                current_unknown_count=None, unknown_order_ids=[],
            ),
            order_execution=dict(
                post_requests_delta=0, delete_requests_delta=0,
                budget_posts_delta=None, budget_notional_delta=None,
                observed_real_order_activity=False,
                latest_open_orders_count=None, latest_positions_count=None,
                latest_order_status_counts={},
            ),
            budget_consumption=dict(
                total_orders=None, consumed_posts=None, remaining_posts=None,
                remaining_posts_ratio=None, posts_consumed_delta=None,
                posts_per_hour=None, posts_per_day=None,
                estimated_hours_until_exhausted=None,
                total_notional=None, consumed_notional=None, remaining_notional=None,
                remaining_notional_ratio=None, notional_consumed_delta=None,
                campaign_remaining_posts=None, campaign_post_budget=None,
                campaign_expires_at=None, budget_monotonic_anomaly=False,
                policy_note='READ_ONLY_ACCOUNTING_NO_RESET_OR_EXPANSION',
            ),
        )

    timestamps = [float(row['observed_at']) for row in in_window]
    first_ts, last_ts = timestamps[0], timestamps[-1]
    observed_span = max(0.0, last_ts - first_ts)
    window_complete = (first_ts - start_ts) <= expected_interval_seconds * 2 and observed_span >= max(
        0.0, window_seconds - expected_interval_seconds * 3
    )

    gaps = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]
    gap_threshold = expected_interval_seconds * 2.5
    missing_intervals = sum(max(0, int(round(g / expected_interval_seconds)) - 1) for g in gaps if g > gap_threshold)
    max_gap = max(gaps) if gaps else 0.0

    expected_full = max(1, int(round(window_seconds / expected_interval_seconds)))
    expected_span = max(1, int(round(observed_span / expected_interval_seconds)) + 1)

    alive_samples = sum(1 for row in in_window if row.get('alive'))
    ready_samples = sum(1 for row in in_window if row.get('ready'))
    execute_healthy_samples = sum(1 for row in in_window if row.get('execute_healthy'))
    dispatch_eligible_samples = sum(1 for row in in_window if row.get('dispatch_eligible'))

    # Restarts accounting (separating verified C07 maintenance restarts from unexpected restarts)
    pid_changes = 0
    unexpected_restarts = 0
    planned_maintenance_restarts = 0
    n_restarts_delta = 0
    starts_delta = 0
    prev_pid = None
    prev_starts = None
    prev_n_restarts = None
    prev_desired = None
    prev_obs = None
    for row in in_window:
        pid = row.get('pid')
        starts = _safe_int(row.get('starts'))
        sd = row.get('systemd') if isinstance(row.get('systemd'), dict) else {}
        n_res = _safe_int(sd.get('n_restarts'))
        desired = row.get('desired')
        obs = _safe_float(row.get('observed_at'))

        n_res_inc = prev_n_restarts is not None and n_res is not None and n_res > prev_n_restarts
        starts_inc = (starts - prev_starts) if (prev_starts is not None and starts is not None and starts > prev_starts) else 0
        pid_changed = prev_pid is not None and pid is not None and pid != prev_pid

        if n_res_inc:
            delta = n_res - prev_n_restarts
            n_restarts_delta += delta
            unexpected_restarts += delta
        if starts_inc > 0:
            starts_delta += starts_inc
        if pid_changed:
            pid_changes += 1

        if (starts_inc > 0 or pid_changed) and not n_res_inc and prev_desired not in {'STOPPED', 'HALTED'}:
            is_marked_maint = bool(row.get('planned_maintenance_restart'))
            matched_maint = None if is_marked_maint else _match_planned_maintenance(
                backup_records,
                unit=row.get('systemd_unit'),
                prev_pid=prev_pid,
                curr_pid=pid,
                prev_observed_at=prev_obs,
                curr_observed_at=obs,
                n_restarts_increased=False,
                starts_delta=max(1, starts_inc),
                service_recovered=_maintenance_service_recovered(row),
                max_downtime_seconds=max_maintenance_downtime_seconds,
                allow_history_fallback=True,
            )
            if is_marked_maint or matched_maint is not None:
                planned_maintenance_restarts += 1
            else:
                unexpected_restarts += max(1, starts_inc)

        if pid is not None:
            prev_pid = pid
        if starts is not None:
            prev_starts = starts
        if n_res is not None:
            prev_n_restarts = n_res
        if desired is not None:
            prev_desired = desired
        if obs is not None:
            prev_obs = obs

    # Account recovery
    account_recovered_samples = sum(1 for row in in_window if row.get('account_recovered'))
    account_recovery_failure_samples = count - account_recovered_samples
    rec_attempts_delta, segments_count = _segment_counter_delta(
        in_window, lambda r: _safe_int((r.get('recovery') or {}).get('recovery_attempts'))
    )
    stale_account_delta, _ = _segment_counter_delta(
        in_window, lambda r: _safe_int((r.get('safety_events') or {}).get('STALE_ACCOUNT'))
    )
    rec_latencies = [
        v for v in (_safe_float(row.get('last_recovery_seconds')) for row in in_window) if v is not None
    ]

    # Market freshness
    market_fresh_samples = sum(1 for row in in_window if row.get('market_fresh'))
    market_ages = [
        v for v in (_safe_float(row.get('market_batch_age_seconds')) for row in in_window) if v is not None
    ]
    stale_poll_delta, _ = _segment_counter_delta(
        in_window, lambda r: _safe_int((r.get('safety_events') or {}).get('STALE_POLL'))
    )
    r13_gap_delta, _ = _segment_counter_delta(
        in_window, lambda r: _safe_int((r.get('safety_events') or {}).get('R13_QUOTE_GAP'))
    )

    # API latency & error rate (segmented by process)
    req_delta, _ = _segment_counter_delta(in_window, lambda r: _safe_int(r.get('api_total_requests')))
    err_delta, _ = _segment_counter_delta(in_window, lambda r: _safe_int(r.get('api_total_errors')))
    sec_delta = _segment_float_delta(in_window, lambda r: _safe_float(r.get('api_total_seconds')))
    clock_skew_delta, _ = _segment_counter_delta(
        in_window, lambda r: _safe_int((r.get('safety_events') or {}).get('CLOCK_SKEW'))
    )
    observed_max_latencies = [
        v for v in (_safe_float(row.get('api_max_seconds')) for row in in_window) if v is not None
    ]
    round_p95_values = [
        v for v in (_safe_float(row.get('api_round_p95_seconds')) for row in in_window) if v is not None
    ]

    # UNKNOWN orders
    unknown_counts = [v for v in (_safe_int(row.get('unknown_count')) for row in in_window) if v is not None]
    samples_with_unknown = sum(1 for row in in_window if (_safe_int(row.get('unknown_count')) or 0) > 0)
    unknown_ids_set: list[str] = []
    for row in in_window:
        for oid in row.get('unknown_order_ids') or []:
            if oid not in unknown_ids_set:
                unknown_ids_set.append(str(oid))

    # Order execution & Budget consumption (persistent global_budget is monotonic across restarts)
    post_req_delta, _ = _segment_counter_delta(
        in_window, lambda r: _safe_int((r.get('requests') or {}).get('POST'))
    )
    delete_req_delta, _ = _segment_counter_delta(
        in_window, lambda r: _safe_int((r.get('requests') or {}).get('DELETE'))
    )

    gb_rows = [
        row['global_budget'] for row in in_window
        if isinstance(row.get('global_budget'), dict) and row['global_budget'].get('consumed_posts') is not None
    ]
    budget_posts_delta = None
    budget_notional_delta = None
    budget_monotonic_anomaly = False
    if gb_rows:
        prev_bp = None
        for gb in gb_rows:
            cp = _safe_int(gb.get('consumed_posts'))
            if prev_bp is not None and cp is not None and cp < prev_bp:
                budget_monotonic_anomaly = True
            if cp is not None:
                prev_bp = cp
        first_cp = _safe_int(gb_rows[0].get('consumed_posts'))
        last_cp = _safe_int(gb_rows[-1].get('consumed_posts'))
        if first_cp is not None and last_cp is not None:
            budget_posts_delta = last_cp - first_cp
        first_cn = _parse_decimal(gb_rows[0].get('consumed_notional'))
        last_cn = _parse_decimal(gb_rows[-1].get('consumed_notional'))
        if first_cn is not None and last_cn is not None:
            budget_notional_delta = str(last_cn - first_cn)

    latest = in_window[-1]
    latest_gb = latest.get('global_budget') if isinstance(latest.get('global_budget'), dict) else {}
    latest_campaign = latest.get('campaign') if isinstance(latest.get('campaign'), dict) else {}

    total_orders = _safe_int(latest_gb.get('total_orders'))
    consumed_posts = _safe_int(latest_gb.get('consumed_posts'))
    remaining_posts = _safe_int(latest_gb.get('remaining_posts'))
    remaining_posts_ratio = (
        round(remaining_posts / total_orders, 6)
        if remaining_posts is not None and total_orders and total_orders > 0 else None
    )

    total_notional_dec = _parse_decimal(latest_gb.get('total_notional'))
    remaining_notional_dec = _parse_decimal(latest_gb.get('remaining_notional'))
    remaining_notional_ratio = (
        round(float(remaining_notional_dec / total_notional_dec), 6)
        if remaining_notional_dec is not None and total_notional_dec and total_notional_dec > 0 else None
    )

    span_hours = observed_span / 3600.0
    posts_per_hour = None
    posts_per_day = None
    est_hours_left = None
    if budget_posts_delta is not None and span_hours > 0:
        rate_hr = max(0, budget_posts_delta) / span_hours
        posts_per_hour = round(rate_hr, 6)
        posts_per_day = round(rate_hr * 24.0, 4)
        if rate_hr > 0 and remaining_posts is not None:
            est_hours_left = round(remaining_posts / rate_hr, 2)

    observed_real_order_activity = bool(
        post_req_delta > 0
        or delete_req_delta > 0
        or (budget_posts_delta is not None and budget_posts_delta > 0)
    )

    return dict(
        label=label,
        window_seconds=window_seconds,
        window_start_utc=_iso_utc(start_ts),
        window_end_utc=_iso_utc(now_ts),
        first_sample_utc=_iso_utc(first_ts),
        last_sample_utc=_iso_utc(last_ts),
        sample_count=count,
        observed_span_seconds=round(observed_span, 2),
        window_complete=window_complete,
        service_availability=dict(
            alive_samples=alive_samples,
            alive_ratio=round(alive_samples / count, 6),
            ready_samples=ready_samples,
            ready_ratio=round(ready_samples / count, 6),
            execute_healthy_samples=execute_healthy_samples,
            execute_healthy_ratio=round(execute_healthy_samples / count, 6),
            dispatch_eligible_samples=dispatch_eligible_samples,
            dispatch_eligible_ratio=round(dispatch_eligible_samples / count, 6),
        ),
        sample_coverage=dict(
            expected_interval_seconds=expected_interval_seconds,
            expected_samples_full_window=expected_full,
            expected_samples_observed_span=expected_span,
            full_window_coverage_ratio=round(min(1.0, count / expected_full), 6),
            observed_span_coverage_ratio=round(min(1.0, count / expected_span), 6),
            missing_intervals=missing_intervals,
            max_gap_seconds=round(max_gap, 2),
        ),
        restarts=dict(
            systemd_n_restarts_delta=n_restarts_delta,
            starts_delta=starts_delta,
            pid_changes=pid_changes,
            unexpected_restarts=unexpected_restarts,
            planned_maintenance_restarts=planned_maintenance_restarts,
            process_segments=segments_count,
        ),
        account_recovery=dict(
            account_recovered_samples=account_recovered_samples,
            account_recovered_ratio=round(account_recovered_samples / count, 6),
            account_recovery_failure_samples=account_recovery_failure_samples,
            recovery_attempts_delta=rec_attempts_delta,
            stale_account_delta=stale_account_delta,
            recovery_seconds_avg=round(sum(rec_latencies) / len(rec_latencies), 4) if rec_latencies else None,
            recovery_seconds_p95=round(_percentile(rec_latencies, 0.95), 4) if rec_latencies else None,
            recovery_seconds_max=round(max(rec_latencies), 4) if rec_latencies else None,
        ),
        market_freshness=dict(
            market_fresh_samples=market_fresh_samples,
            market_fresh_ratio=round(market_fresh_samples / count, 6),
            market_stale_samples=count - market_fresh_samples,
            market_age_avg_seconds=round(sum(market_ages) / len(market_ages), 4) if market_ages else None,
            market_age_p95_seconds=round(_percentile(market_ages, 0.95), 4) if market_ages else None,
            market_age_max_seconds=round(max(market_ages), 4) if market_ages else None,
            stale_poll_delta=stale_poll_delta,
            r13_quote_gap_delta=r13_gap_delta,
        ),
        api_latency=dict(
            requests_delta=req_delta,
            errors_delta=err_delta,
            window_error_rate=round(err_delta / req_delta, 6) if req_delta > 0 else None,
            avg_request_seconds=round(sec_delta / req_delta, 6) if req_delta > 0 else None,
            observed_max_request_seconds=round(max(observed_max_latencies), 6) if observed_max_latencies else None,
            round_p95_max_seconds=round(max(round_p95_values), 6) if round_p95_values else None,
            clock_skew_delta=clock_skew_delta,
            latency_boundary='OBSERVED_TAIL_BOUND_NOT_FULL_P99',
            abrupt_segment_losses_possible=segments_count > 1,
        ),
        unknown_orders=dict(
            samples_with_unknown=samples_with_unknown,
            max_unknown_count=max(unknown_counts) if unknown_counts else None,
            current_unknown_count=_safe_int(latest.get('unknown_count')),
            unknown_order_ids=unknown_ids_set,
        ),
        order_execution=dict(
            post_requests_delta=post_req_delta,
            delete_requests_delta=delete_req_delta,
            budget_posts_delta=budget_posts_delta,
            budget_notional_delta=budget_notional_delta,
            observed_real_order_activity=observed_real_order_activity,
            latest_open_orders_count=latest.get('open_orders_count'),
            latest_positions_count=latest.get('positions_count'),
            latest_order_status_counts=latest.get('recent_order_status_counts') or {},
        ),
        budget_consumption=dict(
            total_orders=total_orders,
            consumed_posts=consumed_posts,
            remaining_posts=remaining_posts,
            remaining_posts_ratio=remaining_posts_ratio,
            posts_consumed_delta=budget_posts_delta,
            posts_per_hour=posts_per_hour,
            posts_per_day=posts_per_day,
            estimated_hours_until_exhausted=est_hours_left,
            total_notional=latest_gb.get('total_notional'),
            consumed_notional=latest_gb.get('consumed_notional'),
            remaining_notional=latest_gb.get('remaining_notional'),
            remaining_notional_ratio=remaining_notional_ratio,
            notional_consumed_delta=budget_notional_delta,
            campaign_remaining_posts=latest_campaign.get('remaining_posts'),
            campaign_post_budget=latest_campaign.get('post_budget'),
            campaign_expires_at=latest_campaign.get('expires_at'),
            budget_monotonic_anomaly=budget_monotonic_anomaly,
            policy_note='READ_ONLY_ACCOUNTING_NO_RESET_OR_EXPANSION',
        ),
    )


def summarize_all_windows(
    samples: list[dict],
    *,
    now: float | None = None,
    expected_interval_seconds: int = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    backup_records: dict | None = None,
    max_maintenance_downtime_seconds: float = DEFAULT_MAINTENANCE_MAX_DOWNTIME_SECONDS,
) -> dict[str, dict]:
    now_ts = float(time() if now is None else now)
    return {
        label: summarize_window(
            samples, label=label, window_seconds=seconds, now=now_ts,
            expected_interval_seconds=expected_interval_seconds,
            backup_records=backup_records,
            max_maintenance_downtime_seconds=max_maintenance_downtime_seconds,
        )
        for label, seconds in WINDOWS
    }


def _default_alert_state() -> dict:
    return dict(
        version=1,
        updated_at=None,
        last_sample=None,
        consecutive_unhealthy=0,
        consecutive_account_failures=0,
        consecutive_healthy_since_restart=0,
        restart_timestamps=[],
        active_alerts={},
        delivery_timestamps=[],
    )


def load_alert_state(monitor_dir: Path) -> dict:
    path = monitor_dir / 'alert-state.json'
    if not path.exists():
        return _default_alert_state()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('active_alerts'), dict):
            base = _default_alert_state()
            base.update(data)
            return base
    except (OSError, ValueError):
        pass
    return _default_alert_state()


def _severity_rank(severity: str | None) -> int:
    return {'INFO': 1, 'WARNING': 2, 'CRITICAL': 3}.get(str(severity or '').upper(), 0)


def evaluate_alerts(
    sample: dict,
    state: dict,
    *,
    thresholds: MonitorThresholds = MonitorThresholds(),
    alert_config: AlertDeliveryConfig = AlertDeliveryConfig(),
    now: float | None = None,
    backup_records: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Evaluate all 7 alert classes with deduplication, cooldown/rate limiting, and recovery notifications."""
    now_ts = float(sample.get('observed_at') if now is None else now)
    new_state = json.loads(json.dumps(state))
    prev_sample = new_state.get('last_sample') if isinstance(new_state.get('last_sample'), dict) else None

    # Update streaks
    snapshot_current = sample.get('alive') is True and sample.get('snapshot_error') is None
    is_healthy = snapshot_current and sample.get('ready') is True
    if is_healthy:
        new_state['consecutive_unhealthy'] = 0
    else:
        new_state['consecutive_unhealthy'] = int(new_state.get('consecutive_unhealthy') or 0) + 1

    if sample.get('account_recovered'):
        new_state['consecutive_account_failures'] = 0
    else:
        new_state['consecutive_account_failures'] = int(new_state.get('consecutive_account_failures') or 0) + 1

    # A sample can detect a fault without carrying enough evidence to clear it later.
    detected: dict[str, dict] = {}
    recovery_confirmed: set[str] = set()

    # 1. Process exit / stale heartbeat / systemd mismatch
    systemd = sample.get('systemd') if isinstance(sample.get('systemd'), dict) else {}
    systemd_available = bool(systemd.get('available'))
    systemd_running = (
        not systemd_available
        or (
            systemd.get('active_state') == 'active'
            and systemd.get('sub_state') == 'running'
            and systemd.get('main_pid') == sample.get('pid')
        )
    )
    if (
        not sample.get('alive')
        or sample.get('status') in {'EXITED', 'STOPPED', 'HALTED'}
        or not systemd_running
        or sample.get('snapshot_error') is not None
    ):
        desired = sample.get('desired')
        status = sample.get('status')
        if desired == 'STOPPED' and status == 'STOPPED':
            sev = 'WARNING'
            summary = 'Demo Execute is stopped by operator control (desired=STOPPED)'
        else:
            sev = 'CRITICAL'
            summary = (
                f"Demo Execute process is not alive/running (status={status}, desired={desired}, "
                f"systemd={systemd.get('active_state')}/{systemd.get('sub_state')}, "
                f"snapshot_error={sample.get('snapshot_error')})"
            )
        detected['process_exited'] = dict(
            severity=sev,
            summary=summary,
            details=dict(
                alive=sample.get('alive'),
                status=status,
                desired=desired,
                pid=sample.get('pid'),
                systemd_active_state=systemd.get('active_state'),
                systemd_sub_state=systemd.get('sub_state'),
                systemd_main_pid=systemd.get('main_pid'),
                snapshot_error=sample.get('snapshot_error'),
            ),
        )
    elif snapshot_current and sample.get('status') is not None and sample.get('pid') is not None:
        recovery_confirmed.add('process_exited')

    # 2. Sustained health check failure
    unhealthy_streak = int(new_state['consecutive_unhealthy'])
    if unhealthy_streak >= thresholds.health_failure_streak:
        detected['sustained_health_failure'] = dict(
            severity='CRITICAL' if unhealthy_streak >= thresholds.health_failure_streak * 2 else 'WARNING',
            summary=(
                f"Health check failed for {unhealthy_streak} consecutive samples "
                f"(status={sample.get('status')}, reason={sample.get('reason')}, "
                f"last_exception={sample.get('last_exception')})"
            ),
            details=dict(
                consecutive_unhealthy=unhealthy_streak,
                status=sample.get('status'),
                desired=sample.get('desired'),
                reason=sample.get('reason'),
                alive=sample.get('alive'),
                account_recovered=sample.get('account_recovered'),
                market_fresh=sample.get('market_fresh'),
                last_exception=sample.get('last_exception'),
            ),
        )
    elif is_healthy:
        recovery_confirmed.add('sustained_health_failure')

    # 3. Order UNKNOWN (immediate CRITICAL)
    unknown_count = _safe_int(sample.get('unknown_count'))
    if unknown_count is not None and unknown_count > 0:
        detected['order_unknown'] = dict(
            severity='CRITICAL',
            summary=f"Order UNKNOWN detected: unknown_count={unknown_count}",
            details=dict(
                unknown_count=unknown_count,
                unknown_order_ids=list(sample.get('unknown_order_ids') or [])[:10],
                status=sample.get('status'),
            ),
        )
    elif snapshot_current and unknown_count == 0:
        recovery_confirmed.add('order_unknown')

    # 4. Account recovery abnormal
    recovery = sample.get('recovery') if isinstance(sample.get('recovery'), dict) else {}
    rec_failures = _safe_int(recovery.get('consecutive_failures'))
    acct_streak = int(new_state['consecutive_account_failures'])
    if (
        acct_streak >= thresholds.account_failure_streak
        or (rec_failures is not None and rec_failures >= thresholds.account_recovery_consecutive_failures)
    ):
        detected['account_recovery_abnormal'] = dict(
            severity='CRITICAL' if acct_streak >= thresholds.account_failure_streak else 'WARNING',
            summary=(
                f"Account recovery abnormal (account_recovered={sample.get('account_recovered')}, "
                f"failure_streak={acct_streak}, worker_consecutive_failures={rec_failures}, "
                f"last_failure_reason={recovery.get('last_failure_reason')})"
            ),
            details=dict(
                account_recovered=sample.get('account_recovered'),
                account_age_seconds=sample.get('account_age_seconds'),
                consecutive_account_failures=acct_streak,
                recovery_state=recovery.get('state'),
                worker_consecutive_failures=rec_failures,
                last_failure_reason=recovery.get('last_failure_reason'),
                stale_account_events=(sample.get('safety_events') or {}).get('STALE_ACCOUNT'),
            ),
        )
    elif snapshot_current and sample.get('account_recovered') is True and rec_failures is not None:
        recovery_confirmed.add('account_recovery_abnormal')

    # 5. Disk space low
    disk_free = _safe_int(sample.get('disk_free_bytes'))
    disk_total = _safe_int(sample.get('disk_bytes'))
    free_ratio = (disk_free / disk_total) if (disk_free is not None and disk_total and disk_total > 0) else None
    disk_crit = (
        sample.get('last_exception') == 'DISK_SPACE_LOW'
        or (disk_free is not None and disk_free < thresholds.disk_critical_free_bytes)
        or (free_ratio is not None and free_ratio < thresholds.disk_critical_free_ratio)
    )
    disk_warn = (
        (disk_free is not None and disk_free < thresholds.disk_warn_free_bytes)
        or (free_ratio is not None and free_ratio < thresholds.disk_warn_free_ratio)
    )
    if disk_crit or disk_warn:
        free_gib = round(disk_free / (1024 ** 3), 3) if disk_free is not None else None
        detected['disk_space_low'] = dict(
            severity='CRITICAL' if disk_crit else 'WARNING',
            summary=f"Disk space low: free={free_gib} GiB (ratio={round(free_ratio, 4) if free_ratio is not None else None})",
            details=dict(
                disk_free_bytes=disk_free,
                disk_bytes=disk_total,
                disk_free_gib=free_gib,
                disk_free_ratio=round(free_ratio, 6) if free_ratio is not None else None,
                state_bytes=sample.get('state_bytes'),
            ),
        )
    elif snapshot_current and free_ratio is not None:
        recovery_confirmed.add('disk_space_low')

    # 6. Budget near exhaustion (read-only; never resets or expands)
    gb = sample.get('global_budget') if isinstance(sample.get('global_budget'), dict) else None
    if gb is not None:
        rem_posts = _safe_int(gb.get('remaining_posts'))
        tot_posts = _safe_int(gb.get('total_orders'))
        posts_ratio = (rem_posts / tot_posts) if (rem_posts is not None and tot_posts and tot_posts > 0) else None
        rem_notional = _parse_decimal(gb.get('remaining_notional'))
        tot_notional = _parse_decimal(gb.get('total_notional'))
        notional_ratio = (
            float(rem_notional / tot_notional)
            if (rem_notional is not None and tot_notional and tot_notional > 0) else None
        )
        gb_state = gb.get('state')
        budget_crit = (
            gb_state not in {None, 'RUNNING'}
            or (rem_posts is not None and rem_posts <= thresholds.budget_critical_remaining_posts)
            or (posts_ratio is not None and posts_ratio <= thresholds.budget_critical_remaining_ratio)
            or (notional_ratio is not None and notional_ratio <= thresholds.budget_critical_remaining_ratio)
        )
        budget_warn = (
            (rem_posts is not None and rem_posts <= thresholds.budget_warn_remaining_posts)
            or (posts_ratio is not None and posts_ratio <= thresholds.budget_warn_remaining_ratio)
            or (notional_ratio is not None and notional_ratio <= thresholds.budget_warn_remaining_ratio)
        )
        if budget_crit or budget_warn:
            detected['budget_low'] = dict(
                severity='CRITICAL' if budget_crit else 'WARNING',
                summary=(
                    f"Global Demo POST/notional budget low: state={gb_state}, "
                    f"remaining_posts={rem_posts}/{tot_posts}, "
                    f"remaining_notional={gb.get('remaining_notional')}/{gb.get('total_notional')}"
                ),
                details=dict(
                    global_budget_state=gb_state,
                    remaining_posts=rem_posts,
                    total_orders=tot_posts,
                    remaining_posts_ratio=round(posts_ratio, 6) if posts_ratio is not None else None,
                    remaining_notional=gb.get('remaining_notional'),
                    total_notional=gb.get('total_notional'),
                    remaining_notional_ratio=round(notional_ratio, 6) if notional_ratio is not None else None,
                    policy_note='READ_ONLY_NO_RESET_OR_AUTO_EXPANSION',
                ),
            )
        elif snapshot_current and gb_state == 'RUNNING' and posts_ratio is not None and notional_ratio is not None:
            recovery_confirmed.add('budget_low')

    # 7. Unexpected restart detection vs verified C07 planned maintenance restart
    restart_detected = False
    restart_reason = None
    planned_maintenance: dict | None = None
    if prev_sample is not None:
        prev_sd = prev_sample.get('systemd') if isinstance(prev_sample.get('systemd'), dict) else {}
        prev_n_res = _safe_int(prev_sd.get('n_restarts'))
        curr_n_res = _safe_int(systemd.get('n_restarts'))
        prev_pid = _safe_int(prev_sample.get('pid'))
        curr_pid = _safe_int(sample.get('pid'))
        prev_starts = _safe_int(prev_sample.get('starts'))
        curr_starts = _safe_int(sample.get('starts'))
        n_res_increased = prev_n_res is not None and curr_n_res is not None and curr_n_res > prev_n_res
        starts_inc = (curr_starts - prev_starts) if (prev_starts is not None and curr_starts is not None and curr_starts > prev_starts) else 0
        pid_changed = prev_pid is not None and curr_pid is not None and curr_pid != prev_pid

        if n_res_increased:
            restart_detected = True
            restart_reason = f'systemd NRestarts increased {prev_n_res}->{curr_n_res}'
        elif prev_sample.get('desired') not in {'STOPPED', 'HALTED'} and (pid_changed or starts_inc > 0):
            planned_maintenance = _match_planned_maintenance(
                backup_records,
                unit=sample.get('systemd_unit'),
                prev_pid=prev_pid,
                curr_pid=curr_pid,
                prev_observed_at=_safe_float(prev_sample.get('observed_at')),
                curr_observed_at=now_ts,
                n_restarts_increased=False,
                starts_delta=max(1, starts_inc),
                service_recovered=_maintenance_service_recovered(sample),
                max_downtime_seconds=thresholds.maintenance_max_downtime_seconds,
                allow_history_fallback=False,
            )
            if planned_maintenance is None:
                restart_detected = True
                restart_reason = f'worker restarted (pid {prev_pid}->{curr_pid}, starts {prev_starts}->{curr_starts})'

    # 7b. Failed, timed-out, unreconciled, or unrecovered C07 backup maintenance window must alert
    maint_failure, maint_recovered = _evaluate_maintenance_failure(
        backup_records,
        sample=sample,
        maintenance_alert_active=('maintenance_failure' in new_state['active_alerts']),
        max_downtime_seconds=thresholds.maintenance_max_downtime_seconds,
        now_ts=now_ts,
    )
    if maint_failure is not None:
        detected['maintenance_failure'] = maint_failure
    elif snapshot_current and maint_recovered:
        recovery_confirmed.add('maintenance_failure')

    restart_ts = [
        float(ts) for ts in (new_state.get('restart_timestamps') or [])
        if now_ts - float(ts) <= thresholds.restart_burst_window_seconds
    ]
    if restart_detected:
        restart_ts.append(now_ts)
        new_state['consecutive_healthy_since_restart'] = 0
    elif 'unexpected_restart' in new_state['active_alerts'] and is_healthy:
        new_state['consecutive_healthy_since_restart'] = int(new_state.get('consecutive_healthy_since_restart') or 0) + 1
    elif not is_healthy:
        new_state['consecutive_healthy_since_restart'] = 0
    new_state['restart_timestamps'] = restart_ts

    active_restart = 'unexpected_restart' in new_state['active_alerts']
    restart_still_stabilizing = (
        active_restart
        and int(new_state.get('consecutive_healthy_since_restart') or 0) < thresholds.restart_recovery_healthy_samples
    )
    if restart_detected or restart_still_stabilizing:
        burst = len(restart_ts) >= thresholds.restart_burst_threshold
        prev_active = new_state['active_alerts'].get('unexpected_restart') or {}
        summary = (
            f"Unexpected worker restart detected: {restart_reason} (restarts_in_30m={len(restart_ts)})"
            if restart_detected
            else prev_active.get('summary') or f"Worker stabilizing after restart (restarts_in_30m={len(restart_ts)})"
        )
        sev = 'CRITICAL' if (burst or (restart_reason and 'NRestarts' in restart_reason)) else 'WARNING'
        if active_restart and not restart_detected:
            sev = prev_active.get('severity') or sev
        detected['unexpected_restart'] = dict(
            severity=sev,
            summary=summary,
            details=dict(
                pid=sample.get('pid'),
                starts=sample.get('starts'),
                systemd_n_restarts=systemd.get('n_restarts'),
                restarts_in_window=len(restart_ts),
                healthy_samples_since_restart=new_state['consecutive_healthy_since_restart'],
                recovery_required_samples=thresholds.restart_recovery_healthy_samples,
            ),
        )
    elif is_healthy:
        recovery_confirmed.add('unexpected_restart')

    # State machine transitions: dedupe, cooldown reminder, severity escalation, rate limit, recovery
    renotify_sec = min(thresholds.renotify_seconds, alert_config.renotify_seconds)
    rate_window = alert_config.rate_limit_window_seconds
    rate_max = alert_config.rate_limit_max_deliveries
    delivery_ts = [float(ts) for ts in (new_state.get('delivery_timestamps') or []) if now_ts - float(ts) <= rate_window]

    events: list[dict] = []
    active = new_state['active_alerts']

    if planned_maintenance is not None:
        events.append(dict(
            event='MAINTENANCE_RESTART',
            alert_key='planned_maintenance_restart',
            severity='INFO',
            summary=(
                f"Planned C07 backup maintenance restart verified: backup={planned_maintenance.get('backup_name')}, "
                f"downtime={planned_maintenance.get('downtime_seconds')}s "
                f"(pid {planned_maintenance.get('pre_pid')}->{planned_maintenance.get('post_pid')})"
            ),
            details=planned_maintenance,
            observed_at=now_ts,
            observed_at_utc=_iso_utc(now_ts),
            unit=sample.get('systemd_unit'),
            mode=sample.get('mode'),
            suppressed_by_rate_limit=False,
        ))

    def _can_deliver(is_high_priority: bool) -> bool:
        if len(delivery_ts) < rate_max:
            return True
        # Reserve small burst headroom (+2) for CRITICAL or RESOLVED transitions so critical outages are not silently dropped
        return is_high_priority and len(delivery_ts) < rate_max + 2

    # 1) Fire / Dedupe / Escalate / Remind
    for key, info in sorted(detected.items()):
        existing = active.get(key)
        if existing is None:
            can_send = _can_deliver(info['severity'] == 'CRITICAL')
            event = dict(
                event='ALERT_FIRING',
                alert_key=key,
                severity=info['severity'],
                summary=info['summary'],
                details=info['details'],
                observed_at=now_ts,
                observed_at_utc=_iso_utc(now_ts),
                first_triggered_at=now_ts,
                first_triggered_utc=_iso_utc(now_ts),
                occurrences=1,
                unit=sample.get('systemd_unit'),
                mode=sample.get('mode'),
                suppressed_by_rate_limit=not can_send,
            )
            active[key] = dict(
                alert_key=key,
                severity=info['severity'],
                summary=info['summary'],
                details=info['details'],
                first_triggered_at=now_ts,
                last_seen_at=now_ts,
                last_notified_at=now_ts if can_send else None,
                occurrences=1,
            )
            if can_send:
                delivery_ts.append(now_ts)
            events.append(event)
        else:
            existing['last_seen_at'] = now_ts
            existing['occurrences'] = int(existing.get('occurrences') or 1) + 1
            existing['details'] = info['details']
            escalated = _severity_rank(info['severity']) > _severity_rank(existing.get('severity'))
            last_notified = _safe_float(existing.get('last_notified_at'))
            due_reminder = last_notified is None or (now_ts - last_notified) >= renotify_sec
            if escalated:
                existing['severity'] = info['severity']
                existing['summary'] = info['summary']
            if escalated or due_reminder:
                can_send = _can_deliver(info['severity'] == 'CRITICAL')
                if can_send:
                    existing['last_notified_at'] = now_ts
                    delivery_ts.append(now_ts)
                events.append(dict(
                    event='ALERT_ESCALATED' if escalated else 'ALERT_REMINDER',
                    alert_key=key,
                    severity=existing['severity'],
                    summary=info['summary'],
                    details=info['details'],
                    observed_at=now_ts,
                    observed_at_utc=_iso_utc(now_ts),
                    first_triggered_at=existing['first_triggered_at'],
                    first_triggered_utc=_iso_utc(existing['first_triggered_at']),
                    occurrences=existing['occurrences'],
                    unit=sample.get('systemd_unit'),
                    mode=sample.get('mode'),
                    suppressed_by_rate_limit=not can_send,
                ))

    # 2) Recovery notifications for active alerts with affirmative evidence
    for key in sorted(active.keys() & recovery_confirmed):
        previous = active.pop(key)
        first_ts = _safe_float(previous.get('first_triggered_at')) or now_ts
        duration = max(0.0, now_ts - first_ts)
        can_send = _can_deliver(True)
        if can_send:
            delivery_ts.append(now_ts)
        events.append(dict(
            event='ALERT_RESOLVED',
            alert_key=key,
            severity='INFO',
            previous_severity=previous.get('severity'),
            summary=f"Recovered: {key} (duration={round(duration, 1)}s)",
            details=dict(
                previous_summary=previous.get('summary'),
                duration_seconds=round(duration, 2),
                occurrences=previous.get('occurrences'),
                current_status=sample.get('status'),
                alive=sample.get('alive'),
                ready=sample.get('ready'),
            ),
            observed_at=now_ts,
            observed_at_utc=_iso_utc(now_ts),
            first_triggered_at=first_ts,
            first_triggered_utc=_iso_utc(first_ts),
            duration_seconds=round(duration, 2),
            unit=sample.get('systemd_unit'),
            mode=sample.get('mode'),
            suppressed_by_rate_limit=not can_send,
        ))

    new_state['delivery_timestamps'] = delivery_ts
    new_state['updated_at'] = now_ts
    new_state['last_sample'] = dict(
        observed_at=sample.get('observed_at'),
        pid=sample.get('pid'),
        starts=sample.get('starts'),
        desired=sample.get('desired'),
        status=sample.get('status'),
        systemd=dict(n_restarts=systemd.get('n_restarts')) if systemd else None,
    )
    return new_state, events


def deliver_webhook(event: dict, alert_config: AlertDeliveryConfig) -> dict:
    """Deliver a redacted alert event to the configured HTTPS webhook (if configured)."""
    if event.get('suppressed_by_rate_limit'):
        return dict(
            configured=bool(alert_config.webhook_url),
            mobile_push_configured=False,
            status='RATE_LIMITED',
            delivered=False,
        )
    if not alert_config.webhook_url:
        return dict(
            configured=False,
            mobile_push_configured=False,
            status='NOT_CONFIGURED',
            delivered=False,
            note='External webhook credentials are not configured; alert recorded locally only.',
        )

    url = _validate_webhook_url(
        alert_config.webhook_url,
        allow_insecure_loopback_http=alert_config.allow_insecure_loopback_http,
    )
    payload = dict(
        source='factorlab-demo-monitor',
        environment='DEMO_NO_LIVE_MONEY',
        host=socket.gethostname(),
        event=event.get('event'),
        alert_key=event.get('alert_key'),
        severity=event.get('severity'),
        previous_severity=event.get('previous_severity'),
        unit=event.get('unit'),
        mode=event.get('mode'),
        summary=event.get('summary'),
        details=event.get('details'),
        observed_at_utc=event.get('observed_at_utc'),
        first_triggered_utc=event.get('first_triggered_utc'),
        duration_seconds=event.get('duration_seconds'),
        occurrences=event.get('occurrences'),
    )
    leaked = _contains_sensitive_key(payload)
    if leaked is not None:
        raise SafetyBlocked(f'refusing to send alert payload containing sensitive key: {leaked}')

    body = json.dumps(payload, allow_nan=False, sort_keys=True, default=str).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'User-Agent': 'FactorLab-Demo-Monitor/1.0'}
    if alert_config.custom_headers:
        headers.update(alert_config.custom_headers)
    if alert_config.bearer_token:
        headers['Authorization'] = f'Bearer {alert_config.bearer_token}'

    req = Request(url, data=body, headers=headers, method='POST')
    try:
        with urlopen(req, timeout=alert_config.timeout_seconds) as resp:
            code = getattr(resp, 'status', 200)
            return dict(
                configured=True,
                mobile_push_configured=False,
                status='DELIVERED' if 200 <= code < 300 else 'HTTP_ERROR',
                http_status=code,
                delivered=200 <= code < 300,
            )
    except HTTPError as exc:
        return dict(
            configured=True,
            mobile_push_configured=False,
            status='HTTP_ERROR',
            http_status=exc.code,
            delivered=False,
            error=f'HTTP_{exc.code}',
        )
    except (URLError, TimeoutError, OSError) as exc:
        return dict(
            configured=True,
            mobile_push_configured=False,
            status='DELIVERY_FAILED',
            delivered=False,
            error=type(exc).__name__,
        )


def run_monitor_cycle(
    health_payload: dict,
    monitor_dir: Path | str,
    *,
    alert_config: AlertDeliveryConfig | None = None,
    thresholds: MonitorThresholds = MonitorThresholds(),
    now: float | None = None,
    max_samples_bytes: int = DEFAULT_MAX_SAMPLES_BYTES,
    retention_days: int = DEFAULT_SAMPLE_RETENTION_DAYS,
    backup_root: Path | str | None = None,
) -> dict:
    """Record one health sample, evaluate alerts, deliver webhook if configured, and persist state."""
    monitor_path = ensure_monitor_dir(monitor_dir)
    now_ts = float(time() if now is None else now)
    cfg = load_alert_config() if alert_config is None else alert_config
    backup_records = load_c07_backup_records(backup_root)

    sample = sanitize_sample(health_payload, observed_at=now_ts)
    state = load_alert_state(monitor_path)
    new_state, events = evaluate_alerts(
        sample, state, thresholds=thresholds, alert_config=cfg, now=now_ts,
        backup_records=backup_records,
    )

    for event in events:
        if event.get('event') == 'MAINTENANCE_RESTART':
            sample['planned_maintenance_restart'] = True
            sample['maintenance_backup_name'] = (event.get('details') or {}).get('backup_name')
            break

    samples_file = monitor_path / 'samples.jsonl'
    storage_info = append_bounded_jsonl(
        samples_file,
        sample,
        max_bytes=max_samples_bytes,
        retention_seconds=retention_days * 86400,
        timestamp_key='observed_at',
        now=now_ts,
    )

    events_file = monitor_path / 'alert-events.jsonl'
    dispatched: list[dict] = []
    for event in events:
        if event.get('event') == 'MAINTENANCE_RESTART':
            delivery = dict(
                configured=bool(cfg.webhook_url),
                mobile_push_configured=False,
                status='MAINTENANCE_RECORDED_ONLY',
                delivered=False,
            )
        else:
            delivery = deliver_webhook(event, cfg)
        record = dict(event, delivery=delivery)
        append_bounded_jsonl(
            events_file,
            record,
            max_bytes=DEFAULT_MAX_ALERT_EVENTS_BYTES,
            retention_seconds=DEFAULT_ALERT_EVENT_RETENTION_DAYS * 86400,
            timestamp_key='observed_at',
            now=now_ts,
        )
        dispatched.append(record)

    atomic_json(monitor_path / 'alert-state.json', new_state)
    return dict(
        observed_at_utc=sample['observed_at_utc'],
        monitor_dir=str(monitor_path),
        samples_storage=storage_info,
        active_alert_keys=sorted(new_state['active_alerts'].keys()),
        active_alert_count=len(new_state['active_alerts']),
        events=dispatched,
        external_webhook_configured=bool(cfg.webhook_url),
        mobile_push_configured=False,
    )


def generate_daily_report(
    monitor_dir: Path | str,
    *,
    current_health: dict | None = None,
    alert_config: AlertDeliveryConfig | None = None,
    now: float | None = None,
    write_files: bool = False,
    expected_interval_seconds: int = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    backup_root: Path | str | None = None,
) -> dict:
    """Generate a daily operations report explicitly separating liveness, readiness, and real order execution."""
    monitor_path = ensure_monitor_dir(monitor_dir)
    now_ts = float(time() if now is None else now)
    cfg = load_alert_config() if alert_config is None else alert_config
    backup_records = load_c07_backup_records(backup_root)

    samples = load_jsonl(monitor_path / 'samples.jsonl')
    alert_state = load_alert_state(monitor_path)
    alert_events = load_jsonl(monitor_path / 'alert-events.jsonl')

    latest_sample = sanitize_sample(current_health, observed_at=now_ts) if current_health is not None else (
        samples[-1] if samples else None
    )
    windows = summarize_all_windows(
        samples, now=now_ts, expected_interval_seconds=expected_interval_seconds,
        backup_records=backup_records,
    )
    w24 = windows['24h']

    events_24h = [e for e in alert_events if (_safe_float(e.get('observed_at')) or 0.0) >= now_ts - 86400]
    fired_24h = sum(1 for e in events_24h if e.get('event') in {'ALERT_FIRING', 'ALERT_ESCALATED'})
    resolved_24h = sum(1 for e in events_24h if e.get('event') == 'ALERT_RESOLVED')
    maintenance_events_24h = sum(1 for e in events_24h if e.get('event') == 'MAINTENANCE_RESTART')

    # Three explicitly separated pillars
    service_liveness = dict(
        classification='SERVICE_ALIVE' if (latest_sample and latest_sample.get('alive')) else 'SERVICE_NOT_ALIVE',
        current_alive=bool(latest_sample.get('alive')) if latest_sample else False,
        current_pid=latest_sample.get('pid') if latest_sample else None,
        current_status=latest_sample.get('status') if latest_sample else None,
        systemd=latest_sample.get('systemd') if latest_sample else None,
        uptime_seconds=latest_sample.get('uptime_seconds') if latest_sample else None,
        availability_24h_ratio=w24['service_availability']['alive_ratio'],
        restarts_24h=w24['restarts'],
    )

    trading_readiness = dict(
        classification=(
            'DISPATCH_ELIGIBLE' if (latest_sample and latest_sample.get('dispatch_eligible'))
            else 'EXECUTE_HEALTHY_WAITING_LIVE_BAR' if (latest_sample and latest_sample.get('execute_healthy'))
            else 'NOT_TRADING_READY'
        ),
        current_ready=bool(latest_sample.get('ready')) if latest_sample else False,
        current_execute_healthy=bool(latest_sample.get('execute_healthy')) if latest_sample else False,
        current_dispatch_eligible=bool(latest_sample.get('dispatch_eligible')) if latest_sample else False,
        current_desired=latest_sample.get('desired') if latest_sample else None,
        account_recovered=bool(latest_sample.get('account_recovered')) if latest_sample else False,
        account_age_seconds=latest_sample.get('account_age_seconds') if latest_sample else None,
        market_fresh=bool(latest_sample.get('market_fresh')) if latest_sample else False,
        market_batch_age_seconds=latest_sample.get('market_batch_age_seconds') if latest_sample else None,
        grid=latest_sample.get('grid') if latest_sample else None,
        unknown_count=latest_sample.get('unknown_count') if latest_sample else None,
        ready_24h_ratio=w24['service_availability']['ready_ratio'],
        dispatch_eligible_24h_ratio=w24['service_availability']['dispatch_eligible_ratio'],
        distinction_note='Service liveness (systemd active/PID alive) does not imply trading readiness.',
    )

    exec_24h = w24['order_execution']
    real_order_execution = dict(
        classification=(
            'REAL_DEMO_ORDER_ACTIVITY_OBSERVED' if exec_24h['observed_real_order_activity']
            else 'NO_NEW_ORDER_SUBMISSION_IN_24H_WINDOW'
        ),
        observed_real_order_activity_24h=exec_24h['observed_real_order_activity'],
        post_requests_delta_24h=exec_24h['post_requests_delta'],
        delete_requests_delta_24h=exec_24h['delete_requests_delta'],
        budget_posts_delta_24h=exec_24h['budget_posts_delta'],
        budget_notional_delta_24h=exec_24h['budget_notional_delta'],
        current_open_orders_count=latest_sample.get('open_orders_count') if latest_sample else None,
        current_positions_count=latest_sample.get('positions_count') if latest_sample else None,
        recent_order_status_counts=latest_sample.get('recent_order_status_counts') if latest_sample else {},
        last_order_updated_at=latest_sample.get('last_order_updated_at') if latest_sample else None,
        distinction_note='Trading readiness does not prove new orders were posted or filled; check POST/budget deltas.',
    )

    report = dict(
        version=1,
        strategy_release=latest_sample.get('strategy_release') if latest_sample else None,
        strategy_switch_events=list({
            e['seq']: e for sample in samples + ([latest_sample] if latest_sample else [])
            for e in (sample.get('strategy_release') or {}).get('events', [])
            if e.get('at', 0) >= now_ts - 86400
        }.values()),
        report_date_utc=datetime.fromtimestamp(now_ts, timezone.utc).strftime('%Y-%m-%d'),
        generated_at_utc=_iso_utc(now_ts),
        environment='BINANCE_USDM_DEMO_NO_LIVE_MONEY',
        service_liveness=service_liveness,
        trading_readiness=trading_readiness,
        real_order_execution=real_order_execution,
        windows=windows,
        budget_summary=dict(
            latest_global_budget=latest_sample.get('global_budget') if latest_sample else None,
            latest_campaign=latest_sample.get('campaign') if latest_sample else None,
            consumption_24h=w24['budget_consumption'],
            consumption_72h=windows['72h']['budget_consumption'],
            consumption_7d=windows['7d']['budget_consumption'],
            policy='READ_ONLY_NEVER_RESET_OR_AUTO_EXPAND',
        ),
        alerts=dict(
            active_count=len(alert_state.get('active_alerts') or {}),
            active_keys=sorted((alert_state.get('active_alerts') or {}).keys()),
            fired_24h=fired_24h,
            resolved_24h=resolved_24h,
            maintenance_events_24h=maintenance_events_24h,
            external_webhook_configured=bool(cfg.webhook_url),
            mobile_push_configured=False,
        ),
        storage=dict(
            disk_free_bytes=latest_sample.get('disk_free_bytes') if latest_sample else None,
            disk_bytes=latest_sample.get('disk_bytes') if latest_sample else None,
            state_bytes=latest_sample.get('state_bytes') if latest_sample else None,
            database_bytes=latest_sample.get('database_bytes') if latest_sample else None,
            sidecar_bytes=latest_sample.get('sidecar_bytes') if latest_sample else None,
            monitor_samples_bytes=(monitor_path / 'samples.jsonl').stat().st_size if (monitor_path / 'samples.jsonl').exists() else 0,
            monitor_alert_events_bytes=(monitor_path / 'alert-events.jsonl').stat().st_size if (monitor_path / 'alert-events.jsonl').exists() else 0,
        ),
    )

    if write_files:
        reports_dir = ensure_monitor_dir(monitor_path / 'daily-reports')
        day_tag = report['report_date_utc']
        atomic_json(reports_dir / f'daily-report-{day_tag}.json', report)
        atomic_json(reports_dir / 'daily-report-latest.json', report)
        md_text = render_daily_report_markdown(report)
        _atomic_write_lines(reports_dir / f'daily-report-{day_tag}.md', [md_text.encode('utf-8')])
        _atomic_write_lines(reports_dir / 'daily-report-latest.md', [md_text.encode('utf-8')])
        _prune_old_reports(reports_dir, keep_days=DEFAULT_DAILY_REPORT_RETENTION_DAYS)

    return report


def _prune_old_reports(reports_dir: Path, *, keep_days: int) -> None:
    dated = sorted(
        [p for p in reports_dir.iterdir() if p.is_file() and p.name.startswith('daily-report-20')],
        key=lambda p: p.name,
    )
    # Keep at most keep_days * 2 files (.json + .md per date)
    excess = len(dated) - keep_days * 2
    for path in dated[:max(0, excess)]:
        path.unlink(missing_ok=True)


def _fmt_pct(value: float | None) -> str:
    return 'N/A' if value is None else f'{value * 100:.2f}%'


def _fmt_num(value, suffix: str = '') -> str:
    if value is None:
        return 'N/A'
    if isinstance(value, float):
        return f'{value:.3f}{suffix}'
    return f'{value}{suffix}'


def render_daily_report_markdown(report: dict) -> str:
    """Render a concise, honest Markdown daily operations report."""
    live = report['service_liveness']
    ready = report['trading_readiness']
    orders = report['real_order_execution']
    budget = report['budget_summary']
    alerts = report['alerts']
    storage = report['storage']

    disk_free_gib = (
        round(storage['disk_free_bytes'] / (1024 ** 3), 2)
        if storage.get('disk_free_bytes') is not None else None
    )
    lines = [
        f"# FactorLab Azure Demo Daily Operations Report ({report['report_date_utc']} UTC)",
        '',
        f"- Generated (UTC): `{report['generated_at_utc']}`",
        f"- Environment: `{report['environment']}`",
        f"- External Webhook Configured: `{alerts['external_webhook_configured']}` (Mobile Push Configured: `{alerts['mobile_push_configured']}`)",
        '',
        '## 1. Service Liveness (服务存活)',
        f"- Classification: `{live['classification']}`",
        f"- Alive: `{live['current_alive']}` | Status: `{live['current_status']}` | PID: `{live['current_pid']}` | Uptime: `{_fmt_num(live['uptime_seconds'], 's')}`",
        f"- 24h Alive Ratio: `{_fmt_pct(live['availability_24h_ratio'])}` | 24h Unexpected Restarts: `{live['restarts_24h']['unexpected_restarts']}` | Planned Maintenance Restarts: `{live['restarts_24h'].get('planned_maintenance_restarts', 0)}` (systemd NRestarts delta: `{live['restarts_24h']['systemd_n_restarts_delta']}`)",
        '',
        '## 2. Trading Readiness (交易就绪)',
        f"- Classification: `{ready['classification']}`",
        f"- Desired: `{ready['current_desired']}` | Ready: `{ready['current_ready']}` | Execute Healthy: `{ready['current_execute_healthy']}` | Dispatch Eligible: `{ready['current_dispatch_eligible']}`",
        f"- Account Recovered: `{ready['account_recovered']}` (age: `{_fmt_num(ready['account_age_seconds'], 's')}`) | Market Fresh: `{ready['market_fresh']}` (age: `{_fmt_num(ready['market_batch_age_seconds'], 's')}`) | UNKNOWN: `{ready['unknown_count']}`",
        f"- 24h Ready Ratio: `{_fmt_pct(ready['ready_24h_ratio'])}` | 24h Dispatch Eligible Ratio: `{_fmt_pct(ready['dispatch_eligible_24h_ratio'])}`",
        '',
        '## 3. Real Order Execution (真实订单执行)',
        f"- Classification: `{orders['classification']}`",
        f"- 24h New Order Activity Observed: `{orders['observed_real_order_activity_24h']}`",
        f"- 24h Deltas: POST requests `{orders['post_requests_delta_24h']}` | DELETE requests `{orders['delete_requests_delta_24h']}` | Budget POSTs `{orders['budget_posts_delta_24h']}` | Submitted Notional `{orders['budget_notional_delta_24h']}`",
        f"- Current Open Orders: `{orders['current_open_orders_count']}` | Current Positions: `{orders['current_positions_count']}`",
        '',
        '## 4. Rolling Window Metrics (24h / 72h / 7d)',
        '| Window | Samples | Window Complete | Span Coverage | Full Coverage | Alive | Ready | Unexpected / Maint Restarts | Account Recovered | Market Fresh | API Avg / Max Lat (s) | Max UNKNOWN |',
        '| --- | ---: | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: |',
    ]
    for label in ('24h', '72h', '7d'):
        w = report['windows'][label]
        cov = w['sample_coverage']
        avail = w['service_availability']
        acc = w['account_recovery']
        mkt = w['market_freshness']
        lat = w['api_latency']
        unk = w['unknown_orders']
        lines.append(
            f"| {label} | {w['sample_count']} | {w['window_complete']} | "
            f"{_fmt_pct(cov['observed_span_coverage_ratio'])} | {_fmt_pct(cov['full_window_coverage_ratio'])} | "
            f"{_fmt_pct(avail['alive_ratio'])} | {_fmt_pct(avail['ready_ratio'])} | "
            f"{w['restarts']['unexpected_restarts']} / {w['restarts'].get('planned_maintenance_restarts', 0)} | {_fmt_pct(acc['account_recovered_ratio'])} | "
            f"{_fmt_pct(mkt['market_fresh_ratio'])} | {_fmt_num(lat['avg_request_seconds'])} / {_fmt_num(lat['observed_max_request_seconds'])} | "
            f"{_fmt_num(unk['max_unknown_count'])} |"
        )

    gb = budget.get('latest_global_budget') or {}
    c24 = budget['consumption_24h']
    c72 = budget['consumption_72h']
    c7d = budget['consumption_7d']
    lines.extend([
        '',
        '## 5. POST Budget & Consumption Rate (Read-Only)',
        f"- Global Budget State: `{gb.get('state')}` | Remaining POSTs: `{gb.get('remaining_posts')}/{gb.get('total_orders')}` ({_fmt_pct(c24.get('remaining_posts_ratio'))}) | Remaining Notional: `{gb.get('remaining_notional')}/{gb.get('total_notional')}` USDT",
        f"- 24h Rate: delta `{c24['posts_consumed_delta']}` POSTs, `{_fmt_num(c24['posts_per_hour'])}` POST/h (`{_fmt_num(c24['posts_per_day'])}` POST/day), est. hours left: `{_fmt_num(c24['estimated_hours_until_exhausted'], 'h')}`",
        f"- 72h Rate: delta `{c72['posts_consumed_delta']}` POSTs, `{_fmt_num(c72['posts_per_hour'])}` POST/h | 7d Rate: delta `{c7d['posts_consumed_delta']}` POSTs, `{_fmt_num(c7d['posts_per_hour'])}` POST/h",
        f"- Policy: `{budget['policy']}`",
        '',
        '## 6. Alerts & Storage',
        f"- Active Alerts: `{alerts['active_count']}` (`{alerts['active_keys']}`) | 24h Fired: `{alerts['fired_24h']}` | 24h Resolved: `{alerts['resolved_24h']}` | 24h Maintenance Events: `{alerts.get('maintenance_events_24h', 0)}`",
        f"- Disk Free: `{_fmt_num(disk_free_gib, ' GiB')}` | Trading State Size: `{storage['state_bytes']}` B | Monitor Samples Size: `{storage['monitor_samples_bytes']}` B",
    ])
    if report.get('strategy_release'):
        release = report['strategy_release']
        lines.extend(['', '## Strategy versions and switch outcomes',
            f"- Active: `{release.get('active')}` | Previous: `{release.get('previous')}`",
            f"- Latest switch: `{release.get('switch')}`"])
        for event in report.get('strategy_switch_events', []):
            lines.append(f"- #{event['seq']} `{event['event']}`: `{event.get('old')}` → `{event.get('new')}`; `{event.get('reason')}`")
    return '\n'.join(lines) + '\n'


def run_alert_self_test(*, work_dir: Path | None = None) -> dict:
    """Run an isolated end-to-end alert trigger, dedupe, rate-limit, and recovery test against a local loopback webhook."""
    received: list[dict] = []
    lock = Lock()

    class _Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode('utf-8'))
            with lock:
                received.append(body)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, format, *args):  # noqa: A002
            return

    server = ThreadingHTTPServer(('127.0.0.1', 0), _Receiver)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    temp_ctx = None
    try:
        if work_dir is None:
            temp_ctx = tempfile.TemporaryDirectory(prefix='factorlab-c06-selftest-')
            target_dir = Path(temp_ctx.name)
        else:
            target_dir = ensure_monitor_dir(work_dir)

        cfg = AlertDeliveryConfig(
            webhook_url=f'http://127.0.0.1:{port}/webhook',
            timeout_seconds=5.0,
            allow_insecure_loopback_http=True,
            renotify_seconds=3600,
            rate_limit_window_seconds=300,
            rate_limit_max_deliveries=10,
        )
        thresholds = MonitorThresholds(
            health_failure_streak=2,
            account_failure_streak=2,
            restart_recovery_healthy_samples=2,
        )

        base_ts = 1_800_000_000.0
        def make_health(**overrides):
            base = dict(
                pid=1001, heartbeat_at=base_ts, started_at=base_ts - 600, starts=1, restart_count=0,
                systemd_unit='factorlab-demo-execute.service',
                systemd=dict(unit='factorlab-demo-execute.service', available=True, active_state='active',
                             sub_state='running', main_pid=1001, n_restarts=0, result='success', exec_main_status=0),
                mode='EXECUTE', status='RUNNING', desired='RUNNING', reason=None,
                alive=True, ready=True, execute_healthy=True, dispatch_eligible=True, demo_execution_ready=True,
                uptime_seconds=600.0, account_recovered=True, account_age_seconds=0.5,
                last_reconciled_at=_iso_utc(base_ts), last_recovery_seconds=0.4,
                market_fresh=True, market_batch_age_seconds=1.0, last_valid_market_at=_iso_utc(base_ts),
                unknown_count=0, unknown_orders=[], open_orders=[], positions=[], recent_orders=[],
                grid=dict(deployment_id='dep-1', state='RUNNING', effective='RUNNING', running=True, live_bar=True),
                requests=dict(GET=100, POST=0, DELETE=0), api=dict(endpoints=[]),
                recovery=dict(state='RECOVERED', consecutive_failures=0, recovery_attempts=0, startup_recovered=True),
                safety_events=dict(STALE_ACCOUNT=0, STALE_POLL=0, R13_QUOTE_GAP=0, CLOCK_SKEW=0),
                last_exception=None,
                campaign=dict(enabled=True, post_budget=4, posts_consumed=0, remaining_posts=4),
                global_budget=dict(state='RUNNING', total_orders=100, remaining_posts=90,
                                   total_notional='700', remaining_notional='650',
                                   consumed=dict(posts=10, submitted_notional='50')),
                disk_free_bytes=20 * 1024 ** 3, disk_bytes=60 * 1024 ** 3,
                state_bytes=16_000_000, database_bytes=16_000_000, sidecar_bytes=0,
            )
            base.update(overrides)
            return base

        # Step 1: Healthy baseline (0 alerts)
        r0 = run_monitor_cycle(make_health(), target_dir, alert_config=cfg, thresholds=thresholds, now=base_ts)
        # Step 2: Trigger UNKNOWN + disk low + budget low + unhealthy #1
        fault_health = make_health(
            ready=False, execute_healthy=False, dispatch_eligible=False,
            account_recovered=False, unknown_count=1,
            unknown_orders=[{'client_order_id': 'fl-test-unknown-01'}],
            disk_free_bytes=400 * 1024 ** 2,
            global_budget=dict(state='RUNNING', total_orders=100, remaining_posts=8,
                               total_notional='700', remaining_notional='60',
                               consumed=dict(posts=92, submitted_notional='640')),
        )
        r1 = run_monitor_cycle(fault_health, target_dir, alert_config=cfg, thresholds=thresholds, now=base_ts + 60)
        # Step 3: Repeat same fault -> dedupe existing alerts, trigger streak alerts (health + account)
        r2 = run_monitor_cycle(fault_health, target_dir, alert_config=cfg, thresholds=thresholds, now=base_ts + 120)
        # Step 4: Repeat again -> full dedupe (0 new deliveries)
        r3 = run_monitor_cycle(fault_health, target_dir, alert_config=cfg, thresholds=thresholds, now=base_ts + 180)
        # Step 5: Simulate unexpected restart + recovery of previous faults
        restarted_healthy = make_health(
            pid=2002, starts=2, restart_count=1,
            systemd=dict(unit='factorlab-demo-execute.service', available=True, active_state='active',
                         sub_state='running', main_pid=2002, n_restarts=1, result='success', exec_main_status=0),
        )
        r4 = run_monitor_cycle(restarted_healthy, target_dir, alert_config=cfg, thresholds=thresholds, now=base_ts + 240)
        # Step 6 & 7: Two healthy cycles stabilize restart and emit ALERT_RESOLVED for unexpected_restart
        r5 = run_monitor_cycle(restarted_healthy, target_dir, alert_config=cfg, thresholds=thresholds, now=base_ts + 300)
        r6 = run_monitor_cycle(restarted_healthy, target_dir, alert_config=cfg, thresholds=thresholds, now=base_ts + 360)

        # Also verify unconfigured delivery honesty
        unconfigured_delivery = deliver_webhook(
            dict(event='ALERT_FIRING', alert_key='order_unknown', severity='CRITICAL', summary='test'),
            AlertDeliveryConfig(),
        )

        with lock:
            captured = list(received)

        fired_keys = sorted({e['alert_key'] for e in captured if e.get('event') == 'ALERT_FIRING'})
        resolved_keys = sorted({e['alert_key'] for e in captured if e.get('event') == 'ALERT_RESOLVED'})
        sensitive_leak = any(_contains_sensitive_key(e) is not None for e in captured)

        return dict(
            passed=(
                r0['active_alert_count'] == 0
                and len(r3['events']) == 0  # Deduplication worked
                and set(fired_keys) == {
                    'account_recovery_abnormal', 'budget_low', 'disk_space_low',
                    'order_unknown', 'sustained_health_failure', 'unexpected_restart',
                }
                and set(resolved_keys) == set(fired_keys)
                and r6['active_alert_count'] == 0
                and not sensitive_leak
                and unconfigured_delivery['status'] == 'NOT_CONFIGURED'
                and unconfigured_delivery['mobile_push_configured'] is False
            ),
            loopback_endpoint=f'http://127.0.0.1:{port}/webhook',
            total_webhook_deliveries=len(captured),
            fired_alert_keys=fired_keys,
            resolved_alert_keys=resolved_keys,
            dedupe_verified=(len(r3['events']) == 0),
            recovery_verified=(set(resolved_keys) == set(fired_keys) and r6['active_alert_count'] == 0),
            sensitive_key_leak_detected=sensitive_leak,
            unconfigured_behavior=unconfigured_delivery,
            step_event_counts=[len(r['events']) for r in (r0, r1, r2, r3, r4, r5, r6)],
        )
    finally:
        server.shutdown()
        server.server_close()
        if temp_ctx is not None:
            temp_ctx.cleanup()
