"""Read-only, exact tri-source diagnostics; private checkpoints never belong in Git."""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import csv
import hashlib
import io
import itertools
import json
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import pandas as pd
import psutil
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

pa.set_cpu_count(1)
pa.set_io_thread_count(1)

ROOT = Path(__file__).resolve().parents[2]
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
sys.path.insert(0, str(ROOT / 'src'))
from factorlab.data.csmar.native import rows, sheets, SafeXML, NS
from factorlab.data.csmar.profiles import MARKETS, PROFILES

LIMITS = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']
FIELDS = ['open', 'high', 'low', 'close', 'volume', 'amount', 'pre_close', 'return', 'turnover']
CS_FIELDS = dict(zip(['Opnprc', 'Hiprc', 'Loprc', 'Clsprc', 'Dnshrtrd', 'Dnvaltrd', 'PreClosePrice', 'ChangeRatio'], FIELDS[:8]))
CN_FIELDS = dict(zip(['Dopnprc', 'Dhiprc', 'Dloprc', 'Dclsprc', 'Dtrdvol', 'Dtrdamt', 'Dclsprcp', 'Dret', 'Dtnor'], FIELDS))
RESULTS = ['MATCHED', 'MISMATCH', 'SOURCE_MISSING', 'SEMANTICALLY_INCOMPARABLE', 'INSUFFICIENT_EVIDENCE']
EXTRA = ['limit_up', 'limit_down', 'limit_status', 'post_volume', 'post_amount']


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def dump(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def memory():
    info = psutil.Process().memory_info()
    return getattr(info, 'peak_wset', info.rss)


def strings(book):
    """Stream the large CNSP string table using the existing native XML guard."""
    result = []
    if 'xl/sharedStrings.xml' not in book.namelist():
        return result
    with book.open('xl/sharedStrings.xml') as stream:
        root = None
        for event, node in ET.iterparse(SafeXML(stream), events=('start', 'end')):
            if root is None:
                root = node
            if event == 'end' and node.tag == NS + 'si':
                result.append(''.join(t.text or '' for t in node.iter(NS + 't')))
                node.clear()
                root.clear()
    return result


def workbook_rows(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as book:
        shared = strings(book)
        for sheet, member in sheets(book).items():
            for number, values in rows(book, member, shared):
                yield sheet, number, values


def ingest(args):
    """One native workbook scan per checkpoint; retain lexemes and coordinates."""
    out = Path(args.private_root)
    out.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(Path(args.csmar_catalog).read_text(encoding='utf-8'))
    jobs = []
    for source in catalog['sources']:
        for member in source['members']:
            if member.get('family') == 'TRDNEW_Dalyr':
                jobs.append(('CSMAR', Path(args.csmar_root) / source['name'], member['name'], member['sha256']))
    with zipfile.ZipFile(args.cnrds) as archive:
        for name in sorted(archive.namelist()):
            if name.endswith('.xlsx') and int(name.split('/')[0]) >= args.first_year:
                jobs.append(('CNRDS', Path(args.cnrds), name, None))
    jobs = [j for j in jobs if args.ingest_source in {'ALL', j[0]} and (j[0] != 'CNRDS' or int(j[2][:4]) <= args.last_year)]
    # Validate the reader on the small research scope before the broad native scan.
    if args.symbols:
        jobs = [j for j in jobs if (j[0] == 'CNRDS' and j[2][:4] in {'2017', '2018'})]
    jobs = [job for i, job in enumerate(jobs) if i % args.workers == args.worker_index]
    started = time.perf_counter()
    for source, archive_path, member, expected in jobs:
        ident = digest((source + '/' + archive_path.name + '/' + member).encode())[:20]
        part = out / (source + '-' + ident + '.parquet')
        receipt = part.with_suffix('.json')
        if receipt.exists():
            saved = json.loads(receipt.read_text(encoding='utf-8'))
            if saved['archive_bytes'] != archive_path.stat().st_size or saved['archive_mtime_ns'] != archive_path.stat().st_mtime_ns:
                raise ValueError('checkpoint source identity changed')
            print('checkpoint ' + member, flush=True)
            continue
        t = time.perf_counter()
        print('scan ' + source + ' ' + member, flush=True)
        with zipfile.ZipFile(archive_path) as archive:
            raw = archive.read(member)
            actual = digest(raw)
            if expected is not None and actual != expected:
                raise ValueError('CSMAR member identity differs from pinned catalog')
            if source == 'CSMAR':
                dictionary_name = 'TRDNEW_Dalyr[DES][xlsx].txt'
            else:
                dictionary_name = member.split('/')[0] + '/个股日回报率_字段说明.txt'
            dictionary = archive.read(dictionary_name)
            if source == 'CSMAR' and digest(dictionary) != PROFILES['TRDNEW_Dalyr'][0]:
                raise ValueError('unreviewed CSMAR dictionary')
            text = dictionary.decode('utf-8-sig')
            if source == 'CNRDS':
                for field in CN_FIELDS:
                    if field + '[' not in text:
                        raise ValueError('CNRDS dictionary field missing: ' + field)
                for field in ['Dopnprc', 'Dhiprc', 'Dloprc', 'Dclsprc', 'Dtrdamt']:
                    line = next(line for line in text.splitlines() if line.startswith(field + '['))
                    if '单位：元' not in line:
                        raise ValueError('CNRDS unit contract changed')
                if '单位：股' not in next(line for line in text.splitlines() if line.startswith('Dtrdvol[')):
                    raise ValueError('CNRDS volume contract changed')
        writer = None
        batch = []
        count = 0
        headers = {}
        selected_codes = set(args.symbols.split(',')) if args.symbols else None
        schema = pa.schema([(k, pa.string()) for k in ['date', 'code', 'symbol', 'sheet', 'row', *FIELDS,
            'filling', 'trade_code', 'name', 'adj_close', *EXTRA, 'native_record']])
        for sheet, number, values in workbook_rows(raw):
            if number == 1:
                headers[sheet] = values
                continue
            if number <= 3 and ('Trddt' not in headers[sheet] or not str(values[headers[sheet].index('Trddt')]).startswith(tuple(str(y) for y in range(1990, 2027)))):
                continue
            record = dict(zip(headers[sheet], values))
            code = record.get('Stkcd' if source == 'CSMAR' else 'Scode')
            date = record.get('Trddt')
            if not date or len(date) < 10 or not date[:4].isdigit():
                raise ValueError(f'invalid native date at {member}/{sheet}/{number}')
            if selected_codes is not None and code not in selected_codes:
                continue
            symbol = None
            if source == 'CSMAR':
                market = str(record['Markettype']).removesuffix('.0')
                markets = MARKETS | {'64': 'BJ'}
                symbol = code + '.' + markets[market] if market in markets else 'CSMAR:' + code + ':' + market
            mapped = {target: record.get(field) for field, target in (CS_FIELDS if source == 'CSMAR' else CN_FIELDS).items()}
            batch.append(dict(date=date[:10], code=code, symbol=symbol, sheet=sheet, row=str(number),
                **{f: mapped.get(f) for f in FIELDS}, filling=record.get('Filling'), trade_code=record.get('Trdsta'),
                name=record.get('Coname'), adj_close=record.get('Adjprcwd' if source == 'CSMAR' else 'Adclsprc'),
                **dict(zip(EXTRA, [record.get(k) for k in ['LimitUp', 'LimitDown', 'LimitStatus', 'Ahshrtrd_D', 'Ahvaltrd_D']])),
                native_record=json.dumps(record, ensure_ascii=False, separators=(',', ':'))))
            count += 1
            if len(batch) == 20000:
                table = pa.Table.from_pylist(batch, schema=schema)
                if writer is None:
                    writer = pq.ParquetWriter(part, schema, compression='zstd')
                writer.write_table(table)
                batch.clear()
        if writer is None:
            writer = pq.ParquetWriter(part, schema, compression='zstd')
        if batch:
            writer.write_table(pa.Table.from_pylist(batch, schema=schema))
        writer.close()
        dump(receipt, dict(source=source, archive=archive_path.name, member=member, sha256=actual,
            dictionary_member=dictionary_name, dictionary_sha256=digest(dictionary), dictionary_text=text,
            archive_bytes=archive_path.stat().st_size, archive_mtime_ns=archive_path.stat().st_mtime_ns,
            parquet=part.name, selected_rows=count, seconds=time.perf_counter()-t, peak_bytes=memory(),
            code_sha256=CODE_SHA256, code_identity_kind='PROCESS_START_SOURCE_SHA256', transforms='native text preserved; explicit field alias only; no imputation'))
        print(json.dumps(dict(member=member, rows=count, seconds=round(time.perf_counter()-t, 2), peak_bytes=memory())), flush=True)
        del raw
    print('ingest completed seconds=' + str(time.perf_counter()-started), flush=True)


def validate_keys(frame, source):
    if frame.index.has_duplicates:
        raise ValueError(source + ' duplicate security/date keys')
    if frame.index.get_level_values('date').isna().any():
        raise ValueError(source + ' missing date')


def comparable(left, right, field):
    if left == right or {left, right} != {'CSMAR', 'CNRDS'}:
        return False
    return field in FIELDS[:6]


def exact_decimal(value):
    if value is None or pd.isna(value) or value in {'', '-', '--'}:
        return None
    result = Decimal(str(value))
    return result if result.is_finite() else None


def classify(left, right, field, a, b, *, present_a=True, present_b=True):
    if not present_a or not present_b:
        return 'SOURCE_MISSING'
    if exact_decimal(a) is None or exact_decimal(b) is None:
        return 'INSUFFICIENT_EVIDENCE'
    if not comparable(left, right, field):
        return 'SEMANTICALLY_INCOMPARABLE'
    return 'MATCHED' if exact_decimal(a) == exact_decimal(b) else 'MISMATCH'


def filled_execution(filling, volume):
    return exact_decimal(filling) != Decimal(0) and exact_decimal(volume) not in {None, Decimal(0)}


class BQReader:
    def __init__(self, path, category, manifest):
        self.archive = zipfile.ZipFile(path)
        self.category = category
        self.manifest = manifest
        self.names = self.archive.namelist()
        self.schemas = {}

    def read(self, table, start, end, columns=None):
        if table not in self.schemas:
            name = f'{self.category}/{table}/schema.json'
            raw = self.archive.read(name)
            schema = json.loads(raw)
            if schema['datasource_id'] != table:
                raise ValueError('BigQuant source/table mismatch')
            self.schemas[table] = schema
            self.manifest.append(dict(source='BigQuant', archive=self.category + '.zip', member=name, sha256=digest(raw)))
        frames = []
        for name in self.names:
            if not name.startswith(f'{self.category}/{table}/') or not name.endswith('.parquet'):
                continue
            receipt_name = name + '.receipt.json'
            receipt_raw = self.archive.read(receipt_name)
            receipt = json.loads(receipt_raw)
            if receipt.get('window_end', '9999')[:10] < start or receipt.get('window_start', '0000')[:10] > end:
                continue
            raw = self.archive.read(name)
            actual = digest(raw)
            if actual != receipt['sha256']:
                raise ValueError('BigQuant receipt identity mismatch: ' + name)
            frame = pq.read_table(pa.BufferReader(raw), columns=columns).to_pandas()
            frame['row'] = np.arange(len(frame)).astype(str)
            frame['member'] = name
            self.manifest.append(dict(source='BigQuant', archive=self.category + '.zip', member=name,
                sha256=actual, receipt_sha256=digest(receipt_raw), physical_rows=len(frame),
                exported_at_utc=receipt['exported_at_utc'], selected_columns=columns))
            if 'date' in frame:
                dates = pd.to_datetime(frame.date)
                if dates.dt.tz is not None or not dates.equals(dates.dt.normalize()):
                    raise ValueError('BigQuant date role/timezone not daily naive midnight')
                frame['date'] = dates.dt.strftime('%Y-%m-%d')
                frame = frame.loc[frame.date.between(start, end)]
            frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def partition_native(args):
    """Repartition each native checkpoint once, without reopening vendor ZIPs."""
    root = Path(args.private_root)
    for receipt_path in sorted(root.glob('*.json')):
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        if receipt.get('source') not in {'CSMAR', 'CNRDS'}:
            continue
        destination = root / 'monthly' / receipt_path.stem
        done = destination / 'complete.json'
        if done.exists():
            continue
        destination.mkdir(parents=True, exist_ok=True)
        writers = {}
        for batch in pq.ParquetFile(receipt_path.with_suffix('.parquet')).iter_batches(batch_size=20000):
            frame = batch.to_pandas()
            for month, selected in frame.groupby(frame.date.str[:7], sort=False):
                table = pa.Table.from_pandas(selected, schema=batch.schema, preserve_index=False)
                if month not in writers:
                    writers[month] = pq.ParquetWriter(destination / (month+'.parquet'), table.schema, compression='zstd')
                writers[month].write_table(table)
        for writer in writers.values():
            writer.close()
        dump(done, dict(source_sha256=receipt['sha256'], months=sorted(writers), rows=receipt['selected_rows']))
        print('partitioned ' + receipt_path.stem, flush=True)


def native_frame(private_root, source, start, end, manifest):
    frames = []
    for receipt_path in sorted(Path(private_root).glob(source + '-*.json')):
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        partition_root = Path(private_root) / 'monthly' / receipt_path.stem
        if (partition_root / 'complete.json').exists():
            path = partition_root / (start[:7] + '.parquet')
            if not path.exists():
                continue
        else:
            path = receipt_path.with_suffix('.parquet')
        dataset = ds.dataset(path)
        table = dataset.to_table(filter=(ds.field('date') >= start) & (ds.field('date') <= end),
            columns=['date', 'code', 'symbol', 'sheet', 'row', *FIELDS, 'filling', 'trade_code', 'name',
                     *(f for f in EXTRA if f in dataset.schema.names)])
        if table.num_rows:
            frame = table.to_pandas()
            frame[FIELDS] = frame[FIELDS].replace({'': None, '-': None, '--': None})
            frame['member'] = receipt['archive'] + '/' + receipt['member']
            frame['evidence_id'] = receipt['sha256']
            frames.append(frame)
            manifest[receipt_path.name] = {k: v for k, v in receipt.items() if k not in {'archive_mtime_ns', 'dictionary_text', 'parquet'}}
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=['date', 'symbol', 'code', *FIELDS, 'filling', 'trade_code', 'name', 'member', 'sheet', 'row'])


def indexed(frame):
    frame = frame.copy()
    loc = frame['member'].astype(str) if 'member' in frame else pd.Series('prepared:bars.parquet', index=frame.index)
    sheet = frame['sheet'].astype(str) if 'sheet' in frame else pd.Series('parquet', index=frame.index)
    row = frame['row'].astype(str) if 'row' in frame else pd.Series('key', index=frame.index)
    frame['audit_locator'] = loc + ':' + sheet + ':' + row
    return frame.set_index(['date', 'symbol'])


def locator(frame, index):
    return frame.at[index, 'audit_locator'] if index in frame.index else 'ABSENT'


def audit(args):
    started = time.perf_counter()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    manifest, native_manifest = [], {}
    stock = BQReader(args.stock, 'stock', manifest)
    common = BQReader(args.common, 'common', manifest)
    prepared_manifest_raw = Path(args.prepared_manifest).read_bytes()
    prepared_manifest = json.loads(prepared_manifest_raw)
    prepared_bytes = Path(args.prepared).read_bytes()
    if prepared_manifest['metadata']['source_id'] != 'CSMAR_TRDNEW_Dalyr_CANDIDATE':
        raise ValueError('prepared source identity is not CSMAR')
    if digest(prepared_bytes) != prepared_manifest['bars_sha256']:
        raise ValueError('prepared input differs from its certified manifest; supply the matching manifest')
    prepared = pd.read_parquet(io.BytesIO(prepared_bytes)).rename(columns={f'raw_{f}': f for f in FIELDS[:4]})
    prepared['date'] = pd.to_datetime(prepared.date).dt.strftime('%Y-%m-%d')
    fixed = set(prepared.symbol)
    with zipfile.ZipFile(args.membership) as archive:
        raw = archive.read('members_daily.csv')
        members = pd.read_csv(io.BytesIO(raw), encoding='utf-8-sig')
        members['symbol'] = members.stock_code.str.replace('.XSHG', '.SH', regex=False).str.replace('.XSHE', '.SZ', regex=False)
        members = members.loc[members.date.between('2021-09-13', '2026-09-11'), ['date', 'symbol']]
        member_index = pd.MultiIndex.from_frame(members)
        member_symbols = set(members.symbol)
        manifest.append(dict(source='membership', archive='csi500_full_history.zip', member='members_daily.csv', sha256=digest(raw)))
    scopes = ['pilot_601857', 'fixed24', 'csi500', 'remaining']
    counts = {s: Counter() for s in scopes}
    comparisons, coverage, status_rows = [], [], []
    anomaly_file = (Path(args.private_root) / ('audit-' + args.stage + '-' + args.start + '-' + args.end + '-anomalies-complete.csv')).open('w', encoding='utf-8', newline='')
    anomaly_columns = ['scope', 'symbol', 'date', 'field', 'sources', 'classification', 'issue', 'left_locator', 'right_locator']
    aw = csv.DictWriter(anomaly_file, fieldnames=anomaly_columns)
    aw.writeheader()
    examples, anomaly_counts = [], Counter()

    def evidence(scope, key, field, sources, classification, issue, a=None, b=None, locations=None):
        record = dict(scope=scope, date=key[0], symbol=key[1], field=field, sources=sources,
            classification=classification, issue=issue,
            left_locator=locator(a, key) if a is not None else '', right_locator=locator(b, key) if b is not None else '')
        if locations is not None:
            record['left_locator'] = locations
        aw.writerow(record)
        group = (scope, key[0][:7], field, sources, classification, issue)
        anomaly_counts[group] += 1
        if anomaly_counts[group] <= 10:
            examples.append((group, record))

    start, end = args.start, args.end
    for month in pd.period_range(start[:7], end[:7], freq='M'):
        lo, hi = max(start, str(month.start_time.date())), min(end, str(month.end_time.date()))
        print('audit ' + lo + '..' + hi, flush=True)
        bq = stock.read('cn_stock_real_bar1d', lo, hi, ['date', 'instrument', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pre_close', 'turn'])
        if bq.empty:
            bq = pd.DataFrame(columns=['date', 'symbol', *FIELDS])
        else:
            bq = bq.rename(columns={'instrument': 'symbol', 'turn': 'turnover'})
        stat = stock.read('cn_stock_status', lo, hi)
        if not stat.empty:
            stat = stat.rename(columns={'instrument': 'symbol'})
        cal = common.read('trading_days', lo, hi, ['date', 'market_code'])
        calendar = set(cal.loc[cal.market_code.eq('CN'), 'date']) if not cal.empty else set()
        status_rows.extend(dict(month=str(month), source='BigQuant', field='CN_session', raw_code=d, count=1, meaning='independent vendor calendar table; not exchange-official certification') for d in sorted(calendar))
        if not calendar:
            status_rows.append(dict(month=str(month), source='BigQuant', field='CN_session', raw_code='NO_LOCAL_COVERAGE', count=0, meaning='absence of export is not exchange closure'))
        cs = native_frame(args.private_root, 'CSMAR', lo, hi, native_manifest) if args.stage == 'full' else prepared.loc[prepared.date.between(lo, hi)].copy()
        cn = native_frame(args.private_root, 'CNRDS', lo, hi, native_manifest)
        mapping = pd.concat([cs[['symbol']], bq[['symbol']]]).dropna().drop_duplicates()
        mapping = mapping.loc[mapping.symbol.str.match(r'^\d{6}\.(SH|SZ|BJ)$')]
        mapping['code'] = mapping.symbol.str[:6]
        unique = mapping.groupby('code').symbol.nunique()
        code_map = mapping.loc[mapping.code.isin(unique[unique.eq(1)].index)].set_index('code').symbol.to_dict()
        if not cn.empty:
            cn['symbol'] = cn.code.map(code_map).fillna('CNRDS:' + cn.code)
        if args.stage == 'pilot':
            cs = cs.loc[cs.symbol.eq('601857.SH')]
            cn = cn.loc[cn.symbol.eq('601857.SH')]
            bq = bq.loc[bq.symbol.eq('601857.SH')]
        elif args.stage == 'fixed':
            cs, cn, bq = [f.loc[f.symbol.isin(fixed)] for f in (cs, cn, bq)]
        cs = indexed(cs)
        cn = indexed(cn)
        bq = indexed(bq)
        frames = dict(CSMAR=cs, CNRDS=cn, BigQuant=bq)
        ambiguous = {source: set() for source in frames}
        for source, frame in list(frames.items()):
            duplicates = frame.index.duplicated(keep=False)
            if duplicates.any():
                affected = frame.index[duplicates].unique()
                ambiguous[source] = set(affected)
                counts['remaining'][source+'_duplicate_rows'] += int(duplicates.sum())
                counts['remaining'][source+'_ambiguous_duplicate_keys'] += len(affected)
                duplicate_rows = frame.loc[duplicates, ['audit_locator']]
                grouped_locations = duplicate_rows.groupby(level=['date','symbol'], sort=False)['audit_locator'].agg(';'.join)
                for key, locations in grouped_locations.items():
                    evidence('remaining', key, '*', source, 'INSUFFICIENT_EVIDENCE', 'DUPLICATE_KEY_ALL_ROWS_EXCLUDED', locations=locations)
                frames[source] = frame.loc[~duplicates]
            validate_keys(frames[source], source)
        cs, cn, bq = (frames[s] for s in ['CSMAR','CNRDS','BigQuant'])
        if 'filling' in cs:
            holidays = pd.to_numeric(cs.filling, errors='raise').eq(2)
            for key in cs.index[holidays]:
                if key[0] in calendar:
                    evidence('remaining', key, 'filling', 'CSMAR|BigQuant-calendar', 'MISMATCH', 'HOLIDAY_FILL_ON_BQ_SESSION', cs)
            counts['remaining']['CSMAR_holiday_fill_excluded'] += int(holidays.sum())
            cs = cs.loc[~holidays]
            frames['CSMAR'] = cs
        union = cs.index.union(cn.index).union(bq.index)
        status = indexed(stat) if not stat.empty else pd.DataFrame(index=pd.MultiIndex.from_tuples([], names=['date', 'symbol']))
        validate_keys(status, 'BigQuant-status')
        for source, frame in frames.items():
            missing_ohlc = frame.reindex(columns=FIELDS[:4]).isna().any(axis=1)
            v = pd.to_numeric(frame.get('volume', pd.Series(index=frame.index, dtype=float)), errors='raise')
            p = frame.reindex(columns=FIELDS[:4]).apply(pd.to_numeric, errors='raise').astype('float64')
            nonfinite = p.notna() & ~np.isfinite(p)
            bad = nonfinite.any(axis=1) | (p.notna().all(axis=1) & ((p.high < p[['open', 'close', 'low']].max(axis=1)) | (p.low > p[['open', 'close', 'high']].min(axis=1)) | p.le(0).any(axis=1)))
            counts['remaining'][source + '_missing_OHLC_security_days'] += int(missing_ohlc.sum())
            for key in frame.index[missing_ohlc]:
                evidence('remaining', key, 'OHLC', source, 'INSUFFICIENT_EVIDENCE', 'MISSING_OHLC_EXISTING_KEY', frame)
            amount = pd.to_numeric(frame.get('amount', pd.Series(index=frame.index, dtype=float)), errors='raise')
            badflow = v.lt(0) | amount.lt(0) | (v.notna() & ~np.isfinite(v)) | (amount.notna() & ~np.isfinite(amount))
            counts['remaining'][source + '_invalid_volume_amount'] += int(badflow.sum())
            for key in frame.index[badflow]:
                evidence('remaining', key, 'volume|amount', source, 'MISMATCH', 'NEGATIVE_OR_NONFINITE_VOLUME_AMOUNT', frame)
            for key in frame.index[bad]:
                evidence('remaining', key, 'OHLC', source, 'MISMATCH', 'INVALID_RAW_OHLC', frame)
            counts['remaining'][source + '_invalid_OHLC'] += int(bad.sum())
            counts['remaining'][source + '_no_volume'] += int(v.eq(0).sum())
            if source == 'BigQuant':
                extended = frame.index.get_level_values('date').to_series(index=frame.index).between('2021-01-13', '2026-10-11') & frame.index.get_level_values('symbol').isin(member_symbols)
                counts['csi500']['extension_invalid_OHLC'] += int((bad & extended).sum())
            if calendar:
                for key in frame.index[~frame.index.get_level_values('date').isin(calendar)]:
                    evidence('remaining', key, 'date', source + '|BigQuant-calendar', 'MISMATCH', 'OBSERVATION_ON_BQ_NONSESSION', frame)
            else:
                counts['remaining'][source + '_keys_without_calendar_coverage'] += len(frame)
            if source == 'CSMAR' and 'filling' in frame:
                filling = pd.to_numeric(frame.filling, errors='raise')
                badfill = filling.ne(0) & v.gt(0)
                for key in frame.index[badfill]:
                    evidence('remaining', key, 'filling|volume', source, 'MISMATCH', 'FILL_WITH_POSITIVE_VOLUME', frame)
                counts['remaining']['CSMAR_fill_positive_volume'] += int(badfill.sum())
                status_rows.extend(dict(month=str(month), source=source, field='Trdsta', raw_code=str(k), count=int(n), meaning='CSMAR documented historical observation; not PIT') for k, n in frame.trade_code.value_counts(dropna=False).items())
                status_rows.extend(dict(month=str(month), source=source, field='Filling', raw_code=str(k), count=int(n), meaning='0 unfilled; 1 nonholiday fill; 2 holiday fill excluded before comparison') for k,n in filling.value_counts(dropna=False).items())
                if 'limit_status' in frame:
                    limit_status = pd.to_numeric(frame.limit_status, errors='raise')
                    upper = pd.to_numeric(frame.limit_up, errors='raise')
                    lower = pd.to_numeric(frame.limit_down, errors='raise')
                    status_rows.extend(dict(month=str(month), source=source, field='LimitStatus', raw_code=str(k), count=int(n), meaning='1 upper limit; -1 lower limit; 0 neither; historical vendor observation') for k,n in limit_status.value_counts(dropna=False).items())
                    counts['remaining']['CSMAR_upper_limit_price_present'] += int(upper.notna().sum())
                    counts['remaining']['CSMAR_lower_limit_price_present'] += int(lower.notna().sum())
                    contradiction = (limit_status.eq(1) & upper.notna() & p.close.notna() & p.close.ne(upper)) | (limit_status.eq(-1) & lower.notna() & p.close.notna() & p.close.ne(lower))
                    for key in frame.index[contradiction]:
                        evidence('remaining', key, 'limit_status|limit_price|close', source, 'MISMATCH', 'LIMIT_FLAG_CLOSE_NOT_AT_REPORTED_LIMIT', frame)
                    counts['remaining']['CSMAR_limit_flag_close_conflict'] += int(contradiction.sum())
                    # Limits and reference prices are observations, not a hardcoded 5/10/20% rule.
                    ref = pd.to_numeric(frame.pre_close, errors='raise')
                    valid_limits = ref.gt(0) & upper.gt(0) & lower.gt(0)
                    counts['remaining']['CSMAR_limit_and_reference_keys'] += int(valid_limits.sum())
                    outside = valid_limits & p.notna().all(axis=1) & ((p.high > upper) | (p.low < lower))
                    for key in frame.index[outside]:
                        evidence('remaining', key, 'OHLC|limit_price', source, 'MISMATCH', 'RAW_PRICE_OUTSIDE_REPORTED_BOUNDS', frame)
                    counts['remaining']['CSMAR_price_outside_reported_bounds'] += int(outside.sum())
        if 'name' in cn:
            hints = cn.name.fillna('').str.contains('ST', regex=False)
            status_rows.extend(dict(month=str(month), source='CNRDS', field='name_contains_ST', raw_code=str(k), count=int(n), meaning='name-text observation only; not historical ST certification') for k,n in hints.value_counts().items())
        cn_status_keys = cn.index.intersection(status.index)
        if len(cn_status_keys):
            observed_suspended = status.reindex(cn_status_keys).suspended.eq(1)
            cn_volume = pd.to_numeric(cn.reindex(cn_status_keys).volume, errors='raise')
            suspicious = observed_suspended & cn_volume.gt(0)
            counts['remaining']['CNRDS_positive_volume_on_BQ_suspended'] += int(suspicious.sum())
            for key in cn_status_keys[suspicious]:
                evidence('remaining', key, 'volume|suspended', 'CNRDS|BigQuant-status', 'INSUFFICIENT_EVIDENCE', 'POSITIVE_VOLUME_ON_BQ_SUSPENDED_OBSERVATION', cn, status)
            ordered = cn.sort_index(level=['symbol','date'])
            values = ordered.reindex(columns=FIELDS[:6]).apply(pd.to_numeric, errors='raise').astype('float64')
            previous = values.groupby(level='symbol', sort=False).shift()
            repeated = values.eq(previous).all(axis=1).reindex(cn_status_keys, fill_value=False)
            counts['remaining']['CNRDS_repeated_six_fields_on_BQ_suspended'] += int((suspicious & repeated).sum())
            for key in cn_status_keys[suspicious & repeated]:
                evidence('remaining', key, 'OHLC|volume|amount', 'CNRDS|BigQuant-status', 'INSUFFICIENT_EVIDENCE', 'SIX_FIELDS_REPEAT_PREVIOUS_WITHIN_MONTH_OBSERVATION_WHILE_SUSPENDED', cn, status)
        overlap_all = cs.index.intersection(status.index)
        if len(overlap_all):
            suspended = status.reindex(overlap_all).suspended.eq(1)
            missing_prices = cs.reindex(overlap_all)[FIELDS[:4]].isna().all(axis=1)
            for is_suspended, missing_prices_flag in itertools.product([False, True], repeat=2):
                n = int((suspended.eq(is_suspended) & missing_prices.eq(missing_prices_flag)).sum())
                status_rows.append(dict(month=str(month), source='CSMAR|BigQuant', field='suspended_vs_all_raw_OHLC_missing', raw_code=f'{int(is_suspended)}|{int(missing_prices_flag)}', count=n, meaning='historical observed association; no enum or causal certification'))
        for field in ['suspended', 'st_status', 'is_risk_warning', 'price_limit_status', 'exdr']:
            if field in status:
                status_rows.extend(dict(month=str(month), source='BigQuant', field=field, raw_code=str(k), count=int(n), meaning='raw enum; independent historical meaning/PIT unverified') for k, n in status[field].value_counts(dropna=False).items())
        for scope in scopes:
            if scope == 'pilot_601857':
                keys = union[(union.get_level_values('symbol') == '601857.SH') & (union.get_level_values('date') >= '2017-01-01') & (union.get_level_values('date') <= '2018-12-31')]
            elif scope == 'fixed24':
                keys = union[union.get_level_values('symbol').isin(fixed) & (union.get_level_values('date') <= '2018-12-31')]
            elif scope == 'csi500':
                keys = member_index[(member_index.get_level_values('date') >= lo) & (member_index.get_level_values('date') <= hi)]
            else:
                keys = union
            if not len(keys):
                continue
            scoped = {s: f.loc[f.index.intersection(keys)] for s, f in frames.items()}
            keysets = {s: set(f.index) for s, f in scoped.items()}
            common3 = set.intersection(*keysets.values())
            if scope == 'csi500':
                for source, frame in scoped.items():
                    for key in keys.difference(frame.index):
                        duplicate = key in ambiguous[source]
                        evidence(scope, key, '*', source+'|membership',
                            'INSUFFICIENT_EVIDENCE' if duplicate else 'SOURCE_MISSING',
                            'EXPECTED_MEMBER_DAY_AMBIGUOUS' if duplicate else 'EXPECTED_MEMBER_DAY_ABSENT', frame)
            counts[scope]['union_or_expected_security_days'] += len(keys)
            counts[scope]['three_source_keys'] += len(common3)
            for source, frame in scoped.items():
                counts[scope][source + '_keys'] += len(frame)
                only = keysets[source] - set.union(*(v for k,v in keysets.items() if k != source))
                counts[scope][source + '_exclusive_keys'] += len(only)
                coverage.append(dict(scope=scope, month=str(month), sources=source, denominator='single_source_exclusive', count=len(only), date_min=lo, date_max=hi))
                coverage.append(dict(scope=scope, month=str(month), sources=source, denominator='unique_security_days', count=len(frame), date_min=frame.index.get_level_values('date').min() if len(frame) else '', date_max=frame.index.get_level_values('date').max() if len(frame) else ''))
            coverage.append(dict(scope=scope, month=str(month), sources='CSMAR|CNRDS|BigQuant', denominator='three_source_intersection', count=len(common3), date_min=lo, date_max=hi))
            for a, b in itertools.combinations(frames, 2):
                ka, kb = keysets[a], keysets[b]
                intersection = ka & kb
                counts[scope][a + '|' + b + '_keys'] += len(intersection)
                coverage.append(dict(scope=scope, month=str(month), sources=a + '|' + b, denominator='pair_intersection', count=len(intersection), date_min=lo, date_max=hi))
                for missing_source, missing_keys, present in [(a, kb-ka, b), (b, ka-kb, a)]:
                    for key in sorted(missing_keys):
                        duplicate = key in ambiguous[missing_source]
                        evidence(scope, key, '*', a + '|' + b,
                            'INSUFFICIENT_EVIDENCE' if duplicate else 'SOURCE_MISSING',
                            missing_source + ('_AMBIGUOUS_DUPLICATE' if duplicate else '_ABSENT'), scoped[present])
                for field in FIELDS:
                    idx = pd.MultiIndex.from_tuples(sorted(intersection), names=['date', 'symbol'])
                    left = scoped[a].reindex(idx).reindex(columns=[field])[field]
                    right = scoped[b].reindex(idx).reindex(columns=[field])[field]
                    va = pd.to_numeric(left, errors='raise')
                    vb = pd.to_numeric(right, errors='raise')
                    valid = va.notna() & vb.notna() & np.isfinite(va) & np.isfinite(vb)
                    binary_equal = va.eq(vb) & valid
                    semantic = comparable(a, b, field)
                    if semantic:
                        da, db = left.map(exact_decimal), right.map(exact_decimal)
                        equal = da.eq(db) & valid
                    else:
                        equal = binary_equal
                    result = Counter({r: 0 for r in RESULTS})
                    ambiguous_missing = len((kb - ka) & ambiguous[a]) + len((ka - kb) & ambiguous[b])
                    result['SOURCE_MISSING'] = len(ka ^ kb) - ambiguous_missing
                    result['INSUFFICIENT_EVIDENCE'] = int((~valid).sum()) + ambiguous_missing
                    if semantic:
                        result['MATCHED'] = int(equal.sum())
                        result['MISMATCH'] = int((valid & ~equal).sum())
                    else:
                        result['SEMANTICALLY_INCOMPARABLE'] = int(valid.sum())
                    comparisons.append(dict(scope=scope, month=str(month), sources=a+'|'+b, field=field,
                        denominator=len(ka|kb), pair_keys=len(intersection), numeric_equal_observations=int(binary_equal.sum()),
                        numeric_unequal_observations=int((valid & ~binary_equal).sum()), **result))
                    counts[scope].update({a+'|'+b+'_'+r: n for r,n in result.items()})
                    # Unknown-scale derived/reference fields have no numeric-difference claim.
                    difference_keys = idx[valid & ~equal] if field in FIELDS[:6] else []
                    for key in difference_keys:
                        evidence(scope, key, field, a+'|'+b, 'MISMATCH' if semantic else 'SEMANTICALLY_INCOMPARABLE',
                            'EXACT_DECIMAL_DIFFERENCE' if semantic else 'BINARY64_DIFFERENCE_NO_UNIT_CERTIFICATION', scoped[a], scoped[b])
            # Independent three-source denominator: pair totals are not added.
            idx3 = pd.MultiIndex.from_tuples(sorted(common3), names=['date', 'symbol'])
            for field in FIELDS:
                values = [pd.to_numeric(f.reindex(idx3).reindex(columns=[field])[field], errors='raise') for f in scoped.values()]
                valid = np.logical_and.reduce([v.notna() & np.isfinite(v) for v in values])
                eq = valid & values[0].eq(values[1]) & values[1].eq(values[2])
                comparisons.append(dict(scope=scope, month=str(month), sources='CSMAR|CNRDS|BigQuant', field=field,
                    denominator=len(idx3), pair_keys=len(idx3), numeric_equal_observations=int(eq.sum()),
                    numeric_unequal_observations=int((valid & ~eq).sum()), MATCHED=0, MISMATCH=0, SOURCE_MISSING=0,
                    SEMANTICALLY_INCOMPARABLE=int(valid.sum()), INSUFFICIENT_EVIDENCE=int((~valid).sum())))
            if scope == 'csi500':
                counts[scope]['raw_missing_keys'] += len(keys.difference(bq.index))
                present = bq.reindex(keys.intersection(bq.index))
                counts[scope]['raw_OHLC_missing_member_days'] += int(present[FIELDS[:4]].isna().any(axis=1).sum())
                counts[scope]['status_missing_keys'] += len(keys.difference(status.index))
                present_status = status.reindex(present.index).reindex(columns=['suspended'])
                counts[scope]['missing_OHLC_with_suspended_1'] += int((present[FIELDS[:4]].isna().any(axis=1) & present_status.suspended.eq(1)).sum())
            if scope in {'fixed24', 'pilot_601857'}:
                overlap = scoped['CSMAR'].index.intersection(scoped['BigQuant'].index)
                csprices = scoped['CSMAR'].reindex(overlap)[FIELDS[:4]]
                susp = status.reindex(overlap).reindex(columns=['suspended']).suspended.eq(1)
                missing = csprices.isna().all(axis=1)
                counts[scope]['BQ_suspended_keys'] += int(susp.sum())
                counts[scope]['BQ_suspended_CS_missing_OHLC'] += int((susp & missing).sum())
                if args.stage == 'full':
                    native = scoped['CSMAR'].reindex(overlap)
                    filled = pd.to_numeric(native.filling, errors='raise').eq(1)
                    counts[scope]['BQ_suspended_CS_native_filled_OHLC'] += int((susp & filled & ~missing).sum())
                    prepared_prices = prepared.set_index(['date','symbol']).reindex(overlap)[FIELDS[:4]]
                    prepared_missing = prepared_prices.isna().all(axis=1)
                    counts[scope]['BQ_suspended_CS_prepared_missing_OHLC'] += int((susp & prepared_missing).sum())
                    counts[scope]['prepared_missing_native_filled_OHLC'] += int((prepared_missing & filled & ~missing).sum())
                counts[scope]['BQ_nonsuspended_CS_missing_OHLC'] += int((~susp & missing).sum())
                counts[scope]['BQ_status_keys'] += len(scoped['BigQuant'].index.intersection(status.index))
                observed_dates = set(scoped['CSMAR'].index.get_level_values('date'))
                counts[scope]['CS_observed_sessions'] += len(observed_dates)
                counts[scope]['BQ_calendar_sessions'] += len(calendar)
                counts[scope]['CS_observed_not_BQ_calendar'] += len(observed_dates-calendar)
        anomaly_file.flush()
        dump(out / 'summary.json', dict(task_id='B_TRI_SOURCE_MARKET', baseline_sha=args.baseline_sha,
            sources=['CSMAR','CNRDS','BigQuant'], scope=dict(stage=args.stage,start=start,end=end,last_completed_month=str(month)),
            metrics={s:dict(c) for s,c in counts.items()}, findings=[], gaps=[], artifacts=['anomalies.csv'],
            status='RUNNING', provenance=dict(limits=LIMITS, peak_bytes=memory(), seconds=time.perf_counter()-started)))
    anomaly_file.close()
    pd.DataFrame([dict(record, group_total=anomaly_counts[group], publication='first_10_per_scope_month_field_pair_class_issue; exhaustive index private') for group,record in examples], columns=anomaly_columns+['group_total','publication']).to_csv(out/'anomalies.csv', index=False)
    pd.DataFrame(coverage).to_csv(out/'coverage.csv', index=False)
    pd.DataFrame(comparisons).to_csv(out/'comparisons.csv', index=False)
    pd.DataFrame(status_rows).to_csv(out/'status-calendar.csv', index=False)
    unique_manifest = {json.dumps(x,sort_keys=True):x for x in manifest}
    dump(out/'input-manifest.json', dict(native=list(native_manifest.values()), bigquant_and_membership=list(unique_manifest.values()),
        prepared=dict(sha256=digest(prepared_bytes), manifest_sha256=digest(prepared_manifest_raw), schema='P5_R03_CSMAR_PRIVATE_V1'),
        processing=dict(code_sha256=CODE_SHA256, numeric='native Decimal exact; BigQuant binary64 observation only; no tolerance',
            identity='CNRDS bare codes mapped only through unique exact CSMAR/BQ symbols in same month; not stable historical issuer identity',
            calendar='BigQuant independent trading_days CN table, not official exchange certification')))
    summary = json.loads((out/'summary.json').read_text(encoding='utf-8'))
    scan_receipts = [json.loads(p.read_text(encoding='utf-8')) for p in Path(args.private_root).glob('*.json')]
    scan_receipts = [r for r in scan_receipts if r.get('source') in {'CSMAR','CNRDS'}]
    native_resources = dict(workbooks=len(scan_receipts), selected_rows=sum(r['selected_rows'] for r in scan_receipts),
        summed_workbook_seconds=sum(r['seconds'] for r in scan_receipts), peak_bytes=max((r['peak_bytes'] for r in scan_receipts), default=0))
    summary.update(status='PARTIAL_SEMANTIC_AND_HISTORICAL_EVIDENCE_GAPS',
        artifacts=['REPORT.md','coverage.csv','comparisons.csv','anomalies.csv','status-calendar.csv','input-manifest.json','summary.json'],
        gaps=['BigQuant economic units/precision and enum contract unverified', 'CNRDS pre_close versus exchange ex-rights reference is not certified equivalent',
              'Historical stable security identity, IPO/delist/code-change intervals and PIT unverified',
              'Adjusted prices intentionally excluded from numeric certification; HFQ unverified',
              'Exchange official calendars unavailable; CNRDS/CSMAR observed dates not exchange calendars'],
        findings=dict(interpretation=['Only CSMAR/CNRDS raw OHLC, shares and CNY amount are eligible for exact-decimal certification.',
                  'Three-way agreement is descriptive binary64 equality, not independent correctness.'],
                  anomaly_groups=[dict(zip(['scope','month','field','sources','classification','issue'],group), count=n) for group,n in sorted(anomaly_counts.items())]),
        provenance=dict(limits=LIMITS,seconds=time.perf_counter()-started,peak_bytes=memory(), native_ingest=native_resources,
            private_checkpoints='native lexemes, complete native records, sheet/row and member SHA retained locally; not published',
            pr79='OPEN, unmerged at initial inspection; no runtime dependency'))
    dump(out/'summary.json',summary)
    (out/'REPORT.md').write_text('# 三源行情与交易状态审计\n\n'+' / '.join(LIMITS)+'\n\n'+
        '逐源、两两及三源分母独立保存在 coverage.csv；comparisons.csv 区分同义精确比较与未认证单位的数值观察。'+
        '原生十进制直接比较，无任意容差。原价不与复权价混合。差异键和原件坐标保存在 anomalies.csv，完整原值只留本地。\n\n'+
        '运行状态及真实范围见 summary.json。BigQuant 单位和历史枚举合同缺失，不据数值相等授予一致性认证；'+
        '三源可能共享上游，三源相同不等于独立正确。\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def audit_parallel(args):
    """Bounded independent year processes; aggregate only completed outputs."""
    import concurrent.futures
    import os
    import subprocess
    from dataclasses import dataclass
    started = time.perf_counter()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    years = range(int(args.start[:4]), int(args.end[:4])+1)
    env = os.environ | {k:'1' for k in ['OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','ARROW_NUM_THREADS']}
    parts = Path(args.private_root) / 'audit-parts'
    parts.mkdir(exist_ok=True)

    jobs = [(year, None) for year in years if year != 2021]
    jobs.extend((2021, month) for month in range(1,13) if args.start <= f'2021-{month:02d}-31' and args.end >= f'2021-{month:02d}-01')

    def worker(job):
        year, month = job
        label = str(year) if month is None else f'{year}-{month:02d}'
        target = parts / label
        if not args.rerun and (target/'summary.json').exists():
            saved = json.loads((target/'summary.json').read_text(encoding='utf-8'))
            if saved['status'] != 'RUNNING':
                print('AUDIT YEAR CHECKPOINT '+str(year),flush=True)
                return target
        command = [sys.executable,'-I','-B','-X','utf8',str(Path(__file__).resolve()),'audit',
            '--private-root',args.private_root,'--output',str(target),'--stage','full',
            '--start',max(args.start,f'{year}-01-01' if month is None else f'{year}-{month:02d}-01'),
            '--end',min(args.end,f'{year}-12-31' if month is None else str(pd.Period(f'{year}-{month:02d}',freq='M').end_time.date())),
            '--stock',args.stock,'--common',args.common,'--prepared',args.prepared,
            '--prepared-manifest',args.prepared_manifest,'--membership',args.membership,'--baseline-sha',args.baseline_sha]
        with (parts / f'{label}.log').open('w',encoding='utf-8') as log:
            subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,env=env,check=True)
        print('AUDIT YEAR COMPLETE '+str(year),flush=True)
        return target

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        targets = list(pool.map(worker,jobs))
    summaries = [json.loads((p/'summary.json').read_text(encoding='utf-8')) for p in targets]
    summary = summaries[0]
    metrics = {}
    for item in summaries:
        for scope, values in item['metrics'].items():
            metrics.setdefault(scope,Counter()).update(values)
    summary['metrics'] = {s:dict(v) for s,v in metrics.items()}
    summary['scope'] = dict(stage='full',start=args.start,end=args.end,completed_years=list(years))
    summary['findings']['anomaly_groups'] = [g for item in summaries for g in item['findings']['anomaly_groups']]
    summary['provenance'].update(seconds=time.perf_counter()-started,workers=args.workers,
        max_individual_worker_peak_bytes=max(item['provenance']['peak_bytes'] for item in summaries),
        sum_individual_worker_peaks_upper_bound_bytes=sum(item['provenance']['peak_bytes'] for item in summaries),
        resource_note='Sum of individual peaks is a conservative upper bound, not simultaneous measured RSS.')
    summary['provenance'].pop('peak_bytes',None)
    for name in ['coverage.csv','comparisons.csv','anomalies.csv','status-calendar.csv']:
        with (out/name).open('w',encoding='utf-8',newline='') as destination:
            for i,target in enumerate(targets):
                with (target/name).open(encoding='utf-8',newline='') as source:
                    header = next(source)
                    if i == 0:
                        destination.write(header)
                    for line in source:
                        destination.write(line)
    manifests = [json.loads((p/'input-manifest.json').read_text(encoding='utf-8')) for p in targets]
    manifest = manifests[0]
    for field in ['native','bigquant_and_membership']:
        unique = {json.dumps(x,sort_keys=True):x for m in manifests for x in m[field]}
        manifest[field] = list(unique.values())
    manifest['processing']['code_sha256'] = CODE_SHA256
    dump(out/'input-manifest.json',manifest)
    receipts = [json.loads(p.read_text(encoding='utf-8')) for p in Path(args.private_root).glob('*.json')]
    summary['provenance']['native_ingest'] = dict(workbooks=len(receipts),
        selected_rows=sum(r['selected_rows'] for r in receipts),
        summed_workbook_seconds=sum(r['seconds'] for r in receipts),
        max_individual_process_peak_bytes=max(r['peak_bytes'] for r in receipts))
    comparisons = pd.read_csv(out/'comparisons.csv')
    summary['metrics']['numeric_observations_by_scope_pair'] = [dict(scope=scope,sources=pair,
        numeric_equal_observations=int(group.numeric_equal_observations.sum()),
        numeric_unequal_observations=int(group.numeric_unequal_observations.sum()),
        MATCHED=int(group.MATCHED.sum()),MISMATCH=int(group.MISMATCH.sum()))
        for (scope,pair),group in comparisons.groupby(['scope','sources'])]
    fixed, csi, pilot = (summary['metrics'].get(s,{}) for s in ['fixed24','csi500','pilot_601857'])
    baseline_checks = [
        ('pilot_common_keys',487,pilot.get('CSMAR|BigQuant_keys')),
        ('fixed24_common_keys',12648,fixed.get('CSMAR|BigQuant_keys')),
        ('fixed24_BQ_status_keys',12648,fixed.get('BQ_status_keys')),
        ('fixed24_BQ_calendar_sessions',527,fixed.get('BQ_calendar_sessions')),
        ('fixed24_suspended_CS_prepared_missing',216,fixed.get('BQ_suspended_CS_prepared_missing_OHLC')),
        ('fixed24_suspended_CS_native_filled',216,fixed.get('BQ_suspended_CS_native_filled_OHLC')),
        ('fixed24_earlier_uncovered_sessions',448,fixed.get('CS_observed_not_BQ_calendar')),
        ('csi500_missing_keys',292,csi.get('raw_missing_keys')),
        ('csi500_OHLC_missing_member_days',564,csi.get('raw_OHLC_missing_member_days')),
        ('csi500_extension_invalid_OHLC',10,csi.get('extension_invalid_OHLC'))]
    pilot_amount = comparisons.loc[(comparisons.scope=='pilot_601857') & (comparisons.sources=='CSMAR|BigQuant') & (comparisons.field=='amount')]
    baseline_checks.append(('pilot_amount_binary64_differences',4,int(pilot_amount.numeric_unequal_observations.sum())))
    summary['findings']['historical_baseline_regressions'] = [dict(name=name,prior=prior,actual=actual,
        result='REPRODUCED' if prior==actual else 'CHANGED_INPUT_OR_SCOPE_REQUIRES_EXPLANATION') for name,prior,actual in baseline_checks]
    dump(out/'summary.json',summary)
    print('PARALLEL AUDIT COMPLETE seconds='+str(time.perf_counter()-started),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['ingest', 'partition', 'audit', 'audit-parallel'])
    parser.add_argument('--private-root', required=True)
    parser.add_argument('--csmar-root', default='E:/CSMAR Raw')
    parser.add_argument('--csmar-catalog', default='E:/FactorLabData/candidates/csmar-601857-2018-20261105/inventory/catalog.json')
    parser.add_argument('--cnrds', default='E:/CNRDS Raw/CNSP_个股日回报率_ALL.zip')
    parser.add_argument('--first-year', type=int, default=2015)
    parser.add_argument('--last-year', type=int, default=2026)
    parser.add_argument('--rerun', action='store_true')
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--worker-index', type=int, default=0)
    parser.add_argument('--ingest-source', choices=['ALL', 'CSMAR', 'CNRDS'], default='ALL')
    parser.add_argument('--symbols')
    parser.add_argument('--stock', default='E:/BigQuant Raw/stock.zip')
    parser.add_argument('--common', default='E:/BigQuant Raw/common.zip')
    parser.add_argument('--prepared', default='E:/FactorLabData/candidates/p5-r03-real-pairs-20261110-v2/bars.parquet')
    parser.add_argument('--prepared-manifest', default=str(ROOT / 'docs/validation/p6-d01/manifest.json'))
    parser.add_argument('--membership', default='E:/FactorLabData/validation/joinquant_csi500_full_20261024/csi500_full_history.zip')
    parser.add_argument('--output')
    parser.add_argument('--stage', choices=['pilot', 'fixed', 'full'], default='pilot')
    parser.add_argument('--start', default='2017-01-01')
    parser.add_argument('--end', default='2018-12-31')
    parser.add_argument('--baseline-sha', default='2ce3e5ee6e704819a2c155ca833b30e5bb7c296c')
    args = parser.parse_args()
    if args.command == 'ingest':
        ingest(args)
    elif args.command == 'partition':
        partition_native(args)
    elif args.command == 'audit-parallel':
        audit_parallel(args)
    else:
        audit(args)


if __name__ == '__main__':
    main()
