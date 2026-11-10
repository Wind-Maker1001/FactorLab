"""Append-only raw receipts and bounded Parquet batches; offline-only replay."""
import json
from pathlib import Path
import uuid

import pandas as pd

from factorlab.data.evidence import json_bytes, sha256, utc_now
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.errors import DataQualityError
from . import BOUNDARIES, SCHEMA_VERSION, SOURCE_VERSION

TABLES = ('symbols', 'quotes', 'prices', 'bars', 'open_bars', 'sessions', 'gaps')


class Sink:
    def __init__(self, path):
        self.root = Path(path)
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / 'raw').mkdir()
        self.started_at = utc_now()
        self.count = 0
        self.buffers = {name: [] for name in TABLES}
        self.parts = {name: [] for name in TABLES}
        self.capabilities = {}

    def raw(self, body, **metadata):
        identifier = f'{self.count:09d}'
        (self.root / 'raw' / (identifier + '.bin')).write_bytes(body)
        receipt = dict(metadata, sha256=sha256(body), size=len(body), source_version=SOURCE_VERSION)
        (self.root / 'raw' / (identifier + '.json')).write_bytes(json_bytes(receipt))
        self.count += 1
        return identifier

    def add(self, table, rows):
        self.buffers[table].extend(rows)
        if len(self.buffers[table]) >= 1000:
            self._flush(table)

    def _flush(self, table):
        rows = self.buffers[table]
        if not rows:
            return
        name = f'{table}-{len(self.parts[table]):06d}.parquet'
        frame = pd.DataFrame(rows)
        # Nullable epoch/count values must never take a float round-trip.
        for column in ('event_time', 'server_event_time', 'open_time', 'close_time',
                       'trade_count', 'listingTime', 'delistingTime'):
            if column in frame:
                frame[column] = pd.array([row.get(column) for row in rows], dtype='Int64')
        frame.to_parquet(self.root / name, index=False)
        self.parts[table].append(dict(path=name, rows=len(rows), sha256=sha256((self.root / name).read_bytes())))
        rows.clear()

    def close(self, *, state='COMPLETE'):
        for table in TABLES:
            self._flush(table)
        raw_index = []
        for index in range(self.count):
            name = f'raw/{index:09d}.json'
            raw_index.append(dict(path=name, sha256=sha256((self.root / name).read_bytes())))
        manifest = dict(schema_version=SCHEMA_VERSION, source_version=SOURCE_VERSION,
                        boundaries=list(BOUNDARIES), started_at=self.started_at, acquired_at=utc_now(),
                        state=state, tables=self.parts, raw_receipts=raw_index,
                        capabilities=self.capabilities, historical_market_data='UNVERIFIED',
                        historical_rules='UNVERIFIED', calendar='EXCHANGE_REPORTED_PHASES_ONLY')
        manifest['data_version'] = sha256(json_bytes(manifest))
        temporary = self.root / ('.manifest-' + uuid.uuid4().hex)
        temporary.write_bytes(json_bytes(manifest))
        temporary.replace(self.root / 'manifest.json')
        return Reader(self.root)


class Reader:
    def __init__(self, path):
        self.root = Path(path).resolve()
        self.manifest = json.loads((self.root / 'manifest.json').read_bytes())
        identity = {key: value for key, value in self.manifest.items() if key != 'data_version'}
        if (self.manifest['schema_version'] != SCHEMA_VERSION
                or tuple(self.manifest['boundaries']) != BOUNDARIES
                or sha256(json_bytes(identity)) != self.manifest['data_version']):
            raise DataQualityError('invalid Equity manifest version or boundaries')

    def _checked(self, descriptor):
        path = (self.root / descriptor['path']).resolve()
        if not path.is_relative_to(self.root):
            raise DataQualityError('Equity snapshot path escapes its root')
        body = path.read_bytes()
        if sha256(body) != descriptor['sha256']:
            raise DataQualityError('Equity snapshot content digest differs')
        return path, body

    def verify_raw(self):
        for descriptor in self.manifest['raw_receipts']:
            path, body = self._checked(descriptor)
            receipt = json.loads(body)
            self._checked(dict(path=path.relative_to(self.root).with_suffix('.bin').as_posix(),
                               sha256=receipt['sha256']))
        return len(self.manifest['raw_receipts'])

    def read(self, table):
        if table not in TABLES:
            raise ValueError('unknown Equity read-only table')
        parts = []
        for descriptor in self.manifest['tables'][table]:
            path, _ = self._checked(descriptor)
            frame = pd.read_parquet(path)
            if len(frame) != descriptor['rows']:
                raise DataQualityError('Equity Parquet row count differs')
            parts.append(frame)
        frame = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        return QueryResult(frame, SourceMetadata(
            source_id='BINANCE_EQUITY', data_version=self.manifest['data_version'],
            calendar_id='BINANCE_EQUITY_REPORTED_PHASE', calendar_kind='STOCK_SESSIONS',
            acquired_at=self.manifest['acquired_at'], coverage='OBSERVED_ONLY; SEE_GAPS',
            time_fields={'effective': 'event_time (nullable, epoch milliseconds)',
                         'acquired': 'acquired_at', 'historical_available': 'UNKNOWN'},
            evidence_status=BOUNDARIES,
            provenance={'source_version': self.manifest['source_version'],
                        'state': self.manifest['state'], 'table': table,
                        'historical_market_data': 'UNVERIFIED', 'historical_rules': 'UNVERIFIED'},
            field_semantics={'prices': {'currency': 'USD', 'adjustment': 'NATIVE_UNADJUSTED',
                                        'precision': 'original decimal strings'},
                             'quantity': {'unit': 'shares', 'precision': 'exact strings'},
                             'session': {'meaning': 'exchange-reported phase; UNKNOWN if absent'},
                             'bars': {'meaning': 'closed received candles only; no historical backfill'}},
        ))
