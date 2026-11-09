"""Shared D06 computation and explicitly bounded historical evidence fixtures."""
from dataclasses import replace
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.data.evidence import sha256
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.diagnostics.research_readiness import research_readiness
from factorlab.diagnostics import research_coverage as shared
from factorlab.universe.filters import build_filter_flags, UniverseFilterConfig


def fixture():
    dates = pd.bdate_range('2021-01-04', '2023-04-14')
    pieces = []
    for number, symbol in enumerate(['600000.SH', '000001.SZ']):
        frame = pd.DataFrame(dict(date=dates, symbol=symbol))
        price = 10 + np.arange(len(dates)) / 100 + number
        for prefix in ('raw_', 'adj_'):
            for field in ('open', 'close', 'high', 'low'):
                frame[prefix + field] = price + {'open': 0, 'close': .1, 'high': .2, 'low': -.1}[field]
        frame['volume'], frame['amount'], frame['turnover'] = 1000., price * 1000, .02
        pieces.append(frame)
    bars = pd.concat(pieces, ignore_index=True)
    members = bars.loc[bars.date.between('2021-09-13', '2023-03-13'), ['date', 'symbol']].reset_index(drop=True)
    frames = dict(bars=bars, membership=members, calendar=pd.DataFrame(dict(date=dates)),
        benchmark=pd.DataFrame(dict(date=dates, open=100.)),
        status=bars[['date', 'symbol']].assign(trade_status=1, is_st=False),
        listings=pd.DataFrame(dict(symbol=['600000.SH', '000001.SZ'], listing_date=pd.Timestamp('2010-01-01'), delist_date=pd.NaT)))
    inputs = {kind: QueryResult(frame, SourceMetadata('SYNTHETIC', 'v1',
        acquired_at='2026-11-09T00:00:00+00:00', provenance={'source_kind': 'SYNTHETIC'}))
        for kind, frame in frames.items()}
    return inputs, dict(identities={kind: ('SYNTHETIC', 'v1') for kind in inputs},
        start='2021-01-04', end='2023-04-14', research_start='2021-09-13', research_end='2023-03-13')


def test_shared_d06_entrypoints_and_full_window_equivalence():
    spec = importlib.util.spec_from_file_location('d06_shared_contract', Path(__file__).parents[2] / 'tools/data/audit_p3_d06.py')
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    for name in ('factor_diagnostics', 'window_profiles', 'label_availability', 'market_quality', 'join_members', 'field_profile'):
        assert getattr(audit, name) is getattr(shared, name)
    inputs, options = fixture()
    report = research_readiness(inputs, **options)
    bars = inputs['bars'].data
    bars = bars.loc[bars.date.le(options['research_end'])]
    members = inputs['membership'].data
    calendar = pd.DatetimeIndex(inputs['calendar'].data.date)
    factor_calendar = calendar[calendar <= options['research_end']]
    factors, finite, snapshot, quality = audit.factor_diagnostics(bars, members, factor_calendar)
    assert report['factors'] == factors.to_dict('records')
    assert report['source_quality'] == quality
    assert report['catalog_snapshot_hash'] == snapshot
    labels = audit.label_availability(inputs['bars'].data, members, calendar, calendar)
    flags = build_filter_flags(bars.rename(columns={f'raw_{f}': f for f in shared.OHLC}),
        inputs['status'].data, inputs['listings'].data, factor_calendar, UniverseFilterConfig())
    labels['structural_eligible'] = members.merge(flags, on=shared.KEYS, how='left').eligible.eq(True).to_numpy()
    frame = finite.merge(labels, on=shared.KEYS).sort_values(shared.KEYS).reset_index(drop=True)
    assert report['windows'] == audit.window_profiles(frame, calendar[(calendar >= options['research_start']) & (calendar <= options['research_end'])], candidates=(('minimum_12_3_3', 12),))
    first = report['windows'][:3]
    assert [w['window_start'] for w in first] == ['2021-09-13', '2022-09-13', '2022-12-13']
    assert [w['window_end_exclusive'] for w in first] == ['2022-09-13', '2022-12-13', '2023-03-13']
    assert report['common78'] > 0
    assert report['historical_identity'] == 'UNKNOWN'
    assert report['formal_admission'] == 'FAIL'
    assert all(w['financial_admitted_days'] == 0 for w in report['windows'])


