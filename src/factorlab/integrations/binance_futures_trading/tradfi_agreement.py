"""Explicit one-time TradFi-Perps agreement acceptance on Binance Demo."""

from datetime import datetime, timezone
from urllib.request import build_opener

from factorlab.errors import DataQualityError

from .protocol import (
    Environment, ExchangeError, FuturesClient, OutcomeUnknown, ReadOnlyNetworkTransport,
    SafetyBlocked, _request_http, credential_account_id,
)


AGREEMENT_PATH = '/fapi/v1/stock/contract'


def sign_tradfi_agreement(credential, *, account_id, accept_agreement, requests, reserve):
    """Bind the saved key and authorize exactly the documented Demo agreement request."""
    if accept_agreement is not True:
        raise SafetyBlocked('explicit --accept-agreement is required')
    environment = Environment.DEMO
    if credential is None or credential.environment is not environment:
        raise SafetyBlocked('saved Demo credential required')
    if credential_account_id(environment, credential.key) != account_id:
        raise SafetyBlocked('persisted Demo account binding mismatch')

    transport = ReadOnlyNetworkTransport(environment=environment, permit_account_read=True)
    client = FuturesClient(credential, environment=environment, transport=transport)
    requests['GET'] += 1
    client.sync_time()
    requests['GET'] += 1
    account = client.request('GET', '/fapi/v3/account')
    if not isinstance(account, dict) or not isinstance(account.get('assets'), list):
        raise DataQualityError('signed Demo account response is incomplete')

    receipt = dict(environment=environment.name, host=environment.host, method='POST', path=AGREEMENT_PATH,
        account_binding='…' + account_id[-12:], requested_at=datetime.now(timezone.utc).isoformat(),
        http_status=None, body_bytes=None, body_format=None, code=None, message=None,
        category='OUTCOME_UNKNOWN', success=False)
    reserve(receipt.copy())

    # This endpoint is deliberately outside FuturesClient.build/request and the order transport.
    request = client._signed_request('POST', AGREEMENT_PATH, {})
    requests['POST'] += 1
    try:
        response = _request_http(environment.host, request, client.timeout, opener=build_opener)
    except OSError:
        return receipt
    receipt['http_status'] = response.status
    receipt['body_bytes'] = len(response.body)
    receipt['body_format'] = 'UNPARSED' if response.body.strip() else 'EMPTY'

    def observe_diagnostics(code, message):
        receipt.update(body_format='JSON', code=code, message=message)

    try:
        payload = client._parse_response(request, response, operation='agreement',
                                         observe_diagnostics=observe_diagnostics)
    except ExchangeError as error:
        receipt['category'] = error.category
    except OutcomeUnknown:
        pass
    else:
        if payload == {'code': 200, 'msg': 'success'} and type(payload['code']) is int:
            receipt.update(category='SUCCESS', success=True)
        else:
            receipt['category'] = 'AGREEMENT_REVIEW_REQUIRED'

    if response.status in {404, 405, 501}:
        receipt['category'] = 'ENDPOINT_UNAVAILABLE'
    elif response.status in {401, 403} or receipt['category'] == 'AUTH':
        receipt['category'] = 'PERMISSION_DENIED'
    return receipt
