"""Refine only unresolved amount signatures; retain a complete private residual index."""
from collections import Counter
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP
import struct
import argparse
import csv
import json
from pathlib import Path
import time
import pandas as pd
import pyarrow.parquet as pq
import pyarrow as pa
import psutil

pa.set_cpu_count(1)
pa.set_io_thread_count(1)


def run(native,work,output):
    started,cpu=time.perf_counter(),time.process_time()
    receipts={p.stem:json.loads(p.read_text(encoding='utf-8')) for p in native.glob('*-*.json') if p.name.startswith(('CSMAR-','CNRDS-'))}
    profile=pd.read_csv(output/'amount-difference-profile.csv',dtype=str,keep_default_na=False)
    corrections=Counter()
    examples=[]
    residual_count=0
    with (work/'amount-residual-complete.csv').open('w',encoding='utf-8',newline='') as stream:
        fields=['date','symbol','left_locator','right_locator','left','right','delta','relative','post_amount','feature']
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        for path in sorted(work.glob('????-??.csv')):
            month=path.stem
            prior=profile.loc[(profile.dimension=='month') & (profile.group==month)].iloc[0]
            if json.loads(prior.reasons).get('RESIDUAL_NATIVE_VALUE_DIFFERENCE',0)==0:
                continue
            index=pd.read_csv(path,dtype=str,keep_default_na=False)
            index=index.loc[index.field.eq('amount')]
            lookup={}
            for name,receipt in receipts.items():
                part=native/'monthly'/name/(month+'.parquet')
                if not part.exists():
                    continue
                frame=pq.read_table(part,columns=['amount','close','post_amount','sheet','row']).to_pandas()
                prefix=receipt['archive']+'/'+receipt['member']+':'
                lookup.update({prefix+r.sheet+':'+r.row:(name,r) for r in frame.itertuples(index=False)})
            changes=Counter()
            for entry in index.itertuples(index=False):
                csname,cs=lookup[entry.left_locator]
                cnname,cn=lookup[entry.right_locator]
                left,right=Decimal(cs.amount),Decimal(cn.amount)
                # Reproduce the old classifier before testing a new tie rule.
                if abs(left-right)<=1 and left==left.to_integral_value() and right==right.to_integral_value():
                    continue
                if Decimal.from_float(struct.unpack('f',struct.pack('f',float(left)))[0])==right:
                    continue
                if any(left.quantize(Decimal(10)**-p,rounding=ROUND_HALF_UP)==right or right.quantize(Decimal(10)**-p,rounding=ROUND_HALF_UP)==left for p in [0,1,2]):
                    continue
                tied=any(left.quantize(Decimal(10)**-p,rounding=ROUND_HALF_EVEN)==right or right.quantize(Decimal(10)**-p,rounding=ROUND_HALF_EVEN)==left for p in [0,1,2])
                post=Decimal(cs.post_amount) if cs.post_amount not in {None,'','-','--'} else None
                if tied:
                    feature='HALF_EVEN_QUANTIZATION_COMPATIBLE_NOT_PROVEN'
                    corrections[feature]+=1
                    price=Decimal(cs.close)
                    price_bucket='<5' if price<5 else '5-20' if price<20 else '20-100' if price<100 else '>=100'
                    size='<1m' if abs(left)<1000000 else '1m-10m' if abs(left)<10000000 else '10m-100m' if abs(left)<100000000 else '100m-1b' if abs(left)<1000000000 else '>=1b'
                    for dimension,group in [('all','all'),('year',month[:4]),('month',month),('security',entry.symbol),('price_CSMAR',price_bucket),('amount_CSMAR',size),('native_batch',csname+'|'+cnname)]:
                        changes[(dimension,group)]+=1
                else:
                    feature='EXACT_POST_MARKET_COMPONENT_RELATION' if post and (left+post==right or right+post==left) else 'RESIDUAL_NATIVE_VALUE_DIFFERENCE'
                    corrections[feature]+=1
                    residual_count+=1
                    record=dict(date=entry.date,symbol=entry.symbol,left_locator=entry.left_locator,right_locator=entry.right_locator,left=str(left),right=str(right),delta=str(left-right),relative=str(abs(left-right)/abs(left)),post_amount=str(post) if post is not None else '',feature=feature)
                    writer.writerow(record)
                    if len(examples)<20 or abs(left-right)>10000000:
                        examples.append({k:v for k,v in record.items() if k not in {'left','right','post_amount'}})
            for (dimension,group),count in changes.items():
                selected=(profile.dimension==dimension)&(profile.group==group)
                row=profile.index[selected][0]
                reasons=json.loads(profile.at[row,'reasons'])
                reasons['RESIDUAL_NATIVE_VALUE_DIFFERENCE']-=count
                reasons['HALF_EVEN_QUANTIZATION_COMPATIBLE_NOT_PROVEN']=count
                profile.at[row,'reasons']=json.dumps(reasons,sort_keys=True)
    profile.to_csv(output/'amount-difference-profile.csv',index=False)
    result=dict(counts=corrections,residual_rows=residual_count,examples=examples,resource=dict(seconds=time.perf_counter()-started,cpu_seconds=time.process_time()-cpu,peak_process_bytes=getattr(psutil.Process().memory_info(),'peak_wset',psutil.Process().memory_info().rss),workers=1),confidence='EXACT_NUMERIC_SIGNATURES; SUPPLIER_PRECISION_AND_SESSION_CONTRACTS_NOT_PROVEN')
    (output/'amount-refinement.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(counts=corrections,residual_rows=residual_count,resource=result['resource']),indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native',required=True,type=Path)
    p.add_argument('--work',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    a=p.parse_args()
    run(a.native,a.work,a.output)
