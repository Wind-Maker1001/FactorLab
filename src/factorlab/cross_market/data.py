"""Projected, predicate-filtered local providers and content-bound resumable tasks."""
import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
from .contracts import ContractError, session_date


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def implementation_sha():
    root = Path(__file__).parents[1]
    files = sorted(root.rglob('*.py'))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class ParquetDataProvider:
    def __init__(self, root, version):
        self.root, self.version = Path(root), version
        self.dataset = ds.dataset(str(self.root), format='parquet', partitioning='hive')
        if not version:
            raise ContractError('source version required')

    def batches(self, start, end, instrument_ids=None, columns=None, batch_size=65536):
        start, end = session_date(start), session_date(end)
        predicate = (ds.field('date') >= start) & (ds.field('date') <= end)
        if 'year' in self.dataset.schema.names:
            predicate = predicate & (ds.field('year') >= start.year) & (ds.field('year') <= end.year)
        if instrument_ids is not None:
            predicate = predicate & ds.field('instrument_id').isin(list(instrument_ids))
        scanner = self.dataset.scanner(columns=columns, filter=predicate, batch_size=batch_size, use_threads=True)
        for batch in scanner.to_batches():
            if len(batch):
                yield batch.to_pandas()

    def read(self, start, end, instrument_ids=None, columns=None):
        parts = list(self.batches(start, end, instrument_ids, columns))
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=columns)


class CSVDataProvider:
    """Explicit source field mapping. No supplier field names in the interface."""
    def __init__(self, path, mapping, version):
        self.path, self.mapping, self.version = Path(path), dict(mapping), version
        if not version or not {'date', 'instrument_id'} <= set(mapping.values()):
            raise ContractError('CSV requires explicit identity/date mapping and source version')

    def batches(self, start, end, instrument_ids=None, columns=None, batch_size=65536):
        for f in pd.read_csv(self.path, usecols=list(self.mapping), chunksize=batch_size):
            f = f.rename(columns=self.mapping)
            f['date'] = pd.to_datetime(f.date)
            f = f.loc[f.date.between(session_date(start), session_date(end))]
            if instrument_ids is not None:
                f = f.loc[f.instrument_id.isin(instrument_ids)]
            yield f.loc[:, columns] if columns else f


class TaskCache:
    """Exclusive immutable job root; output hashes verify completed resumptions."""
    def __init__(self, root, inputs, config):
        self.root = Path(root)
        payload = {'inputs': inputs, 'config': config, 'implementation_sha256': implementation_sha()}
        self.key = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / 'job.json'
        if path.exists():
            if json.loads(path.read_text()) != json.loads(json.dumps(payload, default=str)):
                raise ContractError('cache inputs/config/implementation changed; use a new job root')
        else:
            with path.open('x') as out:
                json.dump(payload, out, sort_keys=True, default=str)

    def get(self, name):
        receipt = self.root / (name + '.json')
        if not receipt.exists():
            return None
        r = json.loads(receipt.read_text())
        path = self.root / (name + '.parquet')
        if r['key'] != self.key or file_sha(path) != r['sha256']:
            raise ContractError('cache artifact changed')
        return pd.read_parquet(path)

    def put(self, name, frame):
        if self.get(name) is not None:
            raise ContractError('completed task cannot be overwritten')
        path = self.root / (name + '.parquet')
        temp = path.with_suffix('.partial')
        with temp.open('xb') as out:
            frame.to_parquet(out, index=False)
        os.replace(temp, path)
        with (self.root / (name + '.json')).open('x') as out:
            json.dump({'key': self.key, 'sha256': file_sha(path), 'rows': len(frame)}, out)
