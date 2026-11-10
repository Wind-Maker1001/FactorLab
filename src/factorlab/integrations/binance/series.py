"""Additional historical series in the existing Binance snapshot format."""
import json
from pathlib import Path
import shutil

from factorlab.data.evidence import utc_now
from .market import (bars, completeness, milliseconds, paginate, price_bars, window)
from .store import Reader, _write_json, publish

ENDPOINT_TABLE = {'klines': 'bars', 'markPriceKlines': 'mark_bars',
                  'indexPriceKlines': 'index_bars'}


def collect_series(client, *, baseline, output, start, end, interval, endpoint='klines'):
    """Read-only contract identity from a verified snapshot, not a new rules observation."""
    source = Reader(baseline)
    source.verify_raw()
    table = ENDPOINT_TABLE[endpoint]
    a, b, step = window(start, end, interval)
    cutoff = utc_now()
    if b > milliseconds(cutoff) // step * step:
        raise ValueError('series requires completed historical intervals')
    bounds = source.manifest['range']
    if a < bounds['start_ms'] or b > bounds['end_ms']:
        raise ValueError('series must be contained in verified baseline range')
    root = Path(output)
    scope = dict(source_version=source.manifest['data_version'], start_ms=a, end_ms=b,
                 endpoint=endpoint, interval=interval)
    if (root / 'latest.json').exists():
        completed = Reader(root)
        meta = completed.manifest
        existing_scope = dict(source_version=meta['parent_version'],
                              start_ms=meta['range']['start_ms'], end_ms=meta['range']['end_ms'],
                              endpoint=meta['historical_series']['endpoint'], interval=meta['interval'])
        if existing_scope != scope:
            raise ValueError('completed output has a different series scope')
        completed.verify_raw()
        completed.read(table)
        return completed
    pending = root / 'pending-series'
    pending.mkdir(parents=True, exist_ok=True)
    state_path = pending / 'series.json'
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state['scope'] != scope:
            raise ValueError('resume requires identical series scope')
        cutoff = state['cutoff']
    else:
        state = dict(scope=scope, cutoff=cutoff)
        shutil.copytree(source.root / 'raw', pending / 'raw', dirs_exist_ok=True)
        shutil.copytree(source.root / 'requests', pending / 'requests', dirs_exist_ok=True)
        _write_json(state_path, state)
    contract = source.manifest['contract']
    rows = paginate(client, endpoint, symbol=contract['symbol'], start=a, end=b,
                    archive=pending, interval=interval)
    normalize = bars if endpoint == 'klines' else price_bars
    frame = normalize(rows, a, b, step, milliseconds(cutoff))
    report = completeness(frame, a, b, step, milliseconds(cutoff), int(contract['onboard_time']))
    report['response_status'] = 'OBSERVED' if rows else 'EMPTY_RESPONSE_NOT_PROOF_OF_NO_HISTORY'
    receipts = [json.loads(p.read_text()) for p in (pending / 'requests').glob('*.json')
                if len(p.stem) != 64]
    meta = dict(contract=contract, interval=interval, acquired_at=max(r['acquired_at'] for r in receipts),
                history_cutoff_at=cutoff, range=dict(start_ms=a, end_ms=b, end_exclusive=True),
                integrity=report, parent_version=source.manifest['data_version'],
                historical_series=dict(endpoint=endpoint, identity_observed_at=source.manifest['acquired_at']))
    return publish(root, pending, {table: frame}, meta)
