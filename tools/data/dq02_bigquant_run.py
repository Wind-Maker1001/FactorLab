"""Run all discovered BigQuant shards with four-process calibration and receipts."""
from __future__ import annotations
import os
os.environ.update({k:'1' for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS')})
import argparse
from concurrent.futures import ProcessPoolExecutor,as_completed
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import traceback
sys.path.insert(0,str(Path(__file__).resolve().parent))
import dq02_parquet
from dq02_native import dump


def execute(job,root):
    import pyarrow as pa
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    directory=Path(root)/job['id']
    receipt=Path(root)/(job['id']+'.json')
    stat=Path(job['archive']).stat()
    identity=dict(bytes=stat.st_size,mtime_ns=stat.st_mtime_ns)
    code=hashlib.sha256(Path(dq02_parquet.__file__).read_bytes()).hexdigest()
    if receipt.exists():
        saved=json.loads(receipt.read_text(encoding='utf-8'))
        if saved['identity']!=identity or saved['code_identity']!=code:
            raise ValueError('Checkpoint source or code identity changed')
        return saved
    try:
        if directory.exists():
            shutil.rmtree(directory)
        result=dq02_parquet.scan(job,root)
        result.update(status='FULL_CONTENT_SCANNED',identity=identity,code_identity=code)
        dump(receipt,result)
        print(json.dumps(dict(event='completed',table=job['table'],id=job['id'],rows=result['rows'],timing=result['timing'],memory=result['memory'])),flush=True)
        return result
    except Exception as error:
        result=dict(id=job['id'],source=job['source'],table=job['table'],job=job,status='UNREADABLE',reason=type(error).__name__+': '+str(error),traceback=traceback.format_exc())
        dump(Path(root)/(job['id']+'-failure.json'),result)
        print(json.dumps(dict(event='failed',**result)),flush=True)
        return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--private-root',required=True)
    p.add_argument('--source-root',required=True)
    args=p.parse_args()
    root=Path(args.private_root)
    root.mkdir(parents=True,exist_ok=True)
    jobs=dq02_parquet.discover(args.source_root)
    dump(root/'discovery.json',jobs)
    # Nonmarket tables go first; all discovered shards remain scheduled.
    jobs.sort(key=lambda j: ('bar1d' in j['table'] or 'factors_' in j['table'],j['table'],j['member']))
    results=[]
    start=time.perf_counter()
    with ProcessPoolExecutor(max_workers=4) as pool:
        for r in pool.map(execute,jobs[:4],[str(root)]*min(4,len(jobs))):
            results.append(r)
    peak=sum(r.get('memory',{}).get('process_peak_rss_bytes',0) for r in results)
    workers=4  # Native scan uses eight concurrent workers; combined ceiling is twelve.
    calibration=dict(initial_workers=4,next_workers=workers,wall_seconds=time.perf_counter()-start,peak_sum_bytes=peak,soft_budget_bytes=14*1024**3,native_budget_bytes=14*1024**3)
    dump(root/'calibration.json',calibration)
    print(json.dumps(dict(event='calibration',**calibration)),flush=True)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(execute,j,str(root)) for j in jobs[4:]]
        for future in as_completed(futures):
            results.append(future.result())
    dump(root/'results.json',dict(receipts=results,wall_seconds=time.perf_counter()-start,calibration=calibration))


if __name__=='__main__':
    main()
