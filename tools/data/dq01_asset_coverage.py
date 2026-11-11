"""Metadata-first, resumable asset coverage audit. Inputs remain read-only."""
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
from pathlib import Path, PurePosixPath

import psutil
from pypdf import PdfReader

GUARDS = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']
DATA_EXT = {'.xlsx', '.xls', '.csv', '.parquet', '.dbf', '.dta'}
META_LIMIT = 8 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def csvwrite(path, rows, empty_fields=('source', 'asset', 'status', 'reason')):
    fields = list(dict.fromkeys(k for row in rows for k in row)) or list(empty_fields)
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (list, dict)) else v for k, v in row.items()})


def format_of(header):
    if header.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')):
        return 'ZIP'
    if header.startswith(b'7z\xbc\xaf\x27\x1c'):
        return '7Z'
    if header.startswith(b'%PDF-'):
        return 'PDF'
    return 'UNKNOWN'


def category(name):
    rules = [('adjustment_factors', r'adjust.?factor|调整因子'),
             ('capital_structure', r'Capchg|Cptl|股本'),
             ('identity_names', r'basic_info|name_change|Co$|公司基本|公司曾用|招股|首发上市|instruments'),
             ('industry', r'industry|行业'), ('indices_constituents', r'IDX|index|指数|成分'),
             ('trading_status', r'status|suspend|_st$|limit_price|停牌|特殊处理'),
             ('corporate_actions', r'dividend|allotment|分红|分配|增发|配股'),
             ('financial_statements', r'FS_|FRDT|finance|financial|利润|负债|现金流|财务|业绩|费用|存货|应收|应付|投资收益|政府补助|无形资产|固定资产|减值|非经常|税费|债权债务'),
             ('stock_intraday', r'bar1m|分钟'), ('stock_daily', r'bar1d|Dalyr|CNSP|日回报|日行情'),
             ('flows_valuation_other', r'.')]
    return next(label for label, pattern in rules if re.search(pattern, name, re.I))


def table_name(source, asset, member, catalog_member=None):
    path = PurePosixPath(member)
    if source == 'BigQuant' and len(path.parts) >= 3:
        return '/'.join(path.parts[:2])
    if catalog_member and catalog_member.get('family'):
        return catalog_member['family']
    stem = path.stem
    if source == 'CSMAR':
        return re.sub(r'\d+$', '', stem)
    if source == 'CNRDS':
        return re.sub(r'(?:[-_]?(?:19|20)\d{2})?(?:[-_]\d+)?$', '', stem)
    return stem


def pdf_info(path):
    reader = PdfReader(path)
    if reader.is_encrypted:
        return {'status': 'ENCRYPTED', 'reason': 'PDF_PASSWORD_REQUIRED', 'text_status': 'NOT_RUN'}
    texts = [page.extract_text() or '' for page in reader.pages]
    text = '\n'.join(texts)
    return {'status': 'ACCESSIBLE', 'pages': len(texts), 'text_characters': len(text),
            'text_pages': sum(bool(t.strip()) for t in texts),
            'text_status': 'OBSERVED' if text.strip() else 'NOT_RUN_NO_TEXT_LAYER',
            'outline_entries': len(reader.outline),
            'definition_topics': sorted({w for w in ['字段', '单位', '版本', '覆盖', '日期', '财务', '股票', '定义', '修订'] if w in text}),
            'unit_mentions': sorted(set(re.findall(r'(?:单位[：:]\s*)([^\s，。；;]{1,24})', text)))[:40],
            'year_mentions': sorted(set(re.findall(r'\b(?:19|20)\d{2}\b', text))),
            'semantic_scope': 'DOCUMENT_TEXT_ONLY_NOT_OBSERVATION_OR_PIT_EVIDENCE'}


def directory_listing(archive):
    return [{'member': i.filename, 'bytes': i.file_size, 'crc32': f'{i.CRC:08x}',
             'compressed_bytes': i.compress_size, 'encrypted': bool(i.flag_bits & 1)}
            for i in archive.infolist() if not i.is_dir()]


def old_directory_hash(listing):
    return sha(json.dumps(listing, sort_keys=True, ensure_ascii=False).encode())


def failure(error):
    if isinstance(error, PermissionError):
        return 'UNREADABLE', 'PERMISSION_DENIED'
    if isinstance(error, FileNotFoundError):
        return 'MISSING', 'FILE_NOT_FOUND'
    if isinstance(error, (zipfile.BadZipFile, EOFError)):
        return 'CORRUPTED', type(error).__name__
    return 'UNREADABLE', type(error).__name__


