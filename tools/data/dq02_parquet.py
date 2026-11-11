"""Full-record BigQuant parquet shard reader; raw row material stays private."""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import time
import zipfile
import zlib

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

BATCH_SIZE = 65536
# Only declared categorical fields receive value-frequency aggregation. Frequencies
# live on disk so neither cardinality nor row count limits audit coverage.
ENUM_FIELDS = frozenset('type method market_code exchange security_status suspended st_status exdr in_delist crd_buy_flag crd_sell_flag day_trading is_trading call_put product_class position_type position_date_type inst_life_phase combination_type interest_rate_type inout_flag change_flag industry industry_level kind special_treatment suspend_period price_limit_status security_type list_sector corp_nature corp_scale invest_first_classi_type invest_second_classi_type'.split())


def discover(root):
    jobs = []
    for archive in sorted(Path(root).glob('*.zip')):
        with zipfile.ZipFile(archive) as z:
            for member in sorted(n for n in z.namelist() if n.lower().endswith('.parquet')):
                table = member.rsplit('/', 1)[0] if '/' in member else archive.stem
                identity = archive.name + '/' + member
                jobs.append(dict(archive=str(archive.resolve()), member=member, source='BigQuant', table=table,
                                 id=hashlib.sha256(identity.encode()).hexdigest()))
    return jobs


def candidate_key(table, names):
    """Schema-supported business candidates, not claims of upstream uniqueness."""
    name = table.rsplit('/', 1)[-1]
    special = {
        'all_trading_days': ['date', 'market_code'], 'trading_days': ['date', 'market_code'],
        'papertrading_trading_days': ['date', 'market_code'], 'holidays': ['date', 'market_code'],
        'all_instruments': ['instrument_id'], 'cn_stock_revoked_instruments': ['instrument', 'change_date'],
        'cn_future_product_info': ['code'], 'cn_stock_name_change': ['instrument', 'name', 'start_date', 'end_date'],
        'cn_stock_dividend': ['instrument', 'report_date', 'publish_date', 'register_date', 'ex_date'],
        'cn_stock_allotment': ['instrument', 'publish_date', 'register_date', 'exright_date', 'allot_listdate'],
        'cn_stock_suspend': ['date', 'instrument', 'suspend_period', 'suspend_reason'],
        'cn_stock_st': ['instrument', 'special_treatment'],
        'cn_stock_adjust_factor': ['date', 'instrument', 'type', 'method'],
        'cn_stock_industry_bar1d': ['date', 'instrument', 'method'],
        'cn_stock_industry_real_bar1d': ['date', 'instrument', 'method'],
        'cn_stock_industry_component': ['date', 'instrument', 'industry'],
        'cn_stock_industry_change': ['date', 'instrument', 'industry', 'industry_level', 'industry_code', 'change_flag'],
        'cn_stock_industry_index_mapper': ['industry_level1_code', 'kind'],
        'cn_stock_index_change': ['date', 'instrument', 'member_code', 'inout_flag'],
    }
    unknown = {'cn_stock_industry'}
    if name in unknown:
        return []
    if name in special:
        key = special[name]
    elif 'member_code' in names and 'date' in names and 'instrument' in names:
        key = ['date', 'instrument', 'member_code']
    elif name.endswith(('_basic_info', '_index_info')):
        key = ['instrument']
    elif name.endswith(('_bar1d', '_static_data', '_instruments')) or name.startswith('cn_stock_factors_') or name in {'cn_stock_valuation', 'cn_stock_limit_price', 'cn_stock_status'}:
        key = ['date', 'instrument']
    else:
        return []
    return key if set(key) <= set(names) else []


