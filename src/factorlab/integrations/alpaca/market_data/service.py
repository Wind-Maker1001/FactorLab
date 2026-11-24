"""Composition boundary for historical research and causal online inputs."""
from dataclasses import dataclass, replace

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
    prepared: 'PreparedHistory'
    decision_at: pd.Timestamp

    @property
    def adjusted_close(self):
        return self.prepared.adjusted_close

    @property
    def adjusted_open(self):
        return self.prepared.adjusted_open

    @property
    def adjusted_volume(self):
        return self.prepared.adjusted_volume

    @property
    def adjusted_close_validity(self):
        return self.adjusted_close.notna()

    @property
    def adjusted_open_validity(self):
        return self.adjusted_open.notna()

    @property
    def adjusted_volume_validity(self):
        return self.adjusted_volume.notna()


@dataclass(frozen=True)
class PreparedHistory:
    bars: pd.DataFrame
    adjusted_close: pd.DataFrame
    adjusted_open: pd.DataFrame
    adjusted_volume: pd.DataFrame
    adjustment_basis: str
    prepared_at: pd.Timestamp


class MarketDataService:
    def __init__(self, client, cache, calendar, *, history_client=None):
        self.client, self.cache, self.calendar = client, cache, calendar
        self.history_client = history_client if history_client is not None else client

    @classmethod
    def connect(cls, client, cache, *, start, end):
        rows, _ = client.calendar(start, end)
        return cls(client, cache, AlpacaSessionCalendar(rows, coverage_start=start, coverage_end=end,
                                                       version='ALPACA:v2/calendar'))

    def history(self, instrument, start, end, *, timeframe='1Day', adjustment='raw'):
        client = self.history_client
        asof = instrument.asof.tz_convert('America/New_York').date().isoformat()
        series = Series(instrument.instrument_id, instrument.symbol, timeframe, client.feed, adjustment, asof)
        def fetch(a, b):
            local_end = pd.Timestamp(b).tz_localize('America/New_York') + pd.DateOffset(days=1) - pd.Timedelta(nanoseconds=1)
            local_start = pd.Timestamp(a).tz_localize('America/New_York')
            rows = client.bars([instrument.symbol], local_start.isoformat(), local_end.isoformat(), timeframe=timeframe,
                               adjustment=adjustment, asof=asof)
            return normalize_bars(rows, instrument, calendar=self.calendar, timeframe=timeframe,
                                  received_at=client.now(), feed=client.feed, adjustment=adjustment,
                                  source='ALPACA:' + client.feed)
        frame = self.cache.update(series, fetch, start=start, end=end, calendar_version=self.calendar.version,
                                  captured_at=client.now())[0]
        return frame.loc[frame.session.between(pd.Timestamp(start), pd.Timestamp(end))].copy()

    def query(self, instrument, start, end, *, timeframe='1Day', adjustment='raw'):
        frame = self.history(instrument, start, end, timeframe=timeframe, adjustment=adjustment)
        series = Series(instrument.instrument_id, instrument.symbol, timeframe, self.history_client.feed, adjustment,
                        instrument.asof.tz_convert('America/New_York').date().isoformat())
        _, manifest = self.cache.load(series)
        return QueryResult(frame, SourceMetadata(
            source_id='ALPACA:' + self.history_client.feed, data_version=manifest['generation'],
            calendar_id=self.calendar.version, calendar_kind='OFFICIAL_SESSION_SCHEDULE',
            acquired_at=manifest['captured_at'], coverage='REQUESTED_NOT_GUARANTEED_OBSERVED',
            time_fields={'event_at': 'vendor event', 'received_at': 'local acquisition',
                         'identity_asof': 'symbol mapping, not knowledge time'},
            provenance={'series': manifest['series'], 'sha256': manifest['sha256']},
            field_semantics={name: {'unit': 'shares' if name.endswith('volume') else 'USD/share',
                                    'adjustment': adjustment, 'scope': 'vendor',
                                    'volume_scope': 'single_exchange' if self.history_client.feed == 'iex' else self.history_client.feed}
                             for name in frame if name.startswith(('raw_', 'adjusted_'))}))

    def prepare_history(self, instruments, *, start, end, now=None, adjustment='all',
                        adjustment_basis='vendor'):
        """Prepare prior-session inputs; yahoo_ratio is a formula, not Yahoo data equivalence."""
        instruments = tuple(instruments)
        if adjustment_basis not in {'vendor', 'yahoo_ratio'}:
            raise ValueError('unknown adjustment basis')
        cutoff = None if now is None else pd.Timestamp(now)
        now = pd.Timestamp(self.client.now() if now is None else now)
        if now.tzinfo is None:
            raise ValueError('aware preparation instant required')
        today = now.tz_convert('America/New_York').tz_localize(None).normalize()
        last = min(pd.Timestamp(end), self.calendar.schedule.index[self.calendar.schedule.index < today][-1])
        frames = []
        keys = ['instrument_id', 'event_at']
        provenance = ['source', 'feed', 'received_at', 'closed_at', 'complete', 'adjustment']
        for instrument in instruments:
            raw = self.history(instrument, start, last)
            adjusted = self.history(instrument, start, last, adjustment=adjustment)
            if adjustment == 'raw':
                adjusted = adjusted.rename(columns={c: 'adjusted_' + c for c in ('open', 'high', 'low', 'close', 'volume')})
            columns = keys + [c for c in adjusted if c.startswith('adjusted_')] + provenance
            adjusted = adjusted[columns].rename(columns={c: 'adjusted_bar_' + c for c in provenance})
            merged = raw.merge(adjusted, on=keys, how='left', validate='one_to_one')
            merged['vendor_adjusted_volume'] = merged.adjusted_volume
            if adjustment_basis == 'yahoo_ratio':
                merged['adjust_ratio'] = merged.adjusted_close / merged.raw_close
                merged['adjusted_open'] = merged.raw_open * merged.adjust_ratio
                merged['adjusted_volume'] = merged.raw_volume / merged.adjust_ratio
            frames.append(merged)
        now = pd.Timestamp(self.client.now()) if cutoff is None else cutoff
        bars = pd.concat(frames, ignore_index=True)
        available = (bars.adjusted_bar_complete.eq(True) &
                     (bars.adjusted_bar_received_at <= now) & (bars.adjusted_bar_closed_at <= now))
        eligible = bars.copy()
        fields = ['adjusted_close', 'adjusted_open', 'adjusted_volume']
        eligible.loc[~available, fields] = float('nan')
        expected = self.calendar.sessions(start, last).index
        ids = [i.instrument_id for i in instruments]
        matrices = [completed_daily_matrix(eligible, now=now, value=f).reindex(index=expected, columns=ids)
                    for f in fields]
        return PreparedHistory(bars, *matrices, adjustment_basis, now)

    def refresh_quotes(self, instruments, *, decision_at, decision_window,
                       max_age=pd.Timedelta(seconds=5), openings=None):
        """One batch after portfolio formation; all quotes share one TTL observation time."""
        instruments = tuple(instruments)
        payload, received = self.client.latest('quotes', [i.symbol for i in instruments])
        now = self.client.now()
        quotes = {}
        for instrument in instruments:
            row = payload.get('quotes', {}).get(instrument.symbol)
            quotes[instrument.instrument_id] = None if row is None else execution_quote(
                row, instrument, calendar=self.calendar, received_at=received, now=now,
                feed=self.client.feed, required_feed=self.client.feed,
                decision_window=decision_window, decision_at=decision_at, max_age=max_age,
                opening=(openings or {}).get(instrument.instrument_id), source='ALPACA:' + self.client.feed)
        return quotes

    def inputs(self, instruments, *, start, end, now, decision_window, adjustment='all',
               use_opening=False, prepared=None, adjustment_basis='vendor',
               max_age=pd.Timedelta(seconds=5)):
        instruments = tuple(instruments)
        now = pd.Timestamp(now)
        if now.tzinfo is None:
            raise ValueError('aware decision instant required')
        if prepared is None:
            prepared = self.prepare_history(instruments, start=start, end=end,
                                            adjustment=adjustment, adjustment_basis=adjustment_basis)
            now = max(now, pd.Timestamp(self.client.now()))
        elif prepared.prepared_at > now:
            raise ValueError('history was not available at decision instant')
        today = now.tz_convert('America/New_York').tz_localize(None).normalize()
        openings = {i.instrument_id: None for i in instruments}
        if use_opening and self.calendar.regular_session(now) is not None:
            # asof is identity-specific; group equal mappings for batch acquisition.
            groups = {}
            for instrument in instruments:
                asof = instrument.asof.tz_convert('America/New_York').date().isoformat()
                groups.setdefault(asof, []).append(instrument)
            for asof, group in groups.items():
                rows = self.client.bars([i.symbol for i in group], self.calendar.at(today, 'open').isoformat(),
                                        now.isoformat(), timeframe='1Min', adjustment='raw', asof=asof)
                received = self.client.now()
                for instrument in group:
                    selected = [r for r in rows if r.get('symbol', r.get('S')) == instrument.symbol]
                    openings[instrument.instrument_id] = opening_observation(
                        selected, instrument, calendar=self.calendar, session=today, received_at=received,
                        now=received, feed=self.client.feed, kind='bar', source='ALPACA:' + self.client.feed)
        clock, received = self.client.clock()
        decision_at = max(now, pd.Timestamp(self.client.now()))
        quotes = self.refresh_quotes(instruments, decision_at=decision_at, decision_window=decision_window,
                                     max_age=max_age, openings=openings)
        if use_opening:
            quotes = {k: replace(q, validity_reasons=q.validity_reasons + ('opening_unavailable',))
                      if q is not None and openings[k] is None else q for k, q in quotes.items()}
        return StrategyInputs(prepared.adjusted_close, prepared.adjusted_close.notna(), openings, quotes,
                              self.calendar.regular_session(now), self.calendar.next_session(now),
                              diagnose_clock(clock, self.calendar, now=received), prepared, decision_at)