def test_future_outside_consumption_window_cannot_quarantine_history():
    inputs, options = fixture()
    baseline = research_readiness(inputs, **options)
    future = inputs['bars'].data.iloc[[0]].copy()
    future['date'], future['raw_high'] = pd.Timestamp('2024-01-01'), -1000.
    inputs['bars'] = replace(inputs['bars'], data=pd.concat([inputs['bars'].data, future], ignore_index=True))
    assert research_readiness(inputs, **options) == baseline


@pytest.mark.parametrize('change', ['source', 'version', 'content'])
def test_input_identity_rejected(change):
    inputs, options = fixture()
    metadata = inputs['bars'].metadata
    if change == 'source':
        metadata = replace(metadata, source_id='OTHER')
    elif change == 'version':
        metadata = replace(metadata, data_version='v2')
    else:
        metadata = replace(metadata, provenance={'table_sha256': '0' * 64})
    inputs['bars'] = replace(inputs['bars'], metadata=metadata)
    with pytest.raises(ValueError, match='identity'):
        research_readiness(inputs, **options)


def test_missing_membership_and_export_time_never_infer_pit():
    inputs, options = fixture()
    del inputs['membership']
    del options['identities']['membership']
    report = research_readiness(inputs, **options)
    assert report['engineering_numerical'] == 'UNKNOWN'
    assert report['historical_identity'] == 'UNKNOWN'
    assert report['bindings']['bars']['known_at'] == 'UNKNOWN'
    assert report['windows'] == []


def test_missing_and_invalid_values_remain_and_boundaries_are_not_vendor_omissions():
    inputs, options = fixture()
    bars = inputs['bars'].data.copy()
    target = bars.loc[bars.date.eq(pd.Timestamp('2021-09-13')) & bars.symbol.eq('600000.SH')].index[0]
    bars.loc[target + 1, 'raw_close'] = np.nan
    bars.loc[target + 2, 'volume'] = -1
    bars = bars.drop(target)
    inputs['bars'] = replace(inputs['bars'], data=bars)
    listings = inputs['listings'].data.copy()
    listings.loc[0, 'listing_date'] = pd.Timestamp('2021-09-14')
    inputs['listings'] = replace(inputs['listings'], data=listings)
    before = bars.copy(deep=True)
    report = research_readiness(inputs, **options)
    assert report['missing_rows']['by_reason']['PRE_LISTING_OBSERVED_BOUNDARY'] == 1
    assert report['missing_rows']['supplier_omission'] == 'UNKNOWN'
    assert report['source_quality']['quarantined_securities'] == 1
    assert report['engineering_numerical'] == 'FAIL'
    assert next(f for f in report['raw_fields'] if f['field'] == 'raw_close')['null_on_present_keys'] == 1
    pd.testing.assert_frame_equal(inputs['bars'].data, before)


def test_missing_adjusted_fields_are_not_replaced_by_raw():
    inputs, options = fixture()
    inputs['bars'] = replace(inputs['bars'], data=inputs['bars'].data.drop(columns='adj_close'))
    report = research_readiness(inputs, **options)
    assert report['missing_factor_fields'] == ['adj_close']
    assert report['common78'] == 'UNKNOWN'
    assert report['formal_admission'] == 'FAIL'


def test_batch_calls_shared_diagnostics_without_cross_source_fusion():
    from factorlab.data.hub import DataHub
    from factorlab.diagnostics.candidate_preflight import CandidateQuery
    from factorlab.diagnostics.real_data_readiness import batch_readiness
    inputs, options = fixture()

    class Adapter:
        capabilities = frozenset({'daily_bars'})

        def read(self, kind, *, symbol, start, end, **kwargs):
            frame = inputs['bars'].data
            return replace(inputs['bars'], data=frame.loc[frame.symbol.eq(symbol) & frame.date.between(start, end)].copy())

    queries = tuple(CandidateQuery('vendor', 'v1', symbol, options['start'], options['end'],
        options['research_start'], options['research_end']) for symbol in ['600000.SH', '000001.SZ'])
    result = batch_readiness(DataHub({'vendor': Adapter()}), queries,
        calendar=inputs['calendar'].data.date, calendar_id='FIXTURE', calendar_kind='SYNTHETIC', units={},
        research_inputs={'vendor': dict(inputs=inputs, identities=options['identities'])})
    assert result['research_diagnostics']['vendor'] == research_readiness(inputs, **options)
    assert result['comparisons'] == []
    assert all(source['gates']['FORMAL']['status'] == 'FAIL' for source in result['sources'])


