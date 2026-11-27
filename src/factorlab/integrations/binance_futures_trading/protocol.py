"""Signed USD-M protocol over an injected transport; mutating network IO absent."""
from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from math import floor
import hashlib
import hmac
import json
import os
import time
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import Response, _NoRedirect

class Environment(Enum):
    PRODUCTION = ('https://fapi.binance.com', 'BINANCE_READONLY', 'FactorLab-ReadOnly')
    DEMO = ('https://demo-fapi.binance.com', 'BINANCE_DEMO', 'FactorLab-Demo')

    @property
    def host(self):
        return self.value[0]

    @property
    def credential_prefix(self):
        return self.value[1]

    @property
    def account_id(self):
        return self.value[2]


def credential_account_id(environment, key):
    digest = hashlib.sha256(key.encode()).hexdigest()
    return f'{environment.name}:{environment.account_id}:{digest}'


READ_PATHS = frozenset({
    '/fapi/v1/exchangeInfo', '/fapi/v3/balance', '/fapi/v1/time', '/fapi/v1/order', '/fapi/v1/openOrders', '/fapi/v1/allOrders',
    '/fapi/v2/account', '/fapi/v3/account', '/fapi/v3/positionRisk', '/fapi/v1/positionSide/dual',
    '/fapi/v1/leverageBracket', '/fapi/v1/userTrades', '/fapi/v1/income', '/fapi/v1/symbolConfig',
    '/fapi/v1/apiTradingStatus', '/fapi/v1/premiumIndex', '/fapi/v1/ticker/bookTicker', '/fapi/v1/klines',
})
WRITE_PATHS = frozenset({('POST', '/fapi/v1/order'), ('DELETE', '/fapi/v1/order'), ('POST', '/fapi/v1/leverage')})

DEMO_TIME_SAMPLES = 3
DEMO_TIME_TTL_SECONDS = 45
DEMO_TIME_MAX_UNCERTAINTY_MS = 950
DEMO_TIME_DRIFT_MS_PER_SECOND = 1


class SafetyBlocked(PermissionError):
    pass


def rejection_message(payload, *, credentials=None, request=None):
    """Return bounded display text, suppressing credential or request echoes entirely."""
    import re
    message = payload.get('msg') if isinstance(payload, dict) else None
    if not isinstance(message, str):
        return None
    secrets = [] if credentials is None else [credentials.key, credentials.secret]
    if request is not None:
        from urllib.parse import parse_qs
        secrets += parse_qs(request.query).get('signature', [])
    if (len(message) > 512 or not message.isprintable()
            or any(value and value in message for value in secrets)
            or re.search(r'[a-z]+://|signature|api.?key|secret|authorization|[A-Za-z0-9_-]{32,}', message, re.I)):
        return '[SUPPRESSED_UNSAFE_EXCHANGE_MESSAGE]'
    return message


class ExchangeError(DataQualityError):
    def __init__(self, code, status, category, retry_after=None, *, message=None):
        self.code, self.status, self.category, self.retry_after = code, status, category, retry_after
        self.message = message
        # Never include server messages, URLs, signatures, keys or payloads.
        super().__init__(f'Binance error code={code} HTTP={status} category={category}')


class TimeCalibrationError(DataQualityError):
    category = 'TIME_CALIBRATION'


class AccountReadExpired(DataQualityError):
    category = 'STALE_ACCOUNT'


@dataclass
class AccountReadWindow:
    deadline: float
    calibration_attempted: bool = False


class ReadTransportError(DataQualityError):
    def __init__(self, category):
        self.category = category
        super().__init__(f'account read failed; reconciliation required; category={category}')


def transport_failure_category(error):
    reason = error.reason if isinstance(error, URLError) else error
    return 'TRANSPORT_TIMEOUT' if isinstance(reason, TimeoutError) else 'TRANSPORT_ERROR'


class OutcomeUnknown(TimeoutError):
    pass


@dataclass(frozen=True, repr=False)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)
    environment: Environment | None = None

    def __post_init__(self):
        if not self.key or not self.secret:
            raise ValueError('both API key and secret required')

    @classmethod
    def from_env(cls, environment=None):
        if environment is None:
            return cls(os.environ.get('BINANCE_API_KEY', ''), os.environ.get('BINANCE_API_SECRET', ''))
        prefix = environment.credential_prefix
        return cls(os.environ.get(prefix + '_API_KEY', ''),
                   os.environ.get(prefix + '_API_SECRET', ''), environment)

    def __repr__(self):
        return 'Credentials(<redacted>)'


