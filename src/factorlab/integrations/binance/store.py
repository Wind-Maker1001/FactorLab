"""Immutable raw/Parquet generations and a network-independent replay reader."""
import json
import os
from pathlib import Path
import uuid

import pandas as pd

from factorlab.data.evidence import json_bytes, sha256
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.errors import DataQualityError
from .market import BOUNDARIES

SCHEMA_VERSION = 1
TABLES = ('bars', 'funding', 'mark')


def _write_json(path, value):
    """Commit recovery state/receipts only after their bytes are durable."""
    path = Path(path)
    temporary = path.with_name('.tmp-' + uuid.uuid4().hex)
    with temporary.open('xb') as stream:
        stream.write(json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _local(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise DataQualityError('snapshot reference escapes its generation')
    return path


def resolve_snapshot(path):
    path = Path(path)
    if (path / 'latest.json').exists():
        pointer = json.loads((path / 'latest.json').read_text())
        return _local(path, pointer['generation'])
    return path


class Reader:
    """Read a pinned generation without constructing any network client."""

    def __init__(self, path):
        self.root = resolve_snapshot(path)
        self.manifest = json.loads((self.root / 'manifest.json').read_text())
        if self.manifest['schema_version'] != SCHEMA_VERSION:
            raise DataQualityError('unsupported Binance snapshot schema')
        if tuple(self.manifest['boundaries']) != BOUNDARIES:
            raise DataQualityError('snapshot financial boundaries differ')
        identity = {k: v for k, v in self.manifest.items() if k != 'data_version'}
        if sha256(json_bytes(identity)) != self.manifest['data_version']:
            raise DataQualityError('snapshot manifest version mismatch')

    def read(self, kind):
        if kind not in TABLES:
            raise ValueError('unknown read-only table')
        descriptor = self.manifest['tables'][kind]
        path = _local(self.root, descriptor['path'])
        if sha256(path.read_bytes()) != descriptor['sha256']:
            raise DataQualityError('Parquet digest mismatch')
        frame = pd.read_parquet(path)
        if len(frame) != descriptor['rows']:
            raise DataQualityError('Parquet row count mismatch')
        meta = self.manifest
        return QueryResult(frame, SourceMetadata(
            source_id='BINANCE_USDM_PUBLIC_REST', data_version=meta['data_version'],
            calendar_id='BINANCE_PERPETUAL_UTC', calendar_kind='PERPETUAL_UTC_BUCKETS',
            acquired_at=meta['acquired_at'], coverage='REQUESTED_RANGE; SEE_INTEGRITY_REPORT',
            time_fields={'effective': {'bars': 'open_time', 'funding': 'funding_time',
                                       'mark': 'event_time'}[kind],
                         'acquired': 'manifest.acquired_at', 'historical_available': 'UNKNOWN'},
            provenance={'contract': meta['contract'], 'range': meta['range'],
                        'integrity': meta['integrity'], 'boundaries': list(BOUNDARIES)},
            evidence_status=BOUNDARIES,
            field_semantics={'funding_rate': {'meaning': 'settlement rate, not price return'},
                             'prices': {'meaning': 'native unadjusted perpetual contract quotes',
                                        'precision': 'exchange decimal strings'}},
        ))

    def verify_raw(self):
        """Replay every raw receipt, including failed HTTP attempts."""
        contract = self.manifest['contract']
        if contract['underlying'] is not None:
            evidence = _local(self.root, contract['underlying_evidence_path']).read_bytes()
            if sha256(evidence) != contract['underlying']['evidence_sha256']:
                raise DataQualityError('underlying mapping evidence digest mismatch')
        for receipt in self.manifest['raw_receipts']:
            body = _local(self.root, receipt['raw_path']).read_bytes()
            if sha256(body) != receipt['sha256']:
                raise DataQualityError('raw response digest mismatch')
        return len(self.manifest['raw_receipts'])


def publish(root, pending, tables, metadata):
    root, pending = Path(root), Path(pending)
    descriptors = {}
    for name, frame in tables.items():
        path = pending / (name + '.parquet')
        frame.to_parquet(path, index=False)
        descriptors[name] = {'path': path.name, 'sha256': sha256(path.read_bytes()), 'rows': len(frame)}
    receipts = [json.loads(path.read_text()) for path in sorted((pending / 'requests').glob('*.json'))
                if len(path.stem) != 64]  # Cache indexes duplicate an existing attempt receipt.
    manifest = dict(metadata, schema_version=SCHEMA_VERSION, boundaries=list(BOUNDARIES),
                    tables=descriptors, raw_receipts=receipts)
    manifest['data_version'] = sha256(json_bytes(manifest))
    _write_json(pending / 'manifest.json', manifest)
    generation = 'runs/' + uuid.uuid4().hex
    target = root / generation
    target.parent.mkdir(exist_ok=True)
    pending.rename(target)
    _write_json(root / 'latest.json', {'generation': generation})
    return Reader(target)
