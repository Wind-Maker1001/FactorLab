"""Unified Grid/Controller/Broker/Store research integration evidence."""
from dataclasses import replace
from decimal import Decimal as D
import json

import pandas as pd
import pytest

from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.experiments.grid_paper import deterministic_bars, run_comparison, run_experiment
from factorlab.strategies.grid import GridConfig, GridStrategy
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.storage import PaperStore
from factorlab.trading.contracts import Market


@pytest.fixture
def inputs():
    return dict(bars=deterministic_bars(),
        config=GridConfig(lower=80, upper=120, levels=4, initial_cash=1000,
            initial_inventory=0, initial_price=100, quantity=2, atr_window=2,
            max_inventory=100, max_cash_committed=1000, max_drawdown=1,
            fee_bps=0, slippage_bps=0, breakout='pause'),
        rules=GridRules(D(1), D('.1'), D('.1'), D('.1'), D(1)),
        capital=1000, fee_rate=D('.001'), slippage=D('.001'), participation=D('.05'))


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_four_arm_authoritative_receipts(tmp_path, inputs, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy matching engine must not execute')
    monkeypatch.setattr(GridStrategy, 'on_event', forbidden)
    results = run_comparison(tmp_path/'runs', **inputs)
    assert [result.name for result in results] == ['no-trade', 'arithmetic', 'geometric', 'atr']
    assert (tmp_path/'runs'/'comparison.csv').exists()
    baseline = pd.read_csv(tmp_path/'runs'/'no-trade'/'daily.csv')
    assert baseline.strategy_nav.eq(1).all()
    for result in results[1:]:
        directory = tmp_path/'runs'/result.name
        trades = pd.read_csv(directory/'trades.csv')
        daily = pd.read_csv(directory/'daily.csv')
        assert len(trades) > 0
        assert (trades.executed_shares <= 1).all()
        assert trades.fee.tolist() == pytest.approx((trades.trade_notional*.001).tolist())
        assert daily.transaction_cost.sum() == pytest.approx(trades.transaction_cost.sum())
        assert daily.turnover.sum() == pytest.approx(trades.trade_notional.sum()/1000)
        store = PaperStore(directory/'paper.sqlite')
        try:
            events = list(store.order_events())
            fill_ids = {event.event_id for _, event in events if event.fill_price is not None}
            assert set(trades.event_id) <= fill_ids
            assert store.load().positions.get('GRIDUSD') is not None or daily.inventory.iloc[-1] == 0
        finally:
            store.db.close()
        assert 'trades' in result.tables and result.receipts


def test_five_price_grid_path_and_authoritative_equity(tmp_path, inputs):
    bars = deterministic_bars()[:5]
    for bar, price in zip(bars, [100, 90, 100, 110, 100]):
        bar.update(price=price, close=price, high=price+2, low=price-2)
    config = replace(inputs['config'], initial_inventory=2, quantity=1)
    run_experiment(tmp_path/'run', **(inputs | dict(bars=bars, config=config,
                   slippage=0, participation=1)))
    trades = pd.read_csv(tmp_path/'run'/'trades.csv')
    assert trades.side.tolist() == ['BUY', 'SELL', 'SELL', 'BUY']
    assert trades.execution_price.tolist() == [90, 100, 110, 100]
    assert trades.grid_level.tolist() == [1, 2, 3, 2]
    assert pd.to_datetime(trades.event_time, utc=True).dt.hour.eq(23).all()
    assert trades.cumulative_filled_quantity.eq(1).all()
    result = read_json(tmp_path/'run'/'result.json')
    assert result['final']['inventory'] == 2
    assert result['final']['cash'] == pytest.approx(819.6)
    assert result['final']['equity'] == pytest.approx(1019.6)
    assert result['totals']['fees'] == pytest.approx(.4)
    assert result['totals']['turnover'] == pytest.approx(.4)


def test_clipped_execution_receipts_use_confirmed_not_candidate_quantity(tmp_path, inputs):
    config = replace(inputs['config'], max_turnover=.135)
    run_experiment(tmp_path/'run', **(inputs | dict(config=config, slippage=0, participation=1)))
    trades = pd.read_csv(tmp_path/'run'/'trades.csv')
    assert trades.executed_shares.tolist() == [1.5]
    assert trades.trade_notional.sum() == 135
    assert trades.fee.sum() == pytest.approx(.135)
    result = read_json(tmp_path/'run'/'result.json')
    assert result['final']['inventory'] == 1.5
    assert result['totals']['turnover'] == .135
    assert result['totals']['risk_triggers'] == 1
    manifest = read_json(tmp_path/'run'/'manifest.json')
    assert manifest['provenance']['code']['commit']
    assert manifest['window']['bars'] == len(inputs['bars'])
    assert 'bars' not in manifest


def test_later_bar_only_and_shared_budget(tmp_path, inputs):
    inputs['bars'] = inputs['bars'][:3]
    run_experiment(tmp_path/'run', **inputs)
    trades = pd.read_csv(tmp_path/'run'/'trades.csv')
    assert not trades.execution_date.eq(inputs['bars'][0]['time']).any()
    assert (trades.groupby('execution_date').executed_shares.sum() <= 1).all()


def test_zero_participation_and_window(tmp_path, inputs):
    result = run_experiment(tmp_path/'run', **(inputs | dict(participation=0,
        start=inputs['bars'][2]['time'], end=inputs['bars'][5]['time'])))
    assert len(result.tables['daily']) == 4
    assert result.tables['trades'].empty
    assert result.tables['daily'].strategy_nav.eq(1).all()


def test_fee_estimate_is_not_broker_minimum(tmp_path, inputs):
    schedule = FeeSchedule([FeeRule('1900-01-01', commission_bps=0,
                                   minimum_commission_cny=.01, slippage_bps=0)])
    run_experiment(tmp_path/'run', **(inputs | dict(fee_rate=D('.00001'), slippage=0)), fee_estimate=schedule)
    result = read_json(tmp_path/'run'/'result.json')
    trades = pd.read_csv(tmp_path/'run'/'trades.csv')
    assert trades.fee.max() < .01
    assert result['execution']['actual_fee_model'] == 'proportional'
    assert D(result['fee_budget']['estimate_minus_actual']) > 0


def test_usdm_endowment_uses_wallet_and_collateral_not_spot_cash(tmp_path, inputs):
    config = replace(inputs['config'], initial_inventory=2)
    run_experiment(tmp_path/'run', **(inputs | dict(config=config, no_trade=True,
                   market=Market.BINANCE_USDM, instrument_id='NVDAUSDT')))
    daily = pd.read_csv(tmp_path/'run'/'daily.csv')
    assert daily.cash.eq(1000).all()
    assert daily.committed.eq(200).all()
    assert daily.equity.iloc[1] == 980
    assert daily.strategy_nav.tolist() == daily.benchmark_nav.tolist()


def test_endowed_inventory_and_determinism(tmp_path, inputs):
    config = replace(inputs['config'], initial_inventory=2)
    for name in ('a', 'b'):
        run_experiment(tmp_path/name, **(inputs | dict(config=config, no_trade=True)))
    assert (tmp_path/'a'/'daily.csv').read_bytes() == (tmp_path/'b'/'daily.csv').read_bytes()
    daily = pd.read_csv(tmp_path/'a'/'daily.csv')
    assert daily.cash.eq(800).all()
    assert daily.inventory.eq(2).all()
    assert daily.equity.iloc[1] == 980


@pytest.mark.parametrize('change', [dict(participation=2), dict(slippage=1), dict(fee_rate=-1),
                                   dict(capital=0), dict(start='2030-01-01')])
def test_invalid_inputs(tmp_path, inputs, change):
    with pytest.raises(ValueError):
        run_experiment(tmp_path/'run', **(inputs | change))
