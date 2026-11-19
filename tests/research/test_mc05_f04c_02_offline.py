from contextlib import nullcontext
from datetime import datetime, timedelta
from decimal import Decimal
import importlib
import json
from pathlib import Path
import shutil
import socket
import sys
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
import urllib.request

import pytest

from factorlab.experiments.mc_grid_tail_f04c_data import MarketData

from .test_mc05_f04c_02 import screen_inputs


ROOT = Path(__file__).resolve().parents[2]
REPORTS = ('DATA_COVERAGE.md', 'EXECUTION_CASES.md', 'FINANCIAL.md', 'PARTICIPATION.md', 'REPORT.md')


def _fail_unexpected_call(*args, **kwargs):
    pytest.fail(f'unexpected call: {args!r} {kwargs!r}')


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


@pytest.fixture
def study(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'tools/research'))
    runner = importlib.import_module('run_mc05_f04c_02')
    paper_runner = importlib.import_module('run_mc05_f04c')

    hourly, settings = screen_inputs()
    hourly = hourly[:3]
    hourly[1]['low'] = Decimal(90)
    start = datetime.fromisoformat(hourly[1]['time'])
    end = start + timedelta(hours=2)
    minutes = []
    for offset in range(120):
        time = start + timedelta(minutes=offset)
        close = Decimal(90 if offset == 0 else 100)
        minutes.append(dict(time=time.isoformat(), open=close, high=close, low=close, close=close,
                            volume=Decimal(1), trade_count=1, taker_buy_volume=Decimal('.5'),
                            taker_buy_quote_volume=close / 2))
    evidence = dict(start=start.isoformat(), end_exclusive=end.isoformat(), status='MARKET_DATA_ACCEPTED',
                    seed=hourly[0]['time'], minute_rows=len(minutes), hourly_rows=2, daily_rows=0,
                    minute_to_hour=dict(ohlcv_matches=2, auxiliary_mismatches={}, examples=[]),
                    hour_to_day=dict(ohlcv_matches=0, auxiliary_mismatches={}, examples=[]),
                    hourly_source=dict(data_version='test', bars_sha256='test',
                                       acquired_at=start.isoformat(), raw_receipts=[]),
                    archive_requests=[])
    market_data = MarketData(tuple(hourly), tuple(minutes), (), evidence)
    bars = [{key: str(value) if isinstance(value, Decimal) else value for key, value in row.items()}
            for row in hourly]
    settings.update(bars=bars, interval='1h', slippage=Decimal('.0006'), research_wal=True)
    monkeypatch.setattr(paper_runner, '_paper_inputs', lambda data: (settings, Decimal('.10')))
    accounts = tmp_path / 'accounts'
    runner.run(accounts / 'I1', market_data, account_prefix='mc05-f04c-02-I1')
    shutil.copytree(accounts / 'I1', accounts / 'repeat/I1')
    frozen = accounts / 'frozen-regression'
    frozen.mkdir()
    frozen_hash = _read(ROOT / 'docs/validation/mc-05-f04c/AUDIT.json')['result_sha256']
    (frozen / 'AUDIT.json').write_bytes(runner._encode(dict(result_sha256=frozen_hash)))
    cached = _read(accounts / 'I1/AUDIT.json')

    archive = tmp_path / 'archive'
    archive.mkdir()
    receipt = dict(name='fixture.zip', window='I1', bytes=1, fetched_at=start.isoformat())
    (archive / 'requests.json').write_bytes(runner._encode([receipt]))
    candidate = dict(id='I1', sample='INDEPENDENT_HISTORICAL_WINDOW', selected=True, screening=None,
                     start=start.isoformat(), end_exclusive=end.isoformat(),
                     anchor_time=hourly[0]['time'], anchor_close=hourly[0]['close'])
    monkeypatch.setattr(runner, '_hourly_source', lambda snapshot: (hourly, {}, None, None))
    monkeypatch.setattr(runner, 'select_windows', lambda hourly, manifest: [candidate])
    monkeypatch.setattr(runner, '_bind_hourly_raw', lambda *args: None)
    monkeypatch.setattr(runner, 'archive_objects', lambda selected: [receipt])
    monkeypatch.setattr(runner, 'admit_market_data', lambda *args: market_data)
    monkeypatch.setattr(runner, 'run', _fail_unexpected_call)
    output = tmp_path / 'reports'
    argv = [
        'run_mc05_f04c_02.py', '--hourly-snapshot', str(tmp_path / 'snapshot'),
        '--archive-root', str(archive), '--legacy-archive-root', str(tmp_path / 'legacy'),
        '--accounts-root', str(accounts), '--output', str(output), '--resume',
    ]
    return SimpleNamespace(runner=runner, cached=cached, archive=archive, output=output, argv=argv,
                           start=start.isoformat())