def inspect(source, relative, path):
    file = {'source': source, 'asset': relative, 'status': 'NOT_INSPECTED', 'evidence': 'NOT_RUN',
            'payload_integrity': 'NOT_RUN', 'reason': '', 'sha256': '', 'members': 0}
    members, metadata = [], {}
    try:
        stat = path.stat()
        file.update(bytes=stat.st_size, extension=path.suffix.lower())
        with path.open('rb') as stream:
            header = stream.read(16)
            stream.seek(0)
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        file.update(sha256=digest, actual_format=format_of(header), header_hex=header.hex(),
                    extension_matches=path.suffix.lower() == {'ZIP': '.zip', '7Z': '.7z', 'PDF': '.pdf'}.get(format_of(header)),
                    evidence='OBSERVED')
        if file['actual_format'] == 'ZIP':
            with zipfile.ZipFile(path) as archive:
                listing = directory_listing(archive)
                file.update(directory_sha256=old_directory_hash(listing), members=len(listing),
                            directory_status='DIRECTORY_VERIFIED', status='ACCESSIBLE')
                infos = [i for i in archive.infolist() if not i.is_dir()]
                for info, item in zip(infos, listing):
                    row = dict(source=source, asset=relative, **item, format=PurePosixPath(info.filename).suffix.lower(),
                               compression=info.compress_type, inspection='DIRECTORY_VERIFIED', evidence='OBSERVED',
                               content_evidence='NOT_RUN', reason='', payload_sha256='', payload_integrity='NOT_RUN',
                               read_method='ZIP_MEMBER_STREAM', date_hint=';'.join(sorted(set(re.findall(r'(?:19|20)\d{2}', info.filename)))),
                               category=category(relative + '/' + info.filename))
                    if info.flag_bits & 1:
                        row.update(inspection='UNREADABLE', content_evidence='FAILED', reason='MEMBER_ENCRYPTED')
                    elif info.file_size <= META_LIMIT and (info.filename.endswith(('.txt', '/schema.json', '.receipt.json'))):
                        try:
                            raw = archive.read(info)
                            row.update(inspection='PAYLOAD_VERIFIED', content_evidence='OBSERVED', payload_sha256=sha(raw), payload_integrity='CRC_VERIFIED')
                            if info.filename.endswith('.json'):
                                metadata[info.filename] = json.loads(raw)
                            else:
                                for encoding in ['utf-8-sig', 'gb18030']:
                                    try:
                                        metadata[info.filename] = {'dictionary_text': raw.decode(encoding)}
                                        break
                                    except UnicodeDecodeError:
                                        continue
                                else:
                                    row['reason'] = 'DICTIONARY_ENCODING_UNRESOLVED'
                        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
                            row.update(inspection='UNREADABLE', content_evidence='FAILED', reason=type(error).__name__)
                    members.append(row)
                if any(i.flag_bits & 1 for i in infos):
                    file.update(status='ENCRYPTED', reason='ONE_OR_MORE_ENCRYPTED_MEMBERS')
                failed = [m for m in members if m['content_evidence'] == 'FAILED' and m['reason'] != 'MEMBER_ENCRYPTED']
                if failed:
                    file.update(status='CORRUPTED', reason='METADATA_PAYLOAD_READ_FAILED')
        elif file['actual_format'] == '7Z':
            import py7zr
            with py7zr.SevenZipFile(path) as archive:
                if archive.needs_password():
                    file.update(status='ENCRYPTED', reason='ARCHIVE_PASSWORD_REQUIRED')
                else:
                    archive_info = archive.archiveinfo()
                    file.update(status='ACCESSIBLE', directory_status='DIRECTORY_VERIFIED', solid=archive_info.solid)
                    for item in archive.list():
                        if item.is_directory:
                            continue
                        members.append(dict(source=source, asset=relative, member=item.filename, bytes=item.uncompressed,
                                            compressed_bytes=item.compressed, crc32=f'{item.crc32:08x}' if item.crc32 is not None else '',
                                            format=PurePosixPath(item.filename).suffix.lower(), compression=archive_info.method_names,
                                            inspection='DIRECTORY_VERIFIED', evidence='OBSERVED', content_evidence='NOT_RUN',
                                            payload_integrity='NOT_RUN', payload_sha256='', reason='', encrypted=False,
                                            read_method='7Z_SOLID_SINGLE_ARCHIVE_TASK', date_hint=';'.join(sorted(set(re.findall(r'(?:19|20)\d{2}', item.filename)))),
                                            category=category(relative + '/' + item.filename)))
                    file['members'] = len(members)
        elif file['actual_format'] == 'PDF':
            file.update(pdf_info(path))
            file['payload_integrity'] = 'STRUCTURE_AND_TEXT_PARSED_NOT_RENDERED'
        else:
            file.update(status='UNREADABLE', reason='UNSUPPORTED_OR_INVALID_SIGNATURE')
    except Exception as error:
        # Parser failures are audit observations; never silently drop an asset.
        file.update(zip(['status', 'reason'], failure(error)))
        file['evidence'] = 'FAILED'
    return {'file': file, 'members': members, 'metadata': metadata}


