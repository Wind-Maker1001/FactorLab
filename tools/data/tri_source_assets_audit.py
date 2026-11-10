"""Read-only tri-source asset audit; run with python -I -B -X utf8.

Outputs contain metadata and aggregates, never vendor observation rows. Archive
payload reads are bounded; source bytes are neither extracted nor rewritten.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import pandas as pd
import psutil
import pyarrow as pa
import pyarrow.parquet as pq

GUARDS = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']
MAX_MEMBER_BYTES = 64 * 1024 * 1024
SAMPLE_ROWS = 5000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def write_csv(path, rows):
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else v for k, v in row.items()})


def domain(name):
    rules = [
        ('corporate_actions', r'dividend|allotment|adjust.factor|Cptl|Capchg|分红|分配|股本|配股'),
        ('identity', r'basic_info|name_change|TRDNEW_Co|stock_list|delist|_instruments$|公司基本信息|公司曾用名|招股|首发上市|公司文件'),
        ('status', r'status|suspend|_st$|limit_price|停牌|特别处理|特殊处理'),
        ('industry', r'industry|行业'),
        ('financial', r'finance|financial|FS_|FRDT|利润|负债|现金流|财务|业绩'),
        ('index', r'^IDX_|^index/|指数|index_'),
        ('calendar', r'trading_days|holiday|交易日'),
        ('market', r'bar1d|Dalyr|CNSP|日回报|行情|交易'),
    ]
    return next((label for label, pattern in rules if re.search(pattern, name, re.I)), 'other')


def quality_profile(frame, keys=(), date_column=None, code_column=None):
    """Exact within the supplied frame; candidate keys are not certified keys."""
    result = {'rows': len(frame), 'fields': {str(k): str(v) for k, v in frame.dtypes.items()},
              'nulls': {str(k): int(v) for k, v in frame.isna().sum().items()},
              'nonfinite_nonnull': {str(k): int(v.isin([float('inf'), -float('inf')]).sum())
                                     for k, v in frame.select_dtypes(include='number').items()},
              'key_status': 'CANDIDATE_NOT_VENDOR_CERTIFIED' if keys else 'UNKNOWN',
              'candidate_keys': list(keys), 'duplicate_excess': None, 'conflicting_keys': None,
              'economic_anomalies': 'NOT_CERTIFIED', 'calendar_anomalies': 'NOT_RUN_NO_CERTIFIED_CALENDAR'}
    if keys:
        result['null_key_rows'] = int(frame[list(keys)].isna().any(axis=1).sum())
        result['duplicate_excess'] = int(frame.duplicated(list(keys)).sum())
        distinct = frame.drop_duplicates()
        result['conflicting_keys'] = int(distinct.loc[distinct.duplicated(list(keys), keep=False), list(keys)].drop_duplicates().shape[0])
    result['date_fields'] = {str(c): {'min': str(frame[c].min()) if frame[c].notna().any() else None,
                                     'max': str(frame[c].max()) if frame[c].notna().any() else None}
                             for c in frame.select_dtypes(include='datetime')}
    if date_column:
        values = pd.to_datetime(frame[date_column], errors='coerce')
        result.update(date_min=str(values.min()) if values.notna().any() else None,
                      date_max=str(values.max()) if values.notna().any() else None,
                      invalid_date_rows=int((frame[date_column].notna() & values.isna()).sum()),
                      weekend_observations=int((values.dt.dayofweek >= 5).sum()),
                      weekend_interpretation='STATISTICAL_ONLY_NOT_CERTIFIED_TRADING_ERROR')
    if code_column:
        codes = frame[code_column].astype('string')
        pattern = r'\d{6}\.(SH|SZ|BJ)' if code_column == 'instrument' else r'\d{6}'
        result.update(securities=int(codes.nunique()), code_namespace='EXCHANGE_QUALIFIED' if code_column == 'instrument' else 'BARE_VENDOR_CODE_NOT_STABLE_ID',
                      code_format_anomalies=int((~codes.str.fullmatch(pattern, na=False)).sum()))
    for columns in [('open', 'high', 'low', 'close'), ('Dopnprc', 'Dhiprc', 'Dloprc', 'Dclsprc')]:
        if set(columns).issubset(frame):
            o, h, l, c = (pd.to_numeric(frame[x], errors='coerce') for x in columns)
            result['nonpositive_price_rows'] = int(((o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)).sum())
            result['ohlc_order_rows'] = int(((h < l) | (h < o) | (h < c) | (l > o) | (l > c)).sum())
    numeric_columns = ['volume', 'amount'] + [c for c in frame if c not in ['Scode', 'Coname', 'Trddt']] if code_column == 'Scode' else ['volume', 'amount']
    for col in numeric_columns:
        if col in frame:
            numeric = pd.to_numeric(frame[col], errors='coerce')
            result[col + '_nonnumeric_nonnull'] = int((frame[col].notna() & numeric.isna()).sum())
            result[col + '_literal_dash_marker'] = int(frame[col].astype('string').eq('--').sum())
            result[col + '_numeric_nonfinite'] = int(numeric.isin([float('inf'), -float('inf')]).sum())
            if col in ['volume', 'amount', 'Dtrdvol', 'Dtrdamt']:
                result[col + '_negative'] = int((numeric < 0).sum())
    result['replacement_character_cells'] = sum(int(frame[c].astype('string').str.contains('�', regex=False, na=False).sum()) for c in frame.select_dtypes(include=['object', 'string']))
    return result


def overlaps(receipts):
    """Inclusive query-window intersections, not actual record collisions."""
    windows = sorted((r['window_start'], r['window_end']) for r in receipts if r.get('window_start') and r.get('window_end'))
    previous_end = None
    count = 0
    for start, end in windows:
        if previous_end is not None and start <= previous_end:
            count += 1
        previous_end = max(previous_end, end) if previous_end is not None else end
    return count


def run(args):
    started, cpu = time.perf_counter(), time.process_time()
    observed = datetime.now(timezone.utc).isoformat()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    inventory, quality, contracts, identities, partitions = [], [], [], [], []
    resources = Counter()
    repo = Path(__file__).resolve().parents[2]
    baseline = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    catalog = json.loads(Path(args.csmar_catalog).read_text(encoding='utf-8'))
    old_sources = {s['name']: s for s in catalog['sources']}
    prior_members = defaultdict(list)
    preview_path = Path(args.inventory_root) / 'archive_preview.csv'
    with preview_path.open(encoding='utf-8-sig', newline='') as stream:
        for item in csv.DictReader(stream):
            if item['source'] == 'CNRDS Raw':
                # The archived index uses Windows paths regardless of host shell.
                prior_members[item['archive'].replace('\\', '/').rsplit('/', 1)[-1]].append(item)
    roots = {'CSMAR': Path(args.csmar_root), 'CNRDS': Path(args.cnrds_root), 'BigQuant': Path(args.bigquant_root)}

    def identity(source, asset, **kwargs):
        identities.append({'source': source, 'asset': asset, **kwargs})

    for source, root in roots.items():
        # Recursion is limited to the source root and two descendant levels.
        files = []
        for item in sorted(root.iterdir()):
            if item.is_file():
                files.append(item)
            elif item.is_dir():
                for child in sorted(item.iterdir()):
                    if child.is_file():
                        files.append(child)
                    elif child.is_dir():
                        for grandchild in sorted(child.iterdir()):
                            if grandchild.is_file():
                                files.append(grandchild)
                            elif grandchild.is_dir():
                                inventory.append({'source': source, 'asset': grandchild.relative_to(root).as_posix(), 'level': 'directory', 'status': 'NOT_RUN_DEPTH_LIMIT', 'domain': 'other'})
        for path in files:
            asset = path.relative_to(root).as_posix()
            stat = path.stat()
            base = {'source': source, 'asset': asset, 'level': 'file', 'format': path.suffix.lower(), 'bytes': stat.st_size,
                    'domain': domain(asset), 'domain_evidence': 'NAME_HINT_NOT_CERTIFIED_MEANING',
                    'role': 'DOCUMENTATION' if path.suffix.lower() in ['.pdf', '.txt', '.md'] else 'DATA_ASSET',
                    'table': path.stem if path.suffix.lower() == '.7z' else None,
                    'status': 'METADATA_ONLY', 'observed_at': observed, 'collected_at': 'UNKNOWN',
                    'mtime_semantics': 'FILESYSTEM_ONLY_NOT_ACQUISITION', 'primary_key': 'UNKNOWN',
                    'currency': 'UNKNOWN', 'precision': 'UNKNOWN', 'stable_identity': 'UNKNOWN',
                    'upstream_independence': 'UNKNOWN', 'asset_class': 'UNKNOWN_UNLESS_FIELD_CONTRACT',
                    'unread_scope': 'CONTENT_NOT_RUN', 'adjustment': 'UNKNOWN_UNLESS_FIELD_CONTRACT'}
            resources['files'] += 1
            if path.suffix.lower() != '.zip':
                base['status'] = 'NOT_RUN_UNSUPPORTED_FORMAT'
                inventory.append(base)
                identity(source, asset, status=base['status'], hash_status='NOT_RUN', bytes=stat.st_size)
                if source == 'CNRDS' and path.suffix.lower() == '.7z':
                    for old in prior_members.get(path.name, []):
                        inventory.append({'source': source, 'asset': asset, 'member': old['member'], 'level': 'member',
                                          'table': Path(old['member']).stem, 'format': old['extension'],
                                          'domain': domain(old['member'] + ' ' + asset), 'bytes': old['bytes_uncompressed'],
                                          'status': 'PRIOR_DIRECTORY_ONLY_CURRENT_CONTENT_NOT_RUN',
                                          'record_evidence': 'NOT_RUN', 'primary_key': 'UNKNOWN', 'fields': 'UNKNOWN',
                                          'unread_scope': 'CURRENT_ARCHIVE_LISTING_AND_ALL_CONTENT_NOT_RUN'})
                        resources['reused_7z_member_entries'] += 1
                continue
            try:
                archive = zipfile.ZipFile(path)
            except (OSError, zipfile.BadZipFile) as error:
                base['status'] = 'NOT_ACCESSIBLE_' + type(error).__name__
                inventory.append(base)
                identity(source, asset, status=base['status'])
                continue
            with archive:
                entries = [i for i in archive.infolist() if not i.is_dir()]
                resources['archive_members'] += len(entries)
                listing = [{'member': i.filename, 'bytes': i.file_size, 'crc32': f'{i.CRC:08x}', 'compressed_bytes': i.compress_size,
                            'encrypted': bool(i.flag_bits & 1)} for i in entries]
                listing_sha = digest(json.dumps(listing, sort_keys=True, ensure_ascii=False).encode())
                historical = old_sources.get(path.name) if source == 'CSMAR' else None
                identity(source, asset, status='ZIP_DIRECTORY_READ', directory_sha256=listing_sha,
                         hash_semantics='DIRECTORY_NAME_SIZE_CRC_NOT_PAYLOAD_SHA256', members=listing,
                         historical_payload_sha256=historical.get('sha256') if historical else None,
                         historical_hash_status='REUSED_CATALOG_NOT_REHASHED' if historical else 'UNKNOWN',
                         historical_size_matches=historical['bytes'] == stat.st_size if historical else None)
                base.update(status='ZIP_DIRECTORY_READ', members=len(entries), directory_sha256=listing_sha)
                inventory.append(base)
                names = set(archive.namelist())
                if source == 'BigQuant':
                    tables = sorted({n.rsplit('/', 1)[0] for n in names if n.endswith('schema.json') or n.endswith('.parquet')})
                    for table in tables:
                        parts = sorted(n for n in names if n.startswith(table + '/') and n.endswith('.parquet') and n.rsplit('/', 1)[0] == table)
                        receipts = []
                        for part in parts:
                            receipt_name = part + '.receipt.json'
                            receipt = json.loads(archive.read(receipt_name)) if receipt_name in names else None
                            if receipt:
                                receipts.append(receipt)
                            partitions.append({'source': source, 'asset': asset, 'table': table, 'member': part,
                                               'bytes': archive.getinfo(part).file_size, 'rows_claim': receipt.get('rows') if receipt else None,
                                               'sha256_claim': receipt.get('sha256') if receipt else None,
                                               'window_start_claim': receipt.get('window_start') if receipt else None,
                                               'window_end_claim': receipt.get('window_end') if receipt else None,
                                               'exported_at': receipt.get('exported_at_utc') if receipt else None,
                                               'upstream_claim': receipt.get('origin') if receipt else None,
                                               'receipt_sha256': digest(archive.read(receipt_name)) if receipt else None,
                                               'hash_status': 'RECEIPT_ONLY_NOT_REHASHED', 'records_status': 'NOT_RUN'})
                        schema_name = table + '/schema.json'
                        schema_bytes = archive.read(schema_name) if schema_name in names else None
                        schema = json.loads(schema_bytes) if schema_bytes else {}
                        fields = schema.get('fields', [])
                        for field in fields:
                            contracts.append({'source': source, 'asset': asset, 'table': table, 'field': field['name'], 'type': field.get('type'),
                                              'meaning': 'UNKNOWN', 'unit': 'UNKNOWN', 'currency': 'UNKNOWN', 'precision': 'UNKNOWN',
                                              'dictionary_version': digest(schema_bytes), 'valid_from': 'UNKNOWN', 'valid_to': 'UNKNOWN',
                                              'evidence': 'ZERO_ROW_SCHEMA_NOT_ECONOMIC_DICTIONARY'})
                        inventory.append({'source': source, 'asset': asset, 'table': table, 'level': 'table', 'domain': domain(table),
                                          'format': 'parquet', 'partitions': len(parts), 'receipts': len(receipts),
                                          'records_claim': sum(r['rows'] for r in receipts) if len(receipts) == len(parts) else None,
                                          'record_evidence': 'RECEIPT_CLAIM_NOT_RECORD_SCAN', 'status': 'SCHEMA_RECEIPT_ONLY',
                                          'fields': fields, 'schema_sha256': digest(schema_bytes) if schema_bytes else None,
                                          'query_window_start_claim': min((r['window_start'] for r in receipts if r.get('window_start')), default=None),
                                          'query_window_end_claim': max((r['window_end'] for r in receipts if r.get('window_end')), default=None),
                                          'primary_key': 'UNKNOWN', 'unit': 'UNKNOWN', 'currency': 'UNKNOWN', 'precision': 'UNKNOWN',
                                          'adjustment': 'NAME_ONLY_NOT_CERTIFIED', 'date_min': None, 'date_max': None,
                                          'exported_at': sorted({r['exported_at_utc'] for r in receipts}),
                                          'unread_scope': 'ALL_RECORDS_EXCEPT_EXPLICIT_CONTENT_PROFILES'})
                        quality.append({'source': source, 'asset': asset, 'table': table, 'check': 'partition_query_window_overlap',
                                        'status': 'OBSERVED' if len(receipts) == len(parts) else 'PARTIAL', 'value': overlaps(receipts),
                                        'scope': 'QUERY_WINDOWS_ONLY_NOT_RECORD_DUPLICATES', 'missing_partitions': 'UNKNOWN_NO_EXPECTED_PARTITION_CONTRACT'})
                        if table in ['stock/cn_stock_basic_info', 'stock/cn_stock_name_change']:
                            frames, schemas = [], set()
                            for part in parts:
                                info = archive.getinfo(part)
                                if info.file_size > MAX_MEMBER_BYTES:
                                    quality.append({'source': source, 'asset': asset, 'table': table, 'check': 'content', 'status': 'NOT_RUN_BYTE_BUDGET', 'member': part})
                                    continue
                                payload = archive.read(part)
                                resources['payload_bytes'] += len(payload)
                                arrow = pq.read_table(pa.BufferReader(payload))
                                schemas.add(str(arrow.schema))
                                frames.append(arrow.to_pandas())
                                receipt = json.loads(archive.read(part + '.receipt.json')) if part + '.receipt.json' in names else {}
                                actual = digest(payload)
                                identity(source, asset + '!' + part, actual_sha256=actual, receipt_sha256=receipt.get('sha256'),
                                         hash_matches_receipt=actual == receipt.get('sha256') if receipt.get('sha256') else None,
                                         actual_rows=arrow.num_rows, receipt_rows=receipt.get('rows'),
                                         receipt_rows_match=arrow.num_rows == receipt.get('rows') if 'rows' in receipt else None,
                                         schema_matches_declared=[{'name': f.name, 'type': str(f.type)} for f in arrow.schema] == fields,
                                         exported_at=receipt.get('exported_at_utc'), observed_at=observed,
                                         published_at='UNKNOWN', available_at='UNKNOWN', collected_at='UNKNOWN')
                                for item in partitions:
                                    if item['asset'] == asset and item['member'] == part:
                                        item.update(hash_status='ACTUAL_MEMBER_SHA256', actual_sha256=actual, actual_rows=arrow.num_rows, records_status='FULL_PARTITION_READ')
                            if frames:
                                frame = pd.concat(frames, ignore_index=True)
                                keys = ['instrument', 'start_date'] if table.endswith('/cn_stock_name_change') else ['instrument']
                                profile = quality_profile(frame, keys, 'start_date' if 'start_date' in frame else None, 'instrument')
                                quality.append({'source': source, 'asset': asset, 'table': table, 'check': 'content_profile', 'status': 'OBSERVED',
                                                'scope': 'FULL_TABLE' if len(frames) == len(parts) else 'PARTIAL_PARTITIONS',
                                                'schema_variants': len(schemas), 'profile': profile})
                                resources['new_profile_rows'] += len(frame)
                    continue
                previous_members = {m['name']: m for m in historical['members']} if historical else {}
                for entry in entries:
                    name = entry.filename
                    old = previous_members.get(name)
                    row = {'source': source, 'asset': asset, 'member': name, 'level': 'member', 'domain': domain(name + ' ' + asset),
                           'table': old.get('family') if old and old.get('family') else Path(name).stem,
                           'format': Path(name).suffix.lower(), 'bytes': entry.file_size, 'crc32': f'{entry.CRC:08x}',
                           'status': 'CATALOG_REUSED' if old else 'DIRECTORY_ONLY', 'record_evidence': 'NOT_RUN',
                           'primary_key': 'UNKNOWN', 'precision': 'UNKNOWN', 'currency': 'UNKNOWN',
                           'unread_scope': 'CURRENT_CONTENT_NOT_RUN', 'adjustment': 'UNKNOWN_UNLESS_DICTIONARY'}
                    if old:
                        row.update(records_claim=old.get('records'), date_min=old.get('date_min'), date_max=old.get('date_max'),
                                   security_count_claim=old.get('security_count'), historical_sha256=old.get('sha256'),
                                   record_evidence='PRIOR_NATIVE_PROFILE_NOT_CURRENT_SCAN' if old.get('profile_available') else 'NOT_RUN',
                                   fields=old.get('schemas'), supplier_vintage=historical.get('supplier_vintage', 'UNKNOWN'))
                        for sheet, schema in old.get('schemas', {}).items():
                            headers, meanings, units = schema['headers'], schema.get('names', []), schema.get('units', [])
                            for idx, field in enumerate(headers):
                                contracts.append({'source': source, 'asset': asset, 'table': row['table'], 'sheet': sheet, 'field': field,
                                                  'type': 'UNKNOWN_NATIVE_CELL_TYPES_NOT_PROFILED',
                                                  'meaning': meanings[idx] if idx < len(meanings) else 'UNKNOWN',
                                                  'unit': units[idx] if idx < len(units) and units[idx] is not None else 'UNKNOWN',
                                                  'currency': 'UNKNOWN_UNLESS_DICTIONARY', 'precision': 'UNKNOWN',
                                                  'dictionary_version': old.get('dictionary_sha256', 'UNKNOWN'),
                                                  'valid_from': 'UNKNOWN', 'valid_to': 'UNKNOWN', 'evidence': 'REUSED_NATIVE_CATALOG'})
                    inventory.append(row)
                    if source == 'CNRDS' and name.endswith('.txt') and '字段说明' in name and entry.file_size <= 1024 * 1024:
                        raw = archive.read(name)
                        resources['payload_bytes'] += len(raw)
                        try:
                            text = raw.decode('utf-8-sig')
                            encoding = 'utf-8-sig'
                        except UnicodeDecodeError:
                            try:
                                text = raw.decode('gb18030')
                                encoding = 'gb18030'
                            except UnicodeDecodeError:
                                quality.append({'source': source, 'asset': asset, 'member': name, 'check': 'dictionary_encoding', 'status': 'UNREADABLE_ENCODING'})
                                continue
                        identity(source, asset + '!' + name, actual_sha256=digest(raw), bytes=len(raw), encoding=encoding,
                                 version_validity='UNKNOWN', observed_at=observed)
                        for line in text.splitlines():
                            match = re.match(r'([^\[]+)\[([^\]]+)\][—-]?(.*)', line)
                            if match:
                                field, meaning, definition = match.groups()
                                unit = re.search(r'单位[：:]\s*([^，。；;]+)', definition)
                                contracts.append({'source': source, 'asset': asset, 'table': Path(name).stem, 'member': name,
                                                  'field': field.strip(), 'type': 'UNKNOWN', 'meaning': meaning + ' ' + definition,
                                                  'unit': unit.group(1) if unit else 'UNKNOWN', 'currency': 'UNKNOWN', 'precision': 'UNKNOWN',
                                                  'dictionary_version': digest(raw), 'valid_from': 'UNKNOWN', 'valid_to': 'UNKNOWN',
                                                  'evidence': 'ACTUAL_DICTIONARY_READ_NOT_HISTORICAL_VALIDITY'})

    # One previously unprofiled native CNRDS workbook; fixed physical prefix.
    cn_archive = roots['CNRDS'] / 'CNSP_个股日回报率_ALL.zip'
    cn_member = '2018/个股日回报率-2018_1.xlsx'
    with zipfile.ZipFile(cn_archive) as archive:
        entry = archive.getinfo(cn_member)
        if entry.file_size > MAX_MEMBER_BYTES:
            quality.append({'source': 'CNRDS', 'asset': cn_archive.name, 'member': cn_member, 'check': 'content', 'status': 'NOT_RUN_BYTE_BUDGET'})
        else:
            raw = archive.read(cn_member)
            resources['payload_bytes'] += len(raw)
            workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            sheet = workbook.worksheets[0]
            iterator = sheet.iter_rows(values_only=True)
            header = list(next(iterator))
            labels = list(next(iterator))
            if labels[:3] != ['股票代码', '公司简称', '交易时间']:
                raise ValueError('CNSP descriptive-header contract differs from inspected workbook')
            rows = []
            for _, values in zip(range(SAMPLE_ROWS), iterator):
                rows.append(values)
            frame = pd.DataFrame(rows, columns=header)
            required = ['Scode', 'Trddt']
            profile = quality_profile(frame, required, 'Trddt', 'Scode')
            quality.append({'source': 'CNRDS', 'asset': cn_archive.name, 'member': cn_member, 'table': 'CNSP',
                            'check': 'content_profile', 'status': 'OBSERVED', 'scope': f'FIRST_{SAMPLE_ROWS}_PHYSICAL_ROWS_FIRST_SHEET_NOT_TARGET_SECURITY_COVERAGE',
                            'sheet_declared_rows': sheet.max_row, 'profile': profile})
            identity('CNRDS', cn_archive.name + '!' + cn_member, actual_sha256=digest(raw), bytes=len(raw),
                     consumed_rows=len(frame), observed_at=observed, collected_at='UNKNOWN', available_at='UNKNOWN', published_at='UNKNOWN')
            resources['new_profile_rows'] += len(frame)
            workbook.close()

    reused = []
    for relative in ['docs/data/p3-d01-statistics.json', 'docs/data/p3-d03-summary.json',
                     'docs/validation/p3-d05/real.json', 'docs/validation/p3-d06/diagnostics.json',
                     'docs/validation/p3-d06/extension-assets.json', 'docs/validation/p3-d06/input-manifest.json']:
        raw = (repo / relative).read_bytes()
        reused.append({'artifact': relative, 'sha256': digest(raw), 'status': 'REUSED_BASELINE_EVIDENCE_NOT_CURRENT_RAW_SCAN'})
    d01 = json.loads((repo / 'docs/data/p3-d01-statistics.json').read_text(encoding='utf-8'))
    # Preserve population-specific historical results; never count them as new reads.
    dump(output / 'reused-quality.json', {'baseline_sha': baseline, 'artifact': 'docs/data/p3-d01-statistics.json', 'diagnostics': d01})
    for table, profile in [('stock/cn_stock_real_bar1d', d01['candidate_population_bars'])] + [
            ('stock/' + table, profile) for table, profile in d01['cross_source']['event_profiles'].items()]:
        quality.append({'source': 'BigQuant', 'asset': 'stock.zip', 'table': table,
                        'check': 'reused_content_profile', 'status': 'REUSED_BASELINE_EVIDENCE_NOT_CURRENT_RAW_SCAN',
                        'scope': 'EXACT_PRIOR_POPULATION_SEE_REUSED_QUALITY_JSON', 'profile': profile})
    for source in roots:
        quality.append({'source': source, 'check': 'unprofiled_content', 'status': 'NOT_RUN',
                        'scope': 'ALL_ASSETS_OUTSIDE_EXPLICIT_CURRENT_PROFILES_AND_BASELINE_REUSED_EVIDENCE',
                        'checks': ['global_duplicates', 'global_conflicts', 'all_partition_actual_ranges', 'certified_calendar', 'stable_id', 'revision_chain']})
    coverage = []
    domains = sorted({row['domain'] for row in inventory})
    for area in domains:
        row = {'domain': area, 'target': 'DOMAIN_NOT_FIXED_SECURITY_DATE_QUERY', 'field_comparability': 'INSUFFICIENT_EVIDENCE'}
        present = 0
        for source in roots:
            assets = [i for i in inventory if i['source'] == source and i['domain'] == area and i.get('role') != 'DOCUMENTATION']
            read = [q for q in quality if q.get('source') == source and q.get('check') in ['content_profile', 'reused_content_profile'] and domain(q.get('table', '')) == area]
            prior = [i for i in assets if i.get('records_claim') is not None and i.get('record_evidence') == 'PRIOR_NATIVE_PROFILE_NOT_CURRENT_SCAN']
            if read:
                state = 'ACTUAL_RECORDS_BOUNDED_SCOPE' if any(q['check'] == 'content_profile' for q in read) else 'PRIOR_RECORDS_BOUNDED_SCOPE'
                present += 1
            elif prior:
                state = 'PRIOR_NATIVE_RECORDS_BOUNDED_SCOPE'
                present += 1
            elif assets:
                state = 'TABLE_OR_FILE_EXISTS_RECORDS_NOT_VERIFIED'
            else:
                state = 'INSUFFICIENT_EVIDENCE'
            row[source] = state
            row[source + '_assets'] = len(assets)
        row['record_presence_class'] = {3: 'THREE_SOURCES_HAVE_BOUNDED_RECORD_EVIDENCE', 2: 'TWO_SOURCES_HAVE_BOUNDED_RECORD_EVIDENCE', 1: 'ONE_SOURCE_HAS_BOUNDED_RECORD_EVIDENCE', 0: 'INSUFFICIENT_EVIDENCE'}[present]
        row['no_target_records'] = 'NOT_EVALUATED_WITHOUT_FIXED_TARGET'
        coverage.append(row)
    findings = []
    for q in quality:
        p = q.get('profile', {}) if q.get('check') == 'content_profile' else {}
        for key in ['duplicate_excess', 'conflicting_keys', 'invalid_date_rows', 'code_format_anomalies', 'nonpositive_price_rows', 'ohlc_order_rows', 'replacement_character_cells']:
            if p.get(key):
                findings.append({'source': q['source'], 'asset': q['asset'], 'table': q.get('table'), 'check': key, 'count': p[key], 'classification': 'STRUCTURAL_OR_STATISTICAL_NOT_CERTIFIED_ECONOMIC', 'scope': q['scope']})
        for field, count in p.items():
            if field.endswith(('_nonnumeric_nonnull', '_numeric_nonfinite')) and count:
                findings.append({'source': q['source'], 'asset': q['asset'], 'table': q.get('table'), 'check': field,
                                 'count': count, 'classification': 'LITERAL_MARKER_OR_TYPE_CONFLICT_REQUIRES_CONTRACT', 'scope': q['scope']})
        for field, count in p.get('nulls', {}).items():
            if count:
                findings.append({'source': q['source'], 'asset': q['asset'], 'table': q.get('table'), 'check': 'nulls', 'field': field, 'count': count, 'classification': 'STATISTICAL_NOT_AUTOMATIC_ERROR', 'scope': q['scope']})
        for field, count in p.get('nonfinite_nonnull', {}).items():
            if count:
                findings.append({'source': q['source'], 'check': 'nonfinite_nonnull', 'field': field, 'count': count, 'scope': q['scope']})
        if q.get('check') == 'partition_query_window_overlap' and q.get('value'):
            findings.append({'source': q['source'], 'asset': q['asset'], 'table': q['table'], 'check': q['check'], 'count': q['value'], 'classification': 'DECLARED_WINDOW_OVERLAP_NOT_RECORD_COLLISION'})
    for item in identities:
        for field in ['hash_matches_receipt', 'receipt_rows_match', 'schema_matches_declared', 'historical_size_matches']:
            if item.get(field) is False:
                findings.append({'source': item['source'], 'asset': item['asset'], 'check': field, 'classification': 'STRUCTURAL_EVIDENCE_CONFLICT'})
    reused.append({'artifact': 'LOCAL_INVENTORY_V2/archive_preview.csv', 'sha256': digest(preview_path.read_bytes()),
                   'status': 'PRIOR_DIRECTORY_INDEX_NOT_CURRENT_7Z_VERIFICATION'})
    provenance = {'observed_at': observed, 'baseline_sha': baseline, 'identities': identities, 'reused': reused,
                  'csmar_catalog': {'sha256': digest(Path(args.csmar_catalog).read_bytes()), 'status': 'PRIOR_NATIVE_CATALOG_NOT_RESCAN'},
                  'time_semantics': {'observed_at': 'THIS_AUDITOR_OBSERVATION_UTC', 'published_at': 'PUBLIC_RELEASE_NOT_ESTABLISHED',
                                     'available_at': 'FIRST_VENDOR_DISTRIBUTION_NOT_ESTABLISHED', 'effective_at': 'ROW_DATE_ONLY_WHERE_FIELD_CONTRACT_PROVES_IT',
                                     'collected_at': 'LOCAL_ACQUISITION_UNKNOWN', 'exported_at': 'VENDOR_RECEIPT_EXPORT_TIME_NOT_PIT'},
                  'historical_identity': {s: {'stable_security_id': 'UNKNOWN', 'code_changes': 'NOT_STABLE_ID_CERTIFIED',
                                             'revisions_replacements_withdrawals': 'UNKNOWN', 'dictionary_validity': 'UNKNOWN',
                                             'historical_listings_delistings_completeness': 'UNKNOWN', 'historical_vintage': 'PIT_UNVERIFIED'} for s in roots}}
    write_csv(output / 'inventory.csv', inventory)
    write_csv(output / 'quality.csv', quality)
    write_csv(output / 'schema-contracts.csv', contracts)
    write_csv(output / 'coverage.csv', coverage)
    write_csv(output / 'partitions.csv', partitions)
    dump(output / 'provenance.json', provenance)
    metrics = dict(resources)
    metrics.update(inventory_rows=len(inventory), field_contract_rows=len(contracts), bigquant_partitions=len(partitions),
                   new_content_profiles=sum(q.get('check') == 'content_profile' for q in quality),
                   finding_items=len(findings), wall_seconds=round(time.perf_counter() - started, 3),
                   cpu_seconds=round(time.process_time() - cpu, 3),
                   peak_working_set_bytes=psutil.Process().memory_info().peak_wset,
                   source_files_by_source={s: sum(i['source'] == s and i['level'] == 'file' for i in inventory) for s in roots},
                   logical_table_entries=sum(i['level'] == 'table' or (i['level'] == 'member' and i['format'] in ['.xlsx', '.xls', '.csv', '.parquet']) for i in inventory))
    summary = {'task_id': 'TRI_SOURCE_ASSETS_A', 'baseline_sha': baseline, 'sources': list(roots),
               'scope': {'directory_depth': 3, 'all_source_files': 'LISTED_WITH_EXPLICIT_NOT_RUN',
                         'new_bigquant_tables': ['cn_stock_basic_info', 'cn_stock_name_change'],
                         'cnrds_sample': {'member': cn_member, 'max_rows': SAMPLE_ROWS}, 'member_byte_budget': MAX_MEMBER_BYTES,
                         'reused_evidence': reused, 'pr79': {'state_at_start': 'OPEN', 'head_sha': '32279f8694b4497d837b940e7565267e7ae720b6', 'role': 'READ_ONLY_REFERENCE_NOT_MAIN'}},
               'metrics': metrics, 'findings': findings,
               'gaps': ['7z members/content NOT_RUN: no installed reader; files explicitly inventoried',
                        'Unselected workbook content and global cross-export key conflicts NOT_RUN',
                        'Actual partition continuity and expected missing partitions UNKNOWN',
                        'Historical stable identity and code-transition validity UNKNOWN',
                        'Vendor first distribution / revision / withdrawal / historical dictionaries UNKNOWN',
                        'BigQuant economic units, currency, precision and adjustment algorithms UNKNOWN',
                        'Full-market / target-security coverage not inferred from schema, receipts or prefix samples',
                        'CNRDS prior FRDT profile remains historical referenced evidence, not revalidated'],
               'artifacts': sorted(p.name for p in output.iterdir()) + ['summary.json', 'REPORT.md'],
               'status': 'PARTIAL_READ_ONLY_AUDIT', 'guards': GUARDS,
               'provenance': {'manifest': 'provenance.json', 'source_paths': 'EXPLICIT_OPERATOR_ARGUMENTS_NOT_PUBLISHED',
                              'originals_modified': False, 'payload_hash_policy': 'REUSE_EXISTING; HASH_ONLY_NEW_CONSUMED_MEMBERS',
                              'raw_rows_published': False}}
    dump(output / 'summary.json', summary)
    print(json.dumps({'status': summary['status'], 'metrics': metrics}, ensure_ascii=False))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['csmar-root', 'cnrds-root', 'bigquant-root', 'csmar-catalog', 'inventory-root', 'output']:
        parser.add_argument('--' + name, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
