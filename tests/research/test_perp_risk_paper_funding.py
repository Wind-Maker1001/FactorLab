"""Funding admission must use complete exports and reject same-time fills."""
import csv
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from factorlab.experiments.perp_risk import START, paper_frames, synthetic_rule
from factorlab.research.perp_risk import D, Frame, Funding, evaluate


def write_csv(path, rows, fields=None):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def export(directory, initial='0', fills=(('BUY', '2'),)):
    inventory = D(initial)
    trades, orders = [], {}
    fee = D(0)
    time = START+timedelta(hours=1, microseconds=-1)
    for i, (side, quantity) in enumerate(fills):
        quantity = D(quantity)
        inventory += quantity if side == 'BUY' else -quantity
        cost = quantity*D('.1')
        fee += cost
        key = f'order-{i}'
        orders[key] = dict(intent=dict(client_order_id=key, instrument_id='SYNTHUSDT',
            side=side, quantity=str(quantity)), cumulative_filled_quantity=str(quantity),
            cumulative_fee=str(cost))
        trades.append(dict(execution_date=START.isoformat(), event_time=time.isoformat(),
            order_id=key, event_id=f'fill-{i}', side=side,
            executed_shares=str(quantity), fee=str(cost),
            cumulative_filled_quantity=str(quantity), cumulative_fee=str(cost)))
    daily = [dict(date=(START+timedelta(hours=i)).isoformat(), close='100',
        inventory=str(inventory), unrealized='0', equity=str(1000-fee),
        fee=str(fee if i == 0 else 0)) for i in range(2)]
    write_csv(directory/'daily.csv', daily)
    write_csv(directory/'trades.csv', trades, fields=['execution_date', 'event_time', 'order_id',
        'event_id', 'side', 'executed_shares', 'fee', 'cumulative_filled_quantity', 'cumulative_fee'])
    result = dict(execution=dict(interval='1h'), instrument_id='SYNTHUSDT', funding='NOT_SIMULATED',
        fills=len(trades), orders=orders, config=dict(initial_inventory=initial),
        rules=dict(quantity_unit='1'), final=dict(inventory=str(inventory), equity=str(1000-fee)))
    (directory/'result.json').write_text(json.dumps(result), encoding='utf-8')
    (directory/'manifest.json').write_text(json.dumps(dict(window=dict(
        start=daily[0]['date'], end=daily[-1]['date'], bars=2))), encoding='utf-8')
    return time


def bridge(directory):
    times = [START+timedelta(hours=i+1, microseconds=-1) for i in range(2)]
    return paper_frames(directory, symbol='SYNTHUSDT', mark_prices=dict.fromkeys(times, D(100)))


def settle(frames, index=0):
    rule = synthetic_rule()
    event = Funding('funding', frames[index].time, rule.symbol, D('.01'), D(100),
        'ASSUMED_SETTLEMENT', 'fixture')
    return evaluate(frames=frames, rules={rule.symbol: rule}, funding=[event], mode='CROSS',
        budget=D(1000), order_margin=D(0), portfolio_complete=True, equity_basis='PAPER_NAV')


@pytest.mark.parametrize('initial,fills', [
    ('0', (('BUY', '2'),)),
    ('2', (('SELL', '2'),)),
    ('2', (('BUY', '1'),)),
    ('2', (('SELL', '1'),)),
    ('2', (('SELL', '1'), ('BUY', '1'))),
])
def test_same_time_open_close_partial_and_multiple_fills_rejected(tmp_path, initial, fills):
    export(tmp_path, initial, fills)
    originals = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    frames = bridge(tmp_path)
    with pytest.raises(ValueError, match='same-time fill'):
        settle(frames)
    # A neighboring completed bar can settle on the unchanged post-fill inventory.
    result = settle(frames, 1)
    quantity = D(initial)+sum((D(q) if side == 'BUY' else -D(q) for side, q in fills), D(0))
    row = result['rows'][-1]
    assert row['funding'] == -quantity
    assert row['equity'] == frames[-1].original_equity-quantity
    assert row['initial_margin'] == abs(quantity)*10
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == originals