def reconcile(roots, known):
    found = {}
    for source, root in roots.items():
        if not root.is_dir():
            raise FileNotFoundError('source root unavailable: ' + source)
        for path in sorted(root.rglob('*')):
            if path.is_file():
                found[source, path.relative_to(root).as_posix()] = path
    return {key: found.get(key, roots[key[0]] / key[1]) for key in sorted(set(found) | set(known))}


def aggregate_tables(members, metadata, catalogs, inherited):
    groups = defaultdict(list)
    for member in members:
        if member['format'] not in DATA_EXT:
            continue
        key = (member['source'], member['asset'], member['member'])
        cat = catalogs.get(key)
        name = table_name(*key, cat)
        member['logical_table'] = name
        groups[member['source'], name].append(member)
    # A schema-only export is still an identified logical table with zero shards.
    for (source, asset, name), value in metadata.items():
        if source == 'BigQuant' and name.endswith('/schema.json'):
            groups.setdefault((source, '/'.join(PurePosixPath(name).parts[:2])), [])
    tables = []
    for (source, name), parts in sorted(groups.items()):
        identities = [(p['source'], p['asset'], p['member']) for p in parts]
        old = [inherited[k] for k in identities if k in inherited]
        catalog = [catalogs[k] for k in identities if k in catalogs]
        schema = [value for (s, a, n), value in metadata.items() if s == source and n == name + '/schema.json']
        dictionaries = [{'member': n, 'decoded_utf8_text_sha256': sha(v['dictionary_text'].encode()),
                         'units': sorted(set(re.findall(r'单位[：:]\s*([^\s，。；;]{1,24})', v['dictionary_text'])))[:40],
                         'fields': re.findall(r'^([^\s\[：:]+)\[', v['dictionary_text'], re.M)}
                        for (s, a, n), v in metadata.items()
                        if s == source and a in {p['asset'] for p in parts} and 'dictionary_text' in v
                        and (source != 'CSMAR' or n.startswith(name + '['))]
        full = len(parts) > 0 and len(old) == len(parts) and all(x['full_rows'] for x in old)
        observed_dates = [(c.get('date_min'), c.get('date_max')) for c in catalog if c.get('profile_available')]
        hints = sorted({h for p in parts for h in p.get('date_hint', '').split(';') if h})
        receipts = [dict(asset=p['asset'], member=p['member'], **{k: v for k, v in metadata.get((source, p['asset'], p['member'] + '.receipt.json'), {}).items()
                         if k in ['table', 'date_column', 'window_start', 'window_end', 'rows', 'bytes', 'sha256', 'exported_at_utc', 'origin']}) for p in parts
                    if (source, p['asset'], p['member'] + '.receipt.json') in metadata]
        row = dict(source=source, logical_table=name, category=category(name + ' ' + ' '.join(p['asset'] for p in parts)),
                   identity_basis='ACTUAL_SCHEMA_PATH' if schema else 'CATALOG_FAMILY_AND_MEMBER_NAMES' if catalog else 'MEMBER_NAME_GROUPING_CANDIDATE',
                   sheet_discovery='INHERITED_CATALOG' if catalog else 'NOT_RUN_WORKBOOK_SHEETS' if any(p['format'] == '.xlsx' for p in parts) else 'NOT_APPLICABLE',
                   partitions=len(parts), assets=sorted({p['asset'] for p in parts}),
                   distribution=[{'asset': p['asset'], 'member': p['member']} for p in parts],
                   schema=schema, dictionary_fields=dictionaries, receipt_metadata=receipts,
                   receipt_window_start=min((r['window_start'] for r in receipts if r.get('window_start')), default='UNKNOWN'),
                   receipt_window_end=max((r['window_end'] for r in receipts if r.get('window_end')), default='UNKNOWN'),
                   receipt_window_semantics='EXPORT_QUERY_CLAIM_NOT_ACTUAL_RECORD_RANGE',
                   catalog_schemas=[c.get('schemas', {}) for c in catalog if c.get('schemas')],
                   field_source='CURRENT_SCHEMA_OR_DICTIONARY_AND_INHERITED_CATALOG_NOT_ROW_VALIDATION',
                   date_min=min((x[0] for x in observed_dates if x[0]), default='UNKNOWN'),
                   date_max=max((x[1] for x in observed_dates if x[1]), default='UNKNOWN'), date_hints=hints,
                   date_scope='INHERITED_CATALOG_OBSERVATIONS' if observed_dates else 'MEMBER_NAME_HINT_ONLY',
                   candidate_primary_key='instrument,date' if source == 'BigQuant' and 'bar1d' in name else 'Stkcd,Trddt' if name == 'TRDNEW_Dalyr' else 'Scode,Trddt' if '日回报率' in name else 'UNKNOWN',
                   candidate_key_status='NOT_CERTIFIED', current_data_payloads_opened=0,
                   inherited_content_partitions=len(old), inherited_full_row_partitions=sum(x['full_rows'] for x in old),
                   full_table_row_scan='INHERITED_VERIFIED' if full else 'NOT_RUN',
                   content_status='INHERITED_VERIFIED' if old else 'NOT_RUN',
                   evidence='INHERITED_VERIFIED' if old else 'METADATA_ONLY',
                   remaining_content_partitions=len(parts) - len(old),
                   evidence_refs=sorted({x['reference'] for x in old}))
        tables.append(row)
    return tables


