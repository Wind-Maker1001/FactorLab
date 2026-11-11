"""Bounded BigQuant content statistics: no full-row materialization or data indexes."""
from __future__ import annotations
import os
for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[name]='1'
import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor,as_completed
import csv
from datetime import date,datetime
import hashlib
import json
from pathlib import Path
import shutil
import time
import zipfile
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import psutil

DISK=4*1024**3
RESERVE=80*1024**3
BATCH=16384
KEY_MEMORY=64*1024**2
WRITE_LOCK=None


def initialize(lock):
    global WRITE_LOCK
    WRITE_LOCK=lock


def write_block(dst,block,root,reservation):
    with WRITE_LOCK:
        check_budget(root,reservation,len(block))
        dst.write(block)
        dst.flush()
ENUM={'type','method','industry','industry_level','change_flag','inout_flag','special_treatment','suspend_period','market_code','exchange'}
KEYS={
 'cn_stock_name_change':['instrument','start_date','name'],
 'cn_stock_adjust_factor':['date','instrument','type','method'],
 'cn_stock_industry_component':['date','instrument','industry'],
 'cn_stock_industry_change':['date','instrument','industry','industry_level','industry_code','change_flag'],
 'cn_stock_index_change':['date','instrument','member_code','inout_flag'],
 'cn_stock_dividend':['instrument','report_date','publish_date','register_date','ex_date'],
 'cn_stock_allotment':['instrument','publish_date','register_date','exright_date','allot_listdate'],
 'cn_stock_suspend':['date','instrument','suspend_period','suspend_reason'],
}

def key_fields(table,names):
    name=table.rsplit('/',1)[-1]
    if name in KEYS:key=KEYS[name]
    elif 'member_code' in names:key=['date','instrument','member_code']
    elif name.endswith('_basic_info'):key=['instrument']
    elif name.endswith('_bar1d') or name.startswith('cn_stock_factors_') or name in {'cn_stock_valuation','cn_stock_status','cn_stock_limit_price'}:key=['date','instrument']
    else:return []
    return key if set(key)<=set(names) else []