def test_no_fill_normal_settlement_and_costs_not_replayed(tmp_path):
    export(tmp_path, '2', ())
    result = settle(bridge(tmp_path))
    assert result['rows'][0]['equity'] == 998
    assert result['settlements'][0]['quantity'] == 2
    export(tmp_path)
    result = settle(bridge(tmp_path), 1)
    assert result['rows'][-1]['original_equity'] == D('999.8')
    assert result['rows'][-1]['equity'] == D('997.8')
    assert result['rule_status'] == 'SCENARIO_ASSUMPTION_ONLY'
    assert result['authoritative_liquidation'] == 'BLOCKED_RULE_EVIDENCE'
    assert result['safety'] == 'NO LIVE MONEY'


@pytest.mark.parametrize('offset', [-1, 1])
def test_funding_time_comparison_is_exact_to_microsecond(tmp_path, offset):
    export(tmp_path)
    frame = bridge(tmp_path)[0]
    frame = replace(frame, time=frame.time+timedelta(microseconds=offset))
    assert settle([frame])['rows'][0]['funding'] == -2


@pytest.mark.parametrize('missing', ['trades.csv', 'result.json', 'manifest.json'])
def test_missing_export_evidence_cannot_bypass_evaluate(tmp_path, missing):
    export(tmp_path)
    (tmp_path/missing).unlink()
    with pytest.raises(ValueError, match='complete validated execution history'):
        settle(bridge(tmp_path))


def test_bare_paper_frame_cannot_bypass_history_requirement():
    rule = synthetic_rule()
    frame = Frame(START, (rule.position(D(2), D(100), D(100)),), D(1000))
    with pytest.raises(ValueError, match='complete validated execution history'):
        settle([frame])


@pytest.mark.parametrize('mutation', [
    'missing_trade', 'duplicate_event', 'wrong_order', 'wrong_side', 'wrong_quantity',
    'wrong_cumulative', 'wrong_fee', 'wrong_inventory', 'naive_time', 'non_utc',
    'sub_microsecond', 'wrong_bar', 'truncated_daily',
])
def test_inconsistent_or_imprecise_history_rejected(tmp_path, mutation):
    export(tmp_path, '0', (('BUY', '1'), ('BUY', '1')))
    with (tmp_path/'trades.csv').open(newline='') as stream:
        trades = list(csv.DictReader(stream))
    with (tmp_path/'daily.csv').open(newline='') as stream:
        daily = list(csv.DictReader(stream))
    if mutation == 'missing_trade':
        trades.pop()
    elif mutation == 'duplicate_event':
        trades[1]['event_id'] = trades[0]['event_id']
    elif mutation == 'wrong_order':
        trades[0]['order_id'] = 'unknown'
    elif mutation == 'wrong_side':
        trades[0]['side'] = 'SELL'
    elif mutation == 'wrong_quantity':
        trades[0]['executed_shares'] = '0.5'
    elif mutation == 'wrong_cumulative':
        trades[0]['cumulative_filled_quantity'] = '0.5'
    elif mutation == 'wrong_fee':
        trades[0]['cumulative_fee'] = '0'
    elif mutation == 'wrong_inventory':
        daily[0]['inventory'] = '2.0000000000000001'
    elif mutation == 'naive_time':
        trades[0]['event_time'] = trades[0]['event_time'].removesuffix('+00:00')
    elif mutation == 'non_utc':
        trades[0]['event_time'] = trades[0]['event_time'].replace('+00:00', '+01:00')
    elif mutation == 'sub_microsecond':
        trades[0]['event_time'] = trades[0]['event_time'].replace('.999999', '.9999991')
    elif mutation == 'wrong_bar':
        trades[0]['execution_date'] = daily[1]['date']
    else:
        daily.pop()
    write_csv(tmp_path/'trades.csv', trades)
    write_csv(tmp_path/'daily.csv', daily)
    with pytest.raises(ValueError):
        settle(bridge(tmp_path))


