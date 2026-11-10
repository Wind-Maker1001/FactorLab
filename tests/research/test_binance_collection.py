"""Offline HTTP fixtures exercise only the independent Binance integration."""
import json
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest

from factorlab.data.evidence import json_bytes
from factorlab.errors import DataQualityError
from factorlab.integrations.binance import Collector, PublicClient, Reader
from factorlab.integrations.binance.__main__ import main
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance.market import (
    BOUNDARIES, bars, completeness, discover, funding, identity, mark, milliseconds,
    paginate, utc, window,
)

DAY = 86_400_000
BASE = milliseconds('2026-11-01')
CONTRACT = dict(symbol='NVDAUSDT', contractType='TRADIFI_PERPETUAL', status='TRADING',
                quoteAsset='USDT', marginAsset='USDT', onboardDate=BASE, underlyingType='EQUITY')


def kline(t, price='180.00'):
    return [t, price, '182.00', '179.00', '181.00', '100.00', t + DAY - 1,
            '18000.00', 50, '40.00', '7200.00', '0']


def funding_row(t, kind='Regular'):
    return dict(symbol='NVDAUSDT', fundingTime=t, fundingRate='0.0001', markPrice='181.00', rateType=kind)


def premium():
    return dict(symbol='NVDAUSDT', time=BASE + 4 * DAY, markPrice='181.12340000',
                indexPrice='180.30000000', lastFundingRate='0.0001',
                nextFundingTime=BASE + 4 * DAY + 28_800_000,
                estimatedSettlePrice='180.40', interestRate='0.0001')


class FixtureExchange:
    def __init__(self):
        self.calls = []
        self.contract = dict(CONTRACT)
        self.bar_rows = [kline(BASE + n * DAY) for n in range(5)]
        self.funding_rows = [funding_row(BASE + 1), funding_row(BASE + DAY + 1, 'Special')]
        self.fail_endpoint = None
        self.page_size = 2

    def __call__(self, url, timeout):
        parsed = urlsplit(url)
        endpoint = parsed.path.rsplit('/', 1)[-1]
        params = {k: values[0] for k, values in parse_qs(parsed.query).items()}
        self.calls.append((endpoint, params))
        if endpoint == self.fail_endpoint:
            raise ConnectionError('fixture disconnect')
        if endpoint == 'exchangeInfo':
            value = {'symbols': [dict(self.contract)]}
        elif endpoint == 'premiumIndex':
            value = premium()
        elif endpoint == 'ping':
            value = {}
        else:
            source = self.bar_rows if endpoint == 'klines' else self.funding_rows
            field = 0 if endpoint == 'klines' else 'fundingTime'
            value = [row for row in source if int(params['startTime']) <= row[field] <= int(params['endTime'])]
            value = value[:min(self.page_size, int(params['limit']))]
        return Response(200, {}, json_bytes(value))


def collector(exchange, now='2026-11-05T12:00:00Z'):
    return Collector(PublicClient(transport=exchange, retries=0, clock=lambda: (BASE + 10 * DAY) / 1000),
                     now=lambda: now)


def collect(instance, root, **kwargs):
    return instance.collect(symbol='NVDAUSDT', start='2026-11-01', end='2026-11-06',
                            output=root, **kwargs)


def test_discovery_uses_class_status_and_equity_not_symbol():
    other = [dict(CONTRACT, symbol='BTCUSDT', contractType='PERPETUAL'),
             dict(CONTRACT, symbol='TSLAUSDT', status='PENDING_TRADING'),
             dict(CONTRACT, symbol='GOLDUSDT', underlyingType='COMMODITY')]
    result = discover({'symbols': [CONTRACT, dict(CONTRACT, symbol='OPAQUE', contractType='TRADFI_PERPETUAL'), *other]})
    assert [row['symbol'] for row in result] == ['NVDAUSDT', 'OPAQUE']
    contract = identity(result[0])
    assert contract['underlying'] is None
    assert contract['underlying_status'] == 'UNVERIFIED'
    assert contract['contract_type'] == 'TRADIFI_PERPETUAL'
    with pytest.raises(ValueError):
        identity(CONTRACT, {'symbol': 'NVDAUSDT', 'security_id': 'NVDA'})


