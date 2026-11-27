"""Declaration-driven causal observations over the existing Alpaca market service."""
from decimal import Decimal as D
import pandas as pd

from factorlab.strategies.events import ReferenceUnavailable, StrategyInputUnavailable
from factorlab.strategies.reference_data import reference_context

from .cloud_execution import PreparationIncomplete
from .portfolio import portfolio_quote
from .scheduling import aggregate_minutes


def _observation(row):
    payload = {key: row[key] for key in ('open', 'high', 'low', 'close', 'volume')}
    for extra in ('adjusted_open', 'adjusted_high', 'adjusted_low', 'adjusted_close', 'adjusted_volume', 'adjust_ratio'):
        if extra in row and pd.notna(row[extra]):
            payload[extra] = row[extra]
    payload.update(symbol=row['symbol'], instrument_id=row['instrument_id'],
                   session=str(pd.Timestamp(row['session']).date()), feed=row['feed'],
                   opened_at=pd.Timestamp(row['opened_at']).to_pydatetime(),
                   closed_at=pd.Timestamp(row['closed_at']).to_pydatetime(), complete=True)
    return dict(observed_at=pd.Timestamp(row['received_at']).to_pydatetime(),
                effective_at=pd.Timestamp(row['closed_at']).to_pydatetime(),
                source=row['source'], payload=payload)


def _bar_window(calendar, cutoff, interval, limit):
    eligible = calendar.schedule.loc[calendar.schedule.open < cutoff]
    if interval is None:
        return eligible.loc[eligible.close <= cutoff, 'close'].iloc[-limit:]
    size = pd.Timedelta(seconds=interval)
    timestamps = []
    for session in reversed(list(eligible.itertuples())):
        count = (min(session.close, cutoff) - session.open) // size
        take = min(count, limit - len(timestamps))
        timestamps.extend((session.Index, session.open + size * offset)
                          for offset in range(count, count - take, -1))
        if len(timestamps) == limit:
            break
    timestamps.reverse()
    return pd.Series([at for _, at in timestamps], index=[day for day, _ in timestamps])


