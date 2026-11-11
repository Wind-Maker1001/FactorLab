"""Lossless columnar migration of private exact audit evidence."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import zlib
import pyarrow as pa
import pyarrow.parquet as pq
sys.path.insert(0,str(Path(__file__).resolve().parent))
from dq02_native import dump

BATCH=16384
FLOOR=12*1024**3


def raw_payload(value):
    return zlib.decompress(value).decode('utf-8') if isinstance(value,bytes) else value


def records(db,native):
    query='select sheet,key,payload,subject from records' if native else 'select key,payload,partition from rows'
    for row in db.execute(query):
        if native:
            sheet,key,payload,subject=row
            yield sheet,key,raw_payload(payload),subject
        else:
            key,payload,partition=row
            yield partition,key,raw_payload(payload),None


def migrate(receipt_path,target_root):
    import shutil
    receipt_path=Path(receipt_path)
    receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
    if receipt.get('evidence_format')=='columnar-json-lexemes-v1':
        return receipt['columnar_migration']
    native='database' in receipt
    path=Path(receipt['database'] if native else receipt['sqlite_path'])
    dest=Path(target_root)/receipt['id']
    dest.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(dest).free<FLOOR:
        raise OSError('DQ02 disk reserve: less than 12 GiB free')
    connection=sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True)
    # Each original field is a separate compressed column. JSON cell lexemes
    # preserve tagged values, nulls and floating-point representations exactly.
    layouts={}
    if native:
        layouts={s['sheet']:s['headers'] for s in receipt['sheets'] if not s.get('empty')}
        names=sorted({h for hs in layouts.values() for h in hs})
    else:
        names=[f['name'] for f in receipt['schema']]
    columns=['_dq_partition','_dq_key','_dq_subject',*['c'+str(i) for i in range(len(names))]]
    schema=pa.schema([(c,pa.string()) for c in columns]).with_metadata({b'dq02_fields':json.dumps(names,ensure_ascii=False).encode(),b'dq02_layouts':json.dumps(layouts,ensure_ascii=False).encode()})
    target=dest/'records.parquet'
    temp=dest/'records.incomplete.parquet'
    digest=hashlib.sha256()
    count=0
    pending=[]
    started=time.perf_counter()
    with pq.ParquetWriter(temp,schema,compression='zstd',compression_level=6,use_dictionary=True) as writer:
        for partition,key,payload,subject in records(connection,native):
            original=json.loads(payload)
            record=dict(zip(layouts[partition],original)) if native else original
            cells=[json.dumps(record[n],ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False) if n in record else None for n in names]
            values=[partition,key,subject,*cells]
            digest.update(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()+b'\n')
            pending.append(dict(zip(columns,values)))
            count+=1
            if len(pending)>=BATCH:
                writer.write_table(pa.Table.from_pylist(pending,schema=schema))
                pending.clear()
                if shutil.disk_usage(dest).free<FLOOR:
                    raise OSError('DQ02 disk reserve reached during columnar migration')
        if pending:
            writer.write_table(pa.Table.from_pylist(pending,schema=schema))
    expected=connection.execute('select count(*) from '+('records' if native else 'rows')).fetchone()[0]
    connection.close()
    actual=0
    check=hashlib.sha256()
    parquet=pq.ParquetFile(temp)
    for batch in parquet.iter_batches(batch_size=BATCH):
        for values in zip(*(batch.column(i).to_pylist() for i in range(batch.num_columns))):
            check.update(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()+b'\n')
            actual+=1
    parquet.close()
    if count!=expected or actual!=count or check.digest()!=digest.digest():
        raise ValueError('Columnar migration content/row conservation failed')
    temp.replace(target)
    result=dict(rows=count,old_bytes=path.stat().st_size,new_bytes=target.stat().st_size,wall_seconds=time.perf_counter()-started,content_sha256=digest.hexdigest())
    receipt.update(evidence_format='columnar-json-lexemes-v1',columnar_path=str(target),columnar_migration=result)
    # Keep categorical evidence until its columnar conversion is implemented.
    receipt['unfinished_storage_items']=['categorical frequencies remain in old SQLite; old index not deleted']
    dump(dest/'receipt.json',receipt)
    dump(receipt_path,receipt)
    print(json.dumps(result),flush=True)
    return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--receipt',required=True)
    p.add_argument('--target-root',required=True)
    args=p.parse_args()
    migrate(args.receipt,args.target_root)


if __name__=='__main__':
    main()
