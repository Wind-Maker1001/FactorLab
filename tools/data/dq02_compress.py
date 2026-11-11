"""Migrate completed exact indexes to lossless compressed BLOB payloads."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import time
import zlib
sys.path.insert(0,str(Path(__file__).resolve().parent))
from dq02_native import dump


def migrate(item):
    receipt_path,native,target_root=item
    receipt_path=Path(receipt_path)
    r=json.loads(receipt_path.read_text(encoding='utf-8'))
    field='database' if native else 'sqlite_path'
    source_path=Path(r[field])
    if r.get('payload_encoding')=='zlib-utf8-v1':
        return dict(id=r['id'],status='ALREADY_MIGRATED')
    target_dir=Path(target_root)/r['id']
    target_dir.mkdir(parents=True,exist_ok=True)
    target=target_dir/source_path.name
    temp=target.with_suffix('.migrating')
    if temp.exists():
        temp.unlink()
    src=sqlite3.connect(f'file:{source_path.as_posix()}?mode=ro',uri=True)
    dst=sqlite3.connect(temp)
    dst.execute('pragma journal_mode=OFF')
    dst.execute('pragma synchronous=OFF')
    dst.execute('pragma cache_size=-16384')
    table='records' if native else 'rows'
    definition=src.execute('select sql from sqlite_master where type=\'table\' and name=?',(table,)).fetchone()[0]
    dst.execute(definition)
    query='select sheet,key,payload,subject from records' if native else 'select key,payload,partition from rows'
    expected=src.execute('select count(*) from '+table).fetchone()[0]
    count=0
    batch=[]
    for row in src.execute(query):
        row=list(row)
        i=2 if native else 1
        raw=row[i].encode('utf-8')
        row[i]=zlib.compress(raw,1)
        batch.append(row)
        count+=1
        if len(batch)>=5000:
            dst.executemany('insert into '+table+' values('+','.join('?' for _ in row)+')',batch)
            batch.clear()
    if batch:
        dst.executemany('insert into '+table+' values('+','.join('?' for _ in batch[0])+')',batch)
    enum_sql=src.execute("select sql from sqlite_master where type='table' and name='enums'").fetchone()
    if enum_sql:
        dst.execute(enum_sql[0])
        dst.executemany('insert into enums values('+('?,?,?,?' if native else '?,?,?')+')',src.execute('select * from enums'))
    dst.commit()
    actual=dst.execute('select count(*) from '+table).fetchone()[0]
    if actual!=expected or count!=expected:
        raise ValueError('Migration row conservation failed')
    # Creating the exact index now avoids needing a second full temporary copy.
    if native:
        dst.execute('create index keys_index on records(sheet,key)')
    else:
        dst.execute('create index keys_index on rows(key)')
    dst.close();src.close()
    temp.replace(target)
    before=source_path.stat().st_size
    r[field]=str(target)
    r['payload_encoding']='zlib-utf8-v1'
    r['storage_migration']=dict(original_code_identity=r['code_identity'],rows=count,original_bytes=before,compressed_bytes=target.stat().st_size,comparison='EXACT_DECOMPRESSED_CANONICAL_PAYLOAD')
    dump(target_dir/'receipt.json',r)
    source_path.unlink()
    dump(receipt_path,r)
    result=dict(id=r['id'],rows=count,before_bytes=before,after_bytes=target.stat().st_size)
    print(json.dumps(result),flush=True)
    return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--private-root',required=True)
    p.add_argument('--target-root',required=True)
    args=p.parse_args()
    root=Path(args.private_root)
    jobs=[(str(p),True,args.target_root+'/native') for p in root.glob('*/receipt.json')]
    for p in (root/'bigquant').glob('*.json'):
        if len(p.stem)==64:
            jobs.append((str(p),False,args.target_root+'/bigquant'))
    start=time.perf_counter()
    with ProcessPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(migrate,jobs))
    dump(Path(args.target_root)/'migration.json',dict(wall_seconds=time.perf_counter()-start,results=results))


if __name__=='__main__':
    main()
