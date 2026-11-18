"""Quote scheduling and price diagnostics for bounded hourly Paper research."""
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from factorlab.trading.contracts import OrderType, Side


def _touches(side, price, limit):
    return price <= limit if side == Side.BUY else price >= limit


class ResearchExecution:
    """An hourly capacity envelope shared by all eligible quotes in that hour."""

    def __init__(self, minutes_by_hour, capacity, *, diagnostic_minutes=None):
        self.minutes_by_hour = minutes_by_hour
        self.diagnostic_minutes = diagnostic_minutes
        self.capacity = Decimal(str(capacity))
        self.counts = Counter()
        self.events = []
        self.price_touches = []
        self.fill_references = {}
        self.missed_order_hours = set()
        self.missed_minutes = set()
        self.dual_side_minutes = set()
        self.nearest_limit = None

    def model(self):
        return dict(price='minute close' if self.minutes_by_hour is not None else 'later-bar close quote',
                    participation=None, synthetic_capacity=str(self.capacity),
                    capacity_reset='1h')

    def start_hour(self, bar, working_orders):
        self.hour = bar['time']
        if self.diagnostic_minutes is not None:
            self._diagnose(self.diagnostic_minutes[self.hour], working_orders, Decimal(str(bar['close'])))
        if self.minutes_by_hour is None:
            self.counts['quote_observations'] += 1
            return ()
        minutes = self.minutes_by_hour[self.hour]
        self.counts['quote_observations'] += len(minutes) or 1
        # The closing minute is handled by the hourly observation and matcher.
        return (
            (datetime.fromisoformat(minute['time']) + timedelta(minutes=1, microseconds=-1),
             Decimal(str(minute['close'])))
            for minute in minutes[:-1]
        )

    def _diagnose(self, minutes, working_orders, hourly_close):
        for minute in minutes:
            if not Decimal(str(minute['volume'])):
                self.counts['zero_volume_minutes'] += 1
                continue
            low, high = (Decimal(str(minute[key])) for key in ('low', 'high'))
            sides = set()
            for key, order in working_orders:
                intent = order.intent
                if intent.order_type != OrderType.LIMIT:
                    continue
                self.counts['eligible_limit_order_minutes'] += 1
                extreme = low if intent.side == Side.BUY else high
                distance = extreme - intent.limit_price if intent.side == Side.BUY else intent.limit_price - extreme
                if self.nearest_limit is None or abs(distance) < abs(self.nearest_limit['distance']):
                    self.nearest_limit = dict(order_id=key, minute=minute['time'], side=intent.side.value,
                        limit=intent.limit_price, observed_extreme=extreme, distance=distance)
                if not _touches(intent.side, extreme, intent.limit_price):
                    continue
                sides.add(intent.side)
                missed = not _touches(intent.side, hourly_close, intent.limit_price)
                if missed:
                    self.missed_order_hours.add((key, self.hour))
                    self.missed_minutes.add(minute['time'])
                    self.counts['missed_order_minutes'] += 1
                self.price_touches.append(dict(order_id=key, hour=self.hour, minute=minute['time'],
                    side=intent.side.value, limit=str(intent.limit_price), minute_extreme=str(extreme),
                    hourly_close=str(hourly_close), absent_at_hour_close=missed))
            if len(sides) == 2:
                self.dual_side_minutes.add(minute['time'])

    def observe_order(self, at, order, reference, budget, candidate, slippage, step):
        intent = order.intent
        self.counts['eligible_order_quotes'] += 1
        limit = intent.limit_price
        if intent.order_type == OrderType.LIMIT:
            if not _touches(intent.side, reference, limit):
                return
            self.counts['limit_touches'] += 1
        remaining = intent.quantity - order.cumulative_filled_quantity
        candidate_quantity = Decimal(0)
        if candidate is not None:
            candidate_quantity, _ = candidate
            self.counts['candidates'] += 1
            status = 'CANDIDATE'
        elif limit is not None and not _touches(
                intent.side, reference * (1 + (slippage if intent.side == Side.BUY else -slippage)), limit):
            self.counts['slippage_blocked_quotes'] += 1
            self.counts['slippage_blocked_quote_quantity'] += remaining
            status = 'SLIPPAGE'
        elif budget < step:
            self.counts['capacity_blocked_quotes'] += 1
            status = 'CAPACITY'
        else:
            status = 'BELOW_STEP'
        if status != 'SLIPPAGE':
            self.counts['capacity_blocked_quote_quantity'] += remaining - candidate_quantity
        self.events.append(dict(hour=self.hour, time=at.isoformat(), order_id=intent.client_order_id,
            side=intent.side.value, close=str(reference), limit=str(limit) if limit is not None else None,
            model=status, remaining=str(remaining), candidate_quantity=str(candidate_quantity)))

    def record_fill(self, event, price_reference):
        self.fill_references[event.event_id] = price_reference

    def summary(self):
        return {
            **{name: self.counts[name] for name in ('quote_observations', 'eligible_order_quotes',
                'limit_touches', 'candidates', 'slippage_blocked_quotes', 'capacity_blocked_quotes',
                'slippage_blocked_quote_quantity', 'capacity_blocked_quote_quantity')},
            'ohlc_order_minute_touches': len(self.price_touches),
            'missed_order_minutes': self.counts['missed_order_minutes'],
            'missed_order_hours': len(self.missed_order_hours),
            'distinct_minutes_with_missed_touches': len(self.missed_minutes),
            'distinct_orders_with_missed_touches': len({key for key, _ in self.missed_order_hours}),
            'dual_side_minutes': len(self.dual_side_minutes),
            'zero_volume_minutes': self.counts['zero_volume_minutes'],
            'eligible_limit_order_minutes': self.counts['eligible_limit_order_minutes'],
            'nearest_limit': self.nearest_limit,
        }