@pytest.mark.parametrize('file', ['daily.csv', 'trades.csv', 'result.json', 'manifest.json'])
def test_conflicting_export_fields_rejected(tmp_path, file):
    export(tmp_path)
    path = tmp_path/file
    text = path.read_text(encoding='utf-8')
    if file.endswith('.json'):
        text = '{"duplicate": 1, "duplicate": 2, '+text[1:]
    else:
        rows = text.splitlines()
        rows[0] += ','+rows[0].split(',')[0]
        text = '\n'.join(rows)+'\n'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError, match='duplicate|unambiguous'):
        settle(bridge(tmp_path))


@pytest.mark.parametrize('omit_first', [False, True])
def test_multiple_partial_fills_of_one_order_require_full_cumulative_history(tmp_path, omit_first):
    export(tmp_path, '0', (('BUY', '1'), ('BUY', '1')))
    with (tmp_path/'trades.csv').open(newline='') as stream:
        trades = list(csv.DictReader(stream))
    trades[1].update(order_id='order-0', cumulative_filled_quantity='2', cumulative_fee='.2')
    result = json.loads((tmp_path/'result.json').read_text())
    result['orders'].pop('order-1')
    result['orders']['order-0'].update(cumulative_filled_quantity='2', cumulative_fee='.2')
    result['orders']['order-0']['intent']['quantity'] = '2'
    if omit_first:
        trades.pop(0)
        result['fills'] = 1
    write_csv(tmp_path/'trades.csv', trades)
    (tmp_path/'result.json').write_text(json.dumps(result), encoding='utf-8')
    if omit_first:
        with pytest.raises(ValueError, match='cumulative execution history'):
            bridge(tmp_path)
    else:
        frames = bridge(tmp_path)
        with pytest.raises(ValueError, match='same-time fill'):
            settle(frames)
        assert settle(frames, 1)['rows'][-1]['equity'] == D('997.8')


def test_actual_hourly_paper_export(tmp_path):
    from factorlab.experiments.grid_paper import run_experiment
    from factorlab.strategies.grid import GridConfig
    from factorlab.strategies.grid_execution import GridRules
    from factorlab.trading.contracts import Market
    bars = [dict(id=f'bar-{i}', time=(START+timedelta(hours=i)).isoformat(), price=p,
                 completed_bar=True, open=p, high=p+2, low=p-2, close=p, volume=100)
            for i, p in enumerate([100, 90, 100, 110, 100])]
    directory = tmp_path/'actual'
    run_experiment(directory, bars=bars,
        config=GridConfig(lower=80, upper=120, levels=4, initial_cash=1000,
            initial_inventory=0, initial_price=100, quantity=1, atr_window=2,
            max_inventory=100, max_cash_committed=1000, max_drawdown=1,
            fee_bps=0, slippage_bps=0, breakout='pause'),
        rules=GridRules(D(1), D('.1'), D('.1'), D('.1'), D(1)),
        capital=1000, fee_rate=D('.001'), slippage=0, participation=1,
        interval='1h', market=Market.BINANCE_USDM, instrument_id='SYNTHUSDT')
    originals = {name: (directory/name).read_bytes() for name in
                 ('daily.csv', 'trades.csv', 'result.json', 'manifest.json', 'paper.sqlite')}
    frames = paper_frames(directory, symbol='SYNTHUSDT', mark_prices={
        START+timedelta(hours=i+1, microseconds=-1): D(p)
        for i, p in enumerate([100, 90, 100, 110, 100])})
    assert frames[0].paper_fill_times
    for i, frame in enumerate(frames):
        if frame.time in frame.paper_fill_times:
            with pytest.raises(ValueError, match='same-time fill'):
                settle(frames, i)
        else:
            row = settle(frames, i)['rows'][i]
            assert row['equity'] == frame.original_equity-frame.positions[0].quantity
    assert {name: (directory/name).read_bytes() for name in originals} == originals
