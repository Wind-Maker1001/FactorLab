"""Bounded, aggregate Demo telemetry; never retain signed queries or payloads."""
from collections import deque
from contextlib import contextmanager
from math import ceil
from datetime import datetime, timezone
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
        self.rounds = None
        self._round = None
        self._round_sink = None

    def enable_rounds(self, *, max_requests=1500, sink=None, max_quote_gap_seconds=30):
        """Opt in to bounded samples; the sink owns any durable evidence."""
        if type(max_requests) is not int or not 1 <= max_requests <= 1500:
            raise ValueError('round request budget must be within 1..1500')
        if max_quote_gap_seconds <= 0:
            raise ValueError('positive maximum quote gap required')
        self.max_quote_gap_seconds = max_quote_gap_seconds
        self._last_book_received = None
        self._last_book_monotonic = perf_counter()
        self.max_round_samples = max_requests
        self.rounds = deque(maxlen=1)
        self._round_sink = sink
        self._round_durations = deque(maxlen=max_requests)
        self._operation_samples = {}
        self._previous_round_started = None

    def begin_round(self):
        if self.rounds is not None:
            started = perf_counter()
            self._round = dict(started=started, started_at=datetime.now(timezone.utc).isoformat(),
                operations={}, requests=[], request_count=0)
            if self._previous_round_started is not None:
                self.sample('start_to_start', started - self._previous_round_started)
            self._previous_round_started = started

    def book_received(self, market_at, received_at):
        if self._round is None:
            return
        self.sample('book_market_to_received', (received_at - market_at).total_seconds())
        if self._last_book_received is not None:
            gap = (received_at - self._last_book_received).total_seconds()
            self.sample('book_received_gap', gap)
            self._round['quote_gap_exceeded'] = (self._round.get('quote_gap_exceeded', False)
                                                 or gap > self.max_quote_gap_seconds)
        self._last_book_received = received_at
        self._last_book_monotonic = perf_counter()
        self._round['book_market_at'] = market_at.isoformat()
        self._round['book_received_at'] = received_at.isoformat()

    def sample(self, name, seconds):
        if self._round is not None:
            values = self._round['operations'].setdefault(name, deque(maxlen=self.max_round_samples))
            values.append(seconds)
            self._operation_samples.setdefault(name, deque(maxlen=self.max_round_samples)).append(seconds)

    @contextmanager
    def measure(self, name):
        """Measure consumer work, including exceptional exits, only in active rounds."""
        active = self._round
        started = perf_counter() if active is not None else None
        try:
            yield
        finally:
            if active is not None:
                self.sample(name, perf_counter() - started)

    def end_round(self, **metadata):
        if self._round is None:
            return None
        current, self._round = self._round, None
        def distribution(values):
            values = sorted(values)
            return dict(count=len(values), total_seconds=sum(values),
                p50_seconds=values[ceil(len(values) * .5) - 1],
                p95_seconds=values[ceil(len(values) * .95) - 1], max_seconds=values[-1])
        requests = current['requests']
        loop_seconds = perf_counter() - current['started']
        self._round_durations.append(loop_seconds)
        summary = dict(phase=self.phase, loop_seconds=loop_seconds,
            started_at=current['started_at'], ended_at=datetime.now(timezone.utc).isoformat(),
            within_max_interval=loop_seconds <= self.max_quote_gap_seconds,
            timing_boundary='ROUND_START_THROUGH_WAIT_EXCLUDES_SUMMARY_SINK',
            previous_start_to_start_within_max_interval=(
                current['operations']['start_to_start'][-1] <= self.max_quote_gap_seconds
                if 'start_to_start' in current['operations'] else None),
            book_market_at=current.get('book_market_at'), book_received_at=current.get('book_received_at'),
            request_count=current['request_count'], retained_requests=len(requests),
            requests=requests, max_quote_gap_seconds=self.max_quote_gap_seconds,
            quote_gap_exceeded=(current.get('quote_gap_exceeded', False)
                or perf_counter() - self._last_book_monotonic > self.max_quote_gap_seconds),
            operations={name: distribution(values)
                for name, values in current['operations'].items()}, **metadata)
        self.rounds.append(summary)
        if self._round_sink is not None:
            try:
                self._round_sink(summary)
            except Exception as error:
                if metadata.get('error') is None:
                    raise
                summary['sink_error'] = type(error).__name__
        return summary

    def before_request(self, source, method, path):
        """Optional admission hook, before a network request is dispatched."""

    def clock_event(self, source, kind, details):
        """Optional sink for calibration and signed request timing."""

    def record(self, source, method, path, seconds, error, weights, *, status=None):
        if self._round is not None:
            market = path in {'/fapi/v1/time', '/fapi/v1/klines', '/fapi/v1/ticker/bookTicker',
                '/fapi/v1/exchangeInfo', '/fapi/v1/depth', '/fapi/v1/premiumIndex'}
            classification = ('market_get' if market else 'account_get') if method == 'GET' else method.lower()
            self._round['request_count'] += 1
            self.sample(classification, seconds)
            if len(self._round['requests']) < self.max_round_samples:
                self._round['requests'].append(dict(source=source, method=method, endpoint=path,
                    classification=classification, rtt_seconds=seconds, error=error, http_status=status,
                    http_failure=status is not None and status >= 400,
                    throttled=error == 'RATE_LIMIT' or status in {418, 429}))
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
        durations = sorted(self._round_durations) if self.rounds is not None else []
        aggregate = (dict(count=len(durations), p50_seconds=durations[ceil(len(durations) * .5) - 1],
            p95_seconds=durations[ceil(len(durations) * .95) - 1], max_seconds=durations[-1])
            if durations else None)
        operations = {}
        if self.rounds is not None:
            for name, samples in self._operation_samples.items():
                values = sorted(samples)
                operations[name] = dict(count=len(values), total_seconds=sum(values),
                    p50_seconds=values[ceil(len(values) * .5) - 1],
                    p95_seconds=values[ceil(len(values) * .95) - 1], max_seconds=values[-1])
        return dict(round_loop=aggregate, round_operations=operations, scope='PROCESS_SINCE_START', endpoints=[dict(phase=phase, source=source,
            method=method, endpoint=path, **row,
            error_rate=sum(row['errors'].values()) / row['count'])
            for (phase, source, method, path), row in sorted(self.endpoints.items())],
            observed_weight_headers=dict(self.weight_headers),
            rounds=list(self.rounds) if self.rounds is not None else None)


