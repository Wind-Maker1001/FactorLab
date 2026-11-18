"""Reconcile the paired execution study against the Paper event journal."""
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta
from itertools import zip_longest
import json
from pathlib import Path

from factorlab.experiments.grid_fifo import D, attribute_events
from factorlab.trading.contracts import OrderStatus, Side, TERMINAL, ZERO
from factorlab.trading.storage import PaperStore


HOUR = timedelta(hours=1)
MICROSECOND = timedelta(microseconds=1)
EXPORT_TOLERANCE = D('0.00000001')


def _read_csv(path):
    with path.open(newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def _order_receipts(state, events, result, references):
    orders = {}
    fills = []
    fill_events = []
    filled = defaultdict(lambda: ZERO)
    fees = defaultdict(lambda: ZERO)
    for order_id, record in state.orders.items():
        snapshot = record.snapshot
        intent = snapshot.intent
        details = result['orders'][order_id]
        orders[order_id] = dict(side=intent.side.value, order_type=intent.order_type.value,
            quantity=intent.quantity, limit_price=intent.limit_price, reduce_only=intent.reduce_only,
            grid_level=details['grid_level'], purpose=details['purpose'],
            final_status=snapshot.status.value, filled_quantity=snapshot.cumulative_filled_quantity,
            cumulative_fee=snapshot.cumulative_fee,
            remaining_quantity=intent.quantity - snapshot.cumulative_filled_quantity, events=[])
    for cursor, event in events:
        order_id = event.client_order_id
        quantity = event.cumulative_filled_quantity - filled[order_id]
        fee = event.cumulative_fee - fees[order_id]
        filled[order_id] = event.cumulative_filled_quantity
        fees[order_id] = event.cumulative_fee
        order = orders[order_id]
        time = event.event_time.isoformat()
        receipt = dict(journal_cursor=cursor, event_id=event.event_id, event_time=time,
            status=event.status.value, filled_quantity=quantity, fill_price=event.fill_price, fee=fee)
        order['events'].append(receipt)
        if event.fill_price is not None:
            reference = references[event.event_id]
            receipt['reference_price'] = reference
            signed = quantity if order['side'] == Side.BUY else -quantity
            fills.append(dict(time=event.event_time, quantity=quantity, inventory_delta=signed,
                event_cash_flow=-signed * event.fill_price - fee, notional=quantity * event.fill_price,
                fee=fee, slippage=quantity * abs(event.fill_price - reference), status=event.status))
            fill_events.append(event)
    for order_id, order in orders.items():
        order['submitted_at'] = order['events'][0]['event_time']
        if (order['events'][-1]['status'], filled[order_id], fees[order_id]) != (
                order['final_status'], order['filled_quantity'], order['cumulative_fee']):
            raise ValueError(f'Paper journal disagrees with order snapshot for {order_id}')
    return orders, fills, fill_events


def _account_path(rows, fills, start, end, capital):
    hourly = defaultdict(list)
    inventory = cash_flow = held_us = inventory_us = max_inventory = ZERO
    cursor = start
    for fill in fills:
        time = fill['time']
        if not cursor <= time < end:
            raise ValueError('Paper fill falls outside the ordered study window')
        elapsed_us = (time - cursor) // MICROSECOND
        held_us += elapsed_us if inventory else 0
        inventory_us += inventory * elapsed_us
        inventory += fill['inventory_delta']
        max_inventory = max(max_inventory, inventory)
        cursor = time
        hourly[time.replace(minute=0, second=0, microsecond=0)].append(fill)
    elapsed_us = (end - cursor) // MICROSECOND
    held_us += elapsed_us if inventory else 0
    inventory_us += inventory * elapsed_us
    holding = dict(holding_hours=held_us / (HOUR // MICROSECOND),
        average_inventory=inventory_us / ((end - start) // MICROSECOND), max_inventory=max_inventory)

    residuals = dict(hourly_equity=ZERO, hourly_inventory=ZERO, hourly_cash=ZERO,
        hourly_fees=ZERO, hourly_slippage=ZERO, hourly_drawdown=ZERO)
    inventory = ZERO
    peak = capital
    max_drawdown = ZERO
    for row in rows:
        time = datetime.fromisoformat(row['date'])
        hour_fills = hourly[time]
        cash_flow += sum((fill['event_cash_flow'] for fill in hour_fills), ZERO)
        inventory += sum((fill['inventory_delta'] for fill in hour_fills), ZERO)
        equity = capital + cash_flow + inventory * D(row['close'])
        peak = max(peak, equity)
        drawdown = 1 - equity / peak
        max_drawdown = max(max_drawdown, drawdown)
        deltas = dict(hourly_equity=D(row['equity']) - equity,
            hourly_inventory=D(row['inventory']) - inventory,
            hourly_cash=D(row['cash']) + D(row['unrealized']) - equity,
            hourly_fees=D(row['fee']) - sum((fill['fee'] for fill in hour_fills), ZERO),
            hourly_slippage=D(row['slippage']) - sum((fill['slippage'] for fill in hour_fills), ZERO),
            hourly_drawdown=D(row['drawdown']) - drawdown)
        for name, delta in deltas.items():
            residuals[name] = max(residuals[name], abs(delta))
    return holding | dict(max_drawdown=max_drawdown), residuals


def audit_account(directory, *, start, end, fill_references):
    """Audit finished USD-M Paper exports and report Decimal-valued economics.

    The journal prices and quantities determine the economic cross-check,
    while archived close quotes supply each event's slippage reference.
    The in-window marks have the same hourly clock for both study accounts.
    """
    directory = Path(directory)
    result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
    rows = _read_csv(directory / 'daily.csv')
    trades = _read_csv(directory / 'trades.csv')
    store = PaperStore(directory / 'paper.sqlite')
    try:
        state = store.load()
        events = list(store.order_events())
    finally:
        store.close()

    orders, fills, fill_events = _order_receipts(state, events, result, fill_references)
    references = {event.event_time: fill_references[event.event_id] for event in fill_events}
    fill_rows = [row | dict(execution_date=row['event_time']) for row in trades]
    fifo = attribute_events(result, fill_rows, references, fill_events, state)
    summary = fifo['summary']

    rows = [row for row in rows if start <= datetime.fromisoformat(row['date']) < end]
    capital = D(result['capital'])
    holding, residuals = _account_path(rows, fills, start, end, capital)
    position = state.positions.get(result['instrument_id'])
    inventory = position.quantity if position else ZERO
    unrealized = position.quantity * (position.mark_price - position.entry_price) if position else ZERO
    cash = state.cash['USDT']
    residuals.update(fifo_nav=abs(summary['reconciliation_residual']),
        fifo_terminal_mark=abs(summary['valuation_adjustment']),
        fifo_inventory=abs(summary['residual_inventory'] - inventory),
        paper_terminal_nav=abs(D(rows[-1]['equity']) - summary['final_equity']),
        paper_terminal_cash=abs(D(rows[-1]['cash']) - cash),
        paper_cash_from_fifo=abs(capital + summary['economic_net'] - unrealized - cash),
        paper_fees=abs(summary['actual_fees'] - sum(
            (order['cumulative_fee'] for order in orders.values()), ZERO)))
    failures = {name: value for name, value in residuals.items() if value > EXPORT_TOLERANCE}
    if failures:
        raise ValueError(f'Paper execution audit reconciliation failed: {failures}')

    committed = [D(row['committed']) for row in rows]
    exposure = [D(row['net_exposure']) for row in rows]
    filled_orders = sum(order['filled_quantity'] > ZERO for order in orders.values())
    open_orders = {key: order['remaining_quantity'] for key, order in orders.items()
                   if OrderStatus(order['final_status']) not in TERMINAL}
    metrics = dict(**holding,
        fills=len(fills), filled_orders=filled_orders, submitted_orders=len(orders),
        fill_rate=D(filled_orders) / len(orders) if orders else ZERO,
        quantity=sum((fill['quantity'] for fill in fills), ZERO),
        notional=sum((fill['notional'] for fill in fills), ZERO),
        first_fill=fills[0]['time'].isoformat() if fills else None,
        partial_fill_events=sum(fill['status'] == OrderStatus.PARTIALLY_FILLED for fill in fills),
        terminal_partial_orders=sum(order['final_status'] == OrderStatus.PARTIALLY_FILLED
                                    for order in orders.values()),
        ttl_cancellations=sum(event['cause'] == 'TTL' and event['status'] == OrderStatus.CANCELED
                              for event in result['lifecycle']),
        cancellations=sum(order['final_status'] == OrderStatus.CANCELED for order in orders.values()),
        submission_rejections=sum(event['kind'] == 'SUBMISSION_REJECTED' for event in result['risk']),
        rejected_orders=sum(order['final_status'] == OrderStatus.REJECTED for order in orders.values()),
        terminal_open_orders=open_orders, terminal_cash=cash, terminal_inventory=inventory,
        terminal_equity=summary['final_equity'], terminal_nav=summary['final_nav'],
        fifo_realized_net=summary['normal_sell']['net'] + summary['risk_exit']['net'],
        fifo_unrealized_net=summary['residual']['net'], paper_unrealized=unrealized,
        fees=summary['actual_fees'], slippage=summary['actual_slippage'],
        average_hourly_committed=sum(committed, ZERO) / len(committed),
        max_hourly_committed=max(committed),
        average_hourly_utilization=sum(committed, ZERO) / len(committed) / capital,
        max_hourly_utilization=max(committed) / capital,
        average_hourly_inventory_notional=sum(exposure, ZERO) / len(exposure),
        max_hourly_inventory_notional=max(exposure))
    return dict(metrics=metrics, fifo=fifo, orders=orders, lifecycle=result['lifecycle'],
        risk=result['risk'], reconciliation=dict(status='PASS', tolerance=EXPORT_TOLERANCE,
                                                maximum_absolute_residual=residuals))


GENESIS_FIELDS = ('submitted_at', 'side', 'grid_level', 'limit_price', 'quantity', 'purpose',
                  'order_type', 'reduce_only')
EVENT_FIELDS = ('event_time', 'status', 'filled_quantity', 'fill_price', 'fee')


def _index_orders(orders):
    indexed = {}
    counts = Counter()
    submission_order = sorted(orders.items(), key=lambda item: item[1]['events'][0]['journal_cursor'])
    for order_id, order in submission_order:
        genesis = tuple(order[field] for field in GENESIS_FIELDS)
        counts[genesis] += 1
        indexed[(*genesis, counts[genesis])] = (order_id, order)
    return indexed


def _event_key(event):
    return tuple(event[field] for field in EVENT_FIELDS) if event is not None else None


def _first_difference(hourly_events, minute_events):
    for hourly, minute in zip_longest(hourly_events, minute_events):
        if _event_key(hourly) == _event_key(minute):
            continue
        time = min(datetime.fromisoformat(event['event_time'])
                   for event in (hourly, minute) if event is not None)
        return dict(event_time=time.isoformat(), hourly=hourly, minute=minute)
    return None


def _compare_order(genesis, hourly, minute):
    hourly_id, hourly_order = hourly
    minute_id, minute_order = minute
    hourly_events = hourly_order['events'] if hourly_order is not None else ()
    minute_events = minute_order['events'] if minute_order is not None else ()
    return dict(identity=dict(zip((*GENESIS_FIELDS, 'ordinal'), genesis)),
        hourly_order_id=hourly_id, minute_order_id=minute_id,
        hourly_final_status=hourly_order['final_status'] if hourly_order is not None else None,
        minute_final_status=minute_order['final_status'] if minute_order is not None else None,
        hourly_filled_quantity=hourly_order['filled_quantity'] if hourly_order is not None else None,
        minute_filled_quantity=minute_order['filled_quantity'] if minute_order is not None else None,
        first_differing_event=_first_difference(hourly_events, minute_events),
        first_differing_fill=_first_difference(
            (event for event in hourly_events if event['fill_price'] is not None),
            (event for event in minute_events if event['fill_price'] is not None)))


def _earliest(differences, kind):
    candidates = [diff | diff[kind] for diff in differences if diff[kind] is not None]
    first = min(candidates, key=lambda diff: datetime.fromisoformat(diff['event_time']), default=None)
    if first is not None:
        del first['first_differing_event'], first['first_differing_fill']
    return first


def pair_orders(hourly_account, minute_account):
    """Match accepted proposals by their exact genesis and compare journal paths."""
    hourly = _index_orders(hourly_account['orders'])
    minute = _index_orders(minute_account['orders'])
    matched = []
    hourly_only = []
    minute_only = []
    matched_count = 0
    missing = (None, None)
    for genesis in dict.fromkeys((*hourly, *minute)):
        diff = _compare_order(genesis, hourly.get(genesis, missing), minute.get(genesis, missing))
        if genesis not in hourly:
            minute_only.append(diff)
        elif genesis not in minute:
            hourly_only.append(diff)
        else:
            matched_count += 1
            if diff['first_differing_event'] is not None:
                matched.append(diff)
    matched_fill_differences = sum(diff['first_differing_fill'] is not None for diff in matched)
    hourly_only_fills = sum(diff['first_differing_fill'] is not None for diff in hourly_only)
    minute_only_fills = sum(diff['first_differing_fill'] is not None for diff in minute_only)
    differences = [*matched, *hourly_only, *minute_only]
    return dict(matched_orders=matched_count, matched_orders_with_different_fills=matched_fill_differences,
        matched_orders_with_different_lifecycle=len(matched),
        matched_orders_with_different_final_status=sum(
            diff['hourly_final_status'] != diff['minute_final_status'] for diff in matched),
        hourly_only_orders=len(hourly_only), minute_only_orders=len(minute_only),
        hourly_only_filled_orders=hourly_only_fills, minute_only_filled_orders=minute_only_fills,
        order_trajectories_with_different_fills=matched_fill_differences + hourly_only_fills + minute_only_fills,
        first_differing_event=_earliest(differences, 'first_differing_event'),
        first_differing_fill=_earliest(differences, 'first_differing_fill'),
        matched_differences=matched, hourly_only=hourly_only, minute_only=minute_only)
