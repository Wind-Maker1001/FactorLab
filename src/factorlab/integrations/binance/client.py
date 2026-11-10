"""Public Binance USD-M GET transport with bounded retries and raw receipts."""
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from factorlab.data.evidence import archive_bytes, sha256, utc_now
from factorlab.errors import DataQualityError
from .store import _write_json

HOST = 'https://fapi.binance.com'
ENDPOINTS = frozenset({'ping', 'exchangeInfo', 'klines', 'fundingRate', 'premiumIndex'})
PARAMETERS = {
    'ping': set(), 'exchangeInfo': set(), 'premiumIndex': {'symbol'},
    'klines': {'symbol', 'interval', 'startTime', 'endTime', 'limit'},
    'fundingRate': {'symbol', 'startTime', 'endTime', 'limit'},
}


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(url, timeout):
    try:
        with build_opener(_NoRedirect()).open(Request(url, method='GET'), timeout=timeout) as response:
            return Response(response.status, dict(response.headers), response.read())
    except HTTPError as exc:
        return Response(exc.code, dict(exc.headers), exc.read())


def powershell_transport(url, timeout):
    """Windows network-stack option; URL is data, never executable script text."""
    script = '''$ErrorActionPreference = 'Stop'
try {
 $r = Invoke-WebRequest -UseBasicParsing -Uri $env:FACTORLAB_PUBLIC_URL -Method Get -MaximumRedirection 0 -TimeoutSec ([int]$env:FACTORLAB_TIMEOUT)
 $body = [Convert]::ToBase64String($r.RawContentStream.ToArray())
 @{status=[int]$r.StatusCode; headers=@{'Retry-After'=$r.Headers['Retry-After']}; body=$body} | ConvertTo-Json -Compress
} catch {
 if ($null -eq $_.Exception.Response) { throw }
 $r=$_.Exception.Response
 $reader=New-Object IO.StreamReader($r.GetResponseStream())
 $body=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($reader.ReadToEnd()))
 @{status=[int]$r.StatusCode; headers=@{'Retry-After'=$r.Headers['Retry-After']}; body=$body} | ConvertTo-Json -Compress
}'''
    import base64
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        env={**os.environ, 'FACTORLAB_PUBLIC_URL': url, 'FACTORLAB_TIMEOUT': str(int(timeout))},
        capture_output=True, timeout=timeout + 15,
    )
    if result.returncode:
        raise OSError(result.stderr.decode(errors='replace'))
    value = json.loads(result.stdout.decode('utf-8-sig'))
    return Response(value['status'], value['headers'], base64.b64decode(value['body']))


class PublicClient:
    """No credentials, signing, arbitrary hosts, or non-GET capabilities."""

    def __init__(self, *, transport=urllib_transport, timeout=30, retries=3,
                 backoff=1.0, sleep=time.sleep, clock=time.time):
        if timeout <= 0 or retries < 0 or backoff < 0:
            raise ValueError('invalid timeout/retry policy')
        self.transport, self.timeout, self.retries = transport, timeout, retries
        self.backoff, self.sleep, self.clock = backoff, sleep, clock

    def get(self, endpoint, params=None, *, archive=None, cache=False):
        params = dict(params or {})
        if endpoint not in ENDPOINTS or not set(params) <= PARAMETERS[endpoint]:
            raise ValueError('only allowlisted public market-data GET requests are supported')
        if cache:
            if endpoint in {'exchangeInfo', 'premiumIndex'}:
                raise ValueError('current snapshots cannot use historical request cache')
            if endpoint in {'klines', 'fundingRate'}:
                end = int(params['endTime']) + 1
                if end > int(self.clock() * 1000):
                    raise ValueError('cannot cache future or unclosed historical requests')
                if endpoint == 'klines':
                    from .market import INTERVALS
                    step = INTERVALS[params['interval']]
                    if end % step:
                        raise ValueError('cached klines must end on a completed UTC interval boundary')
        url = HOST + '/fapi/v1/' + endpoint
        if params:
            url += '?' + urlencode(sorted(params.items()))
        key = sha256(url.encode())
        root = Path(archive) if archive is not None else None
        cached = root / 'requests' / (key + '.json') if root else None
        if cache and cached is not None and cached.exists():
            receipt = json.loads(cached.read_text())
            body = (root / receipt['raw_path']).read_bytes()
            if sha256(body) != receipt['sha256']:
                raise DataQualityError('cached raw response digest mismatch')
            return json.loads(body)
        for attempt in range(self.retries + 1):
            response = None
            try:
                response = self.transport(url, self.timeout)
            except (OSError, URLError, HTTPException, subprocess.TimeoutExpired) as exc:
                if attempt == self.retries:
                    raise DataQualityError(f'public GET exhausted retries: {url}') from exc
            if response is not None:
                if root is not None:
                    acquired = utc_now()
                    artifact = archive_bytes(response.body, root / 'raw', 'BINANCE_USDM', url, acquired)
                    receipt = dict(endpoint=endpoint, params=params, url=url, status=response.status,
                                   acquired_at=acquired, sha256=artifact.sha256,
                                   raw_path='raw/' + Path(artifact.path).name,
                                   headers=response.headers)
                    requests = root / 'requests'
                    requests.mkdir(exist_ok=True)
                    _write_json(requests / (key + '-' + uuid.uuid4().hex + '.json'), receipt)
                if response.status == 200:
                    value = json.loads(response.body)
                    if root is not None and cache:
                        _write_json(cached, receipt)
                    return value
                if response.status not in {429, 500, 502, 503, 504}:
                    raise DataQualityError(f'public GET HTTP {response.status}: {url}')
                if attempt == self.retries:
                    raise DataQualityError(f'public GET HTTP {response.status}; retries exhausted: {url}')
            delay = self.backoff * 2 ** attempt
            if response is not None:
                headers = {k.lower(): v for k, v in response.headers.items()}
                retry_after = headers.get('retry-after')
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - self.clock())
            self.sleep(delay)
