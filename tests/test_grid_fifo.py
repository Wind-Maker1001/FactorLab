"""FIFO partial-fill economics and authoritative evidence checks."""
import csv
from decimal import Decimal as D
import json
from types import SimpleNamespace as NS

import pytest

from factorlab.experiments import grid_fifo


@pytest.fixture
def account(tmp_path, monkeypatch):
    events, rows, orders = [], [], {}
    quantities, fees = {}, {}
    # First lot is partly grid-sold then risk-contaminated; second completes.
    fills = [('a', 'BUY', '.3', '100', '.03', 'GRID'),
             ('a', 'BUY', '.2', '100', '.02', 'GRID'),
             ('b', 'SELL', '.1', '110', '.011', 'GRID'),
             ('c', 'SELL', '.2', '90', '.018', 'RISK_EXIT'),
             ('d', 'SELL', '.2', '110', '.022', 'GRID'),
             ('e', 'BUY', '.1', '100', '.01', 'GRID')]
    for index, (oid, side, q, p, fee, purpose) in enumerate(fills):
        q, p, fee = map(D, (q, p, fee))
        quantities[oid] = quantities.get(oid, D(0)) + q
        fees[oid] = fees.get(oid, D(0)) + fee
        event = NS(event_id=str(index), client_order_id=oid, fill_price=p,
                   cumulative_filled_quantity=quantities[oid], cumulative_fee=fees[oid],
                   event_time=NS(isoformat=lambda: '2026-01-01T00:59:59+00:00'))
        events.append(event)
        orders[oid] = dict(intent=dict(side=side), purpose=purpose)
        slip = q*abs(p-D(101))
        rows.append(dict(event_id=str(index), order_id=oid, side=side,
                         event_time=event.event_time.isoformat(), execution_date='2026-01-01',
                         cumulative_filled_quantity=quantities[oid], cumulative_fee=fees[oid],
                         executed_shares=q, execution_price=p, fee=fee,
                         trade_notional=q*p, slippage=slip, transaction_cost=fee+slip))
    result = dict(config=dict(initial_inventory=0), rules=dict(quantity_unit='1'),
                  orders=orders, final=dict(close=105, equity='1001.389'), capital='1000',
                  instrument_id='X', market='BINANCE_EQUITY')
    (tmp_path/'result.json').write_text(json.dumps(result))
    (tmp_path/'daily.csv').write_text('date,close\n2026-01-01,101\n')
    def write_rows():
        with (tmp_path/'trades.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0])
            writer.writeheader()
            writer.writerows(rows)
    write_rows()
    state = NS(cash={'USD': D('990.889')},
               orders={key: NS(snapshot=NS(intent=NS(side=NS(value=value['intent']['side']))))
                       for key, value in orders.items()},
               positions={'X': NS(quantity=D('.1'), entry_price=D(100), mark_price=D(105))})
    class Store:
        def __init__(self, path):
            pass
        def order_events(self):
            return enumerate(events)
        def load(self):
            return state
        def close(self):
            pass
    monkeypatch.setattr(grid_fifo, 'PaperStore', Store)
    return tmp_path, rows, write_rows, state


def test_partial_lots_risk_contamination_and_residual(account):
    path, _, _, _ = account
    value = grid_fifo.attribution(path)
    summary = value['summary']
    assert summary['grid_cycles'] == 1
    assert summary['buy_fill_lots'] == 3
    assert summary['completed_cycle']['net'] == D('1.958')
    assert summary['normal_sell']['net'] == D('2.937')
    assert summary['risk_exit']['net'] == D('-2.038')
    assert summary['residual']['net'] == D('.49')
    assert summary['actual_fees'] == D('.111')
    assert summary['reconciliation_residual'] == 0
    assert len(value['matches']) == 3
    for section in ('normal_sell', 'risk_exit', 'residual', 'completed_cycle'):
        part = summary[section]
        assert part['reference_gross']-part['fees']-part['slippage'] == part['net']


def test_explicit_mark_adjustment(account):
    path, _, _, state = account
    state.positions['X'].mark_price = D(106)
    summary = grid_fifo.attribution(path)['summary']
    assert summary['valuation_adjustment'] == D('.1')
    assert summary['reconciliation_residual'] == 0


def test_real_paper_journal(tmp_path):
    from factorlab.experiments.grid_paper import deterministic_bars, run_experiment
    from factorlab.strategies.grid import GridConfig
    from factorlab.strategies.grid_execution import GridRules
    config = GridConfig(lower=80, upper=120, levels=4, initial_cash=1000,
                        initial_inventory=0, initial_price=100, quantity=2,
                        atr_window=2, max_inventory=100, max_cash_committed=1000,
                        max_drawdown=1, fee_bps=0, slippage_bps=0, breakout='pause')
    run_experiment(tmp_path/'run', bars=deterministic_bars(), config=config,
                   rules=GridRules(D(1), D('.1'), D('.1'), D('.1'), D(1)),
                   capital=1000, fee_rate=D('.001'), slippage=D('.001'),
                   participation=D('.05'))
    summary = grid_fifo.attribution(tmp_path/'run')['summary']
    assert abs(summary['reconciliation_residual']) < D('1e-20')
    assert summary['buy_fill_lots'] > 0


@pytest.mark.parametrize('field,value', [('executed_shares', '.4'), ('fee', '.04'),
                                        ('execution_price', '99'), ('slippage', '0')])
def test_rejects_per_event_csv_changes(account, field, value):
    path, rows, write_rows, _ = account
    rows[0][field] = value
    write_rows()
    with pytest.raises(ValueError, match='authoritative'):
        grid_fifo.attribution(path)