def json_value(value):
    if isinstance(value,(date,datetime)):return value.isoformat()
    if isinstance(value,float) and not __import__('math').isfinite(value):return str(value)
    if isinstance(value,dict):return {str(k):json_value(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [json_value(v) for v in value]
    if value is not None and not isinstance(value,(str,int,float,bool)):return str(value)
    return value


def size(root):return sum(p.stat().st_size for p in Path(root).rglob('*') if p.is_file())


def check_budget(root,reservation,additional=0):
    used=size(root)
    if used+reservation+additional>DISK:raise RuntimeError('BLOCKED_RESOURCE: shared 4 GiB disk budget')
    if shutil.disk_usage(root).free-reservation-additional<RESERVE:raise RuntimeError('BLOCKED_RESOURCE: 80 GiB free-space reserve')
    return used


def save(path,value,root,reservation):
    raw=json.dumps(json_value(value),ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>4*1024**2:raise RuntimeError('BLOCKED_RESOURCE: oversized aggregate receipt')
    with WRITE_LOCK:
        check_budget(root,reservation,len(raw))
        temp=path.with_suffix('.pending');temp.write_bytes(raw);temp.replace(path)


def discover(root):
    jobs=[]
    for path in sorted(Path(root).glob('*.zip')):
        st=path.stat()
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.filename.endswith('.parquet'):
                    ident=dict(archive=path.name,archive_bytes=st.st_size,archive_mtime_ns=st.st_mtime_ns,member=info.filename,bytes=info.file_size,compressed=info.compress_size,crc=info.CRC)
                    jobs.append(dict(id=hashlib.sha256(json.dumps(ident,sort_keys=True).encode()).hexdigest()[:24],table=info.filename.rsplit('/',1)[0],path=str(path),identity=ident))
    return jobs


def sum_true(array):return pc.sum(pc.cast(array,pa.int64())).as_py() or 0


def scan(job,root,reservation,metadata=False):
    root=Path(root);receipt=root/'parts'/(job['id']+'.json')
    code=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if receipt.exists():
        saved=json.loads(receipt.read_text(encoding='utf-8'))
        if saved['identity']!=job['identity'] or saved['code']!=code:raise ValueError('Input/code identity changed; refuse checkpoint')
        if not metadata and saved['status']=='FULL_CONTENT_SCANNED':return saved,None
    path=Path(job['path']);st=path.stat()
    if (st.st_size,st.st_mtime_ns)!=(job['identity']['archive_bytes'],job['identity']['archive_mtime_ns']):raise ValueError('Input identity changed')
    check_budget(root,reservation)
    local=root/'stage'/(job['id']+'.parquet')
    start=time.perf_counter();cpu=time.process_time();rows=groups=0;keys=[];key_bytes=0;key_blocked=False
    result=dict(id=job['id'],table=job['table'],identity=job['identity'],code=code,status='METADATA_VERIFIED',rows=0,read_row_groups=0)
    try:
        with zipfile.ZipFile(path) as archive:
            schema_name=job['identity']['member'].rsplit('/',1)[0]+'/schema.json'
            dictionary=json.loads(archive.read(schema_name)) if schema_name in archive.namelist() else {}
            receipt_name=job['identity']['member']+'.receipt.json'
            supplier=json.loads(archive.read(receipt_name)) if receipt_name in archive.namelist() else {}
            with archive.open(job['identity']['member']) as src,local.open('xb') as dst:
                for block in iter(lambda:src.read(1024**2),b''):
                    write_block(dst,block,root,reservation)
        file=pq.ParquetFile(local)
        fields={f.name:dict(type=str(f.type),nulls=0,nan=0,inf=0,empty_strings=0,negative=0,zero=0,min=None,max=None) for f in file.schema_arrow}
        declared={f['name']:f.get('type') for f in dictionary.get('fields',[])}
        names=file.schema_arrow.names;key=key_fields(job['table'],names)
        result.update(declared_rows=file.metadata.num_rows,row_groups=file.num_row_groups,columns=len(names),schema={f.name:str(f.type) for f in file.schema_arrow},dictionary_missing=sorted(set(declared)-set(names)),dictionary_extra=sorted(set(names)-set(declared)),supplier_receipt=supplier,key_fields=key)
        dates={};enums={};enum_overflow=set();out_of_range=0
        import re
        window=re.search(r'(\d{4}-\d\d-\d\d)_(\d{4}-\d\d-\d\d)',job['identity']['member'])
        for group in range(file.num_row_groups):
            if metadata:break
            for batch in file.iter_batches(batch_size=BATCH,row_groups=[group],use_threads=False):
                rows+=batch.num_rows
                for f,array in zip(batch.schema,batch.columns):
                    stat=fields[f.name];typ=f.type;stat['nulls']+=array.null_count
                    if pa.types.is_dictionary(typ):array=pc.cast(array,typ.value_type);typ=typ.value_type
                    numeric=pa.types.is_integer(typ) or pa.types.is_floating(typ) or pa.types.is_decimal(typ)
                    temporal=pa.types.is_timestamp(typ) or pa.types.is_date(typ)
                    valid=array
                    if pa.types.is_floating(typ):
                        stat['nan']+=sum_true(pc.is_nan(array));stat['inf']+=sum_true(pc.and_(pc.invert(pc.is_finite(array)),pc.invert(pc.is_nan(array))))
                        valid=pc.filter(array,pc.is_finite(array))
                    if numeric:
                        stat['negative']+=sum_true(pc.less(valid,0));stat['zero']+=sum_true(pc.equal(valid,0))
                    if numeric or temporal:
                        bounds=pc.min_max(valid).as_py()
                        for b in ('min','max'):
                            v=bounds[b]
                            if v is not None and (stat[b] is None or (v<stat[b] if b=='min' else v>stat[b])):stat[b]=v
                    if pa.types.is_string(typ) or pa.types.is_large_string(typ):stat['empty_strings']+=sum_true(pc.equal(array,''))
                    if temporal:
                        months=pc.add(pc.multiply(pc.year(array),100),pc.month(array));dest=dates.setdefault(f.name,{})
                        for item in pc.value_counts(months).to_pylist():
                            if item['values'] is not None:
                                m=str(item['values']);dest[m]=dest.get(m,0)+item['counts']
                        if window and f.name=='date':
                            days=pc.cast(array,pa.date32());out_of_range+=sum_true(pc.or_(pc.less(days,pa.scalar(date.fromisoformat(window[1]))),pc.greater(days,pa.scalar(date.fromisoformat(window[2])))))
                    if f.name in ENUM and f.name not in enum_overflow:
                        vc=pc.value_counts(array)
                        if len(vc)>128:enum_overflow.add(f.name);enums.pop(f.name,None)
                        else:
                            dest=enums.setdefault(f.name,{})
                            for item in vc.to_pylist():
                                v=str(item['values']);dest[v]=dest.get(v,0)+item['counts']
                            if len(dest)>128:enum_overflow.add(f.name);enums.pop(f.name,None)
                if key and not key_blocked:
                    projected=pa.Table.from_batches([batch.select(key)])
                    key_bytes+=projected.nbytes
                    if key_bytes>KEY_MEMORY:key_blocked=True;keys.clear()
                    else:keys.append(pa.Table.from_arrays([pa.array(c.to_pylist(),type=c.type) for c in projected.columns],names=key))
                if psutil.Process().memory_info().rss>3*1024**3:raise RuntimeError('BLOCKED_RESOURCE: worker memory ceiling')
            groups+=1
        file.close()
        if not metadata and rows!=result['declared_rows']:raise ValueError('Row conservation failure')
        result.update(status='METADATA_VERIFIED' if metadata else 'FULL_CONTENT_SCANNED',rows=rows,read_row_groups=groups,fields=fields,dates=dates,enums=enums,enum_overflow=sorted(enum_overflow),date_outside_filename=out_of_range,key_memory_blocked=key_blocked,wall_seconds=time.perf_counter()-start,cpu_seconds=time.process_time()-cpu,peak_rss_bytes=getattr(psutil.Process().memory_info(),'peak_wset',psutil.Process().memory_info().rss))
    finally:
        if local.exists():local.unlink()
    result['post_unit_bytes']=check_budget(root,reservation)
    save(receipt,result,root,reservation)
    return json_value(result),keys if not metadata else None


def exact_keys(chunks,key):
    table=pa.concat_tables(chunks)
    nulls=0
    mask=None
    for name in key:
        current=pc.is_null(table[name]);mask=current if mask is None else pc.or_(mask,current)
    nulls=sum_true(mask)
    valid=table.filter(pc.invert(mask))
    counts=valid.group_by(key,use_threads=False).aggregate([(key[0],'count')])
    values=counts.column(-1)
    return dict(rows=table.num_rows,null_key_rows=nulls,duplicate_keys=sum_true(pc.greater(values,1)),duplicate_excess=pc.sum(pc.subtract(values,1)).as_py() or 0,unique_nonnull_keys=counts.num_rows)


def table_job(jobs,root,reservation):
    pa.set_cpu_count(1);pa.set_io_thread_count(1)
    chunks=[];total_bytes=0;blocked=False;key=None;reused=False
    for job in jobs:
        try:r,k=scan(job,root,reservation)
        except (RuntimeError,ValueError,OSError,pa.ArrowException,zipfile.BadZipFile) as error:
            r=dict(id=job['id'],table=job['table'],identity=job['identity'],status='BLOCKED_RESOURCE' if 'RESOURCE' in str(error) else 'READ_FAILED',reason=str(error),rows=0)
            save(Path(root)/'parts'/(job['id']+'.json'),r,root,reservation)
            if 'shared 4 GiB' in str(error) or '80 GiB' in str(error):raise
            blocked=True;continue
        if k is None:reused=True;blocked=True
        fields=r.get('key_fields',[])
        if key is not None and key!=fields:blocked=True
        key=fields
        if r.get('key_memory_blocked'):blocked=True
        if not blocked and k:
            total_bytes+=sum(t.nbytes for t in k)
            if total_bytes>KEY_MEMORY:blocked=True;chunks.clear()
            else:chunks.extend(k)
        if blocked:chunks.clear()
    result=dict(table=jobs[0]['table'],fields=key or [],status='CANDIDATE_KEY_EXACT' if key and not blocked and chunks else 'KEY_CHECK_BLOCKED_RESOURCE' if key else 'KEY_CONTRACT_UNKNOWN',scope_partitions=len(jobs),reason='Compact-key 512 MiB per-table ceiling or checkpoint-only resume' if blocked else 'No supplier uniqueness certification; exact candidate comparison only',reused_content=reused)
    if chunks and not blocked and key:result.update(exact_keys(chunks,key))
    save(Path(root)/'keys'/(hashlib.sha256(jobs[0]['table'].encode()).hexdigest()[:24]+'.json'),result,root,reservation)
    print(json.dumps({'table':jobs[0]['table'],'partitions':len(jobs),'key_status':result['status']}),flush=True)
    return result


def write_csv(path,rows):
    names=list(dict.fromkeys(k for row in rows for k in row)) or ['status']
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=names);w.writeheader()
        for row in rows:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()})


