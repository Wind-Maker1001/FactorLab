"""Forward-only close-to-next-open access to the daily execution engine."""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import pandas as pd

from factorlab.errors import SchemaError
from .execution import _prepare_bars, _prepare_benchmark


@dataclass(frozen=True)
class HoldingSnapshot:
    symbol: str
    shares: int
    sellable_shares: int
    mark_price: float
    mark_time: pd.Timestamp
    market_value: float
    weight: float


@dataclass(frozen=True)
class HoldingsSnapshot:
    """A-share local exchange times; fills post at execution open, marks at close."""
    as_of: pd.Timestamp
    holdings: tuple[HoldingSnapshot, ...]
    cash: float
    nav: float
    cash_weight: float
    pending_targets: tuple[tuple[str, float], ...]
    last_fills: tuple[Mapping, ...]
    last_rejections: tuple[Mapping, ...]
    last_targets: tuple[tuple[str, float], ...] = ()
    corporate_actions_status: str = "UNKNOWN"

    @property
    def security_weights(self):
        return pd.Series({h.symbol: h.weight for h in self.holdings}, dtype=float)


def _day(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is not None or stamp != stamp.normalize():
        raise SchemaError("session dates must be naive exchange-local daily dates")
    return stamp


class ExecutionSession:
    """One FIFO book and cash balance, advanced exactly once per session.

    Bars may contain future rows, but only the advanced day's rows enter the
    execution loop. Orders are submitted at the current close for the next open.
    Corporate-action completeness is UNKNOWN; this is not financial admission.
    """

    def __init__(self, engine, bars, benchmark, *, signal_date):
        self.engine = engine
        self._bars = _prepare_bars(bars, engine.config)
        self._benchmark = _prepare_benchmark(benchmark, engine.config.benchmark_open_price_column,
                                             engine.config.benchmark_close_price_column)
        self._calendar = pd.DatetimeIndex(self._benchmark.date)
        self._initial_day = _day(signal_date)
        if self._initial_day not in self._calendar[:-1]:
            raise SchemaError("initial signal requires a next benchmark session")
        self._executions, self._accepted = {}, []
        self._locked_shares = {}
        self._book = None
        self._rows = None
        self._iterator = engine._iterate_selected(pd.DataFrame(columns=['date']), self._bars,
                                                   self._benchmark, explicit=True, session=self)
        cash = float(engine.config.initial_cash)
        self._snapshot = HoldingsSnapshot(self._initial_day + pd.Timedelta(hours=15), (),
                                           cash, cash, 1., (), (), ())

    @property
    def snapshot(self):
        return self._snapshot

    @property
    def next_execution_date(self):
        position = self._calendar.get_loc(self.snapshot.as_of.normalize())
        if position + 1 == len(self._calendar):
            raise SchemaError("no next benchmark session")
        return self._calendar[position + 1]

    @property
    def result(self):
        from .execution import ExecutionBacktestResult, _TRADE_COLUMNS, _POSITION_COLUMNS, _REJECTION_COLUMNS
        if self._rows is None:
            raise SchemaError("advance a session before requesting execution results")
        nav, trades, positions, rejections, comparisons = self._rows
        nav_frame = pd.DataFrame(nav)
        nav_frame['gross_security_turnover'] = (nav_frame.buy_notional + nav_frame.sell_notional) / nav_frame.turnover_denominator
        return ExecutionBacktestResult(
            nav=nav_frame, trades=pd.DataFrame(trades, columns=_TRADE_COLUMNS),
            positions=pd.DataFrame(positions, columns=_POSITION_COLUMNS),
            rejections=pd.DataFrame(rejections, columns=_REJECTION_COLUMNS),
            signals=pd.concat(self._accepted, ignore_index=True) if self._accepted else pd.DataFrame(),
            target_comparison=pd.DataFrame(comparisons))

    def submit_targets(self, weights, *, optimizer_turnover=None, locked_shares=None):
        day = self.snapshot.as_of.normalize()
        execution_day = self.next_execution_date
        if execution_day in self._executions:
            raise SchemaError("a target is already pending for the next session")
        targets = pd.DataFrame([dict(date=day, symbol=s, target_weight=w) for s, w in weights.items()])
        if optimizer_turnover is not None:
            targets['optimizer_turnover'] = optimizer_turnover
        orders = self.engine._prepare_targets(targets)
        orders['execution_date'] = execution_day
        locks = dict(locked_shares or {})
        shares = {h.symbol: h.shares for h in self.snapshot.holdings}
        for symbol, quantity in locks.items():
            if (symbol not in shares or isinstance(quantity, bool) or not isinstance(quantity, int)
                    or not 0 <= quantity <= shares[symbol]):
                raise SchemaError("locked_shares must be nonnegative integer held quantities")
        self._locked_shares[execution_day] = locks
        self._executions[execution_day] = orders
        self._accepted.append(orders)
        from dataclasses import replace
        self._snapshot = replace(self.snapshot, pending_targets=tuple(zip(orders.symbol, orders.target_weight)))

    def advance(self):
        """Execute the next open, post fills, then expose that session's close."""
        self.next_execution_date
        next(self._iterator)
        return self.snapshot

    def sellable_next_open(self, symbol):
        if self._book is None:
            return 0
        return self._book.sellable_shares(symbol, self.next_execution_date,
                                           t_plus_one=self.engine.config.t_plus_one)

    def _capture(self, day, book, cash, marks, mark_times, trades, rejections):
        self._book = book
        nav = cash + sum(book.total_shares(s) * marks[s] for s in book.symbols())
        holdings = tuple(HoldingSnapshot(s, book.total_shares(s),
            book.sellable_shares(s, day, t_plus_one=self.engine.config.t_plus_one), marks[s],
            mark_times[s], book.total_shares(s)*marks[s], book.total_shares(s)*marks[s]/nav)
            for s in book.symbols())
        def records(rows):
            current = []
            for row in reversed(rows):
                if row['execution_date'] != day:
                    break
                current.append(MappingProxyType(dict(row, reported_at=day + pd.Timedelta(hours=9, minutes=30))))
            return tuple(reversed(current))
        orders = self._executions.get(day)
        last_targets = (self.snapshot.last_targets if orders is None else
                        tuple(zip(orders.symbol, orders.target_weight)))
        self._snapshot = HoldingsSnapshot(day + pd.Timedelta(hours=15), holdings, cash, nav,
                                           cash/nav, (), records(trades), records(rejections), last_targets)
