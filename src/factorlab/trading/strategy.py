"""Causal strategy signals; execution and account ownership remain in trading."""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

import pandas as pd

from factorlab.strategies.dual_ma import crossover_indicators
from .contracts import utc


@dataclass(frozen=True)
class ClosedBar:
    symbol: str
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    interval_seconds: int
    source: str
    complete: bool = True

    def __post_init__(self):
        utc(self.opened_at)
        utc(self.closed_at)
        if (not self.symbol or not self.source or type(self.complete) is not bool
                or type(self.interval_seconds) is not int or self.interval_seconds <= 0
                or self.closed_at - self.opened_at != timedelta(seconds=self.interval_seconds)):
            raise ValueError('invalid bar identity or interval')
        if not all(isinstance(v, Decimal) and v.is_finite() for v in
                   (self.open, self.high, self.low, self.close, self.volume)):
            raise ValueError('finite Decimal OHLCV required')
        if (min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0
                or self.high < max(self.open, self.close, self.low)
                or self.low > min(self.open, self.close, self.high)):
            raise ValueError('invalid OHLCV')


@dataclass(frozen=True)
class StrategySignal:
    strategy_id: str
    strategy_version: str
    symbol: str
    generated_at: datetime
    input_watermark: datetime
    direction: str
    target_weight: Decimal | None
    source: str
    signal_id: str
    status: str = 'SIGNAL'
    model_id: str | None = None
    model_version: str | None = None
    model_bundle_id: str | None = None
    score: float | None = None
    reason: str | None = None

    def __post_init__(self):
        utc(self.generated_at)
        utc(self.input_watermark)
        if self.input_watermark > self.generated_at:
            raise ValueError('future signal input')
        if self.direction not in {'BUY', 'SELL', 'HOLD'} or self.status not in {
                'SIGNAL', 'NO_SIGNAL', 'INSUFFICIENT_DATA', 'REJECTED'}:
            raise ValueError('invalid signal state')
        if not all(isinstance(v, str) and v for v in
                   (self.strategy_id, self.strategy_version, self.symbol, self.source, self.signal_id)):
            raise ValueError('signal identity required')
        if self.target_weight is not None and (not isinstance(self.target_weight, Decimal)
                or not self.target_weight.is_finite() or not 0 <= self.target_weight <= 1):
            raise ValueError('long-only target weight must be in [0,1]')
        if self.direction == 'HOLD' and self.target_weight is not None:
            raise ValueError('HOLD must not change target')
        if self.status == 'SIGNAL' and self.direction != 'HOLD' and self.target_weight is None:
            raise ValueError('action requires target')


class Strategy(Protocol):
    def describe(self) -> dict: ...
    def signal(self, bars: list[ClosedBar], now: datetime) -> StrategySignal: ...


@dataclass(frozen=True)
class DualMAStrategy:
    symbol: str = 'NVDAUSDT'
    interval_seconds: int = 3600
    max_weight: Decimal = Decimal('.1')
    source: str = 'BINANCE_USDM_PUBLIC_REST'
    strategy_id: str = 'dual_ma'
    strategy_version: str = '1'
    history_limit: int = 21

    def __post_init__(self):
        if (self.symbol != 'NVDAUSDT' or type(self.interval_seconds) is not int
                or self.interval_seconds <= 0 or not isinstance(self.max_weight, Decimal)
                or not self.max_weight.is_finite() or not 0 < self.max_weight <= 1):
            raise ValueError('invalid DualMA deployment')

    def describe(self):
        return asdict(self)

    def signal(self, bars, now):
        from .realtime import identity
        bar = bars[-1]
        if any(b.symbol != self.symbol or b.source != self.source
               or b.interval_seconds != self.interval_seconds or not b.complete
               or b.closed_at > now for b in bars):
            raise ValueError('bar deployment mismatch')
        direction, target, status = 'HOLD', None, 'INSUFFICIENT_DATA'
        if len(bars) >= self.history_limit:
            _, _, buy, sell = crossover_indicators(pd.Series([float(b.close) for b in bars]), 5, 20)
            status = 'SIGNAL' if buy.iloc[-1] or sell.iloc[-1] else 'NO_SIGNAL'
            if buy.iloc[-1]:
                direction, target = 'BUY', self.max_weight
            elif sell.iloc[-1]:
                direction, target = 'SELL', Decimal(0)
        return StrategySignal(self.strategy_id, self.strategy_version, self.symbol, now,
            bar.closed_at, direction, target, self.source,
            identity((self.describe(), bar.closed_at)), status=status)