def run(args):
    start = time.perf_counter()
    repo = Path(__file__).resolve().parents[2]
    out, cache = Path(args.output), Path(args.state)
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    roots = {s: Path(p) for s, p in [('CSMAR', args.csmar_root), ('CNRDS', args.cnrds_root), ('BigQuant', args.bigquant_root)]}
    previous = repo / 'docs/validation/tri-source/assets'
    old_inventory = list(csv.DictReader((previous / 'inventory.csv').open(encoding='utf-8', newline='')))
    known = {(r['source'], r['asset']) for r in old_inventory if r['level'] == 'file'}
    local_inventory = list(csv.DictReader((Path(args.inventory_root) / 'files.csv').open(encoding='utf-8-sig', newline='')))
    old_inventory_keys = {(r['source'].removesuffix(' Raw'), r['relative_path'].replace('\\', '/')) for r in local_inventory if r['source'] in ['CSMAR Raw', 'CNRDS Raw']}
    known |= old_inventory_keys
    original = Path(args.original_inventory)
    original_rows = list(csv.DictReader(original.open(encoding='utf-8-sig', newline='')))
    original_assets = {(r['source'].removesuffix(' Raw'), r['path'].replace('\\', '/').split('/' + r['source'] + '/', 1)[-1]): r
                       for r in original_rows if r['source'] in ['CSMAR Raw', 'CNRDS Raw']}
    known |= set(original_assets)
    catalog = load(args.csmar_catalog)
    catalogs = {('CSMAR', s['name'], m['name']): m for s in catalog['sources'] for m in s['members']}
    source_catalogs = {s['name']: s for s in catalog['sources']}
    prior = load(previous / 'provenance.json')
    prior_identities = {(i['source'], i['asset']): i for i in prior['identities'] if 'asset' in i and 'directory_sha256' in i}
    prior_payloads = {(i['source'], *i['asset'].split('!', 1)): i for i in prior['identities']
                      if '!' in i.get('asset', '') and i.get('actual_sha256')}
    market_path = repo / 'docs/validation/tri-source/market/input-manifest.json'
    market = load(market_path)
    market_native = {(r['source'], r['archive'], r['member']): r for r in market['native']}
    market_bq = {(r['source'], r['archive'], r['member']): r for r in market['bigquant_and_membership'] if r['source'] == 'BigQuant'}
    assets = reconcile(roots, known)
    files, members, metadata, inherited, bindings = [], [], {}, {}, []
    cache_hits = 0
    for (source, relative), path in assets.items():
        ident = sha((source + '/' + relative).encode())
        state = cache / (ident + '.json')
        stat = path.stat() if path.exists() else None
        fingerprint = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns] if stat else None
        version = 'DQ01_INSPECTION_V1'
        if state.exists():
            saved = load(state)
        else:
            saved = {}
        if saved.get('fingerprint') == fingerprint and saved.get('version') == version and 'result' in saved:
            result = saved['result']
            cache_hits += 1
        else:
            result = inspect(source, relative, path)
            save(state, {'fingerprint': fingerprint, 'version': version, 'result': result})
        f = result['file']
        f.update(registration='KNOWN_BASELINE' if (source, relative) in known else 'NEWLY_DISCOVERED',
                 observed_at=datetime.now(timezone.utc).isoformat(), hash_scope='FULL_FILE_BYTES',
                 hash_evidence='INHERITED_VERIFIED_LOCAL_CACHE' if saved and saved.get('fingerprint') == fingerprint and saved.get('version') == version else 'OBSERVED')
        original_entry = original_assets.get((source, relative))
        f['original_inventory_bytes_match'] = int(original_entry['bytes']) == f.get('bytes') if original_entry and original_entry.get('bytes') else 'NOT_RUN'
        prior_i = prior_identities.get((source, relative), {})
        f['prior_directory_match'] = f.get('directory_sha256', '') == prior_i.get('directory_sha256') if prior_i else 'NOT_RUN'
        cat = source_catalogs.get(relative) if source == 'CSMAR' else None
        f['historical_file_sha256'] = cat['sha256'] if cat else ''
        f['historical_file_hash_match'] = f['sha256'] == cat['sha256'] if cat else 'NOT_RUN_NO_PRIOR_FILE_HASH'
        files.append(f)
        current_meta = result['metadata']
        for n, value in current_meta.items():
            metadata[source, relative, n] = value
        for member in result['members']:
            key = (source, relative, member['member'])
            receipt = market_native.get(key)
            prior_cat = catalogs.get(key)
            evidence = None
            if prior_cat and prior_cat.get('profile_available') and f['historical_file_hash_match'] is True:
                evidence = dict(reference='CSMAR_NATIVE_CATALOG', full_rows=True, payload_sha256=prior_cat['sha256'],
                                binding='CURRENT_FULL_ARCHIVE_SHA256_EQUALS_CATALOG', rows=prior_cat.get('records'))
            if receipt and evidence is None and f['prior_directory_match'] is True:
                dictionary = current_meta.get(receipt['dictionary_member'], {})
                dictionary_row = next((m for m in result['members'] if m['member'] == receipt['dictionary_member']), {})
                if dictionary and dictionary_row.get('payload_sha256') == receipt['dictionary_sha256']:
                    evidence = dict(reference='PR85_MARKET_NATIVE', full_rows=True, payload_sha256=receipt['sha256'],
                                    binding='PR81_DIRECTORY_NAME_SIZE_CRC_PLUS_PR85_DICTIONARY_SHA256;CURRENT_PAYLOAD_NOT_REHASHED',
                                    rows=receipt['selected_rows'], scanner_sha256=receipt['code_sha256'])
            bq = market_bq.get(key)
            export = current_meta.get(member['member'] + '.receipt.json', {})
            if bq and member['format'] == '.parquet' and export.get('sha256') == bq['sha256'] and f['prior_directory_match'] is True:
                evidence = dict(reference='PR85_MARKET_BIGQUANT', full_rows=True, payload_sha256=bq['sha256'],
                                binding='PR81_DIRECTORY_NAME_SIZE_CRC_PLUS_CURRENT_RECEIPT_PAYLOAD_SHA256;CURRENT_PAYLOAD_NOT_REHASHED', rows=export.get('rows'))
            prior_payload = prior_payloads.get(key)
            if evidence is None and prior_payload and member['format'] in DATA_EXT and f['prior_directory_match'] is True and (source != 'BigQuant' or export.get('sha256') == prior_payload['actual_sha256']):
                evidence = dict(reference='PR81_IDENTITY_PROFILE' if source == 'BigQuant' else 'PR81_BOUNDED_SAMPLE',
                                full_rows=source == 'BigQuant', payload_sha256=prior_payload['actual_sha256'],
                                binding='PR81_DIRECTORY_NAME_SIZE_CRC_AND_RECEIPT_WHERE_AVAILABLE;CURRENT_PAYLOAD_NOT_REHASHED',
                                rows=prior_payload.get('actual_rows', prior_payload.get('consumed_rows')))
            if evidence and receipt:
                evidence.update(market_scanner_sha256=receipt['code_sha256'], market_selected_rows=receipt['selected_rows'],
                                market_reference='docs/validation/tri-source/market/input-manifest.json')
            member.update(inherited_evidence='INHERITED_VERIFIED' if evidence else 'NOT_RUN',
                          inherited_reference=evidence['reference'] if evidence else '',
                          inherited_binding=evidence['binding'] if evidence else '',
                          historical_payload_sha256=evidence['payload_sha256'] if evidence else '',
                          inherited_full_rows=evidence['full_rows'] if evidence else False)
            if evidence:
                inherited[key] = evidence
                bindings.append(dict(source=source, asset=relative, member=member['member'], **evidence))
            members.append(member)
        print(json.dumps({'source': source, 'asset_id': ident[:12], 'status': f['status'], 'members': f['members'], 'format': f.get('actual_format')}), flush=True)
    hashes = defaultdict(list)
    for f in files:
        if f['sha256']:
            hashes[f['sha256']].append(f['source'] + '/' + f['asset'])
    for f in files:
        f['duplicate_content_assets'] = [x for x in hashes.get(f['sha256'], []) if x != f['source'] + '/' + f['asset']]
    tables = aggregate_tables(members, metadata, catalogs, inherited)
    # Directory signatures identify candidate duplicate payloads, not byte equality.
    signatures = Counter((m['bytes'], m.get('crc32')) for m in members if m.get('crc32'))
    for m in members:
        m['duplicate_signature_count'] = signatures[m['bytes'], m.get('crc32')]
        m['duplicate_signature_semantics'] = 'SIZE_CRC_CANDIDATE_NOT_PAYLOAD_EQUALITY'
    unreadable = [dict(level='file', **f) for f in files if f['status'] != 'ACCESSIBLE']
    unreadable += [dict(level='member', **m) for m in members if m['inspection'] == 'UNREADABLE']
    matrix = []
    for source in roots:
        sf = [f for f in files if f['source'] == source]
        sm = [m for m in members if m['source'] == source]
        st = [t for t in tables if t['source'] == source]
        matrix.append(dict(source=source, discovered_files=sum(f['status'] != 'MISSING' for f in sf), registered_files=len(sf),
                           baseline_files=sum(f['registration'] == 'KNOWN_BASELINE' for f in sf),
                           baseline_found=sum(f['registration'] == 'KNOWN_BASELINE' and f['status'] != 'MISSING' for f in sf),
                           inspected_files=sum(f['status'] != 'NOT_INSPECTED' for f in sf),
                           readable_archives=sum(f['status'] in ['ACCESSIBLE', 'ENCRYPTED'] and f.get('actual_format') in ['ZIP', '7Z'] for f in sf),
                           enumerated_archives=sum(f.get('directory_status') == 'DIRECTORY_VERIFIED' for f in sf),
                           discovered_members=len(sm), directory_checked_members=len(sm),
                           current_content_members=sum(m['content_evidence'] == 'OBSERVED' for m in sm),
                           current_data_content_members=sum(m['content_evidence'] == 'OBSERVED' and m['format'] in DATA_EXT for m in sm),
                           inherited_content_members=sum(m['inherited_evidence'] == 'INHERITED_VERIFIED' for m in sm),
                           logical_tables=len(st), data_partitions=sum(t['partitions'] for t in st),
                           full_table_row_scans=sum(t['full_table_row_scan'] == 'INHERITED_VERIFIED' for t in st),
                           tables_with_inherited_content=sum(t['inherited_content_partitions'] > 0 for t in st),
                           tables_without_content=sum(t['inherited_content_partitions'] == 0 for t in st),
                           tables_missing_full_scan=sum(t['full_table_row_scan'] != 'INHERITED_VERIFIED' for t in st)))
    csvwrite(out / 'files.csv', files)
    csvwrite(out / 'archive-members.csv', members)
    csvwrite(out / 'logical-tables.csv', tables)
    csvwrite(out / 'coverage-matrix.csv', matrix)
    csvwrite(out / 'unreadable-assets.csv', unreadable)
    baseline = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    from importlib.metadata import version as package_version
    provenance = dict(observed_at=datetime.now(timezone.utc).isoformat(), baseline=baseline, guards=GUARDS,
                      dependencies={name: package_version(name) for name in ['pypdf', 'psutil']},
                      metadata_limit_bytes=META_LIMIT, cache_contract='DQ01_INSPECTION_V1; size, mtime_ns, ctime_ns; private local trusted checkpoint',
                      scanner_sha256=sha(Path(__file__).read_bytes()),
                      evidence_inputs={'PR81': sha((previous / 'provenance.json').read_bytes()), 'PR85': sha(market_path.read_bytes()),
                                       'native_catalog': sha(Path(args.csmar_catalog).read_bytes()),
                                       'inventory_v2': sha((Path(args.inventory_root) / 'files.csv').read_bytes()),
                                       'LOCAL_ORIGINAL_INVENTORY': sha(original.read_bytes())},
                      original_inventory_rows=len(original_rows), source_aliases=list(roots), inherited_bindings=bindings,
                      inheritance_semantics='Historical row execution evidence only; CRC-directory/receipt bindings are not current payload cryptographic verification.',
                      duplicate_file_groups=[v for v in hashes.values() if len(v) > 1],
                      resource_policy={'workers': 1, 'soft_budget_bytes': 4 * 1024**3, 'payload_scans': 0, 'hash_chunking': 'hashlib.file_digest', 'solid_archive_extractions': 0})
    save(out / 'provenance.json', provenance)
    verification = dict(status='PARTIAL', guards=GUARDS, coverage=matrix,
                        elapsed_seconds=time.perf_counter() - start, peak_working_set_bytes=psutil.Process().memory_info().peak_wset,
                        cache_hits=cache_hits, files_hashed=len(files) - cache_hits,
                        extension_mismatches=sum(f.get('extension_matches') is False for f in files),
                        cnrds_7z_extensions=dict(Counter(f.get('actual_format', 'UNKNOWN') for f in files if f['source'] == 'CNRDS' and f.get('extension') == '.7z')),
                        unreadable_assets=len(unreadable), pdf_results=[{k:v for k,v in f.items() if k not in ['sha256','historical_file_sha256']} for f in files if f.get('actual_format') == 'PDF'],
                        tests='NOT_RUN', payload_verification='DEFERRED_DQ02',
                        unresolved=['Workbook sheet-level discovery outside inherited catalog NOT_RUN', 'Full payload CRC/decompression NOT_RUN except small metadata',
                                    'No historical file hash for CNRDS/BigQuant; new full-file SHA establishes current baseline only',
                                    'Name-based logical grouping is not a certified vendor database table registry'])
    if args.initial_verification:
        initial = load(args.initial_verification)
        verification['initial_inspection'] = {k: initial[k] for k in ['elapsed_seconds', 'peak_working_set_bytes', 'files_hashed', 'cache_hits']}
    verification['identity_conflicts'] = [dict(source=f['source'], asset=f['asset'], kind=k)
                                          for f in files for k in ['historical_file_hash_match', 'prior_directory_match', 'original_inventory_bytes_match']
                                          if f.get(k) is False]
    verification['new_files'] = sum(f['registration'] == 'NEWLY_DISCOVERED' for f in files)
    if args.test_results:
        import xml.etree.ElementTree as ET
        test_root = ET.parse(args.test_results).getroot()
        suites = list(test_root.iter('testsuite'))
        verification['tests'] = dict(tests=sum(int(s.get('tests', 0)) for s in suites),
                                     failures=sum(int(s.get('failures', 0)) for s in suites),
                                     errors=sum(int(s.get('errors', 0)) for s in suites),
                                     skipped=sum(int(s.get('skipped', 0)) for s in suites),
                                     seconds=sum(float(s.get('time', 0)) for s in suites),
                                     junit_sha256=sha(Path(args.test_results).read_bytes()))
    save(out / 'verification.json', verification)
    lines = ['# DQ-01 三源资产可访问性与覆盖清单', '', '**PARTIAL**', '', ' / '.join(GUARDS), '',
             '## 实测口径', '', '文件逐一读取文件头并流式计算完整 SHA256；归档按魔数而非后缀解析。当前载荷读取限于小型字典、schema、receipt 及独立 PDF。目录可读不代表大型载荷已解压、CRC 验证或逐行扫描。', '',
             '|来源|基线找到/登记|本次检查|归档枚举|成员|逻辑表候选|数据分片|整表逐行旧证据|完全无内容证据表|',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in matrix:
        lines.append(f"|{row['source']}|{row['baseline_found']}/{row['baseline_files']}|{row['inspected_files']}|{row['enumerated_archives']}|{row['discovered_members']}|{row['logical_tables']}|{row['data_partitions']}|{row['full_table_row_scans']}|{row['tables_without_content']}|")
    lines += ['', '完整分母见 [coverage-matrix.csv](coverage-matrix.csv)。成员数不含目录；数据分片只计 xlsx/xls/csv/parquet/dbf/dta，字典、PDF、schema 与 receipt 不计数据分片。逻辑表按来源隔离；BigQuant 使用实际 schema 路径，CSMAR 优先原生 family，其余按年度/序号规范化成员名分组。未打开工作簿的未知工作表没有冒充已发现独立逻辑表。', '',
              '## 后缀与实际格式', '', f"CNRDS 的 .7z 文件实测格式：`{verification['cnrds_7z_extensions']}`。后缀不符单列于 files.csv，不据此判损坏。", '',
              '## 当前与继承证据', '',
              'archive-members.csv 的 inspection 只描述当前检查；inherited_evidence 单列旧证据。CSMAR 原生 catalog 的来源以当前完整归档 SHA256 对比；PR #85 用 PR #81 当前目录身份（路径、声明大小、CRC、压缩大小）及实际字典 SHA256 绑定，BigQuant 另要求当前 receipt 载荷 SHA256 等于旧 manifest。CRC 绑定不是本轮大型载荷 SHA256 重验。每个继承项的版本、成员哈希、范围及绑定方式见 provenance.json。', '',
              'PR #85 已完整解析的 43 个原生工作簿包含 25,177,454 条选定原生记录；日期/证券范围和数值比较范围以既有 REPORT、summary 与 input-manifest 为准。完整解析成员不意味着归档中其他年度已扫，也不意味着整个逻辑表已全量扫描。CSMAR catalog 的全量 profile 不是所有字段的数值质量认证。PR #87、#89 是已读行情上的定向归因，不扩展资产内容覆盖。', '',
              '## PDF 与字段语义', '',
              '独立 PDF 均尝试结构解析、逐页文本提取及 outline 检查；verification.json 给出页数、文本页数、文本字符数、定义主题、单位及年份线索。不启动 OCR；文档线索不证明数据值、历史可得性或 PIT。归档内 PDF 仅目录检查，不能与独立的 14 份文档混算正文读取。字典语义不代替实际记录证据。', '',
              '## 未完成内容与失败', '',
              'logical-tables.csv 逐表列出剩余未读分片、字段来源和证据范围；没有内容证据的表为 METADATA_ONLY。unreadable-assets.csv 给出文件及成员失败原因（空表表示本轮指定检查没有读取失败，不代表所有载荷无损）。没有对未解压大型载荷宣称无损、无截断或 CRC 通过。', '',
              'TODO(DQ-02)：验证剩余数据载荷及工作表清单，取得真实记录日期范围；在不重复已完成行情扫描的前提下补齐逐行检查。', '',
              '## 身份、重复与复现', '',
              '所有可读文件建立本次完整 SHA256；file 重复按完整哈希，member 重复候选按 size/CRC，二者不可混用。来源由显式输入根和清单交叉定位，不以文件同名认证供应商来源。无首次分发、修订链及正式 PIT 证据。检查点保存于私有 state 目录，以脚本版本、大小、mtime/ctime 绑定恢复，不在重复执行时重算文件哈希。', '',
              '运行依赖现有 Python、pypdf、psutil；只有真正的 7z 容器需要 py7zr。本次 65 个误命名 ZIP 无须 7z 解压。可用 --test-results 绑定定向测试 JUnit XML，用 --initial-verification 保留首次完整检查资源。', '',
              '```text',
              'python -I -B -X utf8 tools/data/dq01_asset_coverage.py --csmar-root <CS> --cnrds-root <CN> --bigquant-root <BQ> --inventory-root <INV2> --original-inventory <LOCAL_ORIGINAL_INVENTORY.csv> --csmar-catalog <CATALOG> --state <PRIVATE_STATE> --output docs/validation/dq-01',
              'python -I -B -m pytest tests/evidence/test_dq01_asset_coverage.py -q -p no:cacheprovider', '```', '',
              f"本次汇总单进程，墙钟 {verification['elapsed_seconds']:.2f} 秒，峰值工作集 {verification['peak_working_set_bytes']} 字节，缓存命中 {cache_hits}。首次完整检查资源：`{verification.get('initial_inspection', '本次即首次检查，见 verification.json')}`。当前数据行扫描 0，不重读 2,500 万行；没有解压固实归档。",
              '', f"定向测试：`{verification['tests']}`。身份冲突 {len(verification['identity_conflicts'])} 项；新增未登记文件 {verification['new_files']} 个。完整文件重复组见 provenance.json，不能把 member 的 size/CRC 候选当成完整内容重复。"]
    (out / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps(verification, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ['csmar-root', 'cnrds-root', 'bigquant-root', 'inventory-root', 'original-inventory', 'csmar-catalog', 'state', 'output']:
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--initial-verification', help='Optional initial full-run verification receipt for resource attribution')
    parser.add_argument('--test-results', help='JUnit XML from the targeted coverage tests')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
