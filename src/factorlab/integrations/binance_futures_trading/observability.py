"""Bounded, aggregate Demo telemetry; never retain signed queries or payloads."""
from time import perf_counter

from factorlab.integrations.binance.client import Response
from .protocol import FuturesClient


class RequestCounts(dict):
    """Preserve legacy method totals alongside process-local request diagnostics."""
    def __init__(self):
        super().__init__(GET=0, POST=0, DELETE=0)
        self.phase = 'initialization'
        self.endpoints = {}
        self.weight_headers = {}

    def before_request(self, source, method, path):
        """Optional admission hook, before a network request is dispatched."""

    def record(self, source, method, path, seconds, error, weights):
        key = (self.phase, source, method, path)
        row = self.endpoints.setdefault(key, dict(count=0, errors={}, total_seconds=0.,
            last_seconds=0., max_seconds=0.))
        row['count'] += 1
        row['total_seconds'] += seconds
        row['last_seconds'] = seconds
        row['max_seconds'] = max(row['max_seconds'], seconds)
        if error is not None:
            row['errors'][error] = row['errors'].get(error, 0) + 1
        self.weight_headers.update({source + ':' + name: value for name, value in weights.items()})

    def snapshot(self):
        return dict(scope='PROCESS_SINCE_START', endpoints=[dict(phase=phase, source=source,
            method=method, endpoint=path, **row,
            error_rate=sum(row['errors'].values()) / row['count'])
            for (phase, source, method, path), row in sorted(self.endpoints.items())],
            observed_weight_headers=dict(self.weight_headers))


class ObservedClient(FuturesClient):
    def __init__(self, *args, request_counts=None, request_source='session', **kwargs):
        super().__init__(*args, **kwargs)
        self.request_counts = request_counts
        self.request_source = request_source
        self._observed_weights = {}

    def _parse_response(self, request, response, **kwargs):
        if isinstance(response, Response):
            self._observed_weights = {key.lower(): value for key, value in response.headers.items()
                if key.lower().startswith('x-mbx-used-weight')}
        return super()._parse_response(request, response, **kwargs)

    def _request_once(self, method, path, params=None, *, signed=True):
        if isinstance(self.request_counts, RequestCounts):
            self.request_counts.before_request(self.request_source, method, path)
        started = perf_counter()
        error = None
        self._observed_weights = {}
        try:
            return super()._request_once(method, path, params, signed=signed)
        except Exception as exc:
            error = getattr(exc, 'category', type(exc).__name__)
            raise
        finally:
            if isinstance(self.request_counts, RequestCounts):
                self.request_counts.record(self.request_source, method, path,
                    perf_counter() - started, error, self._observed_weights)


def worker_metrics(session, feed, runtime, requests):
    from datetime import datetime, timezone
    from .demo_runtime import SMOKE_CAMPAIGN
    quote_ms = feed.watermarks()[1]
    return dict(api=requests.snapshot() if isinstance(requests, RequestCounts) else None,
        last_valid_market_at=datetime.fromtimestamp(quote_ms / 1000, timezone.utc).isoformat() if quote_ms is not None else None,
        last_reconciled_at=session.last_reconciled_at,
        last_recovery_seconds=session.last_recovery_seconds,
        unknown_count=sum(record.snapshot.reconcile_required for record in session.gateway.state.orders.values()),
        journals=dict(controller=storage_snapshot(session.controller.store),
                      protocol=storage_snapshot(session.adapter.store, protocol=True)),
        campaign=runtime.report(SMOKE_CAMPAIGN))


def storage_snapshot(store, *, protocol=False):
    """Indexed journal high-water mark and SQLite allocation, without payload scans."""
    db = store.db
    path = next(row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main')
    from pathlib import Path
    sizes = {suffix or 'database': Path(path + suffix).stat().st_size
             for suffix in ('', '-wal', '-journal') if path and Path(path + suffix).exists()}
    return dict(files_bytes=sizes,
        allocated_bytes=db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0],
        event_high_watermark=db.execute('SELECT coalesce(max(rowid),0) FROM futures_events' if protocol
                                       else 'SELECT coalesce(max(rowid),0) FROM events').fetchone()[0],
        transactions=store.transaction_count, last_transaction_seconds=store.last_transaction)
