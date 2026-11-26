"""Read-only market benchmark signal adapters (e.g. ^SP500TR) separate from tradable equities.

Alpaca's US equity endpoints (/v2/stocks/*) serve tradable equities and ETFs only; they do
NOT provide the S&P 500 Total Return Index (^SP500TR). Substituting SPY, QQQ, or the price
index (^GSPC) is explicitly prohibited because dividend reinvestment changes the 63-day,
126-day, 200-day moving averages, 252-day drawdown, and residual-return Ridge features.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from factorlab.strategies.v179_v182 import BENCHMARK_SERIES, DISALLOWED_BENCHMARK_SUBSTITUTES
from .cloud_execution import PreparationIncomplete

ALPACA_BENCHMARK_CAPABILITY = {
    'alpaca_stocks_api_supports_sp500tr': False,
    'required_series_id': BENCHMARK_SERIES,
    'disallowed_substitutes': sorted(DISALLOWED_BENCHMARK_SUBSTITUTES),
    'reason': (
        'Alpaca /v2/stocks/bars only provides tradable US equities/ETFs, not S&P 500 Total '
        'Return Index (^SP500TR). A dedicated read-only benchmark provider is required.'
    ),
}


def validate_benchmark_series_id(series_id: str) -> str:
    if not isinstance(series_id, str) or series_id != BENCHMARK_SERIES or series_id in DISALLOWED_BENCHMARK_SUBSTITUTES:
        raise ValueError(
            f'prohibited or unsupported benchmark series {series_id!r}; '
            f'only read-only {BENCHMARK_SERIES} is permitted (never SPY/QQQ/^GSPC)'
        )
    return series_id


@dataclass(frozen=True)
class BenchmarkHistory:
    series_id: str
    source: str
    history_basis: str
    acquired_at: datetime
    records: tuple[dict, ...]
    provenance: dict


def _normalize_benchmark_frame(
    frame: pd.DataFrame,
    *,
    series_id: str,
    source: str,
    calendar,
    received_at: pd.Timestamp,
) -> list[dict]:
    validate_benchmark_series_id(series_id)
    if 'close' not in frame.columns:
        raise PreparationIncomplete('INVALID_BENCHMARK_SCHEMA:' + series_id)
    records = []
    for idx, row in frame.iterrows():
        day = pd.Timestamp(idx).normalize()
        if day not in calendar.schedule.index:
            continue
        close_val = float(row['close'])
        if not np.isfinite(close_val) or close_val <= 0:
            continue
        eff = calendar.at(day, 'close')
        obs = pd.Timestamp(row['received_at']) if 'received_at' in row and pd.notna(row['received_at']) else received_at
        if obs.tzinfo is None:
            obs = obs.tz_localize('UTC')
        complete = bool(row['complete']) if 'complete' in row and pd.notna(row['complete']) else True
        if not complete or eff > obs:
            continue
        records.append({
            'observed_at': obs.to_pydatetime(),
            'effective_at': eff.to_pydatetime(),
            'source': source,
            'payload': {
                'series_id': series_id,
                'session': str(day.date()),
                'close': close_val,
                'history_basis': 'total_return_close',
                'complete': True,
            },
        })
    records.sort(key=lambda item: item['effective_at'])
    return records


class LocalBenchmarkProvider:
    """Causal read-only ^SP500TR provider backed by a verified local Parquet/CSV/JSON snapshot."""

    def __init__(
        self,
        path: Path | str,
        *,
        series_id: str = BENCHMARK_SERIES,
        source: str = 'LOCAL_SNAPSHOT:^SP500TR',
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        expected_sha256: str | None = None,
    ):
        self.series_id = validate_benchmark_series_id(series_id)
        self.path = Path(path)
        self.source = source
        self.clock = clock
        self.expected_sha256 = expected_sha256

    def _load_frame(self) -> tuple[pd.DataFrame, str]:
        if not self.path.is_file():
            raise PreparationIncomplete('MISSING_BENCHMARK_SOURCE:' + self.series_id)
        raw_bytes = self.path.read_bytes()
        digest = sha256(raw_bytes).hexdigest()
        if self.expected_sha256 is not None and digest != self.expected_sha256:
            raise PreparationIncomplete('BENCHMARK_DIGEST_MISMATCH:' + self.series_id)
        suffix = self.path.suffix.lower()
        if suffix == '.parquet':
            df = pd.read_parquet(self.path)
        elif suffix == '.csv':
            df = pd.read_csv(self.path, index_col=0, parse_dates=True)
        elif suffix == '.json':
            payload = json.loads(raw_bytes.decode('utf-8'))
            if isinstance(payload, dict) and 'records' in payload:
                df = pd.DataFrame(payload['records']).set_index('session')
                df.index = pd.to_datetime(df.index)
            else:
                df = pd.DataFrame(payload)
                if 'session' in df.columns:
                    df = df.set_index(pd.to_datetime(df['session']))
        else:
            raise PreparationIncomplete('UNSUPPORTED_BENCHMARK_FORMAT:' + suffix)
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        return df.sort_index(), digest

    def history(
        self,
        series_id: str,
        *,
        calendar,
        days: pd.DatetimeIndex,
        signal_cutoff: datetime,
        now: datetime,
    ) -> BenchmarkHistory:
        validate_benchmark_series_id(series_id)
        if days.empty:
            raise PreparationIncomplete('INSUFFICIENT_BENCHMARK_CALENDAR:' + series_id)
        df, digest = self._load_frame()
        acquired = pd.Timestamp(self.clock())
        now_ts = pd.Timestamp(now)
        cutoff_ts = pd.Timestamp(signal_cutoff)
        window_df = df.loc[df.index.isin(days)]
        records = _normalize_benchmark_frame(
            window_df,
            series_id=series_id,
            source=self.source,
            calendar=calendar,
            received_at=acquired,
        )
        causal = [
            rec for rec in records
            if pd.Timestamp(rec['effective_at']) <= cutoff_ts
            and pd.Timestamp(rec['observed_at']) <= now_ts
        ]
        if len(causal) != len(days) or pd.Timestamp(causal[-1]['effective_at']) != cutoff_ts:
            raise PreparationIncomplete('INCOMPLETE_BENCHMARK:' + series_id)
        return BenchmarkHistory(
            series_id=series_id,
            source=self.source,
            history_basis='total_return_close',
            acquired_at=acquired.to_pydatetime(),
            records=tuple(causal),
            provenance={
                'path': str(self.path.name),
                'sha256': digest,
                'coverage_start': str(days[0].date()),
                'coverage_end': str(days[-1].date()),
            },
        )


def _default_yahoo_transport(url: str, timeout: float) -> tuple[int, dict]:
    req = Request(url, headers={'User-Agent': 'FactorLab/1.0 (read-only benchmark)'}, method='GET')
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except HTTPError as exc:
        return exc.code, {}


class YahooSP500TRProvider:
    """Read-only HTTP provider for Yahoo Finance ^SP500TR chart history with injectable transport."""

    base_url = 'https://query1.finance.yahoo.com/v8/finance/chart/%5ESP500TR'

    def __init__(
        self,
        *,
        transport: Callable[[str, float], tuple[int, dict]] = _default_yahoo_transport,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        timeout: float = 15.0,
    ):
        self.transport = transport
        self.clock = clock
        self.timeout = timeout

    def history(
        self,
        series_id: str,
        *,
        calendar,
        days: pd.DatetimeIndex,
        signal_cutoff: datetime,
        now: datetime,
    ) -> BenchmarkHistory:
        validate_benchmark_series_id(series_id)
        if days.empty:
            raise PreparationIncomplete('INSUFFICIENT_BENCHMARK_CALENDAR:' + series_id)
        period1 = int((pd.Timestamp(days[0]).tz_localize('UTC') - pd.Timedelta(days=5)).timestamp())
        period2 = int((pd.Timestamp(days[-1]).tz_localize('UTC') + pd.Timedelta(days=2)).timestamp())
        url = self.base_url + '?' + urlencode({
            'period1': period1,
            'period2': period2,
            'interval': '1d',
            'includeAdjustedClose': 'true',
        })
        try:
            status, payload = self.transport(url, self.timeout)
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise PreparationIncomplete('BENCHMARK_TRANSPORT_ERROR:' + series_id) from exc
        if status != 200 or not isinstance(payload, dict):
            raise PreparationIncomplete(f'BENCHMARK_HTTP_ERROR:{series_id}:{status}')
        acquired = pd.Timestamp(self.clock())
        try:
            result = payload['chart']['result'][0]
            timestamps = result['timestamp']
            closes = result['indicators']['quote'][0]['close']
        except (KeyError, IndexError, TypeError) as exc:
            raise PreparationIncomplete('INVALID_BENCHMARK_PAYLOAD:' + series_id) from exc
        rows = {}
        for ts, close_val in zip(timestamps, closes):
            if close_val is None or not np.isfinite(float(close_val)):
                continue
            day = pd.Timestamp(ts, unit='s', tz='UTC').tz_convert(calendar.timezone).tz_localize(None).normalize()
            rows[day] = float(close_val)
        df = pd.DataFrame({'close': pd.Series(rows, dtype=float)}).sort_index()
        df = df.loc[df.index.isin(days)]
        records = _normalize_benchmark_frame(
            df,
            series_id=series_id,
            source='YAHOO:^SP500TR',
            calendar=calendar,
            received_at=acquired,
        )
        now_ts = pd.Timestamp(now)
        cutoff_ts = pd.Timestamp(signal_cutoff)
        causal = [
            rec for rec in records
            if pd.Timestamp(rec['effective_at']) <= cutoff_ts
            and pd.Timestamp(rec['observed_at']) <= now_ts
        ]
        if len(causal) != len(days) or pd.Timestamp(causal[-1]['effective_at']) != cutoff_ts:
            raise PreparationIncomplete('INCOMPLETE_BENCHMARK:' + series_id)
        digest = sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        return BenchmarkHistory(
            series_id=series_id,
            source='YAHOO:^SP500TR',
            history_basis='total_return_close',
            acquired_at=acquired.to_pydatetime(),
            records=tuple(causal),
            provenance={
                'url': url,
                'sha256': digest,
                'coverage_start': str(days[0].date()),
                'coverage_end': str(days[-1].date()),
            },
        )


__all__ = [
    'ALPACA_BENCHMARK_CAPABILITY',
    'BENCHMARK_SERIES',
    'BenchmarkHistory',
    'LocalBenchmarkProvider',
    'YahooSP500TRProvider',
    'validate_benchmark_series_id',
]