def test_pagination_short_pages_range_and_no_network(tmp_path):
    exchange = FixtureExchange()
    client = PublicClient(transport=exchange, clock=lambda: (BASE + 10 * DAY) / 1000)
    result = paginate(client, 'klines', symbol='NVDAUSDT', start=BASE, end=BASE + 4 * DAY,
                      interval='1d', archive=tmp_path)
    assert len(result) == 4
    params = [p for e, p in exchange.calls if e == 'klines']
    assert int(params[0]['endTime']) == BASE + 4 * DAY - 1
    assert int(params[1]['startTime']) == BASE + DAY + 1
    assert len(params) == 3  # A short response does not certify the requested tail.


@pytest.mark.parametrize('start,end,interval', [
    ('2026-11-02', '2026-11-01', '1d'), ('2026-11-01', '2026-11-02', '1M'),
    ('2026-11-01T00:01:00Z', '2026-11-02', '1d'),
    ('2026-11-01T00:00:00', '2026-11-02', '1d'),
    ('2026-11-01T00:00:00.000001Z', '2026-11-02', '1d'),
])
def test_invalid_request_ranges(start, end, interval):
    with pytest.raises(ValueError):
        window(start, end, interval)


def test_timezone_daily_close_duplicates_and_gaps():
    assert utc('2026-11-01T08:00:00+08:00') == utc('2026-11-01')
    frame = bars([kline(BASE), kline(BASE), kline(BASE + 2 * DAY), kline(BASE + 3 * DAY)],
                 BASE, BASE + 4 * DAY, DAY, BASE + 3 * DAY + 100)
    assert len(frame) == 2
    assert str(frame.open_time.dtype) == 'datetime64[ns, UTC]'
    report = completeness(frame, BASE, BASE + 4 * DAY, DAY, BASE + 3 * DAY + 100, BASE)
    assert report['expected_closed_bars'] == 3
    assert report['missing_ranges'][0]['bars'] == 1
    assert not report['complete']
    assert report['unclosed_or_future_excluded']
    with pytest.raises(DataQualityError, match='conflicting'):
        bars([kline(BASE), kline(BASE, '180.01')], BASE, BASE + DAY, DAY, BASE + DAY)
    with pytest.raises(DataQualityError, match='outside'):
        bars([kline(BASE - DAY)], BASE, BASE + DAY, DAY, BASE + DAY)
    with pytest.raises(DataQualityError, match='aligned'):
        bars([kline(BASE + 1)], BASE, BASE + DAY, DAY, BASE + DAY)


def test_funding_type_precision_and_mark_semantics():
    unknown = funding_row(BASE + 3)
    unknown.pop('rateType')
    result = funding([funding_row(BASE + 1), funding_row(BASE + 1, 'Special'), unknown],
                     'NVDAUSDT', BASE, BASE + DAY)
    assert result.rate_type.iloc[:2].tolist() == ['Regular', 'Special']
    assert pd.isna(result.rate_type.iloc[-1])
    assert result.funding_rate.iloc[0] == '0.0001'
    assert mark(premium(), 'NVDAUSDT').mark_price.iloc[0] == '181.12340000'
    assert mark(premium(), 'NVDAUSDT').index_price.iloc[0] == '180.30000000'
    with pytest.raises(DataQualityError):
        funding([funding_row(BASE, 'Unexpected')], 'NVDAUSDT', BASE, BASE + DAY)
    with pytest.raises(DataQualityError):
        mark(premium(), 'TSLAUSDT')


