"""Bounded, resumable native DQ02 scan driver."""
from __future__ import annotations
import os
os.environ.update({k:'1' for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS')})
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parent))
from dq02_native import scan, dump


def execute(job,root):
    try:
        result = scan(job,root)
        print(json.dumps(dict(event='completed',id=job['id'],source=job['source'],table=job['table'],rows=result['rows'],wall_seconds=result['wall_seconds'],cpu_seconds=result['cpu_seconds'],peak_memory_bytes=result['peak_memory_bytes'])),flush=True)
        return result
    except Exception as error:
        result = dict(id=job['id'],source=job['source'],table=job['table'],archive=Path(job['archive']).name,member=job['member'],status='UNREADABLE',reason=type(error).__name__+': '+str(error),traceback=traceback.format_exc())
        dump(Path(root)/(job['id']+'-failure.json'),result)
        print(json.dumps(dict(event='failed',**result)),flush=True)
        return result


def inherited(jobs,manifest,root):
    old = {(r['source'],r['archive'],r['member']):r for r in json.loads(manifest.read_text(encoding='utf-8'))['native']}
    scan_jobs=[]
    receipts=[]
    for job in jobs:
        key = (job['source'],Path(job['archive']).name,job['member'])
        if key not in old:
            scan_jobs.append(job)
            continue
        previous=old[key]
        digest=hashlib.sha256()
        with zipfile.ZipFile(job['archive']) as archive,archive.open(job['member']) as stream:
            for block in iter(lambda:stream.read(4*1024*1024),b''):
                digest.update(block)
        if digest.hexdigest()!=previous['sha256']:
            scan_jobs.append(job)
            continue
        receipts.append(dict(id=job['id'],source=job['source'],table=job['table'],archive=key[1],member=key[2],status='PARTIAL_CONTENT_SCANNED',evidence='docs/validation/tri-source/market/input-manifest.json',sha256=previous['sha256'],inherited_rows=previous['selected_rows'],rows=0,scope='Previously selected market audit rows and fields only; not full historical table or full-field coverage',identity=job['identity']))
    dump(root/'inherited.json',receipts)
    return scan_jobs,receipts


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--private-root',required=True)
    p.add_argument('--limit',type=int)
    args=p.parse_args()
    root=Path(args.private_root)
    discovery=json.loads((root/'discovery.json').read_text(encoding='utf-8'))
    jobs=discovery['jobs']
    jobs,receipts=inherited(jobs,Path(__file__).resolve().parents[2]/'docs/validation/tri-source/market/input-manifest.json',root)
    # Small independent units establish four-worker observed resource use first.
    jobs.sort(key=lambda j:j['bytes'])
    if args.limit:
        jobs=jobs[:args.limit]
    started=time.perf_counter()
    initial=[]
    with ProcessPoolExecutor(max_workers=4) as pool:
        for result in pool.map(execute,jobs[:4],[str(root)]*min(4,len(jobs))):
            initial.append(result)
            receipts.append(result)
    elapsed=time.perf_counter()-started
    peak=sum(r.get('peak_memory_bytes',0) for r in initial)
    cpu=sum(r.get('cpu_seconds',0) for r in initial)
    workers=8 if peak*2 < 28*1024**3 else 4
    calibration=dict(initial_workers=4,next_workers=workers,wall_seconds=elapsed,worker_cpu_seconds=cpu,average_busy_cores=cpu/elapsed,worker_peak_sum_bytes=peak,soft_memory_budget_bytes=28*1024**3)
    dump(root/'calibration.json',calibration)
    print(json.dumps(dict(event='calibration',**calibration)),flush=True)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(execute,j,str(root)):j for j in jobs[4:]}
        for future in as_completed(futures):
            receipts.append(future.result())
    dump(root/'native-results.json',dict(receipts=receipts,wall_seconds=time.perf_counter()-started,calibration=calibration))


if __name__=='__main__':
    main()
