from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from factorlab.experiments.mc_grid_tail_f04c_windows import candidate_windows, screen_initial_orders
from factorlab.experiments.mc_grid_tail_f04c_cases import extract_execution_cases
from factorlab.experiments.mc_grid_tail_f04c_data import MarketData
from factorlab.experiments.grid_execution_audit import audit_account
from factorlab.experiments.grid_execution_clock import ResearchExecution
from factorlab.experiments.grid_paper import run_experiment
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.contracts import Market


D = Decimal


def screen_inputs():
    start = datetime(2026, 5, 14, 23, tzinfo=timezone.utc)
    hourly = [dict(id=f'hour-{index}', time=(start + timedelta(hours=index)).isoformat(),
        open=D(100), high=D(100), low=D(100), close=D(100), price=D(100),
        volume=D(10), completed_bar=True) for index in range(169)]
    config = GridConfig(mode='geometric', initial_inventory=0, initial_cash=1000,
        initial_price=100, lower=80, upper=120, levels=8, quantity=10,
        order_ttl=100, breakout='pause', expiry_reseed_limit=0)
    rules = GridRules(D('.01'), D('.01'), D('.01'), D('.01'), D(5))
    settings = dict(config=config, rules=rules, capital=D(1000), fee_rate=D('.0004'),
                    market=Market.BINANCE_USDM, instrument_id='BINANCE:USDM:NVDAUSDT')
    return hourly, settings


def test_screen_retains_last_ttl_hour_but_excludes_expired_hours():
    hourly, settings = screen_inputs()
    hourly[100]['low'] = D(90)
    result = screen_initial_orders(hourly, settings)
    assert result['first_potential_touch']['hour'] == hourly[100]['time']
    assert result['inspected_hours'] == 100
    hourly[100]['low'] = D(100)
    hourly[101]['low'] = D(90)
    assert screen_initial_orders(hourly, settings)['first_potential_touch'] is None


def test_screen_uses_native_rounded_orders_and_window_anchor():
    hourly, settings = screen_inputs()
    hourly[1]['low'] = D('97.97')
    result = screen_initial_orders(hourly, settings)
    assert len(result['initial_orders']) == 5
    assert all(order['side'] == 'BUY' for order in result['initial_orders'])
    assert max(order['limit_price'] for order in result['initial_orders']) == D('97.97')
    assert result['first_potential_touch']['limit_price'] == D('97.97')
    assert result['anchor_time'] == hourly[0]['time']
    scaled = [bar | {key: bar[key] * 2 for key in ('open', 'high', 'low', 'close', 'price')}
              for bar in hourly]
    settings['config'] = replace(settings['config'], initial_price=200, lower=160, upper=240)
    scaled_result = screen_initial_orders(scaled, settings)
    assert max(order['limit_price'] for order in scaled_result['initial_orders']) == D('195.95')


def test_screen_stops_after_prior_hour_breakout_cancels_initial_orders():
    hourly, settings = screen_inputs()
    hourly[1].update(close=D(121), price=D(121), high=D(121))
    hourly[2]['low'] = D(90)
    result = screen_initial_orders(hourly, settings)
    assert result['inspected_hours'] == 1
    assert result['first_potential_touch'] is None


def test_independent_and_conditional_windows_do_not_depend_on_prices():
    windows = candidate_windows()
    assert len(windows) == 11
    assert [(row['start'][:10], row['end_exclusive'][:10]) for row in windows[:3]] == [
        ('2026-05-15', '2026-05-22'), ('2026-05-22', '2026-05-29'),
        ('2026-05-29', '2026-06-05')]
    assert windows[-1]['end_exclusive'][:10] == '2026-07-31'
    assert all(left['end_exclusive'] == right['start'] for left, right in zip(windows, windows[1:]))


def test_nonzero_case_reconstructs_native_wallet_and_excludes_terminal_order_touches(tmp_path):
    hourly, settings = screen_inputs()
    hourly = hourly[:3]
    start = datetime.fromisoformat(hourly[1]['time'])
    end = start + timedelta(hours=2)
    minutes = []
    by_hour = {hourly[0]['time']: []}
    for hour in hourly[1:]:
        opened = datetime.fromisoformat(hour['time'])
        rows = []
        for index in range(60):
            time = opened + timedelta(minutes=index)
            close = D(90) if time == start else D(100)
            low = D(79) if time == start + timedelta(minutes=1) else close
            rows.append(dict(time=time.isoformat(), open=close, close=close, low=low, high=D(100),
                volume=D(1), trade_count=1, taker_buy_volume=D('.5'), taker_buy_quote_volume=close / 2))
        by_hour[hour['time']] = rows
        minutes.extend(rows)
    hourly[1]['low'] = D(79)
    accounts = {}
    bars = [{key: str(value) if isinstance(value, Decimal) else value for key, value in bar.items()}
            for bar in hourly]
    for arm in ('A', 'B'):
        clock = ResearchExecution(by_hour if arm == 'B' else None, D('.10'))
        directory = tmp_path / arm
        run_experiment(directory, bars=bars, interval='1h', slippage=D('.0006'),
                       research_execution=clock, research_wal=True, **settings)
        accounts[arm] = audit_account(directory, start=start, end=end, fill_references=clock.fill_references)
    data = MarketData(tuple(hourly), tuple(minutes), (),
                      dict(start=start.isoformat(), end_exclusive=end.isoformat()))
    cases = extract_execution_cases(accounts, data, D('.0006'))
    assert accounts['A']['metrics']['fills'] == 0
    assert accounts['B']['metrics']['fills'] > 0
    assert cases['first_divergence']['minute_bar']['time'] == start.isoformat()
    for arm in ('A', 'B'):
        terminal = cases['arms'][arm]['terminal_account']
        metrics = accounts[arm]['metrics']
        assert terminal['cash'] == metrics['terminal_cash']
        assert terminal['inventory'] == metrics['terminal_inventory']
        assert terminal['equity'] == metrics['terminal_equity']
    first = next(order for order in cases['arms']['B']['orders'].values() if order['confirmed_fills'])
    first_touch = first['first_raw_ohlc_touch']['minute']
    assert first_touch == start.isoformat()
    assert [touch['minute'] for touch in cases['arms']['B']['touches']
            if touch['order_id'] == next(key for key, value in cases['arms']['B']['orders'].items()
                                       if value is first)] == [start.isoformat()]
