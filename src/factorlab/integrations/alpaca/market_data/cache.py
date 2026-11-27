"""Immutable market-data vintages with an atomic active-generation pointer."""
from collections import OrderedDict
from dataclasses import dataclass, asdict
import hashlib
import io
import json
import os
from pathlib import Path
import uuid

import pandas as pd

from factorlab.cross_market.data import ParquetDataProvider
from factorlab.data.publication import publication_lock


@dataclass(frozen=True)
class Series:
    instrument_id: str
    symbol: str
    timeframe: str
    feed: str
    adjustment: str
    asof: str
    session_scope: str = 'vendor'

    @property
    def key(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


class MarketCache:
    def __init__(self, root, *, max_cached=32):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._max_cached = max(0, int(max_cached))
        self._frames = OrderedDict()

    def _cache_put(self, cache_key, frame):
        if self._max_cached <= 0:
            return
        self._frames[cache_key] = frame
        self._frames.move_to_end(cache_key)
        while len(self._frames) > self._max_cached:
            self._frames.popitem(last=False)

    def load(self, series):
        pointer = self.root / (series.key + '.json')
        if not pointer.exists():
            return pd.DataFrame(), None
        metadata = json.loads(pointer.read_text())
        if metadata['series'] != asdict(series):
            raise ValueError('cache series mismatch')
        name = metadata['generation']
        if Path(name).name != name:
            raise ValueError('invalid cache generation path')
        payload = (self.root / name / 'bars.parquet').read_bytes()
        if hashlib.sha256(payload).hexdigest() != metadata['sha256']:
            raise ValueError('cache generation integrity failure')
        cache_key = (series.key, name, metadata['sha256'])
        cached = self._frames.get(cache_key)
        if cached is not None:
            self._frames.move_to_end(cache_key)
            return cached.copy(), metadata
        frame = pd.read_parquet(io.BytesIO(payload))
        self._cache_put(cache_key, frame)
        return frame.copy(), metadata

    def publish(self, series, rows, *, start, end, calendar_version, captured_at):
        """Replace the requested interval; adjusted updates must cover the old window."""
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        if start > end:
            raise ValueError('reversed capture range')
        with publication_lock(self.root, series.key):
            old, previous = self.load(series)
            if previous and series.adjustment != 'raw':
                if start > pd.Timestamp(previous['start']) or end < pd.Timestamp(previous['end']):
                    raise ValueError('adjusted history requires full-window refresh')
            frame = rows.copy()
            if not {'date', 'instrument_id', 'event_at'} <= set(frame.columns):
                raise ValueError('market cache requires date, identity and event key')
            if not frame.instrument_id.eq(series.instrument_id).all():
                raise ValueError('cache identity mismatch')
            frame['date'] = pd.to_datetime(frame.date)
            if not frame.date.between(start, end).all():
                raise ValueError('response outside requested dates')
            if frame.duplicated(['instrument_id', 'event_at']).any():
                raise ValueError('duplicate source observations')
            if previous:
                old = old.loc[~old.date.between(start, end)]
                frame = pd.concat([old, frame], ignore_index=True)
            frame = frame.sort_values(['date', 'event_at']).reset_index(drop=True)
            generation = series.key + '-' + uuid.uuid4().hex
            folder = self.root / generation
            folder.mkdir()
            payload = frame.to_parquet(index=False)
            with (folder / 'bars.parquet').open('xb') as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            metadata = {'series': asdict(series), 'generation': generation,
                        'predecessor': previous['generation'] if previous else None,
                        'start': str(min(start, pd.Timestamp(previous['start'])) if previous else start),
                        'end': str(max(end, pd.Timestamp(previous['end'])) if previous else end),
                        'requests': (previous['requests'] if previous else []) + [[str(start), str(end)]],
                        'calendar_version': calendar_version, 'captured_at': str(captured_at),
                        'sha256': hashlib.sha256(payload).hexdigest(), 'rows': len(frame)}
            (folder / 'manifest.json').write_text(json.dumps(metadata, sort_keys=True))
            temporary = self.root / (generation + '.pointer')
            with temporary.open('x') as output:
                json.dump(metadata, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.root / (series.key + '.json'))
            self._cache_put((series.key, generation, metadata['sha256']), frame.copy())
            return metadata

    def provider(self, series):
        _, metadata = self.load(series)
        if metadata is None:
            raise ValueError('series not captured')
        return ParquetDataProvider(self.root / metadata['generation'] / 'bars.parquet', metadata['sha256'])

    def update(self, series, fetch, *, start, end, calendar_version, captured_at, overlap_days=5):
        """Fetch missing civil-date ranges and revalidate a raw tail; never fill bars."""
        _, previous = self.load(series)
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        if not previous or series.adjustment != 'raw':
            ranges = [(min(start, pd.Timestamp(previous['start'])) if previous else start,
                       max(end, pd.Timestamp(previous['end'])) if previous else end)]
        else:
            from factorlab.data.cache import _subtract_ranges
            covered = [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in previous['requests']]
            ranges = _subtract_ranges((start, end), covered)
            ranges.append((max(start, pd.Timestamp(previous['end']) - pd.Timedelta(days=overlap_days)), end))
            ranges = [(a, b) for a, b in ranges if a <= b]
        for a, b in ranges:
            rows = fetch(a, b)
            self.publish(series, rows, start=a, end=b, calendar_version=calendar_version, captured_at=captured_at)
        return self.load(series)
