from contextlib import closing
import hashlib
import hmac
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from factorlab.integrations.binance_futures_trading import demo_cli, protocol, tradfi_agreement
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, SMOKE_CAMPAIGN
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment, SafetyBlocked


DEMO = Environment.DEMO
CREDENTIAL = Credentials('synthetic-demo-key', 'synthetic-demo-secret', DEMO)
ACCOUNT = 'DEMO:FactorLab-Demo:' + hashlib.sha256(CREDENTIAL.key.encode()).hexdigest()
PATH = '/fapi/v1/stock/contract'


class Reply(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.status, self.headers = 200, {}


@pytest.mark.parametrize(('http', 'payload', 'success'), [
    (200, {'code': 200, 'msg': 'success'}, True),
    (200, {'code': 200, 'msg': 'success', 'agreementVersion': 'other'}, False),
    (404, {'code': -1002, 'msg': 'Unavailable'}, False),
    (503, {'code': -1007, 'msg': 'Timeout'}, False),
    (200, b'', False),
    (200, b'generic plain-text response', False),
])
def test_demo_agreement_is_its_own_signed_once_command(tmp_path, monkeypatch, http, payload, success):
    counts = dict(GET=0, POST=0, DELETE=0)
    calls = []

    def intercept(request, timeout):
        url = urlsplit(request.full_url)
        assert (url.scheme, url.netloc) == ('https', 'demo-fapi.binance.com')
        calls.append((request.get_method(), url.path))
        if url.path == '/fapi/v1/time':
            return Reply({'serverTime': 1794650000000})
        assert dict(request.header_items())['X-mbx-apikey'] == CREDENTIAL.key
        unsigned, signature = url.query.rsplit('&signature=', 1)
        assert signature == hmac.new(CREDENTIAL.secret.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        assert set(parse_qs(unsigned)) == {'timestamp', 'recvWindow'}
        if request.get_method() == 'GET':
            assert url.path == '/fapi/v3/account'
            return Reply({'assets': [], 'positions': []})
        assert request.get_method() == 'POST' and url.path == PATH
        if http != 200:
            raise HTTPError(request.full_url, http, 'fixture', {}, Reply(payload))
        return Reply(payload)

    def opener(*handlers):
        assert handlers[0].redirect_request(None, None, 302, '', {}, 'https://fapi.binance.com') is None
        return SimpleNamespace(open=intercept)

    monkeypatch.setattr(protocol, 'build_opener', opener)
    monkeypatch.setattr(tradfi_agreement, 'build_opener', opener)
    monkeypatch.setattr(demo_cli, 'load_bound_credential', lambda store: ({'account_id': ACCOUNT}, CREDENTIAL))
    args = demo_cli.parse_args(['tradfi-sign', '--accept-agreement'])
    with closing(DemoRuntime(tmp_path)) as runtime:
        result = demo_cli.run_command(args, tmp_path, None, runtime, counts, SMOKE_CAMPAIGN)
        receipt = result['agreement']
        code = payload['code'] if isinstance(payload, dict) else None
        expected_format = 'JSON' if isinstance(payload, dict) else 'UNPARSED' if payload else 'EMPTY'
        assert (receipt['http_status'], receipt['code'], receipt['success']) == (http, code, success)
        assert receipt['body_format'] == expected_format
        if isinstance(payload, bytes):
            assert receipt['body_bytes'] == len(payload)
            assert receipt['message'] is None
        assert runtime.tradfi_receipt(ACCOUNT) == receipt
        assert runtime.campaign(SMOKE_CAMPAIGN) is None
        repeated = demo_cli.run_command(args, tmp_path, None, runtime, counts, SMOKE_CAMPAIGN)
        assert repeated['status'] == 'DEMO_TRADFI_ATTEMPT_RECORDED'
    assert calls == [('GET', '/fapi/v1/time'), ('GET', '/fapi/v3/account'), ('POST', PATH)]
    assert counts == dict(GET=2, POST=1, DELETE=0)
    assert CREDENTIAL.key not in repr(result) and CREDENTIAL.secret not in repr(result) and ACCOUNT not in repr(result)


def test_agreement_requires_explicit_consent_and_unchanged_generic_boundary():
    with pytest.raises(SystemExit):
        demo_cli.parse_args(['tradfi-sign'])
    counts = dict(GET=0, POST=0, DELETE=0)
    for credential, account, consent in (
        (CREDENTIAL, ACCOUNT, False),
        (CREDENTIAL, ACCOUNT + 'other', True),
        (Credentials('k', 's', Environment.PRODUCTION), ACCOUNT, True),
    ):
        with pytest.raises(SafetyBlocked):
            tradfi_agreement.sign_tradfi_agreement(credential, account_id=account, accept_agreement=consent,
                requests=counts, reserve=lambda receipt: pytest.fail('unexpected agreement reservation'))
    assert counts == dict(GET=0, POST=0, DELETE=0)
    with pytest.raises(SafetyBlocked, match='allowlisted'):
        protocol.FuturesClient(CREDENTIAL, environment=DEMO).build('POST', PATH)
    assert ('POST', PATH) not in protocol.WRITE_PATHS


def test_unconfirmed_agreement_preserves_unspent_smoke_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_cli, 'private_binding', lambda store: {'account_id': ACCOUNT})
    counts = dict(GET=0, POST=0, DELETE=0)
    args = demo_cli.parse_args(['smoke-test', '--require-tradfi-signed'])
    with closing(DemoRuntime(tmp_path)) as runtime:
        runtime.reserve_tradfi(ACCOUNT, {'success': False})
        with pytest.raises(SafetyBlocked, match='successful TradFi agreement receipt'):
            demo_cli.run_command(args, tmp_path, None, runtime, counts, SMOKE_CAMPAIGN)
        assert runtime.campaign(SMOKE_CAMPAIGN) is None
    assert counts == dict(GET=0, POST=0, DELETE=0)
