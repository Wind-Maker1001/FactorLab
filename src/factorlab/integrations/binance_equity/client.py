"""Allowlisted, unsigned Equity MARKET_DATA GET requests."""
from dataclasses import dataclass
from http.client import HTTPException
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from factorlab.data.evidence import utc_now
from factorlab.errors import DataQualityError
from . import REST_HOST


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


class MarketDataFailure(DataQualityError, ValueError):
    """Sanitized acquisition failure, without upstream response or credentials."""

    def __init__(self, reason, *, status=None, code=None):
        self.reason, self.status, self.code = reason, status, code
        super().__init__(f'Equity {reason}' + (f' HTTP {status}' if status is not None else ''))

    def diagnostic(self):
        return dict(reason=self.reason, http_status=self.status, exchange_code=self.code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(url, headers, timeout):
    try:
        with build_opener(_NoRedirect()).open(
                Request(url, headers=headers, method='GET'), timeout=timeout) as response:
            return Response(response.status, response.read())
    except HTTPError as error:
        return Response(error.code, error.read())


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,19}', value):
        raise ValueError('expected uppercase stock ticker')
    return value


class Client:
    """Authentication is read only from the process environment, never CLI args."""

    def __init__(self, *, network=False, request=transport, timeout=30, credentials=None):
        if not network:
            raise ValueError('network access requires explicit opt-in')
        if timeout <= 0:
            raise ValueError('timeout must be positive')
        from factorlab.integrations.binance_futures_trading.protocol import Environment
        if credentials is not None and credentials.environment is not Environment.PRODUCTION:
            raise ValueError('Stocks requires production read-only credentials')
        key = credentials.key if credentials is not None else os.environ.get('BINANCE_EQUITY_API_KEY')
        if not key or not key.isascii() or not key.isalnum():
            raise MarketDataFailure('MISSING_OR_INVALID_API_KEY')
        self._key = key
        self._request, self.timeout = request, timeout

    def _get(self, endpoint, ticker, sink):
        if endpoint not in ('exchangeInfo', 'quote'):
            raise ValueError('only exchangeInfo and quote are allowed')
        params = {} if ticker is None else {'symbol': symbol(ticker)}
        if endpoint == 'quote' and ticker is None:
            raise ValueError('quote requires a ticker')
        url = REST_HOST + '/sapi/v1/equity/market/' + endpoint
        if params:
            url += '?' + urlencode(params)
        try:
            response = self._request(url, {'X-MBX-APIKEY': self._key}, self.timeout)
        except (OSError, URLError, HTTPException):
            raise MarketDataFailure('NETWORK_ERROR') from None
        acquired = utc_now()
        # Even an upstream diagnostic echo must not publish authentication material.
        if self._key.encode() in response.body:
            raise DataQualityError('response contained authentication material; discarded')
        if response.status != 200:
            try:
                error = json.loads(response.body)
            except (ValueError, UnicodeError):
                error = None
            code = error.get('code') if isinstance(error, dict) else None
            code = code if type(code) is int else None
            message = error.get('msg', '') if isinstance(error, dict) else ''
            geographic = isinstance(message, str) and any(term in message.lower() for term in
                ('restricted location', 'restricted country', 'service unavailable from your location'))
            reason = ('REGION_RESTRICTED' if response.status == 451 or geographic else
                      'PERMISSION_DENIED' if response.status == 401 or code in {-1002, -2014, -2015} else
                      'HTTP_ERROR')
            raise MarketDataFailure(reason, status=response.status, code=code)
        receipt = sink.raw(response.body, transport='REST', endpoint=endpoint,
                           acquired_at=acquired, status=response.status, params=params)
        try:
            value = json.loads(response.body) if response.body else None
        except (ValueError, UnicodeError):
            raise DataQualityError('Equity returned invalid JSON') from None
        return value, acquired, receipt

    def discover(self, sink, ticker=None):
        from .market import discovery
        value, acquired, receipt = self._get('exchangeInfo', ticker, sink)
        rows = discovery(value, acquired, receipt)
        sink.add('symbols', rows)
        return rows

    def quote(self, sink, ticker):
        from .market import rest_quote
        value, acquired, receipt = self._get('quote', ticker, sink)
        rows = rest_quote(value, symbol(ticker), acquired, receipt)
        sink.add('quotes', rows)
        return rows
