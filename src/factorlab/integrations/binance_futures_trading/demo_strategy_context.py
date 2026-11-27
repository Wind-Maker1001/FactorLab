"""Causal per-symbol observations for the public strategy event envelope."""
from datetime import timedelta
from decimal import Decimal
from itertools import islice
import json

from factorlab.trading.contracts import utc
from factorlab.trading.realtime import identity
from factorlab.trading.storage import decode, dumps
from .protocol import SafetyBlocked

TYPES = frozenset({'closed_bar', 'quote', 'order_event', 'funding_forecast',
                   'funding_settlement', 'volatility'})


def qualify_instance_data(spec):
    from factorlab.integrations.binance.market import symbol_name
    from factorlab.strategies.loader import StrategyPackageError
    from .demo_market_feed import INTERVALS, SOURCE

    schedule = spec.schedule
    if (schedule['kind'] != 'interval' or schedule['interval_seconds'] not in INTERVALS.values()
            or not set(spec.data['data_types']) <= {
                'closed_bar', 'order_event', 'funding_forecast', 'funding_settlement'
            }):
        raise StrategyPackageError('unsupported Binance Demo schedule or observations')
    if spec.data.get('source', SOURCE) != SOURCE or spec.data.get('price_basis', 'raw') != 'raw':
        raise StrategyPackageError('Binance Demo requires raw observations from its declared feed')
    for symbol, declarations in spec.data.get('references', {}).items():
        try:
            symbol_name(symbol)
        except ValueError as error:
            raise StrategyPackageError('invalid Binance Demo reference symbol') from error
        for kind, declaration in declarations.items():
            if declaration['source'] != SOURCE:
                raise StrategyPackageError('unsupported Binance Demo reference source')
            if kind == 'closed_bar':
                if declaration['price_basis'] != 'raw' or declaration['interval_seconds'] != schedule['interval_seconds']:
                    raise StrategyPackageError('Binance Demo reference bars require raw, aligned bars')
            elif kind not in {'funding_forecast', 'funding_settlement'}:
                raise StrategyPackageError('unsupported Binance Demo reference observations')


class DemoStrategyContext:
    def __init__(self, store):
        self.store = store
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS demo_strategy_observations (
            id TEXT PRIMARY KEY, symbol TEXT NOT NULL, kind TEXT NOT NULL,
            observed_at TEXT NOT NULL, payload TEXT NOT NULL)''')
        self.store.db.execute('CREATE INDEX IF NOT EXISTS demo_strategy_observations_range '
            'ON demo_strategy_observations(symbol, kind, observed_at)')


    def observe(self, symbol, kind, *, event_id, observed_at, effective_at, source, payload):
        utc(observed_at)
        utc(effective_at)
        if kind not in TYPES or not event_id or not source:
            raise ValueError('identified strategy observation required')
        if kind != 'funding_forecast' and effective_at > observed_at:
            raise SafetyBlocked('realized observation cannot precede its effective time')
        observation = dict(observed_at=observed_at, effective_at=effective_at,
                           source=source, payload=payload)
        key = identity((symbol, kind, event_id))
        encoded = dumps(observation)
        with self.store.transaction():
            existing = self.store.db.execute('SELECT payload FROM demo_strategy_observations WHERE id=?', (key,)).fetchone()
            if existing is not None:
                saved = decode(json.loads(existing[0]))
                repeated = dict(observation, observed_at=saved['observed_at'])
                if saved != repeated or observed_at < saved['observed_at']:
                    raise SafetyBlocked('strategy observation identity conflict')
                return False
            self.store.db.execute('INSERT INTO demo_strategy_observations VALUES(?,?,?,?,?)',
                (key, symbol, kind, observed_at.isoformat(), encoded))
        return True

    def history(self, symbol, kind, *, at, history_limit, source=None):
        rows = self.store.db.execute('SELECT id,payload FROM demo_strategy_observations '
            'WHERE symbol=? AND kind=? AND observed_at<=? ORDER BY observed_at DESC,rowid DESC LIMIT ?',
            (symbol, kind, at.isoformat(), history_limit if source is None else -1))
        observations = ((key, decode(json.loads(raw))) for key, raw in rows)
        return list(reversed(list(islice(
            ((key, value) for key, value in observations if source is None or value['source'] == source),
            history_limit))))

    def symbols(self, symbols, *, at, history_limit, data_types=TYPES):
        utc(at)
        if type(history_limit) is not int or history_limit < 1:
            raise ValueError('positive strategy history limit required')
        result = {}
        for symbol in symbols:
            history = {}
            watermarks = []
            for kind in sorted(data_types):
                rows = self.history(symbol, kind, at=at, history_limit=history_limit)
                if rows:
                    history[kind] = [value for _, value in rows]
                    watermarks.append((kind, rows[-1][0]))
            result[symbol] = dict(watermark=identity(watermarks),
                observations={kind: rows[-1] for kind, rows in history.items()}, history=history)
        return result

    def event(self, spec, binding, *, event_id, event_time, data_type, account, allocation):
        """Project only the instance's declared observations into the public envelope."""
        from factorlab.strategies.events import validate_event
        from factorlab.strategies.protocol import V2
        symbols = self.symbols(spec.symbols, at=event_time,
                               history_limit=spec.data['history_limit'], data_types=spec.data['data_types'])
        for context in symbols.values():
            context['watermark'] = identity(context['history'])
        return validate_event(dict(protocol=V2, binding=binding, event_id=event_id,
            event_time=event_time, data_type=data_type, symbols=symbols,
            account=account, allocation=allocation), spec)

    def funding_estimate(self, symbol, *, at, policy):
        from factorlab.experiments.grid_funding_adaptive import causal_funding_rate_bps_h
        rows = self.store.db.execute('''SELECT payload FROM demo_strategy_observations
            WHERE symbol=? AND kind='funding_settlement' AND observed_at<=?
            ORDER BY observed_at,rowid''', (symbol, at.isoformat())).fetchall()
        events = []
        for raw, in rows:
            observation = decode(json.loads(raw))
            payload = observation['payload']
            events.append((observation['effective_at'], Decimal(payload['mark_price']),
                           Decimal(payload['funding_rate']), payload['event_id']))
        events.sort(key=lambda event: event[0])
        return causal_funding_rate_bps_h(events, at, lookback_hours=policy.lookback_hours,
                                         conservative=policy.conservative)

    def volatility(self, symbol, *, at, policy):
        from factorlab.experiments.grid_funding_adaptive import causal_realized_vol_1h, adaptive_half_width
        rows = self.store.db.execute('''SELECT payload FROM demo_strategy_observations
            WHERE symbol=? AND kind='closed_bar' AND observed_at<=?
            ORDER BY observed_at DESC,rowid DESC LIMIT ?''',
            (symbol, at.isoformat(), policy.vol_window_hours + 1)).fetchall()
        observations = [decode(json.loads(row[0])) for row in reversed(rows)]
        if len(observations) < policy.vol_window_hours + 1:
            raise SafetyBlocked('volatility history is not warmed up')
        for previous, current in zip(observations, observations[1:]):
            if current['effective_at'] - previous['effective_at'] != timedelta(hours=1):
                raise SafetyBlocked('volatility requires contiguous closed hourly bars')
        closes = [float(row['payload']['close']) for row in observations]
        sigma = causal_realized_vol_1h(closes, policy.vol_window_hours, policy.reference_vol_1h)
        return dict(estimator='causal_realized_vol_1h', window_hours=policy.vol_window_hours,
                    sigma_ref=policy.reference_vol_1h, sigma=sigma,
                    half_width=adaptive_half_width(sigma, policy))
