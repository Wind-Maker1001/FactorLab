"""Declaration-driven causal observations over the existing Alpaca market service."""
import pandas as pd

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


class StrategyData:
    def __init__(self, assets, *, clock):
        self.assets, self.clock = assets, clock

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
        frames = {}
        for instrument in instruments if needs_bars else ():
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
        return dict(protocol='FACTORLAB_DECISION_V2', binding=binding, event_id=window.event_id,
                    event_time=event_time, data_type=spec.data['data_types'][0], symbols=symbols,
                    account={'market': binding['market'], 'account_id': binding['account_id']},
                    allocation=allocation, context={'execution_session': window.session})

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
