"""Deterministic nonfinancial fixtures with warmup, gaps, zero and extremes."""

import hashlib

import numpy as np
import pandas as pd

from factorlab.data.schema import CANONICAL_BAR_COLUMNS, validate_bars


def make_synthetic_data(seed=20261014, stock_count=40, sessions=320):
    """Explicit synthetic business-day calendar, never an exchange calendar."""
    dates = pd.bdate_range("2023-01-02", periods=sessions)
    frames = []
    for number in range(stock_count):
        random = np.random.RandomState(seed + number)
        t = np.arange(sessions, dtype=float)
        returns = (
            0.00015 * (number % 7 - 3)
            + 0.004 * np.sin(t / (9 + number % 11))
            + random.normal(0, 0.006 + 0.0004 * (number % 9), sessions)
        )
        close = (10 + number) * np.exp(np.cumsum(returns))
        opening = close * (1 + random.normal(0, 0.0015, sessions))
        high = np.maximum(opening, close) * 1.008
        low = np.minimum(opening, close) * 0.992
        volume = (100000 + number * 8000) * np.exp(random.normal(0, 0.3, sessions))
        volume *= 1 + 0.18 * np.sin(t / (5 + number % 13))
        turnover = volume / (20000000 + number * 2000000)
        amount = volume * (opening + close) / 2
        if number == 0 and sessions > 150:
            volume[150], amount[150], turnover[150] = 0, 0, 0
        if number == 0 and sessions > 181:
            volume[181], amount[181], turnover[181] = 1e250, 1e300, 1e200
        factor = 1.1 + number / 200
        frame = pd.DataFrame(
            {
                "date": dates,
                "symbol": "%06d.SZ" % (number + 1),
                "volume": volume,
                "amount": amount,
                "turnover": turnover,
                "adj_factor": factor,
            }
        )
        for name, values in (
            ("open", opening),
            ("high", high),
            ("low", low),
            ("close", close),
        ):
            frame[name] = values
            frame["raw_" + name] = values
            frame["adj_" + name] = values * factor
        if number in (1, 2, 3):
            frame = frame.drop(
                index=[
                    index
                    for index in (145 + number, 180 + number, 220 + number)
                    if index < sessions
                ]
            )
        frames.append(frame.loc[:, CANONICAL_BAR_COLUMNS])
    bars = (
        pd.concat(frames, ignore_index=True)
        .sort_values(["date", "symbol"])
        .reset_index(drop=True)
    )
    benchmark_close = 5000 * np.exp(
        np.cumsum(0.0001 + 0.003 * np.sin(np.arange(sessions) / 23))
    )
    benchmark = pd.DataFrame(
        {"date": dates, "adj_open": benchmark_close, "symbol": "000905.SH"}
    )
    validate_bars(bars, allow_missing_ohlc=True)
    return bars, benchmark, dates


def frame_sha256(frame):
    """Stable exact binary float/key hash; no timings or labels in factor hash."""
    data = frame.copy()
    for column in data:
        if pd.api.types.is_float_dtype(data[column]):
            values = data[column].to_numpy(dtype=np.float64, copy=True)
            values[np.isnan(values)] = np.nan
            data[column] = values
    digest = hashlib.sha256()
    digest.update("\0".join(data.columns).encode("utf-8"))
    digest.update(
        pd.util.hash_pandas_object(data, index=False).to_numpy(dtype="<u8").tobytes()
    )
    return digest.hexdigest()
