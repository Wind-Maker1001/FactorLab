"""Boundary and attribution tests for frozen weekend research."""
import math
import json

import pandas as pd
import pytest

from factorlab.experiments.grid_weekend import account, binary_model, describe, labels, market, weekend_key


def test_dst_opening_and_holiday():
    times=pd.to_datetime(['2026-10-30T13:00Z','2026-10-30T14:00Z',
                          '2026-11-02T14:00Z','2026-11-02T15:00Z',
                          '2026-07-03T14:00Z','2026-07-04T02:00Z'],utc=True)
    result=labels(times)
    assert result.us_session.tolist()==['US_open_mixed_hour','US_regular_full_hour',
        'US_open_mixed_hour','US_regular_full_hour','US_holiday_closed','US_holiday_closed']
    assert result.utc_day.iloc[-1]=='Saturday'


def test_weekend_is_saturday_anchor():
    times=pd.to_datetime(['2026-08-08T00:00Z','2026-08-09T23:00Z'],utc=True)
    assert list(weekend_key(times))==[pd.Timestamp('2026-08-08',tz='UTC')]*2


def test_binary_arithmetic_and_compounding_are_distinct():
    model=binary_model()
    assert model['arithmetic_break_even_p']==pytest.approx(14/15)
    p=model['geometric_break_even_p']
    assert p>model['arithmetic_break_even_p']
    assert p*math.log(1.05)+(1-p)*math.log(.3)==pytest.approx(0,abs=1e-14)
    assert binary_model(.001)['geometric_break_even_p']>p


def test_market_tail_and_complete_weekend():
    times=pd.date_range('2026-08-07T23:00Z',periods=49,freq='h')
    bars=[dict(time=t,close=100.,high=101.,low=98.) for t in times]
    summary,daily,weekends=market(bars)
    assert len(weekends)==1
    assert weekends.complete.iloc[0]
    assert weekends.max_adverse_long.iloc[0]==pytest.approx(-.02)
    assert weekends.close_return.iloc[0]==0
    assert summary.return_observations.sum()==2*48


def test_continuation_does_not_join_disjoint_sessions():
    frame=pd.DataFrame(dict(time=pd.to_datetime(['2026-08-07T20:00Z','2026-08-10T20:00Z']),
        log_return=[.01,.01],range_fraction=[.02,.02]))
    assert math.isnan(describe(frame)['continuation'])


def test_cross_weekend_fifo_and_funding_are_not_double_counted(tmp_path):
    source=tmp_path/'account'
    funding=tmp_path/'funding'
    source.mkdir()
    funding.mkdir()
    times=['2026-08-07T23:00Z','2026-08-08T00:00Z','2026-08-08T01:00Z']
    pd.DataFrame(dict(date=times,equity=[999.9,1001.9,1002.8],inventory=[1,1,0],
        close=[100,102,103],fee=[.1,0,.1],slippage=[0,0,0],
        unrealized=[0,2,0],state=['RUNNING']*3)).to_csv(source/'daily.csv',index=False)
    pd.DataFrame(dict(event_id=['b','s'],event_time=['2026-08-07T23:59:59Z','2026-08-08T01:59:59Z'])).to_csv(source/'trades.csv',index=False)
    (source/'fifo.json').write_text(json.dumps(dict(lots=[dict(event_id='b',completed_grid_cycle=True)],
        matches=[dict(buy_event_id='b',sell_event_id='s',purpose='GRID',net='2.8')],summary={})))
    pd.DataFrame(dict(time=['2026-08-08T00:00:00.001Z','2026-08-08T01:00:00Z'],
        funding=[-.2,-.3])).to_csv(funding/'settlements.csv',index=False)
    hourly,matches,_=account(source,funding)
    assert hourly.pnl.sum()==pytest.approx(2.8)
    assert hourly.adjusted_pnl.sum()==pytest.approx(2.3)
    assert hourly.closed_net_at_sale.sum()==pytest.approx(2.8)
    assert not matches.buy_utc_weekend.iloc[0]
    assert matches.sell_utc_weekend.iloc[0]
    assert matches.spans_weekend.iloc[0]
    assert (hourly.price_carry-hourly.fee-hourly.slippage+hourly.execution_reference).sum()==pytest.approx(2.8)