def _value(value):
    if isinstance(value, float):
        if not math.isfinite(value):
            return {'$float': 'nan' if math.isnan(value) else '+inf' if value > 0 else '-inf'}
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return {'$' + type(value).__name__: value.isoformat()}
    if isinstance(value, dt.timedelta):
        return {'$timedelta': [value.days, value.seconds, value.microseconds]}
    if isinstance(value, decimal.Decimal):
        return {'$decimal': str(value)}
    if isinstance(value, bytes):
        return {'$bytes': base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {k: _value(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_value(v) for v in value]
    return value


def _json(value):
    return json.dumps(_value(value), ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False)


def _sum(values):
    return pc.sum(pc.cast(values, pa.int64())).as_py() or 0


def _peak_rss_bytes():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        get_info = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not get_info(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize
    import resource
    import sys
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)


def scan(job, private_root):
    wall, cpu = time.perf_counter(), time.process_time()
    directory = Path(private_root) / job['id']
    directory.mkdir(parents=True, exist_ok=False)
    local = directory / 'content.parquet'
    with zipfile.ZipFile(job['archive']) as z:
        schema_member = job['member'].rsplit('/', 1)[0] + '/schema.json'
        dictionary = json.loads(z.read(schema_member)) if schema_member in z.namelist() else None
        with z.open(job['member']) as source, local.open('wb') as target:
            shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
    database = directory / 'rows.sqlite'
    connection = sqlite3.connect(database)
    connection.execute('PRAGMA journal_mode=OFF')
    connection.execute('PRAGMA synchronous=OFF')
    connection.execute('CREATE TABLE rows(key TEXT,payload TEXT,partition TEXT)')
    connection.execute('CREATE TABLE enums(field TEXT,value TEXT,count INTEGER,PRIMARY KEY(field,value)) WITHOUT ROWID')
    parquet = pq.ParquetFile(local)
    schema = parquet.schema_arrow
    names = schema.names
    key = candidate_key(job['table'], names)
    declared = {f['name']: f.get('type') for f in (dictionary or {}).get('fields', [])}
    fields = {f.name: dict(type=str(f.type), dictionary_type=declared.get(f.name), nulls=0, nonfinite=0,
                            negative=0, zero=0, positive=0, min=None, max=None) for f in schema}
    dates = {}
    total = key_nulls = 0
    try:
        for batch in parquet.iter_batches(batch_size=BATCH_SIZE):
            total += batch.num_rows
            for field, array in zip(schema, batch.columns):
                stats = fields[field.name]
                stats['nulls'] += array.null_count
                typ = field.type
                if pa.types.is_dictionary(typ):
                    array = pc.cast(array, typ.value_type)
                    typ = typ.value_type
                numeric = pa.types.is_integer(typ) or pa.types.is_floating(typ) or pa.types.is_decimal(typ)
                valid = array
                if pa.types.is_floating(typ):
                    finite = pc.is_finite(array)
                    stats['nonfinite'] += _sum(pc.invert(finite))
                    valid = pc.filter(array, finite)
                if numeric:
                    for label, operation in [('negative', pc.less), ('zero', pc.equal), ('positive', pc.greater)]:
                        stats[label] += _sum(operation(valid, 0))
                temporal = pa.types.is_timestamp(typ) or pa.types.is_date(typ)
                if numeric or temporal:
                    bounds = pc.min_max(valid).as_py()
                    for bound in ('min', 'max'):
                        value = bounds[bound]
                        if value is not None and (stats[bound] is None or (value < stats[bound] if bound == 'min' else value > stats[bound])):
                            stats[bound] = value
                if temporal:
                    # Integer calendar extraction does not require Arrow's external
                    # timezone database for the archive's naive timestamps.
                    months = pc.add(pc.multiply(pc.year(array), 100), pc.month(array))
                    counts = pc.value_counts(months).to_pylist()
                    dest = dates.setdefault(field.name, {})
                    for item in counts:
                        if item['values'] is not None:
                            month = f"{item['values'] // 100:04d}-{item['values'] % 100:02d}"
                            dest[month] = dest.get(month, 0) + item['counts']
                if field.name in ENUM_FIELDS or pa.types.is_boolean(typ) or pa.types.is_dictionary(field.type):
                    counts = pc.value_counts(array).to_pylist()
                    connection.executemany('INSERT INTO enums VALUES(?,?,?) ON CONFLICT(field,value) DO UPDATE SET count=count+excluded.count',
                                           ((field.name, _json(v['values']), v['counts']) for v in counts))
            # Python conversion is restricted to the exact full-row evidence sink;
            # statistical computation above stays vectorized in Arrow.
            columns = batch.to_pydict()
            key_indices = [names.index(k) for k in key]
            def evidence():
                nonlocal key_nulls
                for values in zip(*(columns[n] for n in names)):
                    keys = [values[i] for i in key_indices]
                    missing = bool(key) and any(v is None for v in keys)
                    key_nulls += missing
                    yield (_json(keys) if key and not missing else None, zlib.compress(_json(dict(zip(names, values))).encode('utf-8'), 1), job['member'])
            connection.executemany('INSERT INTO rows VALUES(?,?,?)', evidence())
            connection.commit()
        enum_stats = {name: {'distinct': count, 'counts_private': True} for name, count in connection.execute('SELECT field,count(*) FROM enums GROUP BY field')}
    finally:
        connection.close()
        parquet.close()
        local.unlink()
    return _value(dict(job=job, source=job['source'], table=job['table'], id=job['id'], rows=total,
                       schema=[{'name': f.name, 'type': str(f.type)} for f in schema], dictionary=dictionary,
                       schema_coverage={'missing_dictionary_fields': sorted(set(declared) - set(names)),
                                        'undeclared_fields': sorted(set(names) - set(declared))},
                       fields=fields, date_stats=dates, enum_stats=enum_stats,
                       key_coverage={'status': 'CANDIDATE' if key else 'UNKNOWN', 'fields': key,
                                     'rows': total, 'null_key_rows': key_nulls, 'covered_rows': total-key_nulls if key else 0,
                                     'basis': 'table business grain and actual schema; not an upstream uniqueness contract'},
                       payload_encoding='zlib-utf8-v1', sqlite_path=str(database.resolve()), private_dir=str(directory.resolve()),
                       timing={'cpu_seconds': time.process_time()-cpu, 'wall_seconds': time.perf_counter()-wall},
                       memory={'arrow_current_bytes': pa.total_allocated_bytes(),
                               'arrow_peak_bytes': pa.default_memory_pool().max_memory(),
                               'process_peak_rss_bytes': _peak_rss_bytes(), 'scope': 'process lifetime peak; worker may scan multiple jobs'}))
