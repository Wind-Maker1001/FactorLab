"""Declaration-driven causal observations over the existing Alpaca market service."""
from decimal import Decimal as D
import pandas as pd

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


class StrategyData:
    def __init__(self, assets, *, clock, benchmark_provider=None):
        self.assets, self.clock = assets, clock
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

    def event(self, service, spec, binding, allocation, window, *, owned_orders=()):
        instruments = self.assets.instruments(spec.symbols)
        calendar = service.calendar
        day = pd.Timestamp(window.session)
        available_days = calendar.schedule.index[calendar.schedule.index <= day]
        limit = spec.data['history_limit']
        if window.interval_seconds is None:
            days = available_days[available_days < day][-limit:]
        else:
            counts = ((calendar.schedule.loc[available_days, 'close']
                       - calendar.schedule.loc[available_days, 'open']).dt.total_seconds()
                      // window.interval_seconds).astype(int)
            days = []
            count = 0
            for previous in reversed(available_days):
                days.append(previous)
                count += int(counts.loc[previous])
                if count >= limit + int(counts.loc[day]):
                    break
            days = pd.DatetimeIndex(sorted(days))
        needs_bars = 'closed_bar' in spec.data['data_types']
        if needs_bars and days.empty:
            raise PreparationIncomplete('INSUFFICIENT_CALENDAR_HISTORY')
        adjustment_basis = spec.data.get('adjustment_basis')
        partial_symbols = set(spec.data.get('allow_partial_symbol_history', ()))
        benchmark_series = spec.data.get('benchmark_series')
        frames = {}
        factors = None
        if needs_bars:
            if adjustment_basis is not None and window.interval_seconds is None:
                if len(days) != limit:
                    raise PreparationIncomplete('INSUFFICIENT_CALENDAR_HISTORY')
                frames, factors = self._adjusted_daily_frames(
                    service, instruments, days, window=window,
                    adjustment_basis=adjustment_basis, partial_symbols=partial_symbols,
                )
            else:
                for instrument in instruments:
                    frame = service.history(instrument, days[0], day if window.interval_seconds else days[-1],
                                            timeframe='1Min' if window.interval_seconds else '1Day')
                    acquired = pd.Timestamp(self.clock())
                    if window.interval_seconds is not None:
                        frame = aggregate_minutes(frame, calendar=calendar,
                            interval_seconds=window.interval_seconds, now=acquired)
                    frame = frame.loc[frame.complete & (frame.closed_at <= window.signal_cutoff)
                                      & (frame.received_at <= acquired)].sort_values('closed_at').iloc[-limit:]
                    if len(frame) != limit or pd.Timestamp(frame.iloc[-1].closed_at) != pd.Timestamp(window.signal_cutoff):
                        raise PreparationIncomplete('INCOMPLETE_HISTORY:' + instrument.symbol)
                    frames[instrument.symbol] = frame
        benchmark_context = None
        if benchmark_series is not None:
            if self.benchmark_provider is None:
                raise PreparationIncomplete('MISSING_BENCHMARK_PROVIDER:' + benchmark_series)
            if len(days) != limit:
                raise PreparationIncomplete('INSUFFICIENT_BENCHMARK_HISTORY:' + benchmark_series)
            bench = self.benchmark_provider.history(
                benchmark_series,
                calendar=calendar,
                days=days,
                signal_cutoff=window.signal_cutoff,
                now=self.clock(),
            )
            benchmark_context = {
                bench.series_id: dict(
                    series_id=bench.series_id,
                    source=bench.source,
                    history_basis=bench.history_basis,
                    acquired_at=bench.acquired_at,
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
                history = [_observation(row) for row in frames[instrument.symbol].to_dict('records')]
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
        return dict(protocol='FACTORLAB_DECISION_V2', binding=binding, event_id=window.event_id,
                    event_time=event_time, data_type=spec.data['data_types'][0], symbols=symbols,
                    account={'market': binding['market'], 'account_id': binding['account_id']},
                    allocation=allocation, context=context)

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
