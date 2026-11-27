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
    signal_cutoff: datetime | None = None
    knowledge_cutoff: datetime | None = None


def _to_utc_timestamp(value: datetime | pd.Timestamp, *, name: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError(f'aware {name} timestamp required')
    return ts.tz_convert('UTC')


def _resolve_knowledge_cutoff(
    acquired: pd.Timestamp,
    *,
    knowledge_cutoff: datetime | None = None,
    now: datetime | None = None,
) -> pd.Timestamp:
    """Resolve the knowledge-time horizon for observed_at filtering.

    - Live execution (both ``knowledge_cutoff`` and ``now`` omitted): uses the
      post-acquisition timestamp ``acquired`` so valid newly acquired records
      from completed sessions are never dropped by a pre-request clock read.
    - Historical PIT replay (explicit ``knowledge_cutoff`` or ``now``): enforces
      the caller-specified historical knowledge-time boundary without forging
      observation timestamps.
    """
    if knowledge_cutoff is not None and now is not None:
        kc = _to_utc_timestamp(knowledge_cutoff, name='knowledge_cutoff')
        nw = _to_utc_timestamp(now, name='now')
        if kc != nw:
            raise ValueError('knowledge_cutoff and now must match when both are provided')
        return kc
    explicit = knowledge_cutoff if knowledge_cutoff is not None else now
    if explicit is None:
        return acquired
    return _to_utc_timestamp(explicit, name='knowledge_cutoff')


def _filter_causal_records(
    records: list[dict],
    *,
    series_id: str,
    days: pd.DatetimeIndex,
    signal_cutoff: pd.Timestamp,
    knowledge_cutoff: pd.Timestamp,
    allow_revisions: bool = False,
) -> list[dict]:
    if signal_cutoff > knowledge_cutoff:
        raise PreparationIncomplete('INCOMPLETE_BENCHMARK:' + series_id)
    causal = [
        rec for rec in records
        if pd.Timestamp(rec['effective_at']).tz_convert('UTC') <= signal_cutoff
        and pd.Timestamp(rec['observed_at']).tz_convert('UTC') <= knowledge_cutoff
    ]
    if allow_revisions:
        latest_by_session: dict[str, dict] = {}
        for rec in sorted(
            causal,
            key=lambda item: (
                pd.Timestamp(item['effective_at']).tz_convert('UTC'),
                pd.Timestamp(item['observed_at']).tz_convert('UTC'),
            ),
        ):
            latest_by_session[rec['payload']['session']] = rec
        causal = sorted(
            latest_by_session.values(),
            key=lambda item: pd.Timestamp(item['effective_at']).tz_convert('UTC'),
        )
    expected_sessions = [str(d.date()) for d in days]
    actual_sessions = [rec['payload']['session'] for rec in causal]
    if (
        actual_sessions != expected_sessions
        or not causal
        or pd.Timestamp(causal[-1]['effective_at']).tz_convert('UTC') != signal_cutoff
    ):
        raise PreparationIncomplete('INCOMPLETE_BENCHMARK:' + series_id)
    return causal


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
    received_utc = _to_utc_timestamp(received_at, name='received_at')
    records = []
    for idx, row in frame.iterrows():
        day = pd.Timestamp(idx).normalize()
        if day not in calendar.schedule.index:
            continue
        close_val = float(row['close'])
        if not np.isfinite(close_val) or close_val <= 0:
            continue
        eff = calendar.at(day, 'close').tz_convert('UTC')
        if 'received_at' in row and pd.notna(row['received_at']):
            obs = pd.Timestamp(row['received_at'])
            obs = obs.tz_localize('UTC') if obs.tzinfo is None else obs.tz_convert('UTC')
        else:
            obs = received_utc
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
    records.sort(key=lambda item: (item['effective_at'], item['observed_at']))
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
        now: datetime | None = None,
        knowledge_cutoff: datetime | None = None,
    ) -> BenchmarkHistory:
        validate_benchmark_series_id(series_id)
        if days.empty:
            raise PreparationIncomplete('INSUFFICIENT_BENCHMARK_CALENDAR:' + series_id)
        cutoff_ts = _to_utc_timestamp(signal_cutoff, name='signal_cutoff')
        df, digest = self._load_frame()
        acquired = _to_utc_timestamp(self.clock(), name='acquired_at')
        knowledge_ts = _resolve_knowledge_cutoff(
            acquired,
            knowledge_cutoff=knowledge_cutoff,
            now=now,
        )
        window_df = df.loc[df.index.isin(days)]
        has_explicit_received_at = 'received_at' in window_df.columns
        if window_df.index.has_duplicates and not has_explicit_received_at:
            raise PreparationIncomplete('DUPLICATE_BENCHMARK_SESSION:' + series_id)
        records = _normalize_benchmark_frame(
            window_df,
            series_id=series_id,
            source=self.source,
            calendar=calendar,
            received_at=acquired,
        )
        causal = _filter_causal_records(
            records,
            series_id=series_id,
            days=days,
            signal_cutoff=cutoff_ts,
            knowledge_cutoff=knowledge_ts,
            allow_revisions=has_explicit_received_at,
        )
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
                'signal_cutoff': cutoff_ts.isoformat(),
                'knowledge_cutoff': knowledge_ts.isoformat(),
                'acquired_at': acquired.isoformat(),
            },
            signal_cutoff=cutoff_ts.to_pydatetime(),
            knowledge_cutoff=knowledge_ts.to_pydatetime(),
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
        now: datetime | None = None,
        knowledge_cutoff: datetime | None = None,
    ) -> BenchmarkHistory:
        validate_benchmark_series_id(series_id)
        if days.empty:
            raise PreparationIncomplete('INSUFFICIENT_BENCHMARK_CALENDAR:' + series_id)
        cutoff_ts = _to_utc_timestamp(signal_cutoff, name='signal_cutoff')
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
        acquired = _to_utc_timestamp(self.clock(), name='acquired_at')
        knowledge_ts = _resolve_knowledge_cutoff(
            acquired,
            knowledge_cutoff=knowledge_cutoff,
            now=now,
        )
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
        causal = _filter_causal_records(
            records,
            series_id=series_id,
            days=days,
            signal_cutoff=cutoff_ts,
            knowledge_cutoff=knowledge_ts,
            allow_revisions=False,
        )
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
                'signal_cutoff': cutoff_ts.isoformat(),
                'knowledge_cutoff': knowledge_ts.isoformat(),
                'acquired_at': acquired.isoformat(),
            },
            signal_cutoff=cutoff_ts.to_pydatetime(),
            knowledge_cutoff=knowledge_ts.to_pydatetime(),
        )


__all__ = [
    'ALPACA_BENCHMARK_CAPABILITY',
    'BENCHMARK_SERIES',
    'BenchmarkHistory',
    'LocalBenchmarkProvider',
    'YahooSP500TRProvider',
    'validate_benchmark_series_id',
]
