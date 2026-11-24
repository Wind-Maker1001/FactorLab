"""V69 portfolio migration boundary; original signal sources are unavailable."""
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
    """Fail closed until original sources and all dependencies are frozen.

    Not registered with the single-symbol live loader. Portfolio execution
    requires an account-wide adapter before live use.
    """
    def __init__(self, variant=Variant.REFERENCE):
        self.variant = Variant(variant)

    def decide(self, event, state):
        raise SourceUnavailable('Missing v48/v50/v69 originals; no verified target available')
