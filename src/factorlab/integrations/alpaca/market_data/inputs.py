"""Client-independent, instrument-keyed Alpaca inputs with causal availability.

Feed and adjustment are caller-supplied provenance, never substituted. Daily
labels are New York civil dates; event timestamps retain their original value.
Opening observations are first eligible RTH bars/trades, not auction prices.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping

import pandas as pd

from factorlab.cross_market.contracts import ContractError, instant, session_date
from factorlab.trading.strategy import ClosedBar
from .sessions import AlpacaSessionCalendar


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    symbol: str
    asof: pd.Timestamp
    listing_date: pd.Timestamp | None = None

    def __post_init__(self):
        if not self.instrument_id or not self.symbol:
            raise ValueError('instrument identity required')
        object.__setattr__(self, 'asof', instant(self.asof))
        if self.listing_date is not None:
            object.__setattr__(self, 'listing_date', session_date(self.listing_date))


def normalize_bars(rows: Iterable[Mapping], instrument: Instrument, *,
                   calendar: AlpacaSessionCalendar, timeframe: str, received_at,
                   feed: str, adjustment: str = 'raw', source: str = 'alpaca') -> pd.DataFrame:
    """Normalize raw vendor OHLCV rows; adjusted responses use adjusted_* columns."""
    if timeframe not in {'1Day', '1Min'}:
        raise ValueError('timeframe must be 1Day or 1Min')
    if adjustment not in {'raw', 'split', 'dividend', 'spin-off', 'all'} or not feed or not source:
        raise ValueError('explicit adjustment, source and feed required')
    received = instant(received_at)
    records = []
    prefix = '' if adjustment == 'raw' else 'adjusted_'
    for row in rows:
        if row.get('symbol', row.get('S', instrument.symbol)) != instrument.symbol:
            raise ValueError('bar instrument mismatch')
        received = instant(row.get('received_at', received_at))
        original = row['t']
        event = instant(original)
        day = event.tz_convert(calendar.timezone).tz_localize(None).normalize()
        calendar.sessions(day, day)
        if day not in calendar.schedule.index:
            raise ContractError('bar on unscheduled session')
        opened = calendar.at(day, 'open') if timeframe == '1Day' else event
        closed = calendar.at(day, 'close') if timeframe == '1Day' else event + pd.Timedelta(minutes=1)
        record = dict(instrument_id=instrument.instrument_id, symbol=instrument.symbol,
                      identity_asof=instrument.asof, session=day, event_at=event,
                      original_event_at=original, received_at=received, opened_at=opened,
                      closed_at=closed, source=source, feed=feed, adjustment=adjustment,
                      timeframe=timeframe, complete=bool(row.get('complete', True)) and closed <= received)
        values = [Decimal(str(row[key])) for key in ('o', 'h', 'l', 'c', 'v')]
        o, h, l, c, v = values
        if (not all(x.is_finite() for x in values) or min(o, h, l, c) <= 0 or v < 0
                or h < max(o, l, c) or l > min(o, h, c)):
            raise ValueError('invalid source OHLCV')
        record.update({prefix + name: row[key] for name, key in
                       [('open', 'o'), ('high', 'h'), ('low', 'l'), ('close', 'c'), ('volume', 'v')]})
        records.append(record)
    columns = ['instrument_id', 'symbol', 'identity_asof', 'session', 'event_at',
               'original_event_at', 'received_at', 'opened_at', 'closed_at', 'source',
               'feed', 'adjustment', 'timeframe', 'complete'] + [prefix + x for x in
                                                              ('open', 'high', 'low', 'close', 'volume')]
    frame = pd.DataFrame(records, columns=columns).sort_values(['event_at', 'instrument_id']).reset_index(drop=True)
    frame['date'] = frame.session
    if adjustment == 'raw':
        for name in ('open', 'high', 'low', 'close', 'volume'):
            frame['raw_' + name] = frame[name]
    return frame


def completed_daily_matrix(bars: pd.DataFrame, *, now, value: str = 'close') -> pd.DataFrame:
    """Pivot completed, already received prior-session daily bars by stable ID."""
    now = instant(now)
    today = now.tz_convert('America/New_York').tz_localize(None).normalize()
    eligible = bars.loc[(bars.timeframe == '1Day') & bars.complete &
                        (bars.session < today) & (bars.closed_at <= now) &
                        (bars.event_at <= now) & (bars.received_at <= now)]
    return eligible.pivot(index='session', columns='instrument_id', values=value).sort_index()


def closed_bars(bars: pd.DataFrame, *, now) -> list[ClosedBar]:
    """Convert completed raw bars to the existing trading contract."""
    now = instant(now)
    result = []
    for row in bars.itertuples(index=False):
        if row.adjustment != 'raw':
            raise ValueError('ClosedBar requires raw executable prices')
        if not row.complete or max(row.closed_at, row.received_at, row.event_at) > now:
            continue
        result.append(ClosedBar(row.symbol, row.opened_at.to_pydatetime(), row.closed_at.to_pydatetime(),
                                *(Decimal(str(getattr(row, key))) for key in ('open', 'high', 'low', 'close', 'volume')),
                                int((row.closed_at - row.opened_at).total_seconds()), row.source))
    return result


@dataclass(frozen=True)
class OpeningObservation:
    instrument: Instrument
    price: Decimal
    event_at: pd.Timestamp
    received_at: pd.Timestamp
    session: pd.Timestamp
    source: str
    feed: str
    kind: str


def opening_observation(rows: Iterable[Mapping], instrument: Instrument, *,
                        calendar: AlpacaSessionCalendar, session, received_at,
                        now, feed: str, kind: str = 'trade', source: str = 'alpaca') -> OpeningObservation | None:
    """Select first observed eligible RTH trade or completed minute bar."""
    if kind not in {'trade', 'bar'}:
        raise ValueError('opening kind must be trade or bar')
    day, received, now = session_date(session), instant(received_at), instant(now)
    calendar.sessions(day, day)
    opened, closed = calendar.at(day, 'open'), calendar.at(day, 'close')
    candidates = []
    for row in rows:
        if row.get('symbol', row.get('S', instrument.symbol)) != instrument.symbol:
            raise ValueError('opening instrument mismatch')
        event = instant(row['t'])
        available = event + pd.Timedelta(minutes=1) if kind == 'bar' else event
        if (opened <= event < closed and available <= min(received, now, closed) and received <= now
                and row.get('complete', True)):
            candidates.append((event, row))
    if not candidates:
        return None
    event, row = min(candidates, key=lambda pair: pair[0])
    return OpeningObservation(instrument, Decimal(str(row['p' if kind == 'trade' else 'o'])),
                              event, received, day, source, feed, kind)


@dataclass(frozen=True)
class InstrumentQuote:
    instrument: Instrument
    bid: Decimal
    ask: Decimal
    event_at: pd.Timestamp
    received_at: pd.Timestamp
    source: str
    feed: str
    session: pd.Timestamp | None
    decision_window: tuple[pd.Timestamp, pd.Timestamp]
    validity_reasons: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.validity_reasons


def execution_quote(row: Mapping, instrument: Instrument, *, calendar: AlpacaSessionCalendar,
                    received_at, now, feed: str, required_feed: str,
                    decision_window: tuple, opening: OpeningObservation | None = None,
                    max_age=pd.Timedelta(seconds=30), source: str = 'alpaca',
                    decision_at=None) -> InstrumentQuote:
    if row.get('symbol', row.get('S', instrument.symbol)) != instrument.symbol:
        raise ValueError('quote instrument mismatch')
    event, received, now = instant(row['t']), instant(received_at), instant(now)
    start, end = map(instant, decision_window)
    if start >= end:
        raise ValueError('invalid decision window')
    bid, ask = Decimal(str(row['bp'])), Decimal(str(row['ap']))
    session = calendar.regular_session(event)
    reasons = []
    if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
        reasons.append('invalid_spread')
    if feed != required_feed:
        reasons.append('feed_mismatch')
    if event > now or received > now:
        reasons.append('future_timestamp')
    if received < event:
        reasons.append('receipt_before_event')
    if now - event > pd.Timedelta(max_age):
        reasons.append('stale_event')
    if session is None:
        reasons.append('outside_regular_session')
    if not (start <= event < end and start <= received < end and start <= now < end):
        reasons.append('outside_decision_window')
    if decision_at is not None and (event <= instant(decision_at) or received <= instant(decision_at)):
        reasons.append('not_after_decision')
    if opening is not None:
        if opening.instrument != instrument or opening.session != session:
            reasons.append('opening_identity_mismatch')
        if opening.feed != feed or opening.source != source:
            reasons.append('opening_provenance_mismatch')
        if event <= max(opening.event_at, opening.received_at) or received <= max(opening.event_at, opening.received_at):
            reasons.append('not_after_opening_observation')
    return InstrumentQuote(instrument, bid, ask, event, received, source, feed, session,
                           (start, end), tuple(reasons))
