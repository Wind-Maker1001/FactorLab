"""Native V69 portfolio boundary with explicit checkpoint and causal history."""
from decimal import Decimal
from enum import Enum
import csv

ASSETS = ('AAPL', 'SNDK', 'BA', 'C', 'JPM', 'GE', 'TQQQ', 'QQQ')


class Variant(str, Enum):
    REFERENCE = 'V69_REFERENCE'
    CAUSAL = 'V69_CAUSAL'
    TRADFI = 'V69_TRADFI'


class SourceUnavailable(RuntimeError):
    pass


def validate_weights(weights):
    if set(weights) != set(ASSETS):
        raise ValueError('complete original asset pool required')
    if any(not isinstance(w, Decimal) or not w.is_finite() or w < 0
           for w in weights.values()):
        raise ValueError('finite nonnegative Decimal weights required')


def execution_weights(weights, contracts):
    """Map nominal NAV weights without normalization or leverage conversion."""
    validate_weights(weights)
    if set(contracts) - set(ASSETS):
        raise ValueError('unknown stock')
    if any(not isinstance(c, str) or not c for c in contracts.values()):
        raise ValueError('nonempty contract identifiers required')
    if len(set(contracts.values())) != len(contracts):
        raise ValueError('contract mapping must be one-to-one')
    return {contract: weights[stock] for stock, contract in contracts.items()}


def first_session_of_week(calendar, day):
    """Require coverage through civil Monday, including boundary holidays."""
    import pandas as pd
    day = pd.Timestamp(day)
    if day.tzinfo is not None or day != day.normalize():
        raise ValueError('naive session date required')
    sessions = calendar.sessions(day - pd.Timedelta(days=day.weekday()), day)
    return not sessions.empty and sessions.index[0] == day


def require_causal_execution(observed_at, execute_at):
    if observed_at.tzinfo is None or execute_at.tzinfo is None:
        raise ValueError('timezone-aware instants required')
    if execute_at <= observed_at:
        raise ValueError('execution must follow observation')


def write_weight_diff(stream, session, variant, reference, candidate):
    """Append eight rows: session, variant, symbol, reference, candidate, delta."""
    validate_weights(reference)
    validate_weights(candidate)
    variant = Variant(variant)
    writer = csv.writer(stream)
    for stock in ASSETS:
        old, new = reference[stock], candidate[stock]
        writer.writerow((session, variant.value, stock, str(old), str(new), str(new-old)))


class V69Strategy:
    """Stateless portfolio preparation with explicit JSON checkpoint.

    Event: close/volume frames through previous completed session, calendar,
    session, adjusted holdings and cash. No opening observation required.
    Returned orders remain in adjusted research units.
    """
    def __init__(self, variant=Variant.CAUSAL, *, omit_sndk=False):
        self.variant = Variant(variant)
        self.omit_sndk = omit_sndk

    def decide(self, event, state):
        import pandas as pd
        from types import SimpleNamespace
        from .v69_native import V69Planner
        day = pd.Timestamp(event['session'])
        calendar = event['calendar']
        if not first_session_of_week(calendar, day):
            return dict(weights=None, orders=[], state=dict(state))
        week = list(day.isocalendar()[:2])
        if state.get('last_week') == week:
            return dict(weights=None, orders=[], state=dict(state))
        previous = calendar.shift(day, -1)
        close, volume = event['close'], event['volume']
        if close.index[-1] != previous or volume.index[-1] != previous:
            raise ValueError('history must end at preceding completed session')
        # Append a label only, never a current-session observation.
        engine = V69Planner(close.reindex(close.index.append(pd.DatetimeIndex([day]))),
                            volume.reindex(volume.index.append(pd.DatetimeIndex([day]))),
                            omit_sndk=self.omit_sndk)
        engine.trade_calendar = SimpleNamespace(get_step_time=lambda: (day, day))
        engine.trade_position = SimpleNamespace(
            get_stock_amount_dict=lambda: dict(event['holdings']),
            get_cash=lambda: event['cash'])
        decision = engine.generate_trade_decision()
        weights = {k: engine.decisions[-1][k] for k in ASSETS}
        return dict(weights=weights, orders=[dict(symbol=o.stock_id,
            side='sell' if o.direction == 0 else 'buy', adjusted_quantity=o.amount)
            for o in decision.order_list], state={'last_week': week})