@pytest.mark.parametrize('corrupt', [False, True])
def test_controlled_archive_adapter_and_receipt_rejection(tmp_path, monkeypatch, corrupt):
    import json
    import zipfile
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / 'tools/data'))
    from d06_readiness_adapter import load_research
    inputs, options = fixture()
    bars = inputs['bars'].data
    native = bars.rename(columns={'symbol': 'instrument', 'turnover': 'turn', **{'raw_' + f: f for f in shared.OHLC}})
    tables = dict(stock={
        'cn_stock_real_bar1d': native[['date', 'instrument', *shared.OHLC, 'volume', 'amount', 'turn']],
        'cn_stock_bar1d': bars[['date', 'symbol', *['adj_' + f for f in shared.OHLC]]].rename(columns={'symbol': 'instrument', **{'adj_' + f: f for f in shared.OHLC}}),
        'cn_stock_status': bars[['date', 'symbol']].rename(columns={'symbol': 'instrument'}).assign(st_status=0, is_risk_warning=0, suspended=0, price_limit_status=0, exdr=0),
        'cn_stock_basic_info': inputs['listings'].data.rename(columns={'symbol': 'instrument', 'listing_date': 'list_date'})},
        common={'trading_days': inputs['calendar'].data.assign(market_code='CN')},
        index={'cn_stock_index_bar1d': inputs['benchmark'].data.assign(instrument='000905.SH')})
    for category, entries in tables.items():
        with zipfile.ZipFile(tmp_path / (category + '.zip'), 'w') as archive:
            for table, frame in entries.items():
                payload = frame.to_parquet(index=False)
                name = f'{category}/{table}/2021-01-01_2023-04-30.parquet'
                archive.writestr(name, payload)
                archive.writestr(name + '.receipt.json', json.dumps(dict(sha256='0' * 64 if corrupt else sha256(payload), exported_at_utc='2026-11-09T00:00:00Z')))
    membership = tmp_path / 'membership.zip'
    with zipfile.ZipFile(membership, 'w') as archive:
        archive.writestr('members_daily.csv', inputs['membership'].data.rename(columns={'symbol': 'stock_code'}).to_csv(index=False))
        archive.writestr('manifest.json', '{}')
    if corrupt:
        with pytest.raises(ValueError, match='differs from receipt'):
            load_research(tmp_path, membership, symbols=['600000.SH', '000001.SZ'], start=options['start'], end=options['end'])
    else:
        adapter, loaded = load_research(tmp_path, membership, symbols=['600000.SH', '000001.SZ'], start=options['start'], end=options['end'])
        assert len(adapter.frame) == len(bars)
        assert loaded['inputs']['membership'].metadata.source_id == 'JoinQuant_OBSERVED_MEMBERSHIP'
        assert loaded['inputs']['bars'].metadata.provider_available_at is None
        assert set(loaded['inputs']) == {'bars', 'membership', 'calendar', 'benchmark', 'status', 'listings'}
        import subprocess
        import sys
        output = tmp_path / 'integrated.json'
        command = [sys.executable, '-I', '-B', '-X', 'utf8',
            str(Path(__file__).parents[2] / 'tools/data/audit_p3_d07.py'),
            '--bigquant-root', str(tmp_path), '--membership-zip', str(membership),
            '--symbols', '600000.SH', '000001.SZ', '--output', str(output)]
        for key in ('start', 'end', 'research_start', 'research_end'):
            command.extend(['--' + key.replace('_', '-'), options[key]])
        executed = subprocess.run(command, capture_output=True, text=True)
        assert executed.returncode == 0, executed.stdout + executed.stderr
        report = json.loads(output.read_text(encoding='utf-8'))
        assert report['research_diagnostics']['bigquant']['common78'] > 0
        assert report['upstream_d06']['integration'] == 'MERGED_MAIN_RECEIPT'
        assert report['upstream_d06']['admitted_member_days'] == 0