class ObservedClient(FuturesClient):
    def __init__(self, *args, request_counts=None, request_source='session', **kwargs):
        super().__init__(*args, **kwargs)
        self.request_counts = request_counts
        self.request_source = request_source
        self._observed_weights = {}
        self._observed_status = None
        self._transport_seconds = None

    def build(self, *args, **kwargs):
        request = super().build(*args, **kwargs)
        self._observed_signing = request.signing
        self._transport_started = perf_counter()
        return request

    def _clock_event(self, kind, **details):
        if isinstance(self.request_counts, RequestCounts):
            self.request_counts.clock_event(self.request_source, kind, details)

    def _parse_response(self, request, response, **kwargs):
        if self._transport_started is not None:
            self._transport_seconds = perf_counter() - self._transport_started
        if isinstance(response, Response):
            self._observed_status = response.status
            self._observed_weights = {key.lower(): value for key, value in response.headers.items()
                if key.lower().startswith('x-mbx-used-weight')}
        return super()._parse_response(request, response, **kwargs)

    def _request_once(self, method, path, params=None, *, signed=True):
        if isinstance(self.request_counts, RequestCounts):
            self.request_counts.before_request(self.request_source, method, path)
        started = perf_counter()
        error = None
        self._observed_weights = {}
        self._observed_status = None
        self._observed_signing = None
        self._transport_started = None
        self._transport_seconds = None
        clock_rejection = None
        try:
            return super()._request_once(method, path, params, signed=signed)
        except Exception as exc:
            error = getattr(exc, 'category', type(exc).__name__)
            if error == 'CLOCK_SKEW':
                clock_rejection = dict(code=exc.code, message=exc.message)
            raise
        finally:
            if isinstance(self.request_counts, RequestCounts):
                if self._observed_signing is not None:
                    self._clock_event('signed_request_timing', method=method, endpoint=path,
                        http_status=self._observed_status, error=error, rejection=clock_rejection,
                        **self._observed_signing)
                self.request_counts.record(self.request_source, method, path,
                    self._transport_seconds if self._transport_seconds is not None else
                    perf_counter() - (self._transport_started if self._transport_started is not None else started),
                    error, self._observed_weights, status=self._observed_status)


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
