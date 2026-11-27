"""Declaration-driven causal observations over the existing Alpaca market service."""
import pandas as pd

from factorlab.strategies.events import ReferenceUnavailable, StrategyInputUnavailable
from factorlab.strategies.reference_data import reference_context

from .cloud_execution import PreparationIncomplete
from .portfolio import portfolio_quote
from .scheduling import aggregate_minutes


def _observation(row):
    payload = {key: row[key] for key in ('open', 'high', 'low', 'close', 'volume')}
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
    def __init__(self, assets, *, clock, reference_provider=None):
        self.assets, self.clock = assets, clock
        self.reference_provider = reference_provider

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
        frames = {}
        if needs_bars:
            expected = _bar_window(calendar, window.signal_cutoff, window.interval_seconds, limit)
            frames = {instrument.symbol: self._bars(service, instrument, expected, window.interval_seconds, limit)
                      for instrument in instruments}
        references = self._references(service, spec, window)
        session_open = calendar.at(day, 'open').to_pydatetime()
        session_close = calendar.at(day, 'close').to_pydatetime()
        quotes = service.refresh_quotes(instruments, decision_at=self.clock(),
                                        decision_window=(session_open, session_close)) if 'quote' in spec.data['data_types'] else {}
        event_time = self.clock()
        symbols = {}
        for instrument in instruments:
            observations, histories = {}, {}
            if needs_bars:
                history = [_observation(row) for row in frames[instrument.symbol]]
                observations['closed_bar'] = history[-1]
                histories['closed_bar'] = history
            if 'quote' in spec.data['data_types']:
                quote = quotes.get(instrument.instrument_id)
                if quote is None or not quote.valid:
                    raise PreparationIncomplete('INCOMPLETE_QUOTE:' + instrument.symbol)
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
        event = dict(protocol='FACTORLAB_DECISION_V2', binding=binding, event_id=window.event_id,
                     event_time=event_time, data_type=spec.data['data_types'][0], symbols=symbols,
                     account={'market': binding['market'], 'account_id': binding['account_id']},
                     allocation=allocation, context={'execution_session': window.session})
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