class StrategyData:
    def __init__(self, assets, *, clock, reference_provider=None, benchmark_provider=None):
        self.assets, self.clock = assets, clock
        self.reference_provider = reference_provider
        self.benchmark_provider = benchmark_provider

    def _calendar_rows(self, calendar):
        return [
            dict(
                date=str(day.date()),
                open=row.open.tz_convert(calendar.timezone).strftime('%H:%M'),
                close=row.close.tz_convert(calendar.timezone).strftime('%H:%M'),
            )
            for day, row in calendar.schedule.iterrows()
        ]

    def _adjusted_daily_frames(self, service, instruments, days, *, window, adjustment_basis, partial_symbols):
        prepared = service.prepare_history(
            instruments,
            start=days[0],
            end=days[-1],
            adjustment='all',
            adjustment_basis=adjustment_basis,
        )
        acquired = max(pd.Timestamp(self.clock()), pd.Timestamp(prepared.prepared_at))
        frames = {}
        factors = {}
        for instrument in instruments:
            bars = prepared.bars.loc[prepared.bars.instrument_id == instrument.instrument_id].copy()
            bars = bars.loc[
                bars.complete
                & bars.adjusted_bar_complete.eq(True)
                & (bars.closed_at <= window.signal_cutoff)
                & (bars.received_at <= acquired)
                & (bars.adjusted_bar_closed_at <= window.signal_cutoff)
                & (bars.adjusted_bar_received_at <= acquired)
            ].sort_values('closed_at').iloc[-len(days):]
            if instrument.symbol in partial_symbols:
                # Pre-listing / warmup symbol (e.g. SNDK) may have fewer than `limit` rows,
                # or even 0 rows before listing; once listed, rows must end cleanly if present.
                if not bars.empty and pd.Timestamp(bars.iloc[-1].closed_at) != pd.Timestamp(window.signal_cutoff):
                    raise PreparationIncomplete('INCOMPLETE_HISTORY:' + instrument.symbol)
                if not bars.empty:
                    last = bars.iloc[-1]
                    factor = D(str(last.adjusted_close)) / D(str(last.raw_close))
                else:
                    factor = D(1)
            else:
                if len(bars) != len(days) or pd.Timestamp(bars.iloc[-1].closed_at) != pd.Timestamp(window.signal_cutoff):
                    raise PreparationIncomplete('INCOMPLETE_HISTORY:' + instrument.symbol)
                last = bars.iloc[-1]
                factor = D(str(last.adjusted_close)) / D(str(last.raw_close))
            if not factor.is_finite() or factor <= 0:
                raise PreparationIncomplete('INVALID_ADJUSTMENT_FACTOR:' + instrument.symbol)
            frames[instrument.symbol] = bars
            factors[instrument.symbol] = factor
        return frames, factors

    def qualify(self, service, spec):
        feed = spec.data.get('feed')
        if feed is not None and feed != service.client.feed:
            raise StrategyInputUnavailable('strategy requires its declared Alpaca feed: ' + feed)
        for symbol, streams in spec.data.get('references', {}).items():
            for kind, request in streams.items():
                if request['source'].startswith('ALPACA:'):
                    source = 'ALPACA:' + getattr(service, 'history_client', service.client).feed
                    interval = request.get('interval_seconds')
                    if (kind != 'closed_bar' or request['source'] != source
                            or request['price_basis'] != 'raw'
                            or interval != 86400 and (not 60 <= interval <= 3600 or interval % 60)):
                        raise ReferenceUnavailable('unavailable Alpaca reference series: ' + symbol + '/' + kind)
                elif self.reference_provider is None or not self.reference_provider.supports(symbol, kind, request):
                    raise ReferenceUnavailable('unavailable reference source: ' + request['source'])

    def _bars(self, service, instrument, expected, interval, limit):
        if len(expected) != limit:
            raise PreparationIncomplete('INSUFFICIENT_CALENDAR_HISTORY')
        try:
            frame = service.history(instrument, expected.index[0], expected.index[-1],
                                    timeframe='1Min' if interval is not None else '1Day')
            acquired = pd.Timestamp(self.clock())
            if interval is not None:
                frame = aggregate_minutes(frame, calendar=service.calendar, interval_seconds=interval, now=acquired)
        except ValueError as error:
            raise PreparationIncomplete('INVALID_HISTORY:' + instrument.symbol) from error
        frame = frame.loc[frame.complete & frame.closed_at.isin(expected)
                          & (frame.received_at <= acquired)].sort_values('closed_at')
        if list(frame.closed_at) != list(expected):
            raise PreparationIncomplete('INCOMPLETE_HISTORY:' + instrument.symbol)
        return frame.to_dict('records')

    def _references(self, service, spec, window):
        references = {}
        calendar = service.calendar
        for symbol, streams in spec.data.get('references', {}).items():
            history = {}
            for kind, request in streams.items():
                limit = request['history_limit']
                if request['source'].startswith('ALPACA:'):
                    interval = request['interval_seconds']
                    minute_interval = None if interval == 86400 else interval
                    expected = _bar_window(calendar, window.signal_cutoff, minute_interval, limit)
                    instrument = self.assets.reference_instrument(symbol)
                    rows = self._bars(service, instrument, expected, minute_interval, limit)
                    observations = []
                    for row in rows:
                        observation = _observation(row)
                        observation['payload'].update(price_basis=row['adjustment'], interval_seconds=interval)
                        observations.append(observation)
                else:
                    if kind == 'closed_bar':
                        interval = request['interval_seconds']
                        expected = _bar_window(calendar, window.signal_cutoff,
                                               None if interval == 86400 else interval, limit)
                        days = expected.index
                    else:
                        days = calendar.schedule.index[calendar.schedule.index <= pd.Timestamp(window.session)][-limit:]
                    if days.empty:
                        raise PreparationIncomplete('INSUFFICIENT_REFERENCE_CALENDAR:' + symbol)
                    observations = self.reference_provider.history(symbol, kind, request,
                        start=days[0].date().isoformat(), end=days[-1].date().isoformat(),
                        as_of=self.clock(), effective_through=window.signal_cutoff if kind == 'closed_bar' else None)
                if len(observations) != limit:
                    raise ReferenceUnavailable('incomplete reference history: ' + symbol + '/' + kind)
                history[kind] = observations
            references[symbol] = reference_context(history)
        return references

    def event(self, service, spec, binding, allocation, window, *, owned_orders=()):
        self.qualify(service, spec)
        instruments = self.assets.instruments(spec.symbols)
        calendar = service.calendar
        day = pd.Timestamp(window.session)
        limit = spec.data['history_limit']
        needs_bars = 'closed_bar' in spec.data['data_types']
        adjustment_basis = spec.data.get('adjustment_basis')
        partial_symbols = set(spec.data.get('allow_partial_symbol_history', ()))
        benchmark_series = spec.data.get('benchmark_series')
        frames = {}
        factors = None
        daily_days = None
        if needs_bars:
            expected = _bar_window(calendar, window.signal_cutoff, window.interval_seconds, limit)
            if adjustment_basis is not None and window.interval_seconds is None:
                daily_days = expected.index
                if len(daily_days) != limit:
                    raise PreparationIncomplete('INSUFFICIENT_CALENDAR_HISTORY')
                frames, factors = self._adjusted_daily_frames(
                    service, instruments, daily_days, window=window,
                    adjustment_basis=adjustment_basis, partial_symbols=partial_symbols,
                )
            else:
                frames = {instrument.symbol: self._bars(service, instrument, expected, window.interval_seconds, limit)
                          for instrument in instruments}
        references = self._references(service, spec, window)
        benchmark_context = None
        if benchmark_series is not None:
            if self.benchmark_provider is None:
                raise PreparationIncomplete('MISSING_BENCHMARK_PROVIDER:' + benchmark_series)
            if daily_days is None:
                daily_days = _bar_window(calendar, window.signal_cutoff, None, limit).index
            if len(daily_days) != limit:
                raise PreparationIncomplete('INSUFFICIENT_BENCHMARK_HISTORY:' + benchmark_series)
            bench = self.benchmark_provider.history(
                benchmark_series,
                calendar=calendar,
                days=daily_days,
                signal_cutoff=window.signal_cutoff,
            )
            bench_observed_horizon = pd.Timestamp(self.clock())
            if bench_observed_horizon.tzinfo is None:
                raise ValueError('aware clock timestamp required')
            bench_observed_horizon_utc = bench_observed_horizon.tz_convert('UTC')
            cutoff_utc = pd.Timestamp(window.signal_cutoff).tz_convert('UTC')
            if bench.series_id != benchmark_series:
                raise PreparationIncomplete('INVALID_BENCHMARK_SERIES:' + benchmark_series)
            for rec in bench.records:
                eff_utc = pd.Timestamp(rec['effective_at']).tz_convert('UTC')
                obs_utc = pd.Timestamp(rec['observed_at']).tz_convert('UTC')
                if eff_utc > cutoff_utc or eff_utc > obs_utc:
                    raise PreparationIncomplete('NONCAUSAL_BENCHMARK_EFFECTIVE_TIME:' + benchmark_series)
                if obs_utc > bench_observed_horizon_utc:
                    raise PreparationIncomplete('NONCAUSAL_BENCHMARK_OBSERVATION:' + benchmark_series)
            if (
                len(bench.records) != len(daily_days)
                or pd.Timestamp(bench.records[-1]['effective_at']).tz_convert('UTC') != cutoff_utc
            ):
                raise PreparationIncomplete('INCOMPLETE_BENCHMARK:' + benchmark_series)
            benchmark_context = {
                bench.series_id: dict(
                    series_id=bench.series_id,
                    source=bench.source,
                    history_basis=bench.history_basis,
                    signal_cutoff=bench.signal_cutoff or window.signal_cutoff,
                    acquired_at=bench.acquired_at,
                    knowledge_cutoff=bench.knowledge_cutoff or bench.acquired_at,
                    observed_horizon=bench_observed_horizon.to_pydatetime(),
                    provenance=bench.provenance,
                    history=list(bench.records),
                )
            }
        session_open = calendar.at(day, 'open').to_pydatetime()
        session_close = calendar.at(day, 'close').to_pydatetime()
        quotes = service.refresh_quotes(instruments, decision_at=self.clock(),
                                        decision_window=(session_open, session_close)) if 'quote' in spec.data['data_types'] else {}
        event_time = self.clock()
        symbols = {}
        for instrument in instruments:
            observations, histories = {}, {}
            if needs_bars:
                rows = frames[instrument.symbol]
                records = rows.to_dict('records') if isinstance(rows, pd.DataFrame) else rows
                history = [_observation(row) for row in records]
                if history:
                    observations['closed_bar'] = history[-1]
                histories['closed_bar'] = history
            if 'quote' in spec.data['data_types']:
                quote = quotes.get(instrument.instrument_id)
                if quote is None or not quote.valid:
                    if instrument.symbol not in partial_symbols or allocation.get('positions', {}).get(instrument.symbol, 0):
                        raise PreparationIncomplete('INCOMPLETE_QUOTE:' + instrument.symbol)
                else:
                    observations['quote'] = dict(observed_at=quote.received_at.to_pydatetime(),
                        effective_at=quote.event_at.to_pydatetime(), source=quote.source,
                        payload=dict(bid=quote.bid, ask=quote.ask, feed=quote.feed,
                                     instrument_id=instrument.instrument_id, symbol=instrument.symbol))
                histories['quote'] = []
            if 'order_event' in spec.data['data_types']:
                observations['order_event'] = dict(observed_at=event_time, effective_at=event_time,
                    source='ALPACA_PAPER_EXECUTION_JOURNAL', payload={'orders': [
                        order for order in owned_orders if order['symbol'] == instrument.symbol]})
                histories['order_event'] = []
            symbols[instrument.symbol] = dict(watermark=window.event_id,
                                              observations=observations, history=histories)
        context = {'execution_session': window.session}
        if factors is not None:
            quote_prices = {
                inst.symbol: quotes[inst.instrument_id].ask
                for inst in instruments
                if inst.instrument_id in quotes and quotes[inst.instrument_id] is not None and quotes[inst.instrument_id].valid
            }
            rules = self.assets.rules(
                spec.symbols,
                prices=quote_prices or None,
                fractional_quantities=spec.execution['fractional_quantities'],
            )
            context.update(
                factors=factors,
                quantity_steps={s: r.quantity_step for s, r in rules.items()},
                calendar=self._calendar_rows(calendar),
                coverage_start=str(calendar.coverage_start.date()),
                coverage_end=str(calendar.coverage_end.date()),
                adjustment_basis=adjustment_basis,
            )
        if benchmark_context is not None:
            context['benchmark'] = benchmark_context
        event = dict(protocol='FACTORLAB_DECISION_V2', binding=binding, event_id=window.event_id,
                     event_time=event_time, data_type=spec.data['data_types'][0], symbols=symbols,
                     account={'market': binding['market'], 'account_id': binding['account_id']},
                     allocation=allocation, context=context)
        if references:
            event['references'] = references
        return event

    def quotes(self, service, symbols, *, session, decision_at):
        if not symbols:
            return {}
        instruments = self.assets.instruments(symbols)
        observed = service.refresh_quotes(instruments, decision_at=decision_at,
            decision_window=(service.calendar.at(session, 'open').to_pydatetime(),
                             service.calendar.at(session, 'close').to_pydatetime()))
        return {instrument.symbol: portfolio_quote(observed[instrument.instrument_id],
                instrument_id=instrument.instrument_id, symbol=instrument.symbol)
                for instrument in instruments if observed[instrument.instrument_id] is not None}
