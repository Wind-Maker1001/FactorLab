"""Eligible real-price evidence for the bounded F04C execution case study."""
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from factorlab.experiments.grid_execution_audit import pair_orders
from factorlab.trading.contracts import (
    Market, OrderIntent, OrderSnapshot, OrderStatus, OrderType, Side, TERMINAL,
)
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.state import OrderRecord


ZERO = Decimal(0)
CAPITAL = Decimal(1000)
FEE_RATE = Decimal('.0004')
SYMBOL = 'NVDAUSDT'
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
MICROSECOND = timedelta(microseconds=1)
FIRST_FIELDS = dict(raw_ohlc_touch='first_raw_ohlc_touch',
    slippage_ohlc_touch='first_slippage_ohlc_touch', raw_close_touch='first_raw_close',
    slippage_close_touch='first_slippage_close',
    model_price_eligible_quote='first_model_price_eligible_quote')


def _decimal(value):
    return Decimal(str(value))


def _touches(side, price, limit):
    return price <= limit if side == 'BUY' else price >= limit


def _minute_evidence(minute, quote_time):
    return dict(minute=minute['time'], quote_time=quote_time.isoformat(),
        open=minute['open'], high=minute['high'], low=minute['low'], close=minute['close'],
        volume=minute['volume'], trade_count=minute['trade_count'],
        taker_buy_volume=minute['taker_buy_volume'],
        taker_buy_quote_volume=minute['taker_buy_quote_volume'])


def _enrich_orders(account, arm, minutes, end, slippage):
    orders = {}
    touches = []
    counts = Counter()
    for order_id, original in account['orders'].items():
        submitted = datetime.fromisoformat(original['submitted_at'])
        effective = submitted + MICROSECOND
        terminal = [datetime.fromisoformat(event['event_time']) for event in original['events']
                    if OrderStatus(event['status']) in TERMINAL]
        until = terminal[0] if terminal else end
        order = original | dict(effective_at=effective.isoformat(), eligible_until=until.isoformat(),
            eligibility='minute opens at or after effective_at; quote at or before terminal event',
            first_raw_ohlc_touch=None, first_slippage_ohlc_touch=None, first_raw_close=None,
            first_slippage_close=None, first_model_price_eligible_quote=None,
            confirmed_fills=[event for event in original['events'] if event['fill_price'] is not None])
        limit = _decimal(original['limit_price']) if original['order_type'] == 'LIMIT' else None
        side = original['side']
        factor = 1 + slippage if side == 'BUY' else 1 - slippage
        for opened, quote_time, minute in minutes:
            if opened < effective or quote_time > until:
                continue
            counts['eligible_order_minutes'] += 1
            close = _decimal(minute['close'])
            has_model_quote = arm == 'B' or opened.minute == 59
            if limit is None:
                raw_close = slipped_close = True
                raw_extreme = slipped_extreme = False
            else:
                extreme = _decimal(minute['low'] if side == 'BUY' else minute['high'])
                has_trades = _decimal(minute['volume']) > ZERO
                raw_extreme = has_trades and _touches(side, extreme, limit)
                slipped_extreme = has_trades and _touches(side, extreme * factor, limit)
                raw_close = _touches(side, close, limit)
                slipped_close = _touches(side, close * factor, limit)
            observations = dict(raw_ohlc_touch=raw_extreme, slippage_ohlc_touch=slipped_extreme,
                raw_close_touch=limit is not None and raw_close,
                slippage_close_touch=limit is not None and slipped_close,
                model_price_eligible_quote=has_model_quote and slipped_close)
            for observation, occurred in observations.items():
                if occurred:
                    counts[observation + '_minutes'] += 1
                    field = FIRST_FIELDS[observation]
                    if order[field] is None:
                        order[field] = _minute_evidence(minute, quote_time)
            if raw_extreme:
                touches.append(dict(order_id=order_id, side=side, limit=limit,
                    **_minute_evidence(minute, quote_time), **observations))
        orders[order_id] = order
    for order_id, order in orders.items():
        opposite_side = 'SELL' if order['side'] == 'BUY' else 'BUY'
        opposite_level = order['grid_level'] + (1 if order['side'] == 'BUY' else -1)
        fill_hour_closes = {
            (datetime.fromisoformat(event['event_time']).replace(minute=0, second=0, microsecond=0)
             + HOUR - MICROSECOND).isoformat()
            for event in order['confirmed_fills']
        }
        order['reverse_orders'] = [dict(order_id=other_id, submitted_at=other['submitted_at'],
            side=other['side'], grid_level=other['grid_level'], quantity=other['quantity'],
            linkage='opposite adjacent grid level submitted at the fill hour decision')
            for other_id, other in orders.items()
            if order['grid_level'] >= 0 and other_id != order_id
            and other['side'] == opposite_side and other['grid_level'] == opposite_level
            and other['submitted_at'] in fill_hour_closes]
    counts['touched_orders'] = sum(order['first_raw_ohlc_touch'] is not None for order in orders.values())
    counts['filled_orders'] = sum(bool(order['confirmed_fills']) for order in orders.values())
    return orders, touches, dict(counts)