@dataclass(frozen=True, repr=False)
class SignedRequest:
    method: str
    path: str
    query: str = field(repr=False)
    headers: dict = field(repr=False)
    environment: Environment | None = None
    signing: dict | None = field(default=None, repr=False, compare=False)

    timing_clock: object = field(default=time.monotonic, repr=False, compare=False)

    def __repr__(self):
        return f'SignedRequest({self.method}, {self.path}, <redacted>)'


@dataclass(frozen=True)
class ExchangeTime:
    estimated_ms: float
    uncertainty_ms: float
    observed_monotonic: float

    @property
    def lower_ms(self):
        return self.estimated_ms - self.uncertainty_ms

    @property
    def upper_ms(self):
        return self.estimated_ms + self.uncertainty_ms


@dataclass(frozen=True)
class _TimeSample:
    server_ms: int
    started: float
    received: float
    wall_before_ms: float
    wall_after_ms: float

    @property
    def rtt_ms(self):
        return (self.received - self.started) * 1000

    def at(self, monotonic):
        age = monotonic - self.received
        return ExchangeTime(self.server_ms + self.rtt_ms / 2 + age * 1000,
                            self.rtt_ms / 2 + 1 + age * DEMO_TIME_DRIFT_MS_PER_SECOND,
                            monotonic)

    def diagnostics(self):
        wall_elapsed = self.wall_after_ms - self.wall_before_ms
        return dict(server_ms=self.server_ms, wall_before_ms=self.wall_before_ms,
            wall_after_ms=self.wall_after_ms, rtt_ms=self.rtt_ms,
            midpoint_uncertainty_ms=self.rtt_ms / 2,
            wall_elapsed_ms=wall_elapsed, wall_step_ms=wall_elapsed - self.rtt_ms,
            offset_ms=round(self.at(self.received).estimated_ms - self.wall_after_ms))


class OfflineTransport:
    """Subclass for a deterministic in-process fake; no networking provided."""
    def __call__(self, request: SignedRequest, timeout: float) -> Response:
        raise NotImplementedError


def _request_http(host, request, timeout, *, opener):
    """Local HTTP boundaries, not wire-send, first-byte or server-receipt times."""
    def stage(name):
        if request.signing is not None:
            request.signing[name] = request.timing_clock()

    url = host + request.path + ('?' + request.query if request.query else '')
    http_request = Request(url, headers=request.headers, method=request.method)
    http_opener = opener(_NoRedirect())
    stage('http_opener_enter_monotonic')
    try:
        with http_opener.open(http_request, timeout=timeout) as reply:
            stage('headers_available_monotonic')
            body = reply.read(2 * 1024 * 1024 + 1)
            stage('body_complete_monotonic')
            return Response(reply.status, dict(reply.headers), body)
    except HTTPError as error:
        stage('headers_available_monotonic')
        body = error.read(2 * 1024 * 1024 + 1)
        stage('body_complete_monotonic')
        return Response(error.code, dict(error.headers), body)


@dataclass(frozen=True, kw_only=True)
class ReadOnlyNetworkTransport:
    """Explicit opt-in account GETs only, fixed host, no redirects or writes."""
    permit_account_read: bool = False
    environment: Environment = Environment.PRODUCTION

    def __call__(self, request, timeout):
        if not self.permit_account_read or request.method != 'GET' or request.path not in READ_PATHS:
            raise SafetyBlocked('NO LIVE MONEY: network access disabled or non-read request')
        if request.environment is not self.environment:
            raise SafetyBlocked('credential and transport environment mismatch')
        try:
            return _request_http(self.environment.host, request, timeout, opener=build_opener)
        except OSError as error:
            raise ReadTransportError(transport_failure_category(error)) from None


class DemoClockState:
    """One calibration authority shared by clients with independent transports."""
    def __init__(self):
        self.lock = RLock()
        self.sample = None
        self.valid = False
        self.diagnostics = None
        self.offset_ms = 0


class _RequestCooldown:
    def __init__(self):
        self.lock = RLock()
        self.until = 0.0


