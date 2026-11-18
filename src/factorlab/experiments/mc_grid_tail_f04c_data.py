"""Offline admission of bounded NVDAUSDT trade bars for MC-05-F04C."""
from dataclasses import dataclass
import csv
from decimal import Decimal
from io import BytesIO, TextIOWrapper
import json
from pathlib import Path
from zipfile import ZipFile

import pandas as pd

from factorlab.data.evidence import sha256
from factorlab.errors import DataQualityError
from factorlab.experiments.grid_independent import GENERATION, VERSION
from factorlab.integrations.binance.market import INTERVALS, bars, milliseconds, window
from factorlab.integrations.binance.store import Reader


SYMBOL = 'NVDAUSDT'
ARCHIVE = 'https://data.binance.vision/data/futures/um'
DATA_SOURCE = 'BINANCE_USDM_PUBLIC_DATA'
MINUTE = INTERVALS['1m']
HOUR = INTERVALS['1h']
DAY = INTERVALS['1d']
OHLCV = ('open', 'high', 'low', 'close', 'volume')
ADDITIVE = ('volume', 'quote_volume', 'trade_count', 'taker_buy_volume', 'taker_buy_quote_volume')
MAX_HOURS = 168


@dataclass(frozen=True)
class MarketData:
    hourly: tuple[dict, ...]
    minutes: tuple[dict, ...]
    daily: tuple[dict, ...]
    evidence: dict


def _iso(ms):
    return pd.Timestamp(ms, unit='ms', tz='UTC').isoformat()


def _rows(content, name):
    with ZipFile(BytesIO(content)) as archive:
        member = Path(name).with_suffix('.csv').name
        if archive.namelist() != [member]:
            raise DataQualityError(f'unexpected CSV member in {name}')
        with archive.open(member) as stream:
            reader = csv.reader(TextIOWrapper(stream, encoding='utf-8-sig', newline=''))
            result = list(reader)
    if result and result[0][0] == 'open_time':
        return result[1:]
    return result


