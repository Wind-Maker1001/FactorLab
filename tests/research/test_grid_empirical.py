"""Snapshot quality and timestamp-matched funding attribution contracts."""
import hashlib
import json

import pandas as pd
import pytest

from factorlab.experiments.grid_empirical import funding_sensitivity, load_hourly


def snapshot(tmp_path, *, gap=False, duplicate=False, bad_ohlc=False):
    times = pd.date_range('2026-08-12', periods=3, freq='h', tz='UTC')
    frame = pd.DataFrame(dict(open_time=times,
        close_time=times+pd.Timedelta(hours=1)-pd.Timedelta(milliseconds=1),
        open=['100']*3, high=['102']*3, low=['98']*3, close=['101']*3, volume=['10.25']*3))
    if gap:
        frame = frame.drop(1)
    if duplicate:
        frame.loc[1, 'open_time'] = times[0]
    if bad_ohlc:
        frame.loc[1, 'close'] = '200'
    path = tmp_path/'bars.parquet'
    frame.to_parquet(path, index=False)
    manifest = dict(interval='1h', integrity={'complete':True}, data_version='fixture',
        acquired_at='2026-08-13T00:00:00Z', contract={'instrument_id':'BINANCE:USDM:NVDAUSDT'},
        range={'start_ms':times[0].value//1000000,
               'end_ms':(times[-1]+pd.Timedelta(hours=1)).value//1000000},
        tables={'bars':{'path':'bars.parquet', 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}},
        raw_receipts=[])
    (tmp_path/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return tmp_path


@pytest.mark.parametrize('failure', ['gap', 'duplicate', 'bad_ohlc'])
def test_snapshot_rejects_quality_failures(tmp_path, failure):
    with pytest.raises(ValueError):
        load_hourly(snapshot(tmp_path, **{failure:True}))


def test_snapshot_projection_preserves_real_times(tmp_path):
    bars, identity = load_hourly(snapshot(tmp_path))
    assert len(bars) == 3
    assert bars[0]['volume'] == 10
    assert identity['missing_bars'] == 0
    assert identity['end_exclusive'] == '2026-08-12T03:00:00+00:00'


def test_funding_uses_position_after_last_simulated_close(tmp_path):
    pd.DataFrame({'date':['2026-08-12T00:00:00Z','2026-08-12T01:00:00Z'],
                  'inventory':[2, 5]}).to_csv(tmp_path/'daily.csv', index=False)
    funding = pd.DataFrame(dict(
        funding_time=pd.to_datetime(['2026-08-12T00:30:00Z', '2026-08-12T01:00:00Z',
                                     '2026-08-12T02:00:00Z'], utc=True),
        funding_rate=['0.001']*3, mark_price=['100']*3,
        rate_type=['Regular','Regular','Special']))
    result = funding_sensitivity(tmp_path, funding)
    assert result['regular_linear_cost'] == pytest.approx(.2)
    assert result['special_linear_cost'] == pytest.approx(.5)
