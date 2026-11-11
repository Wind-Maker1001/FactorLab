"""Streaming lexical XLSX quality statistics and disk-backed exact candidate keys."""
from __future__ import annotations
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib

os.environ.update({k: '1' for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS')})
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from factorlab.data.csmar.native import SafeXML, NS, sheets, peak_memory_bytes

MISSING = {'', 'null', 'NULL', 'None', 'N/A', 'NA', 'NaN', 'nan', '--', '-'}


def dump(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def iter_rows(book, member, shared):
    with book.open(member) as stream:
        parent = None
        for event, node in ET.iterparse(SafeXML(stream), events=('start','end')):
            if event == 'start' and node.tag == NS + 'sheetData':
                parent = node
            if event != 'end' or node.tag != NS + 'row':
                continue
            cells = {}
            for cell in node:
                coordinate = cell.get('r', '')
                match = re.match(r'([A-Z]+)', coordinate)
                if match is None:
                    raise ValueError('Missing cell coordinate')
                column = 0
                for c in match[1]:
                    column = column * 26 + ord(c) - 64
                kind = cell.get('t','n')
                value = cell.findtext(NS + 'v')
                if kind == 's' and value is not None:
                    value = shared[int(value)]
                elif kind == 'inlineStr':
                    value = ''.join(x.text or '' for x in cell.iter(NS + 't'))
                cells[column] = (value, kind, cell.find(NS + 'f') is not None)
            yield int(node.get('r')), [cells.get(i, (None,'absent',False)) for i in range(1,max(cells,default=0)+1)]
            node.clear()
            if parent is not None:
                parent.clear()


def shared_strings(book):
    values = []
    if 'xl/sharedStrings.xml' in book.namelist():
        with book.open('xl/sharedStrings.xml') as stream:
            root = None
            for event,node in ET.iterparse(SafeXML(stream), events=('start','end')):
                if root is None:
                    root = node
                if event == 'end' and node.tag == NS + 'si':
                    values.append(''.join(x.text or '' for x in node.iter(NS+'t')))
                    node.clear()
                    root.clear()
    return values


def dictionary(job):
    result = {}
    for text in job['dictionaries'].values():
        for line in text.splitlines():
            match = re.match(r'([A-Za-z][A-Za-z0-9_]*)\s*\[', line)
            if match:
                result[match[1]] = line
    return result


def business_keys(table, headers, descriptions):
    """Candidate granularity; versions and business dimensions remain in the key."""
    # TODO: Review subject/subitem dimensions in vendor dictionaries before
    # interpreting candidate collisions as invalid business-key duplicates.
    h = set(headers)
    code = next((x for x in ('Stkcd','SCode','Scode','Symbol','StockCode','Stockcode','SecCode','Indexcd') if x in h), None)
    keys = []
    reason = 'DICTIONARY_BUSINESS_DIMENSIONS_CANDIDATE'
    if table.startswith('FS_'):
        keys = [code, 'Accper' if 'Accper' in h else 'EndDate', 'Typrep', 'IfCorrect', 'DeclareDate']
        if 'InstitutionID' in h:
            keys.append('InstitutionID')
    elif table.startswith('FN_FN'):
        keys = [code,'Accper','Typrep','DataSources','SubjectCode']
    elif table.startswith('TRDNEW_Cptl'):
        keys = [code,'Disttyp','Exdistdt','Markettype','Annodt','Paydt']
    elif table.startswith('TRDNEW_Capchg'):
        keys = [code,'Shrchgdt','Shrtyp']
    elif table.startswith('TRDNEW_Co'):
        keys = [code,'Markettype']
    elif table.startswith('IDX_'):
        # Encoded index fields are interpreted using their descriptions.
        keys = [x for x in headers if x in {'Indexcd','Stkcd','Enddt'} or any(s in descriptions.get(x,'').split(' - ')[0] for s in ('日期','代码','类型'))]
    elif table.startswith('LT_Largetrd'):
        keys = [code,'Trddt','Trdtim','Trdtims']
    elif table.startswith(('STK_MKT','TRD_','CHN_','TRDNEW_Dalyr')):
        keys = [x for x in (code,'TradingDate','Trddt','Mtdate','Exchangecode','SecurityTypeID') if x in h]
    else:
        # Explicit event IDs supersede date-only guesses.
        if 'Itemid' in h:
            keys = ['Itemid',code]
        elif table == '公司曾用名':
            keys = [code,'Chgnmdt','Chgnmbef','Chgnmaft']
        elif table == '公司基本信息':
            keys = [code]
        elif '重仓流通股' in table:
            keys = [code,'Date','InstName']
        elif table in {'利润表','现金流量表','资产负债表','IPO前利润表','IPO前现金流量表','IPO前资产负债表'}:
            keys = [x for x in (code,'Date','PrspADt') if x in h]
        else:
            dimensions = {'InstName','Borwtype','Loanpj','Rltptnm','Debtornm','Aging','A_code','B_code','Fpenddt','Type','Item','Itemnm','State','SubCat','TCatCd','Mmbbnsnm','Discresn','Exchange','ImProgress','Seoprog','Altprog','PContent','AccFirm','CoName_A'}
            dates = [x for x in headers if is_date(x,descriptions.get(x,'')) or x == 'Year']
            keys = [x for x in headers if x == code or x in dimensions or x in dates]
            if not dates:
                keys = []
                reason = 'UNRESOLVED_EVENT_IDENTITY'
    keys = [x for x in keys if x is not None and x in h]
    return keys, reason


def is_date(field, description):
    label = description.split(' - ')[0]
    return field in {'Date','Trddt','Accper','EndDate','TradingDate','Mtdate'} or '日期' in label or 'YYYY-MM-DD' in description


def parse_date(value, kind):
    if kind == 'n' and re.fullmatch(r'\d+(?:\.0+)?',value):
        return (date(1899,12,30)+timedelta(days=int(float(value)))).isoformat()
    text = value.strip()[:10].replace('/','-')
    if re.fullmatch(r'\d{8}',text):
        text = text[:4]+'-'+text[4:6]+'-'+text[6:]
    return date.fromisoformat(text).isoformat()


def field_state(name, description):
    numeric = bool(re.search(r'单位[：:]\s*(?:元|万元|亿元|股|万股|%|％|次|倍)',description)) or bool(re.fullmatch(r'[ABCD][A-Za-z0-9]{8,}',name))
    dated = is_date(name,description)
    return dict(field=name, description=description, expected='date' if dated else 'numeric' if numeric else 'unspecified', rows=0,
                raw_types={}, null_markers={}, numeric_valid=0, numeric_invalid=0, nonfinite=0, negative=0, zero=0,
                min=None,max=None,max_decimal_places=None,date_valid=0,date_invalid=0,date_min=None,date_max=None,
                date_months={},formula_cells=0)


def observe(state, cell):
    value,kind,formula = cell
    state['rows'] += 1
    state['raw_types'][kind] = state['raw_types'].get(kind,0)+1
    state['formula_cells'] += formula
    marker = '<ABSENT>' if value is None else value
    if value is None or value in MISSING:
        state['null_markers'][marker] = state['null_markers'].get(marker,0)+1
        return
    if state['expected'] == 'date':
        try:
            parsed = parse_date(value,kind)
        except (ValueError,OverflowError):
            state['date_invalid'] += 1
        else:
            state['date_valid'] += 1
            state['date_min'] = min(state['date_min'] or parsed,parsed)
            state['date_max'] = max(state['date_max'] or parsed,parsed)
            month = parsed[:7]
            state['date_months'][month] = state['date_months'].get(month,0)+1
    if state['expected'] == 'numeric' or kind == 'n':
        try:
            number = Decimal(value)
        except InvalidOperation:
            state['numeric_invalid'] += 1
        else:
            state['numeric_valid'] += 1
            if not number.is_finite():
                state['nonfinite'] += 1
                return
            state['negative'] += number < 0
            state['zero'] += number == 0
            if state['min'] is None or number < Decimal(state['min']):
                state['min'] = value
            if state['max'] is None or number > Decimal(state['max']):
                state['max'] = value
            places = max(0,-number.as_tuple().exponent)
            state['max_decimal_places'] = max(state['max_decimal_places'] or 0,places)


def scan(job, private_root):
    started,cpu = time.perf_counter(),time.process_time()
    root = Path(private_root) / job['id']
    root.mkdir(parents=True,exist_ok=True)
    receipt = root/'receipt.json'
    stat = Path(job['archive']).stat()
    identity = dict(bytes=stat.st_size,mtime_ns=stat.st_mtime_ns)
    if identity != job['identity']:
        raise ValueError('Input identity changed')
    code_identity = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if receipt.exists():
        previous = json.loads(receipt.read_text(encoding='utf-8'))
        prior = previous.get('storage_migration',{}).get('original_code_identity')
        compatible = previous.get('payload_encoding')=='zlib-utf8-v1' and prior=='bab8bc1212e0929295605d82507dfa3f854c202ae8c912c47e1284e520de4476'
        if previous['identity'] != identity or (previous['code_identity'] != code_identity and not compatible):
            raise ValueError('Checkpoint identity changed')
        return previous
    temp = root/'input.xlsx'
    member_hash = hashlib.sha256()
    with zipfile.ZipFile(job['archive']) as outer, outer.open(job['member']) as stream, temp.open('wb') as target:
        for block in iter(lambda:stream.read(1024*1024),b''):
            target.write(block)
            member_hash.update(block)
    database = root/'keys.sqlite'
    if database.exists():
        database.unlink()  # Incomplete unit is replayed, never accepted as a checkpoint.
    con = sqlite3.connect(database)
    con.execute('pragma journal_mode=OFF')
    con.execute('pragma synchronous=OFF')
    con.execute('pragma cache_size=-32768')
    con.execute('create table records(sheet text, key text, payload text, subject text)')
    con.execute('create table enums(sheet text, field text, value text, n integer, primary key(sheet,field,value)) without rowid')
    descriptions = dictionary(job)
    result = dict(id=job['id'],source=job['source'],table=job['table'],archive=Path(job['archive']).name,
                  member=job['member'],identity=identity,code_identity=code_identity,sha256=member_hash.hexdigest(),
                  status='FULL_CONTENT_SCANNED',sheets=[],rows=0,physical_rows=0,metadata_rows=0,unresolved=[])
    with zipfile.ZipFile(temp) as book:
        shared = shared_strings(book)
        for sheet,member in sheets(book).items():
            iterator = iter_rows(book,member,shared)
            try:
                _,header_cells = next(iterator)
            except StopIteration:
                result['sheets'].append(dict(sheet=sheet,rows=0,empty=True))
                continue
            headers = [x[0] or f'__unnamed_{i}' for i,x in enumerate(header_cells)]
            if len(set(headers)) != len(headers):
                raise ValueError('Duplicate field names: '+sheet)
            states = [field_state(h,descriptions.get(h,'')) for h in headers]
            keys,reason = business_keys(job['table'],headers,descriptions)
            indexes = [headers.index(k) for k in keys]
            subject_index = next((i for i,h in enumerate(headers) if h.lower() in {'stkcd','scode','symbol','stockcode','seccode','indexcd'}),None)
            sheet_result = dict(sheet=sheet,headers=headers,keys=keys,key_reason=reason,rows=0,physical_rows=0,metadata_rows=0,missing_key_rows=0,fields=states,report_after_disclosure=0,ragged_rows=0)
            pending = []
            enum_counts = Counter()
            for coordinate,cells in iterator:
                sheet_result['physical_rows'] += 1
                # CSMAR supplies two descriptor rows after its field-name row.
                if job['source']=='CSMAR' and coordinate in {2,3}:
                    sheet_result['metadata_rows'] += 1
                    continue
                sheet_result['rows'] += 1
                if len(cells)>len(headers):
                    sheet_result['ragged_rows'] += 1
                cells = (cells+[(None,'absent',False)]*len(headers))[:len(headers)]
                for state,cell in zip(states,cells):
                    observe(state,cell)
                    if state['expected']=='unspecified' and cell[0] is not None and cell[0] not in MISSING:
                        enum_counts[(sheet,state['field'],cell[0])] += 1
                values = [x[0] for x in cells]
                report_field = next((f for f in ('Accper','EndDate','Date') if f in headers),None)
                disclosure_field = next((f for f in ('DeclareDate','PubliDate','ActlDt') if f in headers),None)
                if report_field and disclosure_field:
                    a,b=cells[headers.index(report_field)],cells[headers.index(disclosure_field)]
                    if a[0] and b[0] and a[0] not in MISSING and b[0] not in MISSING:
                        try:
                            sheet_result['report_after_disclosure'] += parse_date(a[0],a[1]) > parse_date(b[0],b[1])
                        except (ValueError,OverflowError):
                            pass  # Invalid dates are counted independently in field statistics.
                key_values = [values[i] for i in indexes]
                missing = any(v is None or v in MISSING for v in key_values)
                sheet_result['missing_key_rows'] += bool(keys and missing)
                key = json.dumps(key_values,ensure_ascii=False,separators=(',',':')) if keys else None
                payload = zlib.compress(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode('utf-8'),1)
                pending.append((sheet,key,payload,values[subject_index] if subject_index is not None else None))
                if len(pending)>=5000:
                    con.executemany('insert into records values(?,?,?,?)',pending)
                    con.executemany('insert into enums values(?,?,?,?) on conflict(sheet,field,value) do update set n=n+excluded.n', ((*k,v) for k,v in enum_counts.items()))
                    con.commit()
                    pending.clear()
                    enum_counts.clear()
            if pending:
                con.executemany('insert into records values(?,?,?,?)',pending)
                con.executemany('insert into enums values(?,?,?,?) on conflict(sheet,field,value) do update set n=n+excluded.n', ((*k,v) for k,v in enum_counts.items()))
                con.commit()
            for state in states:
                state['enum_distinct'] = con.execute('select count(*) from enums where sheet=? and field=?',(sheet,state['field'])).fetchone()[0]
            result['sheets'].append(sheet_result)
            for k in ('rows','physical_rows','metadata_rows'):
                result[k] += sheet_result[k]
    con.execute('create index keys_index on records(sheet,key)')
    for sheet in result['sheets']:
        if sheet.get('empty'):
            continue
        groups = con.execute('select count(*),count(distinct payload) from records where sheet=? and key is not null group by key having count(*)>1',(sheet['sheet'],))
        duplicates=excess=conflicts=0
        for count,distinct in groups:
            duplicates += 1
            excess += count-1
            conflicts += distinct>1
        sheet.update(duplicate_keys=duplicates,duplicate_excess=excess,conflicting_keys=conflicts,
                     subjects=con.execute('select count(distinct subject) from records where sheet=?',(sheet['sheet'],)).fetchone()[0])
    con.close()
    temp.unlink()
    result.update(payload_encoding='zlib-utf8-v1',database=str(database),wall_seconds=time.perf_counter()-started,cpu_seconds=time.process_time()-cpu,peak_memory_bytes=peak_memory_bytes())
    dump(receipt,result)
    return result