def _intent(order_id, order):
    limit = order['limit_price']
    return OrderIntent(Market.BINANCE_USDM, SYMBOL, Side(order['side']),
        OrderType(order['order_type']), _decimal(order['quantity']),
        _decimal(limit) if limit is not None else None, order_id,
        reduce_only=order['reduce_only'])


def _balances(broker, at, mark, fees):
    broker.clock = lambda: at
    asset = broker.read_account().assets[0]
    position = broker.state.positions.get(SYMBOL)
    inventory = position.quantity if position else ZERO
    entry = position.entry_price if position else ZERO
    unrealized = inventory * (mark - entry)
    equity = asset.total + unrealized
    working = []
    for order_id, record in broker.state.orders.items():
        order = record.snapshot
        if order.status in TERMINAL:
            continue
        working.append(dict(order_id=order_id, side=order.intent.side.value,
            status=order.status.value, limit_price=order.intent.limit_price,
            quantity=order.intent.quantity, cumulative_filled_quantity=order.cumulative_filled_quantity,
            remaining_quantity=order.intent.quantity - order.cumulative_filled_quantity,
            reservation_price=record.reservation_price))
    return dict(time=at.isoformat(), wallet_cash=asset.total, reserved_collateral=asset.reserved,
        available_wallet_cash=asset.available, inventory=inventory, entry_price=entry,
        mark_price=mark, unrealized=unrealized, equity=equity, nav=equity / CAPITAL,
        cumulative_fees=fees, working_orders=working)


def _replay_receipts(account, minute_index, hourly_index, start, end, target):
    """Reconstruct journal balances with the existing USD-M posting and reservations."""
    broker = PaperBroker(Market.BINANCE_USDM, 'execution-case-evidence',
        (Instrument(SYMBOL, 'USDT'),), {'USDT': CAPITAL}, fee_rate=FEE_RATE)
    intents = {order_id: _intent(order_id, order) for order_id, order in account['orders'].items()}
    events = sorted((event | dict(order_id=order_id) for order_id, order in account['orders'].items()
                     for event in order['events']), key=lambda event: event['journal_cursor'])

    def reference(at):
        if at < start:
            row = hourly_index[at.replace(minute=0, second=0, microsecond=0).isoformat()]
        else:
            row = minute_index[at.replace(second=0, microsecond=0).isoformat()]
        return _decimal(row['close'])

    path = []
    fees = ZERO
    before_target = after_target = None
    target_mark = reference(target) if target is not None else None
    for event in events:
        at = datetime.fromisoformat(event['event_time'])
        if target is not None:
            if before_target is None and at >= target:
                before_target = _balances(broker, target, target_mark, fees)
            if after_target is None and at > target:
                after_target = _balances(broker, target, target_mark, fees)
        mark = reference(at)
        broker.state.quotes[SYMBOL] = Quote(SYMBOL, mark, at)
        order_id = event['order_id']
        intent = intents[order_id]
        previous = broker.state.orders.get(order_id)
        cumulative_quantity = previous.snapshot.cumulative_filled_quantity if previous else ZERO
        cumulative_fee = previous.snapshot.cumulative_fee if previous else ZERO
        reservation = previous.reservation_price if previous else (
            intent.limit_price if intent.limit_price is not None else mark)
        quantity = _decimal(event['filled_quantity'])
        fee = _decimal(event['fee'])
        if event['fill_price'] is not None:
            before = _balances(broker, at, mark, fees)
            # Native posting keeps the evidence aligned with Paper's USD-M wallet semantics.
            broker._account_fill(intent, quantity, _decimal(event['fill_price']), fee)
        fees += fee
        snapshot = OrderSnapshot(intent, OrderStatus(event['status']),
            cumulative_quantity + quantity, cumulative_fee + fee)
        broker.state.orders[order_id] = OrderRecord(snapshot, at, reservation, snapshot.status)
        if event['fill_price'] is not None:
            path.append(event | dict(side=intent.side.value,
                cumulative_filled_quantity=snapshot.cumulative_filled_quantity,
                cumulative_fee=snapshot.cumulative_fee,
                before=before, after=_balances(broker, at, mark, fees)))
    if target is not None:
        if before_target is None:
            before_target = _balances(broker, target, target_mark, fees)
        if after_target is None:
            after_target = _balances(broker, target, target_mark, fees)
    terminal_time = end - MICROSECOND
    terminal = _balances(broker, terminal_time, reference(terminal_time), fees)
    position_collateral = terminal['inventory'] * terminal['entry_price']
    terminal_account = dict(cash=terminal['wallet_cash'], inventory=terminal['inventory'],
        equity=terminal['equity'], nav=terminal['nav'], entry_price=terminal['entry_price'],
        mark=terminal['mark_price'], pending_reservations=terminal['reserved_collateral'] - position_collateral,
        position_collateral=position_collateral, committed=terminal['reserved_collateral'],
        available=terminal['available_wallet_cash'], unrealized=terminal['unrealized'],
        cumulative_fees=fees, working_orders=terminal['working_orders'])
    return path, dict(before=before_target, after=after_target), terminal_account


