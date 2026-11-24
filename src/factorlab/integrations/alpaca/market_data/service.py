"""Composition boundary for historical research and causal online inputs."""
from dataclasses import dataclass

import pandas as pd

from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from .cache import Series
from .inputs import normalize_bars, completed_daily_matrix, opening_observation, execution_quote
from .sessions import AlpacaSessionCalendar, diagnose_clock


@dataclass(frozen=True)
class StrategyInputs:
    history: pd.DataFrame
    validity: pd.DataFrame
    openings: dict
    quotes: dict
    session: object
    next_session: object
    clock_diagnostic: object


class MarketDataService:
    def __init__(self, client, cache, calendar):
        self.client, self.cache, self.calendar = client, cache, calendar

    @classmethod
    def connect(cls, client, cache, *, start, end):
        rows, _ = client.calendar(start, end)
        return cls(client, cache, AlpacaSessionCalendar(rows, coverage_start=start, coverage_end=end,
                                                       version='ALPACA:v2/calendar'))

    def history(self, instrument, start, end, *, timeframe='1Day', adjustment='raw'):
        asof = instrument.asof.tz_convert('America/New_York').date().isoformat()
        series = Series(instrument.instrument_id, instrument.symbol, timeframe, self.client.feed, adjustment, asof)
        def fetch(a, b):
            local_end = pd.Timestamp(b).tz_localize('America/New_York') + pd.DateOffset(days=1) - pd.Timedelta(nanoseconds=1)
            local_start = pd.Timestamp(a).tz_localize('America/New_York')
            rows = self.client.bars([instrument.symbol], local_start.isoformat(), local_end.isoformat(), timeframe=timeframe,
                                    adjustment=adjustment, asof=asof)
            return normalize_bars(rows, instrument, calendar=self.calendar, timeframe=timeframe,
                                  received_at=self.client.now(), feed=self.client.feed, adjustment=adjustment,
                                  source='ALPACA:' + self.client.feed)
        return self.cache.update(series, fetch, start=start, end=end, calendar_version=self.calendar.version,
                                 captured_at=self.client.now())[0]

    def query(self, instrument, start, end, *, timeframe='1Day', adjustment='raw'):
        frame = self.history(instrument, start, end, timeframe=timeframe, adjustment=adjustment)
        series = Series(instrument.instrument_id, instrument.symbol, timeframe, self.client.feed, adjustment,
                        instrument.asof.tz_convert('America/New_York').date().isoformat())
        _, manifest = self.cache.load(series)
        return QueryResult(frame, SourceMetadata(
            source_id='ALPACA:' + self.client.feed, data_version=manifest['generation'],
            calendar_id=self.calendar.version, calendar_kind='OFFICIAL_SESSION_SCHEDULE',
            acquired_at=manifest['captured_at'], coverage='REQUESTED_NOT_GUARANTEED_OBSERVED',
            time_fields={'event_at': 'vendor event', 'received_at': 'local acquisition',
                         'identity_asof': 'symbol mapping, not knowledge time'},
            provenance={'series': manifest['series'], 'sha256': manifest['sha256']},
            field_semantics={name: {'unit': 'shares' if name.endswith('volume') else 'USD/share',
                                    'adjustment': adjustment, 'scope': 'vendor',
                                    'volume_scope': 'single_exchange' if self.client.feed == 'iex' else self.client.feed}
                             for name in frame if name.startswith(('raw_', 'adjusted_'))}))

    def inputs(self, instruments, *, start, end, now, decision_window, adjustment='all', use_opening=True):
        """Each execution quote request occurs after acquisition of its opening input."""
        now = pd.Timestamp(now)
        if now.tzinfo is None:
            raise ValueError('aware decision instant required')
        today = now.tz_convert('America/New_York').tz_localize(None).normalize()
        last = self.calendar.schedule.index[self.calendar.schedule.index < today][-1]
        frames, openings, quotes = [], {}, {}
        for instrument in instruments:
            raw = self.history(instrument, start, min(pd.Timestamp(end), last))
            if adjustment == 'raw':
                frames.append(raw)
            else:
                adjusted = self.history(instrument, start, min(pd.Timestamp(end), last), adjustment=adjustment)
                columns = ['instrument_id', 'event_at'] + [c for c in adjusted if c.startswith('adjusted_')]
                frames.append(raw.merge(adjusted[columns], on=['instrument_id', 'event_at'], how='left', validate='one_to_one'))
            opening = None
            if use_opening and self.calendar.regular_session(now) is not None:
                session_open = self.calendar.at(today, 'open')
                asof = instrument.asof.tz_convert('America/New_York').date().isoformat()
                rows = self.client.bars([instrument.symbol], session_open.isoformat(), now.isoformat(),
                                        timeframe='1Min', adjustment='raw', asof=asof)
                received = self.client.now()
                opening = opening_observation(rows, instrument, calendar=self.calendar, session=today,
                                              received_at=received, now=received, feed=self.client.feed,
                                              kind='bar', source='ALPACA:' + self.client.feed)
            openings[instrument.instrument_id] = opening
            payload, received = self.client.latest('quotes', [instrument.symbol])
            row = payload.get('quotes', {}).get(instrument.symbol)
            if row is None:
                quotes[instrument.instrument_id] = None
            else:
                quote = execution_quote(row, instrument, calendar=self.calendar, received_at=received,
                                        now=self.client.now(), feed=self.client.feed, required_feed=self.client.feed,
                                        decision_window=decision_window, opening=opening,
                                        source='ALPACA:' + self.client.feed)
                if use_opening and opening is None:
                    from dataclasses import replace
                    quote = replace(quote, validity_reasons=quote.validity_reasons + ('opening_unavailable',))
                quotes[instrument.instrument_id] = quote
        bars = pd.concat(frames, ignore_index=True)
        value = 'close' if adjustment == 'raw' else 'adjusted_close'
        matrix = completed_daily_matrix(bars, now=self.client.now(), value=value)
        expected = self.calendar.sessions(start, last).index
        matrix = matrix.reindex(index=expected, columns=[i.instrument_id for i in instruments])
        clock, received = self.client.clock()
        return StrategyInputs(matrix, matrix.notna(), openings, quotes, self.calendar.regular_session(now),
                              self.calendar.next_session(now), diagnose_clock(clock, self.calendar, now=received))
