"""Targeted temporal and metric contracts on synthetic bars."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np

from factorlab.research.binance_model_study import dataset, partitions, quality, risk_summary, ScoreStrategy, ledger_summary
from factorlab.trading.strategy import ClosedBar
from factorlab.trading.strategy_replay import SOURCE


def bars(count=2160):
    origin = datetime(2026, 8, 12, tzinfo=timezone.utc)
    result = []
    for i in range(count):
        price = Decimal(100) + Decimal(i)/10
        result.append(ClosedBar('NVDAUSDT', origin+timedelta(hours=i),
            origin+timedelta(hours=i+1), price, price+1, price-1, price,
            Decimal(10), 3600, SOURCE))
    return result


def test_temporal_split_and_cross_boundary_purge():
    frame = dataset(bars())
    split = partitions(frame)
    assert [len(split[k]) for k in ('train','validation','test')] == [1294,431,407]
    assert split['train'].exit_time.max() < split['validation'].signal_time.min()
    assert split['validation'].exit_time.max() < split['test'].signal_time.min()
    assert (frame.exit_time > frame.signal_time).all()


def test_features_are_prefix_causal_and_label_is_future_open():
    history = bars(90)
    short = dataset(history[:60])
    full = dataset(history)
    np.testing.assert_array_equal(short.return_1, full.return_1.iloc[:len(short)])
    row = full.iloc[0]
    i = int(row.bar_index)
    assert row.target == float(history[i+2].open/history[i+1].open-1)


def test_zero_prediction_has_zero_skill_not_claimed_directional_alpha():
    metrics = quality([-.01,.01], [0,0])
    assert metrics['mse_skill'] == 0
    assert metrics['correlation'] is None
    assert metrics['directional_accuracy'] == 0


def test_drawdown_includes_initial_equity():
    result = risk_summary({'equity_curve':[{'equity':'990'}, {'equity':'1000'}, {'equity':'980'}]})
    assert np.isclose(result['max_drawdown'], -.02)


def test_no_trade_and_momentum_use_causal_strategy_contract():
    history = bars(25)
    signal = ScoreStrategy('no_trade').signal(history, history[-1].closed_at)
    assert signal.direction == 'HOLD' and signal.target_weight is None
    momentum = ScoreStrategy('momentum').signal(history, history[-1].closed_at)
    assert momentum.direction == 'BUY'
    assert momentum.input_watermark == history[-1].closed_at
    assert ScoreStrategy('momentum').describe()['symbol'] == 'NVDAUSDT'


def test_turnover_uses_incremental_partial_fills(tmp_path):
    import sqlite3
    path = tmp_path/'paper.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE events(payload TEXT)')
    def event(quantity, price, time):
        return dict(client_order_id='one', cumulative_filled_quantity=quantity,
                    fill_price=price, event_time=time)
    replay = dict(order_records=[dict(snapshot=dict(intent=dict(client_order_id='one', side='BUY')))],
        fill_confirmations=[event('1','100','2026-01-01T00:00:00+00:00'),
                            event('2','101','2026-01-01T01:00:00+00:00')],
        equity_curve=[dict(time='2026-01-01T00:00:00.002+00:00', equity='1000'),
                      dict(time='2026-01-01T01:00:00.002+00:00', equity='1000')],
        account=dict(positions=[]))
    result = ledger_summary(path, replay)
    assert result['traded_notional'] == 201
    assert result['time_in_market'] == 1


def test_decimal_partial_fills_duplicates_and_multiple_orders_flatten_exactly(tmp_path):
    import sqlite3
    path = tmp_path/'paper.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE events(payload TEXT)')
    confirmations = [
        ('buy-a', '0.1', '100'),
        ('buy-a', '0.3', '101'),
        ('buy-a', '0.3', '101'),  # repeated cumulative confirmation adds nothing
        ('buy-b', '0.1', '102'),
        ('sell-a', '0.1', '103'),
        ('sell-a', '0.3', '104'),
        ('sell-b', '0.1', '105'),
        ('sell-b', '0.1', '105'),
    ]
    times = [f'2026-01-01T{i:02d}:00:00+00:00' for i in range(9)]
    replay = dict(
        order_records=[dict(snapshot=dict(intent=dict(client_order_id=key, side=side)))
                       for key, side in [('buy-a','BUY'), ('buy-b','BUY'),
                                         ('sell-a','SELL'), ('sell-b','SELL')]],
        fill_confirmations=[dict(client_order_id=key, cumulative_filled_quantity=quantity,
                                 fill_price=price, event_time=time)
                            for (key, quantity, price), time in zip(confirmations, times)],
        equity_curve=[dict(time=time, equity='1000') for time in times],
        account=dict(positions=[]))
    result = ledger_summary(path, replay)
    assert result['time_in_market'] == 6/9
    assert result['traded_notional'] == float(Decimal('82'))
    assert result['turnover_initial_equity'] == float(Decimal('.082'))