def _assert_published(study):
    audit = _read(study.output / 'AUDIT.json')
    window, = audit['windows']
    assert window['result_sha256'] == window['repeat_result_sha256'] == study.cached['result_sha256']
    assert window['accounts'] == study.cached['accounts']
    assert window['accounts']['B']['metrics']['fills'] > 0
    assert audit['validation']['deterministic_replay'] == 'PASS'
    assert all((study.output / name).stat().st_size > 0 for name in REPORTS)
    return audit['finer_evidence']


@pytest.mark.parametrize('prior_location', ['absent', 'output', 'explicit'])
def test_offline_cli_reuses_receipts_and_publishes_cached_accounts(study, monkeypatch, tmp_path, prior_location):
    monkeypatch.setattr(study.runner, 'acquire', _fail_unexpected_call)
    monkeypatch.setattr(study.runner, 'urlopen', _fail_unexpected_call)
    monkeypatch.setattr(urllib.request, 'urlopen', _fail_unexpected_call)
    monkeypatch.setattr(socket.socket, 'connect', _fail_unexpected_call)

    targets = list(study.runner._probe_targets('I1', study.start))
    checked_at = '2026-11-19T10:50:30+00:00'
    original = targets[0] | dict(status=200, checked_at=checked_at, content_length='712')
    if prior_location != 'absent':
        if prior_location == 'output':
            study.output.mkdir()
            audit_path = study.output / 'AUDIT.json'
        else:
            audit_path = tmp_path / 'historical-audit.json'
            study.argv.extend(['--probe-audit', str(audit_path)])
        audit_path.write_bytes(study.runner._encode(dict(finer_evidence=dict(probes=[original]))))
    monkeypatch.setattr(sys, 'argv', study.argv)
    study.runner.main()

    evidence = _assert_published(study)
    rows = evidence['probes']
    if prior_location == 'absent':
        assert evidence['probe_mode'] == 'NOT_RECHECKED_OFFLINE'
        missing = rows
    else:
        assert evidence['probe_mode'] == 'OFFLINE_REUSE'
        assert rows[0] == original | dict(outcome='AVAILABLE')
        missing = rows[1:]
    assert len(rows) == 3
    assert [(row['status'], row['outcome']) for row in missing] == [
        ('NOT_RECHECKED_OFFLINE', 'NOT_RECHECKED_OFFLINE')] * len(missing)
    assert all('checked_at' not in row and 'object_availability' not in row for row in missing)


@pytest.mark.parametrize('network_error', [TimeoutError, socket.timeout, URLError],
                         ids=['timeout', 'socket-timeout', 'url-error'])