def test_raw_parquet_replay_and_incremental_update(tmp_path):
    exchange = FixtureExchange()
    first = collect(collector(exchange), tmp_path)
    assert first.read('bars').data.shape[0] == 4  # Nov 5 is still open.
    assert first.read('funding').data.rate_type.tolist() == ['Regular', 'Special']
    assert first.read('bars').metadata.evidence_status == BOUNDARIES
    assert first.verify_raw() > 0
    assert first.manifest['acquired_at'] == max(row['acquired_at'] for row in first.manifest['raw_receipts'])
    assert first.manifest['history_cutoff_at'] == '2026-11-05T12:00:00Z'
    assert first.manifest['integrity']['complete']
    pinned_version = first.manifest['data_version']
    exchange.calls.clear()
    second = collect(collector(exchange, '2026-11-06T00:00:00Z'), tmp_path, update=True)
    assert len(second.read('bars').data) == 5
    assert first.read('bars').data.shape[0] == 4
    assert second.manifest['parent_version'] == pinned_version
    klines = [params for endpoint, params in exchange.calls if endpoint == 'klines']
    assert min(int(p['startTime']) for p in klines) == BASE + 3 * DAY
    assert Reader(tmp_path).manifest['data_version'] == second.manifest['data_version']
    assert second.verify_raw() > first.verify_raw()
    with pytest.raises(ValueError, match='already contains'):
        collect(collector(exchange), tmp_path)


def test_disconnect_resume_uses_completed_raw_pages(tmp_path):
    exchange = FixtureExchange()
    exchange.fail_endpoint = 'fundingRate'
    with pytest.raises(DataQualityError, match='exhausted'):
        collect(collector(exchange), tmp_path)
    assert not (tmp_path / 'latest.json').exists()
    initial_count = sum(e == 'klines' for e, _ in exchange.calls)
    assert initial_count > 0
    exchange.fail_endpoint = None
    reader = collect(collector(exchange), tmp_path)
    assert sum(e == 'klines' for e, _ in exchange.calls) == initial_count
    assert len(reader.read('bars').data) == 4
    assert not list(tmp_path.glob('pending-*'))


def test_gap_refetched_on_update(tmp_path):
    exchange = FixtureExchange()
    exchange.bar_rows = [row for row in exchange.bar_rows if row[0] != BASE + DAY]
    first = collect(collector(exchange), tmp_path)
    assert first.manifest['integrity']['missing_ranges'][0]['bars'] == 1
    exchange.bar_rows.append(kline(BASE + DAY))
    exchange.bar_rows.sort(key=lambda row: row[0])
    second = collect(collector(exchange), tmp_path, update=True)
    assert second.manifest['integrity']['complete']
    assert len(second.read('bars').data) == 4


def test_pre_onboard_and_closed_cutoff():
    empty = bars([], BASE, BASE + DAY, DAY, BASE)
    report = completeness(empty, BASE, BASE + DAY, DAY, BASE + DAY, BASE + 2 * DAY)
    assert report['expected_closed_bars'] == 0
    assert report['complete']
    closed = bars([kline(BASE)], BASE, BASE + DAY, DAY, BASE + DAY - 1)
    assert closed.empty  # At the inclusive close millisecond, the bucket is not closed yet.


def test_reader_detects_corruption_and_cli_rejects_trades(tmp_path):
    reader = collect(collector(FixtureExchange()), tmp_path)
    (reader.root / 'bars.parquet').write_bytes(b'broken')
    with pytest.raises(DataQualityError, match='digest'):
        Reader(tmp_path).read('bars')
    for command in ['trade', 'cancel', 'transfer', 'withdraw']:
        with pytest.raises(SystemExit):
            main([command])
    with pytest.raises(SystemExit):
        main(['discover', '--output', str(tmp_path)])


def test_pagination_rejects_stuck_and_outside(tmp_path):
    class Stuck:
        def get(self, *args, **kwargs):
            return [kline(BASE)]
    with pytest.raises(DataQualityError, match='advance'):
        paginate(Stuck(), 'klines', symbol='NVDAUSDT', start=BASE, end=BASE + DAY, archive=tmp_path)
    class Outside:
        def get(self, *args, **kwargs):
            return [kline(BASE - DAY)]
    with pytest.raises(DataQualityError, match='out-of-range'):
        paginate(Outside(), 'klines', symbol='NVDAUSDT', start=BASE, end=BASE + DAY, archive=tmp_path)


