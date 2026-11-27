"""Frozen market observations and exact funding coefficients for batch research."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction

import numpy as np

HOUR_US = 3_600_000_000


def _timestamps(values, name):
    raw = np.asarray(values)
    if raw.ndim != 1 or not len(raw):
        raise ValueError(f'{name} requires a nonempty one-dimensional UTC timeline')
    if raw.dtype.kind in 'iu':
        if raw.dtype.kind == 'u' and np.any(raw > np.iinfo(np.int64).max):
            raise ValueError(f'{name} exceeds signed microsecond range')
        result = np.ascontiguousarray(raw, dtype=np.int64)
    else:
        stamps = []
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        for value in values:
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f'{name} requires timezone-aware timestamps or UTC microsecond integers')
            delta = value.astimezone(timezone.utc) - epoch
            stamps.append((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds)
        result = np.asarray(stamps, dtype=np.int64)
    if np.any(result[1:] <= result[:-1]):
        raise ValueError(f'{name} must be strictly increasing without duplicates')
    return result


def _matrix(values, paths, columns, name):
    raw = np.asarray(values)
    if raw.shape == (columns,):
        raw = np.broadcast_to(raw, (paths, columns))
    if raw.shape != (paths, columns):
        raise ValueError(f'{name} requires [event] or [path, event] shape')
    return raw


@dataclass(frozen=True)
class BatchMarketInputs:
    """Observation times are completed bar closes; warmup ends at the anchor.

    Funding history before the anchor informs policy signals only. Actual
    charges use events in (anchor, current observation], before current fills.
    Settlements exactly at the endowed anchor require an unsupported holding convention.
    An explicit expected settlement timeline distinguishes missing data from zero.
    """
    observation_times: object
    highs: object | None = None
    lows: object | None = None
    funding_times: object | None = None
    funding_rates: object | None = None
    funding_marks: object | None = None
    expected_funding_times: object | None = None
    warmup_closes: object | None = None

    def prepare(self, prices, config):
        paths, steps = prices.shape
        times = _timestamps(self.observation_times, 'observation_times')
        if times.shape != (steps,):
            raise ValueError('one observation timestamp per quote required')
        hourly = config.funding_policy is not None or config.volatility_policy is not None or self.funding_times is not None
        if hourly and (np.any(np.diff(times) != HOUR_US) or np.any((times + 1) % HOUR_US)):
            raise ValueError('funding and B/C profiles require complete hourly bar-close timestamps (open + 1h - 1us)')
        if (self.highs is None) != (self.lows is None):
            raise ValueError('highs and lows must be supplied together')
        if self.highs is None:
            if config.grid.mode == 'atr':
                raise ValueError('ATR requires actual completed high/low bars')
            bars = np.stack((prices, prices), axis=-1)
        else:
            high, low = np.asarray(self.highs, dtype=float), np.asarray(self.lows, dtype=float)
            if high.shape != prices.shape or low.shape != prices.shape:
                raise ValueError('highs and lows must match [path, observation] quotes')
            if (not np.isfinite(high).all() or not np.isfinite(low).all()
                    or np.any(low <= 0) or np.any(high > 1_000_000)
                    or np.any(low > prices) or np.any(high < prices)):
                raise ValueError('finite positive low <= close <= high <= 1000000 required')
            bars = np.stack((high, low), axis=-1)
        supplied = (self.funding_times, self.funding_rates, self.funding_marks, self.expected_funding_times)
        if any(x is not None for x in supplied) and not all(x is not None for x in supplied):
            raise ValueError('funding requires times, rates, marks and expected_funding_times together')
        if self.funding_times is None:
            if config.funding_policy is not None:
                raise ValueError('funding-sensitive profile requires explicit funding history and schedule')
            event_times = np.empty(0, dtype=np.int64)
            rates = np.empty((paths, 0), dtype=np.float64)
            marks = np.empty((paths, 0), dtype=np.float64)
            raw_rates = raw_marks = np.empty((paths, 0), dtype=object)
            coeff = np.empty((paths, 0, 2), dtype=np.int64)
        else:
            event_times = _timestamps(self.funding_times, 'funding_times')
            expected = _timestamps(self.expected_funding_times, 'expected_funding_times')
            if np.any(event_times == times[0]):
                from . import UnsupportedGridSimulation
                raise UnsupportedGridSimulation('anchor settlement requires an explicit pre-endowment holding convention; begin settlement schedule after anchor')
            if not np.array_equal(event_times, expected):
                raise ValueError('funding schedule is missing settlements or contains unexpected settlements')
            if event_times[-1] > times[-1]:
                raise ValueError('funding events must not exceed final observation')
            common = np.asarray(self.funding_rates).ndim == 1 and np.asarray(self.funding_marks).ndim == 1
            coefficient_paths = 1 if common else paths
            raw_rates = _matrix(self.funding_rates, coefficient_paths, len(event_times), 'funding_rates')
            marks = _matrix(self.funding_marks, coefficient_paths, len(event_times), 'funding_marks')
            raw_rates = np.asarray([[Decimal(str(x)) for x in row] for row in raw_rates], dtype=object)
            raw_marks = np.asarray([[Decimal(str(x)) for x in row] for row in marks], dtype=object)
            rates = np.empty(raw_rates.shape, dtype=np.float64)
            coeff = np.empty((*rates.shape, 2), dtype=np.int64)
            limit = np.iinfo(np.int64).max
            for index in np.ndindex(rates.shape):
                rate, mark = Decimal(str(raw_rates[index])), Decimal(str(marks[index]))
                if not rate.is_finite() or not mark.is_finite() or mark <= 0 or mark > 1_000_000:
                    raise ValueError('finite funding rate and positive settlement mark <=1000000 required')
                rates[index] = float(rate)
                # Fraction multiplication avoids Decimal context rounding before reduction.
                exact = -Fraction(config.rules.step_size) * Fraction(mark) * Fraction(rate) * 100_000_000
                if not -limit - 1 <= exact.numerator <= limit or exact.denominator > limit:
                    raise ValueError('funding coefficient numerator/denominator exceeds signed 64-bit range')
                coeff[index] = exact.numerator, exact.denominator
            if common:
                rates = np.broadcast_to(rates, (paths, len(event_times)))
                marks = np.broadcast_to(marks, rates.shape)
                coeff = np.ascontiguousarray(np.broadcast_to(coeff, (*rates.shape, 2)))
                raw_rates = np.broadcast_to(raw_rates, rates.shape)
                raw_marks = np.broadcast_to(raw_marks, rates.shape)
        if self.warmup_closes is None:
            if config.volatility_policy is not None:
                raise ValueError('volatility profile requires explicit warmup_closes including anchor')
            warmup = np.ascontiguousarray(prices[:, :1])
        else:
            warmup = np.asarray(self.warmup_closes, dtype=float)
            if warmup.ndim == 1:
                warmup = np.broadcast_to(warmup, (paths, len(warmup)))
            window = config.volatility_policy.vol_window_hours if config.volatility_policy is not None else 4096
            if (warmup.ndim != 2 or warmup.shape[0] != paths or not 1 <= warmup.shape[1] <= window + 1
                    or not np.isfinite(warmup).all() or np.any(warmup <= 0)
                    or np.any(warmup > 1_000_000) or np.any(warmup[:, -1] != prices[:, 0])):
                raise ValueError('warmup_closes must be finite positive [path, <=window+1] ending at anchor')
        return PreparedMarketInputs(np.ascontiguousarray(bars), times, event_times,
                                    np.ascontiguousarray(rates), np.ascontiguousarray(marks, dtype=np.float64),
                                    coeff, np.ascontiguousarray(warmup), raw_rates, raw_marks)


@dataclass(frozen=True)
class PreparedMarketInputs:
    bars: np.ndarray
    observation_us: np.ndarray
    funding_us: np.ndarray
    funding_rates: np.ndarray
    funding_marks: np.ndarray
    funding_coeff: np.ndarray
    warmup: np.ndarray
    funding_rates_decimal: np.ndarray
    funding_marks_decimal: np.ndarray

    def batch(self, begin, end):
        return (self.bars[begin:end], self.observation_us, self.funding_us,
                self.funding_rates[begin:end], self.funding_marks[begin:end],
                self.funding_coeff[begin:end], self.warmup[begin:end])
