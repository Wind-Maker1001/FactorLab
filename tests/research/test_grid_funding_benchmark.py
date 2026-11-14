from datetime import timedelta
from decimal import Decimal as D

from factorlab.experiments.grid_funding import (
    START, account_overlay, position_settlements, read_csv, settlements, write_csv,
)
from factorlab.experiments.grid_funding_benchmark import benchmark_trades


def test_actual_close_not_open_label(tmp_path):
    write_csv(tmp_path / 'fills.csv', [dict(execution_date=START.isoformat(),
              side='BUY', executed_shares='.12')])
    trades = benchmark_trades(tmp_path)
    close = START + timedelta(hours=1) - timedelta(microseconds=1)
    events = [(START + timedelta(milliseconds=1), D(100), D('.01'), 'Regular'),
              (close, D(100), D('.01'), 'Special'),
              (START + timedelta(hours=1, milliseconds=1), D(100), D('.01'), 'Regular')]
    flows = position_settlements(trades, events)
    assert [r['quantity'] for r in flows] == [0, 0, D('.12')]
    assert flows[1]['tie_alternative'] == D('-.12')
    assert flows[2]['funding'] == D('-.12')


def test_signs_types_and_accounts():
    time = START + timedelta(milliseconds=1)
    payload = dict(symbol='NVDAUSDT', fundingTime=int(START.timestamp()*1000)+1,
                   fundingRate='.01', markPrice='100', rateType='Regular')
    events = settlements([payload, {**payload, 'rateType': 'Special', 'fundingRate': '-.02'}])
    assert all(e[0] == time for e in events)
    buy = [dict(event_time=START.isoformat(), executed_shares='.2', side='BUY')]
    sell = [dict(event_time=START.isoformat(), executed_shares='.3', side='SELL')]
    assert sum(r['funding'] for r in position_settlements(buy, events)) == D('.2')
    assert sum(r['funding'] for r in position_settlements(sell, events)) == D('-.3')
    assert all(r['quantity'] == 0 for r in position_settlements([], events))
    assert sum(r['funding'] for r in position_settlements(buy, events)) == D('.2')


def test_overlay_nav_and_settlement_before_sell(tmp_path):
    end = START + timedelta(hours=1) - timedelta(microseconds=1)
    trades = [dict(event_time=START.isoformat(), executed_shares='2', side='BUY'),
              dict(event_time=end.isoformat(), executed_shares='1', side='SELL')]
    events = [(START+timedelta(milliseconds=1), D(100), D('.01'), 'Regular'),
              (end, D(100), D('.01'), 'Special')]
    flows = position_settlements(trades, events)
    assert [r['quantity'] for r in flows] == [D(2), D(2)]
    daily = [dict(date=START.isoformat(), inventory='1', equity='990')]
    result = account_overlay(trades, daily, flows, tmp_path / 'account')
    assert result['funding'] == -4
    assert result['research_nav'] == 986
    assert result['adjusted_hourly_drawdown_pct'] == D('1.4')
    assert result['tie_cashflow_difference'] == 1
    point = read_csv(tmp_path / 'account/nav.csv')[0]
    assert D(point['research_nav']) == D(point['original_nav']) + D(point['cumulative_funding'])
