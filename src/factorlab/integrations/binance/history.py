"""Bounded staged history survey using the existing immutable snapshot machinery."""
import json
from pathlib import Path
import shutil
import time

import pandas as pd

from factorlab.data.evidence import utc_now
from factorlab.errors import DataQualityError
from .market import (INTERVALS, bars, completeness, deduplicate, funding, identity,
                     mark, milliseconds, paginate)
from .store import Reader, _write_json, publish


def survey(client, *, baseline, output, end, progress=print):
    """Reuse one verified daily baseline; fetch only disjoint historical windows."""
    baseline = Reader(baseline)
    baseline.verify_raw()
    prior = baseline.manifest
    if prior['interval'] != '1d':
        raise ValueError('history baseline must contain daily bars')
    end_ms = milliseconds(end)
    day = INTERVALS['1d']
    if end_ms % day or end_ms > milliseconds(utc_now()) // day * day:
        raise ValueError('survey end must be a completed UTC daily boundary')
    symbol = prior['contract']['symbol']
    start_ms = end_ms - 90 * day
    if not start_ms <= prior['range']['start_ms'] < prior['range']['end_ms'] <= end_ms:
        raise ValueError('baseline must be contained in the 90-day survey window')
    root = Path(output)
    scope = dict(baseline_version=prior['data_version'], symbol=symbol, end_ms=end_ms)
    if (root / 'latest.json').exists():
        completed = Reader(root)
        if completed.manifest['survey']['scope'] != scope:
            raise ValueError('completed output has a different survey scope')
        completed.verify_raw()
        for table in completed.manifest['tables']:
            completed.read(table)
        progress(json.dumps(dict(status='REUSED', data_version=completed.manifest['data_version'])))
        return completed
    root.mkdir(parents=True, exist_ok=True)
    pending = root / 'pending-history'
    pending.mkdir(exist_ok=True)
    state_path = pending / 'survey.json'
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state['scope'] != scope:
            raise ValueError('resume requires identical survey scope')
    else:
        state = dict(scope=scope, cutoff=utc_now(), batches=[])
        shutil.copytree(baseline.root / 'raw', pending / 'raw', dirs_exist_ok=True)
        shutil.copytree(baseline.root / 'requests', pending / 'requests', dirs_exist_ok=True)
        _write_json(state_path, state)
    frames = [baseline.read('bars').data]
    funds = [baseline.read('funding').data]
    cutoff = milliseconds(state['cutoff'])
    try:
        info = client.get('exchangeInfo', archive=pending)
        matches = [r for r in info['symbols'] if r['symbol'] == symbol]
        if len(matches) != 1:
            raise ValueError('contract not uniquely present in exchangeInfo')
        contract = identity(matches[0])
        for field in ('instrument_id', 'contract_type', 'quote_asset', 'margin_asset', 'onboard_time'):
            if contract[field] != prior['contract'][field]:
                raise DataQualityError('survey contract identity changed')
        marks = mark(client.get('premiumIndex', {'symbol': symbol}, archive=pending), symbol)
        left, right = prior['range']['start_ms'], prior['range']['end_ms']
        # Adjacent block, then the rest of 30 days, then the rest of 90 days.
        edges = [max(start_ms, left - 3 * day), end_ms - 30 * day, start_ms]
        ranges = []
        if right < end_ms:
            ranges.append(('tail', right, end_ms))
        for stage, edge in zip(('adjacent', '30d', '90d'), edges):
            if edge < left:
                ranges.append((stage, edge, left))
                left = edge
        for stage, a, b in ranges:
            t = time.monotonic()
            raw_bars = paginate(client, 'klines', symbol=symbol, start=a, end=b,
                                archive=pending, interval='1d')
            raw_funding = paginate(client, 'fundingRate', symbol=symbol, start=a, end=b,
                                   archive=pending)
            frame = bars(raw_bars, a, b, day, cutoff)
            fund = funding(raw_funding, symbol, a, b)
            frames.append(frame)
            funds.append(fund)
            batch = dict(stage=stage, start_ms=a, end_ms=b, bars=len(frame), funding=len(fund),
                         requests=client.attempts, cache_hits=client.cache_hits,
                         elapsed_seconds=time.monotonic() - t,
                         status='OBSERVED' if len(frame) else 'EMPTY_RESPONSE_NOT_PROOF_OF_NO_HISTORY')
            state['batches'] = [r for r in state['batches'] if r['stage'] != stage]
            state['batches'].append(batch)
            _write_json(state_path, state)
            progress(json.dumps(batch))
            if frame.empty:
                break
        state.pop('failure', None)
        daily = deduplicate(pd.concat(frames, ignore_index=True), ['open_time'])
        events = deduplicate(pd.concat(funds, ignore_index=True), ['funding_time', 'rate_type'])
        observed_start = min(start_ms, prior['range']['start_ms'])
        report = completeness(daily, observed_start, end_ms, day, cutoff, int(contract['onboard_time']))
        report.update(funding_rows=len(events), mark_rows=len(marks),
                      funding_coverage='OBSERVED_EVENTS_ONLY; NO_ASSUMED_SCHEDULE; NO_FILL')
        receipts = [json.loads(p.read_text()) for p in (pending / 'requests').glob('*.json')
                    if len(p.stem) != 64]
        metadata = dict(contract=contract, interval='1d', acquired_at=max(r['acquired_at'] for r in receipts),
                        history_cutoff_at=state['cutoff'], range=dict(start_ms=observed_start,
                        end_ms=end_ms, end_exclusive=True), funding_end_ms=end_ms,
                        integrity=report, parent_version=prior['data_version'], survey=state)
        result = publish(root, pending, dict(bars=daily, funding=events, mark=marks), metadata)
        progress(json.dumps(dict(generation=str(result.root), data_version=result.manifest['data_version'])))
        return result
    except (DataQualityError, OSError, ValueError) as exc:
        state['failure'] = dict(type=type(exc).__name__, reason=str(exc), requests=client.attempts,
                                cache_hits=client.cache_hits, statuses=client.status_counts)
        _write_json(state_path, state)
        raise