class FuturesClient:
    """Writes are never retried; signed GET clock recovery requires an explicit scope."""
    def __init__(self, credentials=None, *, transport=None, recv_window=5000,
                 timeout=10, clock=time.time, monotonic=time.monotonic, environment=None):
        if type(recv_window) is not int or not 1 <= recv_window <= 60000 or timeout <= 0:
            raise ValueError('invalid recvWindow or timeout')
        from .network import DemoNetworkTransport
        if transport is not None and not isinstance(transport, (OfflineTransport, ReadOnlyNetworkTransport, DemoNetworkTransport)):
            raise SafetyBlocked('transport must be offline fake or explicitly permitted read-only transport')
        self.environment = environment if environment is None else Environment(environment)
        if isinstance(transport, ReadOnlyNetworkTransport):
            if self.environment is None or transport.environment is not self.environment:
                raise SafetyBlocked('explicit matching network environment required')
            if credentials is not None and credentials.environment is not self.environment:
                raise SafetyBlocked('credential and client environment mismatch')
        if getattr(transport, 'environment', None) is Environment.DEMO:
            if self.environment is not Environment.DEMO or (credentials is not None and credentials.environment is not Environment.DEMO):
                raise SafetyBlocked('Demo transport requires matching Demo client and credentials')
        self.credentials, self.transport = credentials, transport
        self.recv_window, self.timeout, self.clock = recv_window, timeout, clock
        self.monotonic = monotonic
        self.offset_ms = 0
        self._cooldown = _RequestCooldown()
        self._account_read_window = None
        self._recover_get_clock = False
        self.clock_recoveries = []
        self.time_sync = None
        self.demo_clock = DemoClockState()

    @property
    def _demo_sample(self):
        return self.demo_clock.sample

    @_demo_sample.setter
    def _demo_sample(self, sample):
        self.demo_clock.sample = sample

    @property
    def _demo_time_valid(self):
        return self.demo_clock.valid

    @_demo_time_valid.setter
    def _demo_time_valid(self, valid):
        self.demo_clock.valid = valid

    def share_demo_clock(self, owner):
        """Share calibration, not request parsing, retry scopes or credentials."""
        if self.environment is not Environment.DEMO or owner.environment is not Environment.DEMO:
            raise ValueError('Demo clients required for shared time')
        self.demo_clock = owner.demo_clock
        self.clock, self.monotonic = owner.clock, owner.monotonic
        self.recv_window = owner.recv_window

    def account_reader(self, window):
        """Isolate per-request diagnostics while sharing calibration and cooldown."""
        reader = copy(self)
        reader._account_read_window = window
        return reader

    def _clock_event(self, kind, **details):
        """Allow the observing client to retain credential-free timing evidence."""

    @contextmanager
    def recover_get_clock(self):
        """Retry each signed GET once on CLOCK_SKEW, without replaying its caller."""
        previous = self._recover_get_clock
        self._recover_get_clock = True
        try:
            yield
        finally:
            self._recover_get_clock = previous

    def sync_time(self):
        if self.environment is Environment.DEMO:
            self._calibrate_demo(accept_clock_change=True)
            return self.offset_ms
        before = self.clock()
        started = time.monotonic()
        payload = self.request('GET', '/fapi/v1/time', signed=False)
        elapsed_ms = (time.monotonic() - started) * 1000
        after = self.clock()
        if not isinstance(payload, dict) or type(payload.get('serverTime')) is not int or payload['serverTime'] <= 0:
            raise DataQualityError('invalid server time response')
        self.offset_ms = payload['serverTime'] - int((before + after) * 500)
        self.time_sync = dict(offset_ms=self.offset_ms, rtt_ms=elapsed_ms,
            midpoint_uncertainty_ms=elapsed_ms / 2, wall_elapsed_ms=(after - before) * 1000)
        return self.offset_ms

    def _calibrate_demo(self, *, accept_clock_change=False):
        with self.demo_clock.lock:
            self._calibrate_demo_locked(accept_clock_change=accept_clock_change)

    def _calibrate_demo_locked(self, *, accept_clock_change=False):
        previous = self._demo_sample
        self._demo_time_valid = False
        maximum = min(DEMO_TIME_MAX_UNCERTAINTY_MS, self.recv_window / 4)
        for attempt in range(1, DEMO_TIME_SAMPLES + 1):
            wall_before_ms = self.clock() * 1000
            started = self.monotonic()
            payload = self.request('GET', '/fapi/v1/time', signed=False)
            received = self.monotonic()
            wall_after_ms = self.clock() * 1000
            if not isinstance(payload, dict) or type(payload.get('serverTime')) is not int or payload['serverTime'] <= 0:
                raise TimeCalibrationError('invalid server time response')
            sample = _TimeSample(payload['serverTime'], started, received, wall_before_ms, wall_after_ms)
            reading = sample.at(received)
            diagnostics = sample.diagnostics()
            quality_ok = reading.uncertainty_ms <= maximum
            drift_ms = None
            drift_ok = True
            if previous is not None:
                predicted = previous.at(received)
                drift_ms = reading.estimated_ms - predicted.estimated_ms
                drift_ok = abs(drift_ms) <= reading.uncertainty_ms + predicted.uncertainty_ms
            self._clock_event('clock_sample', attempt=attempt, **diagnostics,
                uncertainty_ms=reading.uncertainty_ms, quality_ok=quality_ok,
                drift_ms=drift_ms, drift_ok=drift_ok, explicit_sync=accept_clock_change)
            if not quality_ok:
                continue
            if not drift_ok and not accept_clock_change:
                raise TimeCalibrationError('Demo server time drift exceeds calibration bounds')
            self._demo_sample = sample
            self._demo_time_valid = True
            self.time_sync = self.demo_clock.diagnostics = diagnostics
            self.offset_ms = self.demo_clock.offset_ms = diagnostics['offset_ms']
            return
        raise TimeCalibrationError('Demo time calibration RTT exceeds signing uncertainty budget')

    def demo_time(self, *, allow_refresh=True, min_valid_seconds=0):
        with self.demo_clock.lock:
            reading = self._demo_time_locked(allow_refresh=allow_refresh,
                                             min_valid_seconds=min_valid_seconds)
            self.time_sync = self.demo_clock.diagnostics
            self.offset_ms = self.demo_clock.offset_ms
            return reading

    def _demo_time_locked(self, *, allow_refresh=True, min_valid_seconds=0):
        now = self.monotonic()
        maximum = min(999, self.recv_window / 2)
        if (not self._demo_time_valid
                or now - self._demo_sample.received + min_valid_seconds >= DEMO_TIME_TTL_SECONDS
                or self._demo_sample.at(now + min_valid_seconds).uncertainty_ms > maximum):
            if not allow_refresh:
                raise TimeCalibrationError('Demo time calibration missing or expired')
            self._calibrate_demo()
            now = self.monotonic()
        return self._demo_sample.at(now)

    def projected_demo_time(self):
        """Project the anchor for local bookkeeping without authorizing a signature."""
        return self._demo_sample.at(self.monotonic())

    def build(self, method, path, params=None, *, signed=True):
        if not ((method == 'GET' and path in READ_PATHS) or (method, path) in WRITE_PATHS):
            raise SafetyBlocked('endpoint is not allowlisted')
        params = dict(params or {})
        if signed:
            return self._signed_request(method, path, params)
        if {'signature', 'timestamp', 'recvWindow'} & params.keys():
            raise ValueError('authentication fields are owned by the signer')
        return SignedRequest(method, path, urlencode(sorted(params.items())), {}, self.environment)

    def _signed_request(self, method, path, params):
        with self.demo_clock.lock:
            return self._signed_request_locked(method, path, params)

    def _signed_request_locked(self, method, path, params):
        if {'signature', 'timestamp', 'recvWindow'} & params.keys():
            raise ValueError('authentication fields are owned by the signer')
        if self.credentials is None:
            raise SafetyBlocked('credentials required for signed protocol')
        if getattr(self.transport, 'environment', None) is not None:
            if self.credentials.environment is not self.environment or self.transport.environment is not self.environment:
                raise SafetyBlocked('credential and client environment mismatch')
        signing = None
        if self.environment is Environment.DEMO and (self._demo_sample is not None
                or not isinstance(self.transport, OfflineTransport)):
            reading = self.demo_time(allow_refresh=False)
            timestamp_ms = floor(reading.estimated_ms)
            wall_ms = self.clock() * 1000
            sample = self._demo_sample
            predicted_wall_ms = sample.wall_after_ms + (reading.observed_monotonic - sample.received) * 1000
            signing = dict(timestamp_ms=timestamp_ms, recv_window_ms=self.recv_window,
                estimated_server_ms=reading.estimated_ms, uncertainty_ms=reading.uncertainty_ms,
                wall_ms=wall_ms, wall_step_since_sync_ms=wall_ms - predicted_wall_ms,
                calibration_age_ms=(reading.observed_monotonic - sample.received) * 1000)
        else:
            timestamp_ms = int(self.clock() * 1000) + self.offset_ms
        params = dict(params, timestamp=timestamp_ms, recvWindow=self.recv_window)
        headers = {'X-MBX-APIKEY': self.credentials.key}
        query = urlencode(sorted(params.items()))
        signature = hmac.new(self.credentials.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        if signing is not None:
            signing['signed_monotonic'] = self.monotonic()
        return SignedRequest(method, path, query + '&signature=' + signature, headers, self.environment, signing, self.monotonic)

    def request(self, method, path, params=None, *, signed=True):
        window = self._account_read_window
        refresh_write = False
        if method != 'GET' and (method, path) in WRITE_PATHS and not isinstance(self.transport, OfflineTransport):
            from .network import demo_network_permitted
            refresh_write = demo_network_permitted(self) and bool(self.transport._admission)
        if (self.environment is Environment.DEMO and signed
                and ((method == 'GET' and path in READ_PATHS) or refresh_write)
                and self.credentials is not None
                and (self._demo_sample is not None or not isinstance(self.transport, OfflineTransport))):
            with self.demo_clock.lock:
                try:
                    self.demo_time(allow_refresh=window is None)
                except (ExchangeError, DataQualityError) as error:
                    if refresh_write and not isinstance(error, TimeCalibrationError):
                        raise TimeCalibrationError('Demo time calibration refresh failed before write') from error
                    raise
                sample = self._demo_sample
        else:
            sample = None
        try:
            return self._request_once(method, path, params, signed=signed)
        except ExchangeError as first:
            if not (self._recover_get_clock and signed and method == 'GET'
                    and first.category == 'CLOCK_SKEW' and first.code == -1021):
                raise
            diagnostic = dict(method=method, endpoint=path, code=first.code,
                http_status=first.status, category=first.category,
                message=first.message, initial_sync=self.time_sync, outcome='SYNC_FAILED')
            self.clock_recoveries.append(diagnostic)
            try:
                with self.demo_clock.lock:
                    if (sample is not None and self._demo_time_valid
                            and self._demo_sample is not sample):
                        self.demo_time(allow_refresh=False)
                    else:
                        if window is not None:
                            if window.calibration_attempted:
                                raise TimeCalibrationError('Demo account read already attempted clock recalibration')
                            window.calibration_attempted = True
                        self.sync_time()
                diagnostic['recovery_sync'] = self.time_sync
                diagnostic['outcome'] = 'RETRY_FAILED'
                result = self._request_once(method, path, params, signed=signed)
            except Exception as error:
                diagnostic['error'] = getattr(error, 'category', type(error).__name__)
                error.add_note(f'CLOCK_SKEW recovery {method} {path}: {diagnostic["outcome"]}; '
                    f'initial code={first.code} HTTP={first.status}; at most one GET retry')
                raise error from first
            diagnostic['outcome'] = 'RECOVERED'
            return result

    def _request_once(self, method, path, params=None, *, signed=True):
        if self.transport is None:
            raise SafetyBlocked('network disabled by default')
        if method != 'GET' and not isinstance(self.transport, OfflineTransport):
            from .network import demo_network_permitted
            if not demo_network_permitted(self):
                raise SafetyBlocked('NO LIVE MONEY: production or unauthorized network write forbidden')
        if isinstance(self.transport, ReadOnlyNetworkTransport) and not self.transport.permit_account_read:
            raise SafetyBlocked('explicit account read permission required')
        cooldown = self._cooldown.until - self.clock()
        if cooldown > 0:
            raise ExchangeError(None, 429, 'RATE_LIMIT', cooldown)
        request = self.build(method, path, params, signed=signed)
        timeout = self.timeout
        if self._account_read_window is not None:
            timeout = min(timeout, self._account_read_window.deadline - self.monotonic())
            if timeout <= 0:
                raise AccountReadExpired('STALE_ACCOUNT: account read set exceeded five seconds')
        try:
            if request.signing is not None:
                request.signing['transport_enter_monotonic'] = self.monotonic()
            response = self.transport(request, timeout)
        except SafetyBlocked:
            raise
        except (OSError, ReadTransportError) as error:
            if method != 'GET':
                raise OutcomeUnknown('order outcome unknown; query by clientOrderId, never resend') from None
            category = error.category if isinstance(error, ReadTransportError) else transport_failure_category(error)
            failure = DataQualityError(f'account read failed; reconciliation required; category={category}')
            failure.category = category
            raise failure from None
        return self._parse_response(request, response)

    def write_outcome_unknown(self, request, response):
        """Classify persisted write evidence using the live parser without changing cooldowns."""
        from urllib.parse import parse_qs
        parser = copy(self)
        parser._cooldown = _RequestCooldown()
        try:
            payload = FuturesClient._parse_response(parser, request, response)
        except OutcomeUnknown:
            return True
        except ExchangeError:
            return False
        params = parse_qs(request.query)
        identity = params['newClientOrderId' if request.method == 'POST' else 'origClientOrderId'][0]
        # A successful HTTP response alone does not confirm an order ACK.
        return not (isinstance(payload, dict)
                    and payload.get('clientOrderId') == identity
                    and payload.get('symbol') == params['symbol'][0]
                    and type(payload.get('orderId')) is int and payload['orderId'] > 0
                    and payload.get('status') in {'NEW', 'PARTIALLY_FILLED', 'FILLED',
                        'CANCELED', 'REJECTED', 'EXPIRED', 'EXPIRED_IN_MATCH'})

    def _parse_response(self, request, response, *, operation='order', observe_diagnostics=None):
        method = request.method
        if not isinstance(response, Response) or len(response.body) > 2 * 1024 * 1024:
            if method != 'GET':
                raise OutcomeUnknown(f'unusable {operation} response; reconcile')
            raise DataQualityError('invalid HTTP response')
        retry_after = None
        if response.status in {418, 429}:
            raw = next((v for k, v in response.headers.items() if k.lower() == 'retry-after'), '60')
            try:
                retry_after = max(0.0, float(raw))
            except ValueError:
                retry_after = max(0.0, parsedate_to_datetime(raw).timestamp() - self.clock())
            with self._cooldown.lock:
                self._cooldown.until = max(self._cooldown.until, self.clock() + retry_after)
        try:
            payload = json.loads(response.body)
        except (ValueError, UnicodeError):
            if method != 'GET':
                raise OutcomeUnknown(f'malformed {operation} response; reconcile') from None
            raise DataQualityError('invalid response JSON') from None
        raw_code = payload.get('code') if isinstance(payload, dict) else None
        code = raw_code if type(raw_code) is int else None
        diagnostic = None
        if observe_diagnostics is not None:
            diagnostic = rejection_message(payload, credentials=self.credentials, request=request)
            observe_diagnostics(code, diagnostic)
        if response.status == 408 or response.status >= 500 or code in {-1006, -1007, -4116}:
            if method != 'GET':
                raise OutcomeUnknown('exchange execution outcome unknown; reconcile')
        if response.status != 200 or (isinstance(code, int) and code < 0):
            category = ({-1021: 'CLOCK_SKEW', -1022: 'SIGNATURE', -2014: 'AUTH', -2015: 'AUTH',
                         -2019: 'INSUFFICIENT_MARGIN', -2027: 'LEVERAGE_LIMIT', -2022: 'REDUCE_ONLY',
                         -2011: 'ORDER_NOT_FOUND', -2013: 'ORDER_NOT_FOUND', -1013: 'FILTER',
                         -1111: 'PRECISION'}.get(code, 'EXCHANGE_REJECTED'))
            if response.status in {418, 429}:
                category = 'RATE_LIMIT'
            if observe_diagnostics is None:
                diagnostic = rejection_message(payload, credentials=self.credentials, request=request)
            raise ExchangeError(code, response.status, category, retry_after, message=diagnostic)
        if not isinstance(payload, (dict, list)):
            if method != 'GET':
                raise OutcomeUnknown(f'unexpected {operation} response; reconcile')
            raise DataQualityError('unexpected response shape')
        return payload