def test_full_funding_page_overlaps_boundary_type(tmp_path):
    regular = [funding_row(BASE + n) for n in range(1000)]
    special = funding_row(BASE + 999, 'Special')
    pages = iter([regular, [regular[-1], special], []])
    calls = []
    class Pages:
        def get(self, endpoint, params, **kwargs):
            calls.append(params)
            return next(pages)
    rows = paginate(Pages(), 'fundingRate', symbol='NVDAUSDT', start=BASE,
                    end=BASE + DAY, archive=tmp_path)
    assert calls[1]['startTime'] == BASE + 999
    parsed = funding(rows, 'NVDAUSDT', BASE, BASE + DAY)
    assert len(parsed) == 1001
    assert parsed.rate_type.iloc[-2:].tolist() == ['Regular', 'Special']


def test_mapping_requires_actual_pinned_bytes(tmp_path):
    from factorlab.data.evidence import sha256
    evidence = b'Offline fixture: official statement explicitly mapping contract to equity'
    mapping = dict(symbol='NVDAUSDT', security_id='US:XNAS:NVDA', name='NVIDIA Corporation',
                   evidence_url='https://www.binance.com/en/support/announcement/fixture',
                   evidence_sha256=sha256(evidence))
    with pytest.raises(ValueError, match='evidence bytes'):
        collect(collector(FixtureExchange()), tmp_path, underlying_mapping=mapping)
    reader = collect(collector(FixtureExchange()), tmp_path, underlying_mapping=mapping,
                     underlying_evidence=evidence)
    assert reader.manifest['contract']['underlying'] == mapping
    reader.verify_raw()
    path = reader.root / reader.manifest['contract']['underlying_evidence_path']
    path.write_bytes(b'changed')
    with pytest.raises(DataQualityError, match='mapping evidence'):
        reader.verify_raw()


def test_numeric_values_are_not_silently_coerced():
    invalid = kline(BASE)
    invalid[4] = 'NaN'
    with pytest.raises(DataQualityError, match='nonfinite'):
        bars([invalid], BASE, BASE + DAY, DAY, BASE + DAY)
    invalid = kline(BASE)
    invalid[4] = '190.00'
    with pytest.raises(DataQualityError, match='OHLC'):
        bars([invalid], BASE, BASE + DAY, DAY, BASE + DAY)


def test_incremental_reconciles_late_special_and_nontrading_contract(tmp_path):
    exchange = FixtureExchange()
    first = collect(collector(exchange), tmp_path)
    assert len(first.read('funding').data) == 2
    exchange.funding_rows.append(funding_row(BASE + 1, 'Special'))
    exchange.funding_rows.sort(key=lambda row: row['fundingTime'])
    exchange.contract['status'] = 'SETTLING'
    assert collector(exchange).discover() == []
    second = collect(collector(exchange), tmp_path, update=True)
    assert len(second.read('funding').data) == 3
    assert second.manifest['contract']['status'] == 'SETTLING'
    assert first.manifest['contract']['status'] == 'TRADING'


def test_collection_state_commit_interruption_is_resumable(tmp_path, monkeypatch):
    from factorlab.integrations.binance import store
    def interrupt(*_):
        raise OSError('fixture interrupted before state commit')
    with monkeypatch.context() as context:
        context.setattr(store.os, 'replace', interrupt)
        with pytest.raises(OSError, match='interrupted'):
            collect(collector(FixtureExchange()), tmp_path)
    assert not list(tmp_path.glob('pending-*/collection.json'))
    reader = collect(collector(FixtureExchange()), tmp_path)
    assert reader.manifest['integrity']['complete']


def test_collect_pre_onboard_empty_tables(tmp_path):
    reader = collector(FixtureExchange()).collect(symbol='NVDAUSDT', start='2026-10-30',
            end='2026-10-31', output=tmp_path)
    assert reader.read('bars').data.empty
    assert str(reader.read('bars').data.open_time.dtype) == 'datetime64[ns, UTC]'
    assert reader.read('funding').data.empty
    assert reader.manifest['integrity']['expected_closed_bars'] == 0
    assert reader.manifest['integrity']['complete']


def test_replay_cli_without_transport(tmp_path, capsys):
    collect(collector(FixtureExchange()), tmp_path)
    assert main(['replay', '--output', str(tmp_path), '--table', 'funding']) == 0
    assert 'Special' in capsys.readouterr().out
