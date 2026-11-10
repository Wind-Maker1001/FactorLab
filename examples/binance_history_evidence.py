"""Offline evidence for pinned Binance survey snapshots; never constructs a client."""
import argparse
import json
from pathlib import Path
import socket
import time
import tracemalloc

import pandas as pd

from factorlab.data.evidence import json_bytes, sha256
from factorlab.integrations.binance.market import INTERVALS, bars, funding, price_bars
from factorlab.integrations.binance.store import Reader
from factorlab.integrations.binance_research import BinanceResearch


def summarize(path):
    reader = Reader(path)
    manifest = reader.manifest
    receipt_count = reader.verify_raw()
    bridge = BinanceResearch('BINANCE_USDM_PUBLIC_REST', reader.root,
                             expected_data_version=manifest['data_version'])
    tables = {}
    raw_rows = {name: [] for name in ('bars', 'funding', 'mark_bars', 'index_bars')}
    mapping = {'klines': 'bars', 'fundingRate': 'funding', 'markPriceKlines': 'mark_bars',
               'indexPriceKlines': 'index_bars'}
    step = INTERVALS[manifest['interval']]
    a, b = manifest['range']['start_ms'], manifest['range']['end_ms']
    cutoff = pd.Timestamp(manifest['history_cutoff_at']).value // 1_000_000
    successful = {}
    for receipt in manifest['raw_receipts']:
        if receipt['status'] != 200:
            continue
        table = mapping.get(receipt['endpoint'])
        if table not in manifest['tables'] or table is None:
            continue
        params = receipt['params']
        if table != 'funding' and params['interval'] != manifest['interval']:
            continue
        # Each request has its own raw receipt; retain one identical URL response.
        body = (reader.root / receipt['raw_path']).read_bytes()
        if receipt['url'] in successful and successful[receipt['url']] != body:
            raise ValueError('conflicting repeated successful request payload')
        successful[receipt['url']] = body
        rows = json.loads(body)
        if table == 'funding':
            rows = [r for r in rows if a <= int(r['fundingTime']) < b]
        else:
            rows = [r for r in rows if a <= int(r[0]) < b]
        raw_rows[table].extend(rows)
    for name, descriptor in manifest['tables'].items():
        frame = reader.read(name).data
        event = {'bars': 'open_time', 'funding': 'funding_time', 'mark': 'event_time',
                 'mark_bars': 'open_time', 'index_bars': 'open_time'}[name]
        record = dict(rows=len(frame), sha256=descriptor['sha256'],
                      first=frame[event].min().isoformat(), last=frame[event].max().isoformat())
        if name == 'funding':
            record['types'] = frame.rate_type.value_counts(dropna=False).to_dict()
            record['null_mark_prices'] = int(frame.mark_price.isna().sum())
        if name != 'mark':
            normalize = funding if name == 'funding' else bars if name == 'bars' else price_bars
            replay = (normalize(raw_rows[name], manifest['contract']['symbol'], a, b)
                      if name == 'funding' else normalize(raw_rows[name], a, b, step, cutoff))
            pd.testing.assert_frame_equal(frame, replay)
            record['exact_raw_roundtrip'] = True
            record['raw_rows_in_window'] = len(raw_rows[name])
            record['repeated_or_unclosed_raw_rows_excluded'] = len(raw_rows[name]) - len(replay)
            record['conflicts'] = 0
            result = bridge.read(name, instrument_id=manifest['contract']['instrument_id'],
                                 start=pd.Timestamp(a, unit='ms', tz='UTC').isoformat(),
                                 end=pd.Timestamp(b, unit='ms', tz='UTC').isoformat(),
                                 observation_cutoff=manifest['acquired_at'])
            if len(result.data) != len(frame):
                raise ValueError('research bridge row count mismatch')
            record['research_rows'] = len(result.data)
            if name != 'funding':
                record['last_close'] = frame.close_time.max().isoformat()
        tables[name] = record
    return dict(data_version=manifest['data_version'], manifest_sha256=sha256(
                (reader.root / 'manifest.json').read_bytes()), interval=manifest['interval'],
                instrument_id=manifest['contract']['instrument_id'], range=manifest['range'],
                acquired_at=manifest['acquired_at'], history_cutoff_at=manifest['history_cutoff_at'],
                raw_receipts_verified=receipt_count, integrity=manifest['integrity'], tables=tables)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', action='append', required=True, help='name=pinned generation path')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    def forbidden(*_args, **_kwargs):
        raise RuntimeError('network prohibited in evidence replay')
    socket.socket.connect = forbidden
    socket.create_connection = forbidden
    tracemalloc.start()
    started = time.monotonic()
    evidence = {name: summarize(path) for name, path in (v.split('=', 1) for v in args.snapshot)}
    result = dict(snapshots=evidence, offline_seconds=time.monotonic() - started,
                  python_peak_bytes=tracemalloc.get_traced_memory()[1], network='DISABLED',
                  boundaries=['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(json_bytes(result))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
