"""Compact observations from a F04A Paper account before its store is removed."""
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import asdict
import json
import sqlite3

import pandas as pd

from factorlab.experiments.grid_paper import D
from factorlab.experiments.mc_grid_tail_convergence import price_digest
from factorlab.experiments.mc_grid_tail_f03_financial import capture_account_diagnostics
from factorlab.trading.contracts import OrderStatus, ZERO
from factorlab.trading.storage import decode


def _journal(db_path, start):
    submitted = {}
    fills = defaultdict(dict)
    previous = defaultdict(lambda: ZERO)
    earliest = None
    with closing(sqlite3.connect(db_path.as_uri()+'?mode=ro', uri=True)) as db:
        statement = "SELECT payload FROM events WHERE json_extract(payload, '$.type') = 'OrderEvent' ORDER BY rowid"
        for (payload,) in db.execute(statement):
            event = decode(json.loads(payload))
            hour = int((event.event_time - start).total_seconds() // 3600)
            key = event.client_order_id
            if event.status == OrderStatus.SUBMITTED:
                submitted[key] = hour
            if event.cumulative_filled_quantity > previous[key]:
                fills[key][hour] = event.cumulative_filled_quantity - previous[key]
                previous[key] = event.cumulative_filled_quantity
                if earliest is None:
                    earliest = hour
    return submitted, fills, earliest


def _limit_touches(prices, start, result, submitted, fills, step):
    def hour(time):
        return int((pd.Timestamp(time) - start).total_seconds() // 3600)

    cancellations = {event['order_id']: (hour(event['time']), event['cause'])
                     for event in result['lifecycle'] if event['status'] == 'CANCELED'}
    denials = {(event['order_id'], hour(event['time'])): event['kind']
               for event in result['risk']
               if event['kind'] in ('FILL_ADMISSION_DENIED', 'INSUFFICIENT_COLLATERAL')}
    slippage = D(result['execution']['slippage'])
    closes = [D(float(price)) for price in prices]
    buy_quotes = [close * (1 + slippage) for close in closes]
    sell_quotes = [close * (1 - slippage) for close in closes]
    capacity = D(result['execution']['synthetic_capacity'])
    budget = [capacity] * len(prices)
    unfilled = Counter()
    after_ttl = []
    # SUBMITTED journal order matches the order Paper uses to allocate quote capacity.
    for key, begin in submitted.items():
        order = result['orders'][key]
        intent = order['intent']
        trades = fills[key]
        for index, quantity in trades.items():
            budget[index] -= quantity
        if intent['order_type'] != 'LIMIT':
            continue
        end, cause = cancellations.get(key, (len(prices) - 1, None))
        if order['status'] == 'FILLED':
            end = max(trades)
        buy = intent['side'] == 'BUY'
        limit = D(intent['limit_price'])
        quotes = buy_quotes if buy else sell_quotes

        def touches(index):
            return quotes[index] <= limit if buy else quotes[index] >= limit

        remaining = D(intent['quantity'])
        for index in range(begin + 1, end + 1):
            if index in trades:
                remaining -= trades[index]
            elif touches(index):
                reason = denials.get((key, index))
                if reason is None:
                    reason = 'CAPACITY_OR_STEP' if min(remaining, budget[index]) < step else 'OTHER'
                unfilled[reason] += 1
        if cause == 'TTL':
            after = next((index for index in range(end + 1, len(prices)) if touches(index)), None)
            if after is not None:
                after_ttl.append(after)
    return unfilled, min(after_ttl, default=None)


def capture_ttl_account(row, nav, result, latent, prices, *, account, definition, config, rules):
    execution = result['execution']
    costs = definition['costs']
    if (D(execution['fee_rate']) != D(costs['fee_rate'])
            or D(execution['slippage']) != D(costs['slippage'])
            or D(execution['synthetic_capacity']) != D(definition['capacity'])
            or D(result['capital']) != D(config['initial_cash'])
            or result['config'] != config
            or {key: D(value) for key, value in result['rules'].items()} != asdict(rules)
            or result['funding'] != costs['funding']):
        raise ValueError('Paper replay differs from frozen capital, Grid rules or execution costs')

    capture_account_diagnostics(row, nav, result, latent, prices)
    start = pd.Timestamp(nav.date.iloc[0])
    submitted, fills, first_fill = _journal(account/'paper.sqlite', start)
    unfilled, first_touch = _limit_touches(prices, start, result, submitted, fills, rules.step_size)
    lifecycle = [event for event in result['lifecycle'] if event['status'] == 'CANCELED']
    rejected_submissions = {event['order_id'] for event in result['risk'] if event['kind'] == 'SUBMISSION_REJECTED'}
    jumps = [event for event in latent or () if 'step' in event]
    row.update(
        first_fill_hour=first_fill,
        order_submissions=len(submitted.keys() | rejected_submissions),
        order_cancellations=len(lifecycle),
        breakout_cancellations=sum(event['cause'] == 'BREAKOUT' for event in lifecycle),
        first_touch_after_ttl_hour=first_touch,
        active_limit_touch_unfilled_order_hours=sum(unfilled.values()),
        active_touch_capacity_blocked_order_hours=unfilled['CAPACITY_OR_STEP'],
        active_touch_collateral_denied_order_hours=unfilled['INSUFFICIENT_COLLATERAL'],
        active_touch_admission_denied_order_hours=unfilled['FILL_ADMISSION_DENIED'],
        active_touch_other_order_hours=unfilled['OTHER'],
        inventory_path_sha256=price_digest(nav.inventory.to_numpy()),
        terminal_inventory_value_fraction=float(nav.net_exposure.iloc[-1] / row['initial_capital']),
        committed_cash_mean=float(nav.committed.mean()),
        committed_cash_peak=float(nav.committed.max()),
        jump_events=len(jumps),
        jumps_with_prior_inventory=sum(event['inventory_before'] != 0 for event in jumps),
        fixed_jump_mark_effect=sum((event['fixed_jump_mark_effect'] for event in jumps), 0.),
    )
