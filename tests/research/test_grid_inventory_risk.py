"""FIFO gates exercised through authoritative Paper transactions."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from factorlab.experiments.grid_inventory_risk import InventoryRiskGrid
from factorlab.experiments.grid_paper import run_experiment
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.contracts import Market, OrderType, Side
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore
from decimal import Decimal as D


def execute(path, rule=None, *, prices=None, volumes=None, capital=1000, participation=1,
            market=Market.BINANCE_EQUITY, config_overrides=None, close_only=False):
    prices = prices or [100, 89] + [89] * 202
    volumes = volumes or [100] * len(prices)
    bars = [dict(id=str(i), time=t.isoformat(), price=p, close=p, high=p+1, low=p-1,
                 volume=v, completed_bar=True)
            for i, (t, p, v) in enumerate(zip(pd.date_range('2026-01-01', periods=len(prices),
                                                        freq='h', tz='UTC'), prices, volumes))]
    scenario_options = {}
    if close_only:
        bars = [{k: v for k, v in bar.items() if k not in {'high', 'low', 'volume'}}
                for bar in bars]
        scenario_options = dict(scenario='CLOSE_ONLY_SCENARIO', synthetic_capacity=D(100))
    run_experiment(path, bars=bars, interval='1h', inventory_risk=rule, market=market,
        **scenario_options,
        config=replace(GridConfig(lower=80, upper=120, levels=4, initial_price=100,
            initial_inventory=0, initial_cash=capital, quantity=10, minimum_quantity=1,
            max_inventory=1000, max_cash_committed=capital, max_drawdown=1,
            max_turnover=100, order_ttl=1000, fee_bps=4, slippage_bps=6),
            **(config_overrides or {})),
        rules=GridRules(D('.01'), D('.01'), D('.01'), D('.01'), D(1)),
        capital=capital, fee_rate=D('.0004'), slippage=D('.0006'), participation=participation)
    return pd.read_csv(path/'daily.csv'), pd.read_csv(path/'trades.csv')


@pytest.mark.parametrize('rule', ['age', 'loss', 'cap'])
def test_close_only_inventory_risk_matches_paper(tmp_path, rule):
    prices = [100, 89, 79] + [20] * 201
    options = dict(prices=prices, config_overrides={'breakout': 'pause'})
    ordinary = tmp_path/'ordinary'
    scenario = tmp_path/'scenario'
    nav, trades = execute(ordinary, rule, **options)
    other_nav, other_trades = execute(scenario, rule, close_only=True, **options)
    pd.testing.assert_frame_equal(nav, other_nav)
    pd.testing.assert_frame_equal(trades, other_trades)
    assert json.loads((ordinary/'inventory-risk.json').read_text()) == json.loads(
        (scenario/'inventory-risk.json').read_text())
    manifest = json.loads((scenario/'manifest.json').read_text())
    assert manifest['inventory_risk'] == rule
    assert manifest['scenario'] == 'CLOSE_ONLY_SCENARIO'
    assert D(manifest['synthetic_capacity']) == D(100)
    assert manifest['interval'] == '1h'
    assert other_trades.groupby('execution_date').executed_shares.sum().le(100).all()
    result = json.loads((scenario/'result.json').read_text())
    assert result['execution']['scenario'] == 'CLOSE_ONLY_SCENARIO'


def test_age_gate_and_later_bar_exit(tmp_path):
    nav, trades = execute(tmp_path/'age', 'age')
    events = json.loads((tmp_path/'age'/'inventory-risk.json').read_text())['events']
    assert events[0]['age_hours'] == 100
    assert events[-1]['age_hours'] == 200
    assert trades.side.tolist() == ['BUY', 'SELL']
    assert pd.Timestamp(trades.event_time.iloc[-1]).tz_localize(None) > pd.Timestamp(events[-1]['time'])
    assert nav.inventory.iloc[-1] == 0
    assert nav.state.iloc[-1] == 'EXITED'
    assert (nav.equity - nav.cash - nav.inventory * nav.close).abs().max() < 1e-9
    assert trades.fee.sum() == pytest.approx(nav.fee.sum())


def test_no_volume_does_not_complete_exit(tmp_path):
    nav, trades = execute(tmp_path/'age', 'age', volumes=[100, 100]+[0]*202)
    assert nav.state.iloc[-1] == 'EXITING'
    assert nav.inventory.iloc[-1] == .1
    assert trades.side.tolist() == ['BUY']


def test_age_gate_preserves_existing_sell(tmp_path):
    prices = [100,89]+[89]*99+[101,101]
    nav, trades = execute(tmp_path/'sell', 'age', prices=prices)
    assert trades.side.tolist() == ['BUY','SELL']
    assert nav.inventory.iloc[-1] == 0
    assert nav.state.iloc[-1] == 'RUNNING'
    events = json.loads((tmp_path/'sell'/'inventory-risk.json').read_text())['events']
    assert events[0]['buy_blocked']
    assert not events[-1]['buy_blocked']


def test_partial_exit_is_not_completed(tmp_path):
    volumes = [2000,2000]+[0]*200+[1,0]
    nav, trades = execute(tmp_path/'partial_exit', 'age', volumes=volumes, participation=D('.05'))
    assert trades.side.tolist() == ['BUY','SELL']
    assert trades.executed_shares.iloc[-1] == .05
    assert nav.inventory.iloc[-1] == .05
    assert nav.state.iloc[-1] == 'EXITING'


def test_cap_counts_pending_and_keeps_sells(tmp_path):
    nav, trades = execute(tmp_path/'cap', 'cap', prices=[100,79,100,110])
    result = json.loads((tmp_path/'cap'/'result.json').read_text())
    buys = [o for o in result['orders'].values() if o['intent']['side'] == 'BUY']
    assert len(buys) == 2
    assert nav.inventory.max() <= .2


def test_partial_fifo_preserves_second_fill_age(tmp_path):
    prices = [100, 89, 89, 101, 89] + [89]*201
    volumes = [2000, 1, 1, 1, 1] + [2000]*201
    nav, trades = execute(tmp_path/'partial', 'age', prices=prices, volumes=volumes, participation=D('.05'))
    assert trades.executed_shares.iloc[:4].tolist() == [.05]*4
    assert trades.side.iloc[:4].tolist() == ['BUY', 'BUY', 'SELL', 'BUY']
    events = json.loads((tmp_path/'partial'/'inventory-risk.json').read_text())['events']
    assert events[0]['age_hours'] == 100
    assert pd.Timestamp(events[0]['time']) == pd.Timestamp('2026-01-05T06:59:59.999999')
    assert nav.inventory.iloc[-1] == 0


def test_future_prices_do_not_change_prefix(tmp_path):
    prices = [100,89,89,89,100]
    nav, trades = execute(tmp_path/'one', 'age', prices=prices)
    other, other_trades = execute(tmp_path/'two', 'age', prices=prices[:-1]+[110])
    pd.testing.assert_frame_equal(nav.iloc[:-1], other.iloc[:-1])
    pd.testing.assert_frame_equal(trades.iloc[:1], other_trades.iloc[:1])


def test_empty_cap_account_summary(tmp_path):
    from tools.research.run_grid_inventory_risk import summarize
    directory = tmp_path/'empty'
    nav, trades = execute(directory, 'cap', prices=[100,100,100], market=Market.BINANCE_USDM)
    events = [(pd.Timestamp(nav.date.iloc[1]).to_pydatetime(), D(100), D('.001'), 'Regular')]
    row = summarize(directory, 'fixture', 'base', 'cap',
                    [dict(time=nav.date.iloc[-1])], events)
    assert row['fills'] == 0
    assert row['exit_fees'] == 0
    assert row['exit_slippage'] == 0
    assert row['mean_holding_hours'] == 0
    assert row['max_cash_equity_residual'] == 0


def test_usdm_cash_and_equity_conserve(tmp_path):
    nav, trades = execute(tmp_path/'usdm', 'age', prices=[100,89,85], market=Market.BINANCE_USDM)
    assert nav.inventory.iloc[-1] == .1
    assert (nav.equity-nav.cash-nav.unrealized).abs().max() < 1e-9
    assert nav.cash.iloc[-1] == pytest.approx(1000-trades.fee.sum())


@pytest.mark.parametrize('tail_volume, expected_inventory, expected_state', [
    (100, 0, 'EXITED'), (0, .1, 'EXITING'), (1, .05, 'EXITING'),
])
def test_paused_inventory_reaches_age_exit(tmp_path, tail_volume, expected_inventory, expected_state):
    path = tmp_path/'paused_age'
    nav, trades = execute(path, 'age', prices=[100, 89, 79]+[79]*201,
        volumes=[2000, 2000]+[0]*200+[tail_volume, 0], participation=D('.05'),
        config_overrides={'breakout': 'pause'})
    assert nav.state.iloc[2] == 'PAUSED'
    assert nav.inventory.iloc[2] == .1
    assert nav.state.iloc[201] == 'EXITING'
    assert nav.state.iloc[-1] == expected_state
    assert nav.inventory.iloc[-1] == expected_inventory
    assert trades.side.tolist() == (['BUY'] if tail_volume == 0 else ['BUY', 'SELL'])
    events = json.loads((path/'inventory-risk.json').read_text())['events']
    assert [e['age_hours'] for e in events] == [100, 200]
    assert events[-1]['exit_reason'] == 'INVENTORY_AGE'
    if tail_volume:
        assert trades.executed_shares.iloc[-1] == .1-expected_inventory
        assert pd.Timestamp(trades.event_time.iloc[-1]).tz_localize(None) > pd.Timestamp(events[-1]['time'])


@pytest.mark.parametrize('same_check', [False, True])
def test_paused_loss_budget_uses_latest_account_and_price(tmp_path, monkeypatch, same_check):
    from factorlab.strategies.grid_execution import GridExecution
    transitions = []
    original = GridExecution._risk

    def record_risk(grid, reason, exit_position=False):
        original(grid, reason, exit_position)
        transitions.append((reason, grid.state))

    monkeypatch.setattr(GridExecution, '_risk', record_risk)
    path = tmp_path/'paused_loss'
    overrides = {'breakout': 'pause', **({'max_drawdown': .005} if same_check else {})}
    prices = [100, 89, 89 if same_check else 79, 20, 20, 20]
    nav, trades = execute(path, 'loss', prices=prices, volumes=[100, 100, 0, 0, 100, 100],
                          config_overrides=overrides)
    assert nav.state.iloc[2] == ('RUNNING' if same_check else 'PAUSED')
    assert nav.state.iloc[3] == 'EXITING'
    assert nav.state.iloc[-1] == 'EXITED'
    assert trades.side.tolist() == ['BUY', 'SELL']
    events = json.loads((path/'inventory-risk.json').read_text())['events']
    assert len(events) == 1
    event = events[0]
    assert event['exit_reason'] == 'LOSS_BUDGET'
    assert float(event['equity']) == pytest.approx(nav.equity.iloc[3])
    assert transitions == [('MAX_DRAWDOWN' if same_check else 'BREAKOUT', 'PAUSED'),
                           ('LOSS_BUDGET', 'EXITING')]
    assert D(event['remaining_budget']) == D('.005')*D(event['equity']) + D(event['realized_net']) + D(event['unrealized'])
    assert D(event['remaining_budget']) <= 0
    result = json.loads((path/'result.json').read_text())
    assert len([r for r in result['risk'] if r['state'] == 'EXITING']) == 1


def test_parent_pauses_on_fill_before_age_threshold(tmp_path):
    path = tmp_path/'drawdown'
    nav, trades = execute(path, 'age', config_overrides={'max_drawdown': .000001})
    assert nav.state.iloc[1] == 'PAUSED'
    assert nav.state.iloc[200] == 'PAUSED'
    assert nav.state.iloc[201] == 'EXITING'
    assert nav.state.iloc[-1] == 'EXITED'
    assert trades.side.tolist() == ['BUY', 'SELL']


@pytest.mark.parametrize('rule', ['age', 'loss'])
def test_paused_below_threshold_never_resumes_buying(tmp_path, rule):
    nav, trades = execute(tmp_path/rule, rule, prices=[100, 89, 79, 100, 89, 79, 100],
                          volumes=[100, 100, 0, 100, 100, 100, 100],
                          config_overrides={'breakout': 'pause'})
    assert nav.state.iloc[2:].tolist() == ['PAUSED']*5
    assert nav.inventory.iloc[-1] == .1
    assert trades.side.tolist() == ['BUY']


@pytest.mark.parametrize('rule', ['age', 'loss'])
def test_empty_paused_account_never_sells(tmp_path, rule):
    path = tmp_path/rule
    nav, trades = execute(path, rule, prices=[100, 121]+[20]*204,
                          config_overrides={'breakout': 'pause'})
    assert nav.state.iloc[1:].eq('PAUSED').all()
    assert nav.inventory.eq(0).all()
    assert trades.empty
    assert not json.loads((path/'inventory-risk.json').read_text())['events']


def test_paused_exit_blocked_by_trading_minimum_keeps_inventory(tmp_path):
    path = tmp_path/'filters'
    nav, trades = execute(path, 'age', prices=[100, 89, 79]+[2]*201,
        volumes=[100, 100, 0]+[100]*201, config_overrides={'breakout': 'pause'})
    assert nav.state.iloc[2] == 'PAUSED'
    assert nav.state.iloc[-1] == 'EXITING'
    assert nav.inventory.iloc[-1] == .1
    assert trades.side.tolist() == ['BUY']
    events = json.loads((path/'inventory-risk.json').read_text())['events']
    assert len([e for e in events if e['exit_reason']]) == 1


def test_loss_budget_and_repeatability(tmp_path):
    prices = [100,89,81,81,81]
    nav, trades = execute(tmp_path/'a', 'loss', prices=prices, capital=100)
    other, other_trades = execute(tmp_path/'b', 'loss', prices=prices, capital=100)
    pd.testing.assert_frame_equal(nav, other)
    pd.testing.assert_frame_equal(trades, other_trades)
    events = json.loads((tmp_path/'a'/'inventory-risk.json').read_text())['events']
    assert events[-1]['exit_reason'] == 'LOSS_BUDGET'
    event = events[-1]
    assert D(event['remaining_budget']) == D('.005')*D(event['equity']) + D(event['realized_net']) + min(D(event['unrealized']),D(0))
    assert D(event['remaining_budget']) <= 0
    assert nav.inventory.iloc[-1] == 0


def test_research_fills_post_fifo_before_hourly_loss_decision(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    market = Market.BINANCE_USDM
    capital = D(100)
    account_id = 'inventory-risk'
    broker = PaperBroker(market, account_id, (Instrument('TEST', 'USDT'),),
                        {'USDT': capital}, fee_rate=D('.0004'))
    store = PaperStore(tmp_path/'paper.sqlite')
    try:
        now = start + timedelta(hours=1, microseconds=-1)
        controller = TradingController(broker, store, clock=lambda: now,
            limits=RiskLimits(capital, capital, capital, 1000))
        config = GridConfig(lower=80, upper=120, levels=4, initial_price=100, quantity=10,
                            initial_cash=100, initial_inventory=0, max_drawdown=1)
        rules = GridRules(D('.01'), D('.01'), D('.01'), D('.01'), D('.01'))
        grid = InventoryRiskGrid(config, rules, deployment_id='grid', account_id=account_id,
            market=market, instrument_id='TEST', initial_equity=capital, inventory_risk='loss')

        grid.observe_paper(controller, dict(id='seed', time='2026-01-01T00:00:00', price=100,
            high=100, low=100, close=100, volume=10, completed_bar=True),
            delivery=grid.delivery+1, observed_at=now)
        buy_id = next(key for key, order in grid.orders.items() if order.intent.limit_price == 90)
        grid.submit_paper(controller, buy_id, delivery=grid.delivery+1)
        reversals = []
        fills = []
        for minute, price in ((1, D('89.05')), (2, D('88.05'))):
            now = start + timedelta(hours=1, minutes=minute, microseconds=-1)
            event, reverse = grid.fill_research_quote(controller, buy_id, D('.05'), price,
                Quote('TEST', D(81), now), delivery=grid.delivery+1)
            reversals.append(reverse)
            fills.append(event)
        assert [(lot.time, lot.quantity, lot.price) for lot in grid.lots] == [
            (fills[0].event_time, D('.05'), D('89.05')),
            (fills[1].event_time, D('.05'), D('88.05')),
        ]
        assert all(reverse[2:] == (Side.BUY, D('.05')) for reverse in reversals)
        assert grid.state == 'RUNNING'
        assert not grid.risk_events

        now = start + timedelta(hours=2, microseconds=-1)
        grid.observe_research_hour(controller, dict(id='hour', time='2026-01-01T01:00:00', price=81,
            high=89, low=81, close=81, volume=10, completed_bar=True), tuple(reversals),
            delivery=grid.delivery+1, observed_at=now)
        assert grid.state == 'EXITING'
        assert grid.risk_events[0]['exit_reason'] == 'LOSS_BUDGET'
        assert D(grid.risk_events[0]['unrealized']) == D('-.755')
        exit_id = next(key for key, order in grid.orders.items()
                       if order.intent.order_type == OrderType.MARKET)
        grid.submit_paper(controller, exit_id, delivery=grid.delivery+1)
        now += timedelta(minutes=1)
        sale, _ = grid.fill_research_quote(controller, exit_id, D('.06'), D('82.01'),
            Quote('TEST', D(82), now), delivery=grid.delivery+1)
        assert [(lot.time, lot.quantity, lot.price) for lot in grid.lots] == [
            (fills[1].event_time, D('.04'), D('88.05')),
        ]
        fifo_gross = D('.05') * (sale.fill_price - D('89.05')) + D('.01') * (sale.fill_price - D('88.05'))
        assert grid.realized_net == fifo_gross - fills[-1].cumulative_fee - sale.cumulative_fee
    finally:
        store.close()
