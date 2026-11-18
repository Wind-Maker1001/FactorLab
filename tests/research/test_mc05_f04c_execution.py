"""Boundary cases for the hourly Grid and the research-only quote clock."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pandas as pd

from factorlab.experiments import grid_paper
from factorlab.experiments.grid_execution_audit import audit_account
from factorlab.experiments.grid_execution_clock import ResearchExecution
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.contracts import Market, OrderStatus
from factorlab.trading.control import RiskLimits
from factorlab.trading.storage import PaperStore


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fixture(closes, intrahour, *, quantity=2, ttl=2, breakout='pause'):
    bars = []
    minutes = {}
    for hour, close in enumerate(closes):
        begin = START + timedelta(hours=hour)
        begin_iso = begin.isoformat()
        previous_close = closes[hour - 1] if hour else close
        candle_minutes = []
        for minute in range(60):
            time = (begin + timedelta(minutes=minute)).isoformat()
            price = close if minute == 59 else intrahour.get((hour, minute), previous_close)
            candle_minutes.append(dict(id=f'minute-{hour}-{minute}', time=time, open=price, price=price,
                close=price, low=price, high=price, volume=1, completed_bar=True))
        minutes[begin_iso] = candle_minutes
        prices = [minute['close'] for minute in candle_minutes]
        bars.append(dict(id=f'hour-{hour}', time=begin_iso, open=prices[0], price=close,
            close=close, low=min(prices), high=max(prices), volume=60, completed_bar=True))
    settings = dict(bars=bars, capital=1000, fee_rate=Decimal('.0004'), slippage=0,
        participation=Decimal('.05'), market=Market.BINANCE_USDM, interval='1h',
        config=GridConfig(lower=80, upper=120, levels=4, initial_cash=1000,
            initial_price=100, initial_inventory=0, quantity=quantity, order_ttl=ttl,
            expiry_reseed_limit=0, fee_bps=4, slippage_bps=0, breakout=breakout),
        rules=GridRules(Decimal(1), Decimal(1), Decimal('.01'), Decimal(1), Decimal(1)),
        provenance={}, research_wal=True)
    return settings, minutes


def _result(path):
    return json.loads((path / 'result.json').read_text(encoding='utf-8'))


def _order_events(path):
    store = PaperStore(path / 'paper.sqlite')
    try:
        return [event for _, event in store.order_events()]
    finally:
        store.close()


def test_minute_quotes_respect_creation_expiry_and_final_quote_priority(tmp_path):
    settings, minutes = _fixture([100, 100, 89, 100], {(0, 0): 79, (3, 0): 79})
    run = tmp_path / 'minute'
    grid_paper.run_experiment(run, **settings, research_execution=ResearchExecution(minutes, Decimal(1)))
    result = _result(run)
    order_id, order = next((key, order) for key, order in result['orders'].items()
                           if Decimal(order['intent']['limit_price']) == 90)
    events = [event for event in _order_events(run) if event.client_order_id == order_id]

    assert result['fills'] == 1
    assert order['status'] == 'CANCELED'
    assert Decimal(order['cumulative_filled_quantity']) == 1
    assert not any(record['intent']['side'] == 'SELL' for record in result['orders'].values())
    assert [event.status for event in events[-2:]] == [OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELED]
    boundary = START + timedelta(hours=3)
    assert boundary - timedelta(seconds=1) <= events[-2].event_time < boundary
    assert events[-2].event_time == events[-1].event_time
    assert any(event['order_id'] == order_id and event['cause'] == 'TTL'
               for event in result['lifecycle'])


def test_hourly_capacity_is_shared_and_reverse_submission_waits_for_hour_close(tmp_path):
    settings, minutes = _fixture([100, 100, 100], {(1, 0): 79, (1, 1): 79}, ttl=100)
    run = tmp_path / 'minute'
    clock = ResearchExecution(minutes, Decimal(1))
    grid_paper.run_experiment(run, **settings, research_execution=clock)
    fills = [event for event in _order_events(run) if event.fill_price is not None]

    assert len(fills) == 2
    buy, sell = fills
    orders = _result(run)['orders']
    assert orders[buy.client_order_id]['intent']['side'] == 'BUY'
    assert orders[sell.client_order_id]['intent']['side'] == 'SELL'
    assert buy.status == OrderStatus.PARTIALLY_FILLED
    assert buy.cumulative_filled_quantity == sell.cumulative_filled_quantity == 1
    assert START + timedelta(hours=1, seconds=59) <= buy.event_time < START + timedelta(hours=1, minutes=1)
    assert START + timedelta(hours=2, seconds=59) <= sell.event_time < START + timedelta(hours=2, minutes=1)
    audit = audit_account(run, start=START + timedelta(hours=1), end=START + timedelta(hours=3),
                          fill_references=clock.fill_references)
    assert audit['metrics']['holding_hours'] == 1
    assert audit['metrics']['terminal_inventory'] == 0
    assert audit['metrics']['fees'] > 0
    assert audit['reconciliation']['status'] == 'PASS'


def test_future_minutes_do_not_change_prior_strategy_or_account_events(tmp_path):
    paths = [tmp_path / name for name in ('first', 'changed')]
    for path, future_price in zip(paths, (100, 81)):
        settings, minutes = _fixture([100, 100, future_price], {(1, 0): 89}, ttl=100)
        grid_paper.run_experiment(path, **settings, research_execution=ResearchExecution(minutes, Decimal(1)))

    boundary = START + timedelta(hours=2)
    first, changed = ([event for event in _order_events(path) if event.event_time < boundary]
                      for path in paths)
    assert any(event.fill_price is not None for event in first)
    assert first == changed
    first_nav, changed_nav = (pd.read_csv(path / 'daily.csv').iloc[:2] for path in paths)
    pd.testing.assert_frame_equal(first_nav, changed_nav)


def test_controller_rejection_does_not_make_a_proposal_matchable(tmp_path, monkeypatch):
    def limits(capital, exposure, budget, rate):
        return RiskLimits(Decimal(70), exposure, budget, rate)

    monkeypatch.setattr(grid_paper, 'RiskLimits', limits)
    settings, minutes = _fixture([100, 89], {(1, 0): 79}, quantity=1)
    run = tmp_path / 'rejected'
    grid_paper.run_experiment(run, **settings, research_execution=ResearchExecution(minutes, Decimal(1)))
    result = _result(run)

    assert any(event['kind'] == 'SUBMISSION_REJECTED' and event['reason'] == 'ORDER_NOTIONAL_LIMIT'
               for event in result['risk'])
    assert _order_events(run) == []
    assert result['orders'] == {}
    assert result['final']['cash'] == 1000
    assert result['final']['inventory'] == 0


def test_risk_exit_is_eligible_on_first_minute_after_hourly_submission(tmp_path):
    settings, minutes = _fixture([100, 90, 70, 75], {}, quantity=1, ttl=100, breakout='exit')
    run = tmp_path / 'exit'
    grid_paper.run_experiment(run, **settings, research_execution=ResearchExecution(minutes, Decimal(1)))
    result = _result(run)
    exit_id, order = next((key, order) for key, order in result['orders'].items()
                          if order['purpose'] == 'RISK_EXIT')
    fill = next(event for event in _order_events(run)
                if event.client_order_id == exit_id and event.fill_price is not None)

    assert order['status'] == 'PARTIALLY_FILLED'
    assert fill.cumulative_filled_quantity == 1
    assert START + timedelta(hours=3, seconds=59) <= fill.event_time < START + timedelta(hours=3, minutes=1)
    assert result['final']['inventory'] == 1


def test_hourly_research_arm_matches_native_hourly_with_same_capacity(tmp_path):
    settings, _ = _fixture([100, 89, 100, 100], {}, quantity=1, ttl=2)
    native = tmp_path / 'native'
    research = tmp_path / 'research'
    grid_paper.run_experiment(native, **settings)
    grid_paper.run_experiment(research, **settings, research_execution=ResearchExecution(None, Decimal(3)))

    assert _result(native)['fills'] > 0
    for artifact in ('daily.csv', 'trades.csv'):
        assert (native / artifact).read_bytes() == (research / artifact).read_bytes()
    assert _order_events(native) == _order_events(research)


def test_ohlc_diagnostic_counts_only_missed_touches_of_working_orders(tmp_path):
    settings, minutes = _fixture([100, 100, 100], {}, quantity=1, ttl=1)
    for hour, (bar, hour_minutes) in enumerate(zip(settings['bars'], minutes.values())):
        extreme = 89 if hour == 1 else 79
        hour_minutes[0]['low'] = extreme
        bar['low'] = extreme
    clock = ResearchExecution(None, Decimal(1), diagnostic_minutes=minutes)
    run = tmp_path / 'touch'
    grid_paper.run_experiment(run, **settings, research_execution=clock)

    assert _result(run)['fills'] == 0
    assert clock.summary()['missed_order_hours'] == 1
    assert clock.summary()['missed_order_minutes'] == 1
    assert clock.price_touches[0]['minute'] == (START + timedelta(hours=1)).isoformat()
    assert clock.price_touches[0]['absent_at_hour_close']