def extract_execution_cases(accounts, market_data, slippage):
    """Separate eligible real touches, model price observations and Paper confirmations.

    Minute extrema remain unordered diagnostics. Terminal confirmation timestamps
    bound each order's diagnostic lifespan, including its final executable quote.
    The account path reuses native Paper posting and collateral projection.
    """
    slippage = _decimal(slippage)
    start = datetime.fromisoformat(market_data.evidence['start'])
    end = datetime.fromisoformat(market_data.evidence['end_exclusive'])
    minute_index = {row['time']: row for row in market_data.minutes}
    hourly_index = {row['time']: row for row in market_data.hourly}
    minutes = [(datetime.fromisoformat(row['time']),
                datetime.fromisoformat(row['time']) + MINUTE - MICROSECOND, row)
               for row in market_data.minutes]
    paired = pair_orders(accounts['A'], accounts['B'])
    first = paired['first_differing_event']
    target = datetime.fromisoformat(first['event_time']) if first is not None else None
    arms = {}
    divergence_accounts = {}
    for arm, account in accounts.items():
        orders, touches, summary = _enrich_orders(account, arm, minutes, end, slippage)
        path, states, terminal = _replay_receipts(account, minute_index, hourly_index, start, end, target)
        arms[arm] = dict(orders=orders, touches=touches, first_fill_path=path,
                         summary=summary, terminal_account=terminal)
        divergence_accounts[arm] = states
    divergence = None
    if first is not None:
        hour = target.replace(minute=0, second=0, microsecond=0).isoformat()
        minute = target.replace(second=0, microsecond=0).isoformat()
        divergence = dict(event=first, minute_bar=minute_index[minute], hour_bar=hourly_index[hour],
            accounts=divergence_accounts,
            snapshot_boundary='before excludes all journal events at this timestamp; after includes all',
            cause='B observes completed intrahour minute closes while A waits for the hour close; '
                  'hourly strategy decisions and the hourly capacity envelope are unchanged')
    return dict(arms=arms, paired_orders=paired, first_divergence=divergence,
        evidence_classes=dict(real_price='positive-volume archived minute OHLC touches an effective limit',
            model_price='completed close satisfies the adverse-slippage limit; capacity and posting are separate',
            paper_fill='authoritative Paper journal confirms an incremental quantity and fee',
            exchange_fill='REAL_FILL_NOT_VERIFIED'),
        diagnostic_boundary='OHLC touch counts exclude minutes before activation and after terminal confirmation; '
                            'an extreme does not determine intraminute order or exchange queue position')
