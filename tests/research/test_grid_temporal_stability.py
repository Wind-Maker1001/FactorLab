from datetime import timedelta
from decimal import Decimal as D
import json
import sqlite3

import pandas as pd
import pytest

from factorlab.experiments.grid_temporal_stability import (
    START, END, load_inputs, control_funding, observed_times,
)
from factorlab.experiments.grid_exposure_benchmark import simulate
from factorlab.experiments.grid_funding import analyze_account, settlements


def bars(prices):
    return [dict(time=(START+pd.Timedelta(hours=i)).isoformat(), close=p, volume=100)
            for i,p in enumerate(prices)]


def test_lagged_prefix_and_completed_hour_funding(tmp_path):
    path = bars([200,200,200,200])
    nav, fills = simulate(path, targets=[D('.12')]*4)
    assert nav.inventory.iloc[0] == 0
    assert fills.execution_date.iloc[0] == path[1]['time']
    altered, altered_fills = simulate(path[:3]+[{**path[3], 'close':900}], targets=[D('.12')]*3+[D('.8')])
    pd.testing.assert_frame_equal(nav.iloc[:3], altered.iloc[:3])
    pd.testing.assert_frame_equal(fills.iloc[:1], altered_fills.iloc[:1])
    time = observed_times(fills.execution_date).iloc[0].to_pydatetime()
    events = [(time,D(200),D('.01'),'Regular'),
              (time+timedelta(microseconds=1),D(200),D('.01'),'Special')]
    result = control_funding(nav, fills, events, tmp_path/'funding')
    assert result['funding'] == pytest.approx(-.24)
    assert result['adjusted_profit'] == pytest.approx(nav.equity.iloc[-1]-1000-.24)


def test_new_window_residual_funding_not_old_end(tmp_path):
    source = tmp_path/'account'
    source.mkdir()
    time = START.to_pydatetime()+timedelta(hours=1)-timedelta(microseconds=1)
    trade = dict(event_id='buy',order_id='order',event_time=time.isoformat(),
                 cumulative_filled_quantity='.1', executed_shares='.1',side='BUY')
    pd.DataFrame([trade]).to_csv(source/'trades.csv',index=False)
    pd.DataFrame([dict(date=START.isoformat(), inventory='.1', equity='1000'),
                  dict(date=(START+pd.Timedelta(hours=1)).isoformat(),inventory='.1',equity='1000')]).to_csv(source/'daily.csv',index=False)
    (source/'fifo.json').write_text(json.dumps(dict(lots=[dict(event_id='buy',remaining='.1')],matches=[])))
    with sqlite3.connect(source/'paper.sqlite') as db:
        db.execute('CREATE TABLE events (id TEXT, payload TEXT)')
        fields = dict(cumulative_filled_quantity={'decimal':'.1'},client_order_id='order',
                      event_time={'utc':time.isoformat()})
        db.execute('INSERT INTO events VALUES (?,?)',('buy',json.dumps(dict(fields=fields))))
    events = [(START.to_pydatetime(),D(200),D('.01'),'Regular'),
              (time+timedelta(microseconds=1),D(200),D('.01'),'Special')]
    result = analyze_account(source,events,tmp_path/'overlay',end=END.to_pydatetime())
    assert result['funding'] == D('-.2')
    assert result['funding_residual'] == D('-.2')
    assert result['adjusted_profit'] == D('-.2')


def test_new_window_event_boundaries():
    row = dict(symbol='NVDAUSDT',fundingTime=START.value//1000000,
               fundingRate='.001',markPrice='200',rateType='Special')
    assert settlements([row],START.to_pydatetime(),END.to_pydatetime())
    with pytest.raises(ValueError,match='duplicate'):
        settlements([row,row],START.to_pydatetime(),END.to_pydatetime())
    with pytest.raises(ValueError,match='outside'):
        settlements([{**row,'fundingTime':END.value//1000000}],START.to_pydatetime(),END.to_pydatetime())


def test_fixed_window_rejects_other_complete_sample(monkeypatch):
    import factorlab.experiments.grid_temporal_stability as study
    monkeypatch.setattr(study,'load_hourly',lambda _: ([{}]*2160,
        dict(start='2026-05-14T00:00:00Z',end_exclusive=START.isoformat())))
    with pytest.raises(ValueError,match='fixed complete'):
        load_inputs('unused','unused')


@pytest.mark.parametrize('rate,mark', [('NaN','200'), ('.001','0')])
def test_invalid_funding_projection_rejected(monkeypatch, rate, mark):
    import factorlab.experiments.grid_temporal_stability as study
    monkeypatch.setattr(study,'load_hourly',lambda _: ([{}]*2160,
        dict(start=START.isoformat(),end_exclusive=END.isoformat())))
    monkeypatch.setattr(study.pd,'read_parquet',lambda _: pd.DataFrame([
        dict(funding_time=START,funding_rate=rate,mark_price=mark,rate_type='Regular')]))
    with pytest.raises(ValueError,match='invalid funding'):
        load_inputs('unused','unused')
