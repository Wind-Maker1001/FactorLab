"""Predeclared windows and initial-order screening; no account execution."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from factorlab.strategies.grid_execution import GridAccount, GridExecution


WEEK = timedelta(days=7)
FIRST = datetime(2026, 5, 15, tzinfo=timezone.utc)


def candidate_windows():
    windows = []
    for index in range(11):
        start = FIRST + index * WEEK
        windows.append(dict(id=f'I{index + 1}' if index < 3 else f'C{index - 2}',
            sample='INDEPENDENT_HISTORICAL_WINDOW' if index < 3
                   else 'CONDITIONALLY_SELECTED_EXECUTION_CASE',
            start=start.isoformat(), end_exclusive=(start + WEEK).isoformat()))
    return windows


def screen_initial_orders(hourly, settings):
    """Use the real proposal engine, then inspect pre-fill initial-order lives."""
    config = settings['config']
    capital = settings['capital']
    grid = GridExecution(config, settings['rules'], deployment_id='research', account_id='screen',
        market=settings['market'], instrument_id=settings['instrument_id'], initial_equity=capital,
        fee_rate=settings['fee_rate'])
    account = GridAccount(Decimal(0), capital, capital, Decimal(0), Decimal(0),
                         'screen', settings['instrument_id'], settings['market'])
    seed = dict(hourly[0])
    seed['time'] = datetime.fromisoformat(seed['time']).replace(tzinfo=None).isoformat()
    seed['volume'] = int(seed['volume'])
    grid.observe(seed, account, delivery=1)
    orders = [dict(order_id=key, side=order.intent.side.value,
                   limit_price=order.intent.limit_price, quantity=order.intent.quantity,
                   grid_level=grid.levels[key]) for key, order in grid.orders.items()]
    opportunities = []
    inspected = 0
    for bar in hourly[1:1 + config.order_ttl]:
        inspected += 1
        for order in orders:
            if order['side'] == 'BUY' and bar['low'] <= order['limit_price']:
                opportunities.append(order | dict(hour=bar['time'], low=bar['low'], high=bar['high']))
        # A pending cancellation is confirmed after the final quote of this hour.
        if not Decimal(str(grid.prices[0])) <= bar['close'] <= Decimal(str(grid.prices[-1])):
            break
    return dict(anchor_time=hourly[0]['time'], anchor_close=hourly[0]['close'],
                initial_orders=orders, inspected_hours=inspected,
                potential_order_hours=len(opportunities),
                first_potential_touch=opportunities[0] if opportunities else None)
