"""Collect one contract/window with resumable request receipts."""
import json
from pathlib import Path
import shutil

import pandas as pd

from factorlab.data.evidence import archive_bytes, json_bytes, sha256, utc_now
from factorlab.errors import DataQualityError
from .client import PublicClient
from .market import (bars, completeness, deduplicate, discover, funding, identity,
                     mark, milliseconds, paginate, symbol_name, window)
from .store import Reader, _write_json, publish


class Collector:
    def __init__(self, client=None, *, now=utc_now):
        self.client = client if client is not None else PublicClient()
        self.now = now

    def discover(self, archive=None):
        return discover(self.client.get('exchangeInfo', archive=archive))

    def collect(self, *, symbol, start, end, interval='1d', output, update=False,
                underlying_mapping=None, underlying_evidence=None):
        if underlying_mapping is not None:
            if (not isinstance(underlying_evidence, bytes)
                    or sha256(underlying_evidence) != underlying_mapping.get('evidence_sha256')):
                raise ValueError('underlying mapping requires matching archived official evidence bytes')
        elif underlying_evidence is not None:
            raise ValueError('underlying evidence requires an explicit mapping')
        symbol_name(symbol)
        start_ms, end_ms, step = window(start, end, interval)
        root = Path(output)
        old = Reader(root) if update else None
        if old is None and (root / 'latest.json').exists():
            raise ValueError('output already contains a snapshot; use update or a new output directory')
        if old is not None:
            prior = old.manifest
            if prior['contract']['symbol'] != symbol or prior['interval'] != interval:
                raise ValueError('incremental update must retain contract and interval')
            if start_ms != prior['range']['start_ms'] or end_ms < prior['range']['end_ms']:
                raise ValueError('update retains start and may only extend end')
        scope = dict(symbol=symbol, start_ms=start_ms, end_ms=end_ms, interval=interval,
                     parent_version=old.manifest['data_version'] if old else None,
                     underlying_mapping=underlying_mapping)
        pending = root / ('pending-' + sha256(json_bytes(scope))[:24])
        pending.mkdir(parents=True, exist_ok=True)
        state_path = pending / 'collection.json'
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state['scope'] != scope:
                raise DataQualityError('pending collection scope mismatch')
        else:
            if old is not None:
                old.verify_raw()
                shutil.copytree(old.root / 'raw', pending / 'raw', dirs_exist_ok=True)
                requests = pending / 'requests'
                requests.mkdir(exist_ok=True)
                for receipt in (old.root / 'requests').glob('*.json'):
                    # Revalidate overlaps; an old successful empty page is not a new observation.
                    if len(receipt.stem) != 64:
                        shutil.copyfile(receipt, requests / receipt.name)
            state = dict(scope=scope, cutoff=self.now())
            _write_json(state_path, state)
        cutoff = milliseconds(state['cutoff'])
        exchange_info = self.client.get('exchangeInfo', archive=pending)
        if old is None:
            contracts = discover(exchange_info)
        else:
            # Historical repair is read-only even after a contract stops trading.
            contracts = [row for row in exchange_info['symbols']
                         if row['contractType'] in {'TRADFI_PERPETUAL', 'TRADIFI_PERPETUAL'}
                         and row.get('underlyingType') == 'EQUITY']
        matches = [row for row in contracts if row['symbol'] == symbol]
        if len(matches) != 1:
            raise ValueError('contract not uniquely present in the required exchangeInfo scope')
        contract = identity(matches[0], underlying_mapping)
        if underlying_mapping is not None:
            artifact = archive_bytes(underlying_evidence, pending / 'raw', 'BINANCE_UNDERLYING_MAPPING',
                                     underlying_mapping['evidence_url'], state['cutoff'])
            contract['underlying_evidence_path'] = 'raw/' + Path(artifact.path).name
        if old is not None:
            old_identity = old.manifest['contract']
            for field in ['instrument_id', 'contract_type', 'quote_asset', 'margin_asset', 'onboard_time',
                          'underlying']:
                if contract[field] != old_identity[field]:
                    raise DataQualityError('incremental contract identity/mapping changed')
        closed_end = min(end_ms, (cutoff // step) * step)
        onboard = int(contract['onboard_time'])
        old_bars = old.read('bars').data if old else bars([], start_ms, end_ms, step, cutoff)
        # Fetch only holes/new tail plus one completed overlap bucket.
        coverage = completeness(old_bars, start_ms, end_ms, step, cutoff, onboard)
        ranges = [(milliseconds(row['start']), milliseconds(row['end_exclusive']))
                  for row in coverage['missing_ranges']]
        if old is not None and not old_bars.empty:
            last = int(old_bars.open_time.iloc[-1].value // 1_000_000)
            ranges.append((last, min(last + step, closed_end)))
        merged = []
        for a, b in sorted(ranges):
            if a >= b:
                continue
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        bar_frames = [old_bars]
        for a, b in merged:
            rows = paginate(self.client, 'klines', symbol=symbol, start=a, end=b,
                            archive=pending, interval=interval)
            bar_frames.append(bars(rows, a, b, step, cutoff))
        normalized_bars = deduplicate(pd.concat(bar_frames, ignore_index=True), ['open_time'])
        funding_end = min(end_ms, cutoff)
        old_funding = old.read('funding').data if old else funding([], symbol, start_ms, end_ms)
        funding_start = max(start_ms, onboard)
        # No documented publication watermark exists for late Regular/Special events.
        # Reconcile the funding range rather than treating request success as finality.
        rows = paginate(self.client, 'fundingRate', symbol=symbol, start=funding_start,
                        end=funding_end, archive=pending) if funding_start < funding_end else []
        normalized_funding = deduplicate(pd.concat([
            old_funding, funding(rows, symbol, funding_start, funding_end)
        ], ignore_index=True), ['funding_time', 'rate_type'])
        normalized_mark = mark(self.client.get('premiumIndex', {'symbol': symbol}, archive=pending), symbol)
        report = completeness(normalized_bars, start_ms, end_ms, step, cutoff, onboard)
        report.update(funding_rows=len(normalized_funding),
                      funding_types=normalized_funding.rate_type.value_counts(dropna=False).rename(
                          index=lambda x: 'UNKNOWN' if pd.isna(x) else str(x)).to_dict(),
                      mark_rows=len(normalized_mark))
        receipts = [json.loads(path.read_text()) for path in (pending / 'requests').glob('*.json')]
        acquired_at = max(row['acquired_at'] for row in receipts)
        metadata = dict(contract=contract, interval=interval, acquired_at=acquired_at,
                        history_cutoff_at=state['cutoff'],
                        range={'start_ms': start_ms, 'end_ms': end_ms, 'end_exclusive': True},
                        funding_end_ms=funding_end, integrity=report,
                        parent_version=scope['parent_version'])
        return publish(root, pending, {'bars': normalized_bars, 'funding': normalized_funding,
                                       'mark': normalized_mark}, metadata)