def main():
    import multiprocessing
    manager=multiprocessing.Manager()
    initialize(manager.RLock())
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--state',required=True);p.add_argument('--output',required=True);p.add_argument('--phase',choices=['preflight','calibrate','scan'],required=True);args=p.parse_args()
    root=Path(args.state);root.mkdir(parents=True,exist_ok=True)
    for child in ('stage','parts','keys','runtime'): (root/child).mkdir(exist_ok=True)
    os.environ['TMP']=os.environ['TEMP']=str(root/'runtime')
    jobs=discover(args.source);maximum=max(j['identity']['bytes'] for j in jobs);reservation=maximum*2+256*1024**2
    check_budget(root,reservation)
    if args.phase=='preflight':
        save(root/'preflight.json',dict(partitions=len(jobs),tables=len({j['table'] for j in jobs}),archives=len({j['path'] for j in jobs}),maximum_member_bytes=maximum,registered_paths=[str(root),args.output],all_jobs=jobs,initial_free_bytes={d:shutil.disk_usage(d+':/').free for d in ('D','E','T')},disk_budget=DISK,reserve=RESERVE),root,reservation);return
    start=time.perf_counter();cpu=time.process_time()
    grouped=defaultdict(list)
    for job in jobs:grouped[job['table']].append(job)
    if args.phase=='calibrate':
        # Whole small logical tables avoid rereading sample shards for later keys.
        selected=sorted(grouped.values(),key=lambda js:sum(j['identity']['bytes'] for j in js))[:3]
        largest=max(jobs,key=lambda j:j['identity']['bytes'])
        selected.append([largest])
        with ProcessPoolExecutor(max_workers=2,initializer=initialize,initargs=(WRITE_LOCK,)) as pool:
            for future in as_completed([pool.submit(table_job,js,str(root),reservation) for js in selected]):future.result()
        save(root/'calibration.json',dict(wall_seconds=time.perf_counter()-start,content_partitions=sum(len(js) for js in selected),largest_member=largest['identity'],disk_bytes=size(root)),root,reservation);return
    with ProcessPoolExecutor(max_workers=2,initializer=initialize,initargs=(WRITE_LOCK,)) as pool:
        for future in as_completed([pool.submit(table_job,js,str(root),reservation) for js in grouped.values()]):future.result()
    save(root/'run.json',dict(wall_seconds=time.perf_counter()-start,coordinator_cpu_seconds=time.process_time()-cpu,ending_bytes=size(root),concurrency=2),root,reservation)


if __name__=='__main__':main()