def test_online_optional_probe_failure_preserves_reports(study, monkeypatch, network_error):
    monkeypatch.setattr(study.runner, 'acquire', lambda *args: dict(mode='ONLINE_ACQUIRE'))
    requests = []

    def probe(request, timeout):
        requests.append(request)
        assert timeout == 20
        if '/aggTrades/' in request.full_url:
            raise network_error('probe failed')
        if '/bookTicker/' in request.full_url:
            raise HTTPError(request.full_url, 404, 'Not Found', {'Date': 'cached'}, None)
        return nullcontext(SimpleNamespace(status=200, headers={'Content-Length': '456'}))

    monkeypatch.setattr(study.runner, 'urlopen', probe)
    monkeypatch.setattr(sys, 'argv', [*study.argv, '--acquire'])
    study.runner.main()

    evidence = _assert_published(study)
    assert evidence['probe_mode'] == 'ONLINE_HEAD'
    failed, absent, available = evidence['probes']
    assert [request.get_method() for request in requests] == ['HEAD'] * 3
    assert [request.full_url for request in requests] == [row['url'] for row in evidence['probes']]
    assert all(datetime.fromisoformat(row['checked_at']).utcoffset() == timedelta(0)
               for row in evidence['probes'])
    assert failed['status'] == failed['outcome'] == 'NETWORK_ERROR'
    assert 'probe failed' in failed['error']
    assert 'object_availability' not in failed
    assert absent['status'] == 404
    assert absent['outcome'] == 'HTTP_NOT_FOUND'
    assert absent['object_availability'] == 'NOT_AVAILABLE_IN_ARCHIVE'
    assert absent['headers'] == {'Date': 'cached'}
    assert available['status'] == 200
    assert available['outcome'] == 'AVAILABLE'
    assert available['headers'] == {'Content-Length': '456'}


def test_probe_does_not_hide_programming_errors(study, monkeypatch):
    def broken_probe(*args, **kwargs):
        raise RuntimeError('unexpected probe bug')

    monkeypatch.setattr(study.runner, 'urlopen', broken_probe)
    with pytest.raises(RuntimeError, match='unexpected probe bug'):
        study.runner.finer_evidence(('I1', study.start), online=True, prior=None)


@pytest.mark.parametrize('empty', [False, True], ids=['partial', 'empty'])
def test_offline_report_only_draws_conclusions_from_admitted_windows(study, monkeypatch, empty):
    runner = study.runner
    (accepted,) = runner.select_windows(None, None)
    blocked = accepted | dict(id='I2')
    monkeypatch.setattr(runner, 'select_windows', lambda *args: [accepted, blocked])
    monkeypatch.setattr(runner, 'archive_objects', lambda *args: [
        dict(name='fixture.zip', window='I1'), dict(name='missing.zip', window='I2'),
    ])
    monkeypatch.setattr(runner, 'urlopen', _fail_unexpected_call)
    monkeypatch.setattr(socket.socket, 'connect', _fail_unexpected_call)
    if empty:
        (study.archive / 'requests.json').write_bytes(runner._encode([]))
    monkeypatch.setattr(sys, 'argv', study.argv)

    runner.main()

    audit = _read(study.output / 'AUDIT.json')
    report = (study.output / 'REPORT.md').read_text(encoding='utf-8')
    assert all((study.output / name).stat().st_size > 0 for name in REPORTS)
    assert audit['windows'][1]['status'] == 'MARKET_DATA_BLOCKED'
    assert 'I2 的分钟组更早成交' not in report
    if empty:
        assert audit['windows'][0]['status'] == 'MARKET_DATA_BLOCKED'
        assert audit['acquisition']['first_fetched_at'] is None
        assert audit['acquisition']['last_fetched_at'] is None
        assert audit['validation']['deterministic_replay'] == 'NOT_RUN_NO_ADMITTED_WINDOWS'
        assert 'NO_ADMITTED_EXECUTION_CASE' in audit['states']
        assert '缺少可比较的 Paper 账户结果' in report
        assert '分钟报价观察改变了配对账户的成交轨迹' not in report
    else:
        assert audit['windows'][0]['status'] == 'MARKET_DATA_ACCEPTED'
        assert '分钟报价观察改变了配对账户的成交轨迹' in report
