"""Read-only Alpaca data requests; feed selection is always explicit."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from email.utils import parsedate_to_datetime
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class MarketDataError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    data: object


def transport(url, headers, timeout):
    request = Request(url, headers=headers, method='GET')
    try:
        with urlopen(request, timeout=timeout) as response:
            return Response(response.status, dict(response.headers), json.load(response))
    except HTTPError as exc:
        body = exc.read()
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            data = None
        return Response(exc.code, dict(exc.headers), data)


@dataclass(frozen=True)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)

    @classmethod
    def from_env(cls):
        return cls(os.environ['APCA_API_KEY_ID'], os.environ['APCA_API_SECRET_KEY'])

    @classmethod
    def from_dpapi(cls, path):
        """Read an explicitly selected Windows user-encrypted SecureString file."""
        import subprocess
        command = ("$ErrorActionPreference='Stop'; "
                   "$s=Import-Clixml -LiteralPath $env:FACTORLAB_CREDENTIAL_FILE; "
                   "$c=New-Object System.Net.NetworkCredential('', $s); "
                   "[Console]::Out.Write($c.Password)")
        environment = dict(os.environ, FACTORLAB_CREDENTIAL_FILE=os.fspath(path))
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
                                env=environment, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise MarketDataError(None, 'cannot decrypt selected local credentials')
        payload = json.loads(result.stdout)
        return cls(payload['key'], payload['secret'])


class MarketDataClient:
    data_url = 'https://data.alpaca.markets'
    paper_url = 'https://paper-api.alpaca.markets'
    feeds = frozenset({'iex', 'sip', 'otc', 'boats'})

    def __init__(self, credentials, *, feed, request=transport, timeout=20,
                 attempts=3, sleep=time.sleep, now=lambda: datetime.now(timezone.utc)):
        if feed not in self.feeds:
            raise ValueError('unsupported historical stock feed')
        if timeout <= 0 or attempts < 1:
            raise ValueError('positive timeout and attempt budget required')
        self.credentials, self.feed = credentials, feed
        self.request, self.timeout, self.attempts = request, timeout, attempts
        self.sleep, self.now = sleep, now
        self.evidence = []

    def get(self, path, params=None, *, paper=False):
        allowed = {'/v2/calendar', '/v2/clock'} if paper else {
            '/v2/stocks/bars', '/v2/stocks/quotes', '/v2/stocks/trades',
            '/v2/stocks/quotes/latest', '/v2/stocks/trades/latest',
            '/v2/stocks/snapshots', '/v1/corporate-actions'}
        if path not in allowed:
            raise ValueError('not a read-only market data endpoint')
        params = dict(params or {})
        if path.startswith('/v2/stocks/'):
            params['feed'] = self.feed
        url = (self.paper_url if paper else self.data_url) + path + '?' + urlencode(params)
        headers = {'APCA-API-KEY-ID': self.credentials.key,
                   'APCA-API-SECRET-KEY': self.credentials.secret}
        for attempt in range(self.attempts):
            requested_at = self.now()
            try:
                response = self.request(url, headers, self.timeout)
            except (URLError, TimeoutError, ConnectionError, OSError) as exc:
                self.evidence.append({'path': path, 'params': params, 'requested_at': requested_at.isoformat(),
                                      'received_at': self.now().isoformat(), 'status': 'NETWORK_ERROR'})
                if attempt + 1 == self.attempts:
                    raise MarketDataError(None, 'market data transport failed') from exc
                self.sleep(2 ** attempt)
                continue
            received_at = self.now()
            self.evidence.append({'path': path, 'params': params, 'requested_at': requested_at.isoformat(),
                                  'received_at': received_at.isoformat(), 'status': response.status,
                                  'request_id': response.headers.get('X-Request-ID')})
            if response.status == 200:
                return response.data, received_at
            if response.status not in {429, 500, 502, 503, 504} or attempt + 1 == self.attempts:
                raise MarketDataError(response.status, f'market data request rejected: HTTP {response.status}')
            lower = {k.lower(): v for k, v in response.headers.items()}
            retry_after = lower.get('retry-after')
            if retry_after is None:
                delay = 2 ** attempt
            else:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = (parsedate_to_datetime(retry_after) - received_at).total_seconds()
            if response.status == 429 and 'x-ratelimit-reset' in lower:
                delay = max(delay, float(lower['x-ratelimit-reset']) - received_at.timestamp())
            if delay > self.timeout:
                raise MarketDataError(429, 'rate-limit delay exceeds request budget')
            self.sleep(max(0, delay))

    def history(self, kind, symbols, start, end, **params):
        if kind not in {'bars', 'quotes', 'trades'}:
            raise ValueError('unsupported history kind')
        query = dict(params, symbols=','.join(symbols), start=str(start), end=str(end), limit=10000, sort='asc')
        result, tokens = [], set()
        while True:
            data, received = self.get('/v2/stocks/' + kind, query)
            for symbol, rows in (data[kind] or {}).items():
                result.extend(dict(row, symbol=symbol, received_at=received.isoformat(),
                                   source='ALPACA:' + self.feed) for row in rows)
            token = data.get('next_page_token')
            if not token:
                return result
            if token in tokens:
                raise MarketDataError(200, 'repeated pagination token')
            tokens.add(token)
            query['page_token'] = token

    def bars(self, symbols, start, end, *, timeframe='1Day', adjustment='raw', asof):
        choices = adjustment.split(',')
        if not choices or any(x not in {'raw', 'split', 'dividend', 'spin-off', 'all'} for x in choices):
            raise ValueError('unsupported adjustment')
        return self.history('bars', symbols, start, end, timeframe=timeframe, adjustment=adjustment, asof=asof)

    def latest(self, kind, symbols):
        if kind not in {'quotes', 'trades'}:
            raise ValueError('unsupported latest kind')
        return self.get('/v2/stocks/' + kind + '/latest', {'symbols': ','.join(symbols)})

    def snapshots(self, symbols):
        return self.get('/v2/stocks/snapshots', {'symbols': ','.join(symbols)})

    def calendar(self, start, end):
        return self.get('/v2/calendar', {'start': str(start), 'end': str(end)}, paper=True)

    def clock(self):
        return self.get('/v2/clock', paper=True)

    def corporate_actions(self, symbols, start, end):
        query = {'symbols': ','.join(symbols), 'start': str(start), 'end': str(end), 'limit': 1000}
        pages, tokens = [], set()
        while True:
            data, received = self.get('/v1/corporate-actions', query)
            pages.append({'data': data, 'received_at': received.isoformat()})
            token = data.get('next_page_token')
            if not token:
                return pages
            if token in tokens:
                raise MarketDataError(200, 'repeated corporate action page token')
            tokens.add(token)
            query['page_token'] = token
