"""Frozen source identity and confirmed, bounded risk liquidation regressions."""
from dataclasses import replace
import json
import shutil

import pandas as pd
import pytest

from factorlab.experiments import grid_independent as study
from factorlab.experiments.grid_fifo import attribution
from factorlab.experiments.grid_paper import D, run_experiment
from factorlab.trading.contracts import Market
from .test_grid_independent import ROOT, hourly_bars


@pytest.mark.parametrize('change', ['config', 'config_and_receipt', 'path', 'delete', 'add', 'lf', 'crlf', 'unchanged'])
def test_frozen_mapping_cannot_be_replaced(tmp_path, change):
    *_, hashes = study.load_design(ROOT)
    receipt_path = 'docs/validation/p7-r04a/verified-assets.json'
    for relative in [*hashes, receipt_path, 'docs/validation/p7-r04a/preregistration-lock.json']:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    path = tmp_path / 'configs/grid_paper_nvda_hourly.json'
    receipt = json.loads((tmp_path / receipt_path).read_text())
    sources = receipt['frozen_sources']
    if change in {'config', 'config_and_receipt'}:
        path.write_text(path.read_text().replace('163.472', '160'), encoding='utf-8')
        if change == 'config_and_receipt':
            sources['configs/grid_paper_nvda_hourly.json']['sha256'] = study.digest(path.read_bytes())
    elif change == 'path':
        sources['configs/replaced.json'] = sources.pop('configs/grid_paper_nvda_hourly.json')
    elif change == 'delete':
        del sources['configs/grid_paper_nvda_rules.json']
    elif change == 'add':
        sources['configs/extra.json'] = sources['configs/grid_paper_nvda_rules.json']
    elif change in {'lf', 'crlf'}:
        for relative in hashes:
            target = tmp_path / relative
            data = target.read_bytes().replace(b'\r\n', b'\n')
            target.write_bytes(data if change == 'lf' else data.replace(b'\n', b'\r\n'))
    (tmp_path / receipt_path).write_text(json.dumps(receipt), encoding='utf-8')
    if change in {'lf', 'crlf', 'unchanged'}:
        study.load_design(tmp_path)
    else:
        with pytest.raises(study.StudyBlocked, match='digest mismatch'):
            study.load_design(tmp_path)


def liquidation(directory, prices, *, volume=100, cap=D('.1')):
    _, _, config, rules, _ = study.load_design(ROOT)
    bars = hourly_bars(prices, volume if isinstance(volume, int) else 100)
    if not isinstance(volume, int):
        for bar, size in zip(bars, volume):
            bar['volume'] = size
    run_experiment(directory, bars=bars,
        config=replace(config, breakout='exit'), rules=rules,
        capital=1000, fee_rate=D('.0004'), slippage=D('.0006'), participation=D('.01'),
        market=Market.BINANCE_USDM, instrument_id='BINANCE:USDM:NVDAUSDT', interval='1h',
        max_exit_quantity=cap)
    result = json.loads((directory / 'result.json').read_text())
    nav = pd.read_csv(directory / 'daily.csv')
    fifo = attribution(directory)['summary']
    assert abs(fifo['reconciliation_residual']) <= D('1e-8')
    assert abs(fifo['reported_equity_difference']) <= D('1e-8')
    return result, nav, fifo


@pytest.mark.parametrize('prices,volume', [
    ([180, 170, 160, 159, 158], 100),
    ([204, 190, 180, 170, 160, 159, 158, 157, 156, 155, 154], 100),
    ([204, 190, 190, 180, 180, 170, 170, 160] + [159] * 16, 5),
])
def test_bounded_risk_exits_follow_real_fills(tmp_path, prices, volume):
    result, nav, fifo = liquidation(tmp_path / 'run', prices, volume=volume)
    orders = result['orders']
    exits = {k: o for k, o in orders.items() if o['purpose'] == 'RISK_EXIT'}
    assert exits
    assert all(D(o['intent']['quantity']) <= D('.1') for o in orders.values())
    trades = pd.read_csv(tmp_path / 'run/trades.csv')
    buys = trades[trades.side == 'BUY']
    if len(prices) > 5:
        assert buys.executed_shares.sum() >= .3
        assert len(exits) >= 3
    assert result['final']['inventory'] == 0
    assert result['final']['state'] == 'EXITED'
    assert nav.inventory.between(0, 1).all()
    assert nav.committed.max() <= 1000
    # Each exit is submitted only after its predecessor's terminal confirmation.
    previous_fill = None
    for key, order in exits.items():
        fills = trades[trades.order_id == key]
        assert D(order['cumulative_filled_quantity']) == D(order['intent']['quantity'])
        assert fills.executed_shares.sum() == pytest.approx(float(D(order['intent']['quantity'])))
        if previous_fill is not None:
            assert pd.to_datetime(fills.event_time, utc=True).min() > previous_fill
        previous_fill = pd.to_datetime(fills.event_time, utc=True).max()
        remaining = D(order['intent']['quantity'])
        for quantity in fills.executed_shares:
            assert 0 < D(quantity) <= remaining
            remaining -= D(quantity)
        assert remaining == 0
    assert fifo['risk_exit']['quantity'] > 0
    if volume == 5:
        assert trades[trades.order_id.isin(exits)].groupby('order_id').size().max() >= 2


def test_bounded_exit_partitions_without_creating_illegal_tail(tmp_path):
    result, _, _ = liquidation(tmp_path / 'partition',
        [204, 190, 190, 180, 180, 170] + [70] * 12, volume=[5] * 6 + [100] * 12)
    exits = [o for o in result['orders'].values() if o['purpose'] == 'RISK_EXIT']
    assert [D(o['intent']['quantity']) for o in exits] == [D('.10'), D('.09'), D('.08'), D('.08')]
    assert all(D(o['intent']['quantity']) * 70 >= 5 for o in exits)
    assert result['final']['state'] == 'EXITED'
    assert result['final']['inventory'] == 0


def test_untradeable_exit_keeps_real_inventory(tmp_path):
    result, nav, fifo = liquidation(tmp_path / 'dust', [204, 190, 180, 170, 10, 10, 10])
    assert result['final']['inventory'] >= .3
    assert result['final']['state'] == 'EXITING'
    assert result['final']['state_reason'] == 'RISK_EXIT_FILTERS'
    assert not any(o['purpose'] == 'RISK_EXIT' for o in result['orders'].values())
    assert nav.inventory.iloc[-1] > 0
    assert fifo['residual']['quantity'] > 0


def test_unconstrained_r03_exit_preserves_aggregate_order(tmp_path):
    result, _, _ = liquidation(tmp_path / 'legacy', [204, 190, 180, 170, 160, 159, 158], cap=None)
    exits = [o for o in result['orders'].values() if o['purpose'] == 'RISK_EXIT']
    assert any(D(o['intent']['quantity']) > D('.1') for o in exits)
    assert result['final']['state'] == 'EXITED'