def _records(frame, start, end, interval, cutoff):
    step = INTERVALS[interval]
    opened = [value.value // 1_000_000 for value in frame.open_time]
    if str(frame.open_time.dt.tz) != 'UTC' or opened != list(range(start, end, step)):
        raise DataQualityError(f'{interval} buckets missing, duplicate, unordered or outside UTC range')
    records = []
    for stamp, row in zip(opened, frame.itertuples(index=False), strict=True):
        closed = row.close_time.value // 1_000_000
        if closed != stamp + step - 1 or closed >= cutoff:
            raise DataQualityError(f'{interval} bucket not complete at {_iso(stamp)}')
        values = {field: Decimal(getattr(row, field)) for field in OHLCV + ADDITIVE[1:]}
        if (not all(value.is_finite() for value in values.values())
                or not 0 < values['low'] <= min(values['open'], values['close'])
                <= max(values['open'], values['close']) <= values['high']
                or any(values[field] < 0 for field in ADDITIVE)):
            raise DataQualityError(f'invalid {interval} OHLCV at {_iso(stamp)}')
        values['trade_count'] = int(row.trade_count)
        records.append(dict(id=f'{interval}-{stamp}', time=row.open_time.isoformat(),
                            open_time=row.open_time.isoformat(), close_time=row.close_time.isoformat(),
                            available_at=_iso(closed + 1), price=values['close'],
                            completed_bar=True, **values))
    return tuple(records)


def _normalize(raw, start, end, interval, cutoff):
    step = INTERVALS[interval]
    if [int(row[0]) for row in raw] != list(range(start, end, step)):
        raise DataQualityError(f'{interval} raw buckets missing, duplicate, unordered or outside UTC range')
    frame = bars(raw, start, end, step, cutoff)
    return _records(frame, start, end, interval, cutoff)


def _hourly_source(snapshot):
    reader = Reader(snapshot)
    manifest = reader.manifest
    if (reader.root.name != GENERATION or manifest['data_version'] != VERSION
            or manifest['interval'] != '1h' or manifest['source'] != DATA_SOURCE):
        raise DataQualityError('the pinned R04B NVDAUSDT hourly snapshot is required')
    bounds = manifest['range']
    start, end = bounds['start_ms'], bounds['end_ms']
    cutoff = milliseconds(manifest['history_cutoff_at'])
    records = _records(reader.read('bars').data, start, end, '1h', cutoff)
    return records, manifest, start, end


def _bind_hourly_raw(hours, manifest, archive_root):
    monthly = {receipt['url'].rsplit('/', 1)[-1]: receipt
               for receipt in manifest['external_raw_receipts']}
    used = []
    for month in sorted({row['time'][:7] for row in hours}):
        name = f'{SYMBOL}-1h-{month}.zip'
        receipt = monthly[name]
        path = Path(receipt['external_raw_path']) if archive_root is None else Path(archive_root) / name
        content = path.read_bytes()
        if sha256(content) != receipt['sha256']:
            raise DataQualityError(f'R04B raw hourly SHA-256 mismatch for {name}')
        expected = tuple(row for row in hours if row['time'].startswith(month))
        low = milliseconds(expected[0]['time'])
        high = milliseconds(expected[-1]['available_at'])
        raw = [row for row in _rows(content, name) if low <= int(row[0]) < high]
        if _normalize(raw, low, high, '1h', milliseconds(receipt['acquired_at'])) != expected:
            raise DataQualityError(f'R04B Parquet differs from the official hourly archive for {month}')
        used.append({field: receipt[field] for field in ('url', 'sha256', 'acquired_at')})
    return used


def _receipt_content(root, receipts, name, url):
    receipt = receipts[name]
    if receipt['url'] != url or int(receipt['status']) != 200:
        raise DataQualityError(f'expected a successful official receipt for {name}')
    content = (root / name).read_bytes()
    if len(content) != receipt['bytes'] or sha256(content) != receipt['sha256']:
        raise DataQualityError(f'archive receipt content mismatch for {name}')
    return content, receipt


def _daily_archive(root, receipts, day, interval):
    date = _iso(day)[:10]
    name = f'{SYMBOL}-{interval}-{date}.zip'
    url = f'{ARCHIVE}/daily/klines/{SYMBOL}/{interval}/{name}'
    content, archive = _receipt_content(root, receipts, name, url)
    checksum, signature = _receipt_content(root, receipts, name + '.CHECKSUM', url + '.CHECKSUM')
    if checksum.decode('ascii').split() != [archive['sha256'], name]:
        raise DataQualityError(f'official checksum differs from the saved archive for {name}')
    records = _normalize(_rows(content, name), day, day + DAY, interval,
                         milliseconds(archive['fetched_at']))
    return records, (archive, signature)


def _aggregate(children, parents, count, name):
    if len(children) != len(parents) * count:
        raise DataQualityError(f'{name} aggregation does not cover the same UTC range')
    differences = {field: 0 for field in ADDITIVE[1:]}
    examples = []
    for index, parent in enumerate(parents):
        rows = children[index * count:(index + 1) * count]
        if rows[0]['time'] != parent['time'] or rows[-1]['available_at'] != parent['available_at']:
            raise DataQualityError(f'{name} aggregation UTC boundary mismatch at {parent["time"]}')
        expected = {field: sum(row[field] for row in rows) for field in ADDITIVE}
        expected.update(open=rows[0]['open'], close=rows[-1]['close'],
                        high=max(row['high'] for row in rows), low=min(row['low'] for row in rows))
        for field in OHLCV:
            if expected[field] != parent[field]:
                raise DataQualityError(f'{name} {field} mismatch at {parent["time"]}: '
                                       f'{expected[field]} vs {parent[field]}')
        for field in differences:
            if expected[field] != parent[field]:
                differences[field] += 1
                if len(examples) < 5:
                    examples.append(dict(time=parent['time'], field=field,
                                         aggregate=str(expected[field]), source=str(parent[field])))
    return dict(ohlcv_matches=len(parents), auxiliary_mismatches=differences, examples=examples)


def admit_market_data(hourly_path, archive_root, start, end, *, hourly_archive_root=None):
    """Use exact checked archives and one completed hourly seed before the study."""
    begin, finish, _ = window(start, end, '1h')
    first_day = begin // DAY * DAY
    last_day = (finish + DAY - 1) // DAY * DAY
    if last_day - first_day > 7 * DAY:
        raise DataQualityError('select at most seven consecutive UTC archive days')
    all_hours, manifest, source_start, source_end = _hourly_source(hourly_path)
    seed = begin - HOUR
    if min(seed, first_day) < source_start or last_day > source_end:
        raise DataQualityError('study and seed must lie within the certified hourly snapshot')
    anchor_index = (seed - source_start) // HOUR
    hourly = all_hours[anchor_index:(finish - source_start) // HOUR]
    first_hour = (first_day - source_start) // HOUR
    last_hour = (last_day - source_start) // HOUR
    full_day_hours = all_hours[first_hour:last_hour]
    raw_hours = all_hours[min(anchor_index, first_hour):last_hour]
    raw_hour_receipts = _bind_hourly_raw(raw_hours, manifest, hourly_archive_root)

    root = Path(archive_root)
    recorded = json.loads((root / 'requests.json').read_text(encoding='utf-8'))
    receipts = {row['name']: row for row in recorded}
    if len(receipts) != len(recorded):
        raise DataQualityError('duplicate archive acquisition receipt')
    minutes, daily, used = [], [], []
    for day in range(first_day, last_day, DAY):
        minute_rows, minute_receipts = _daily_archive(root, receipts, day, '1m')
        day_rows, day_receipts = _daily_archive(root, receipts, day, '1d')
        minutes.extend(minute_rows)
        daily.extend(day_rows)
        used.extend(minute_receipts + day_receipts)
    minute_hour = _aggregate(minutes, full_day_hours, 60, 'minute to hour')
    hour_day = _aggregate(full_day_hours, daily, 24, 'hour to day')
    minutes = tuple(minutes[(begin - first_day) // MINUTE:(finish - first_day) // MINUTE])
    hours = len(hourly) - 1
    evidence = dict(status='MINUTE_DATA_READY' if hours == MAX_HOURS else 'PARTIAL_MINUTE_COVERAGE',
                    source=DATA_SOURCE, interval='1h',
                    instrument_id=manifest['contract']['instrument_id'],
                    start=_iso(begin), end_exclusive=_iso(finish), seed=hourly[0]['time'],
                    hourly_rows=hours, minute_rows=len(minutes), daily_rows=len(daily),
                    hourly_source=dict(generation=GENERATION, data_version=VERSION,
                                       bars_sha256=manifest['tables']['bars']['sha256'],
                                       acquired_at=manifest['acquired_at'], rows=len(all_hours),
                                       start=_iso(source_start), end_exclusive=_iso(source_end),
                                       raw_receipts=raw_hour_receipts),
                    contract=manifest['contract'],
                    contract_metadata={k: manifest['identity_evidence'][k]
                                       for k in ('classification', 'sha256')},
                    archive_requests=used, minute_to_hour=minute_hour, hour_to_day=hour_day,
                    units=dict(price='USDT per base unit', volume='base units',
                               quote_volume='USDT', trade_count='exchange trade count'))
    return MarketData(hourly, minutes, tuple(daily), evidence)
