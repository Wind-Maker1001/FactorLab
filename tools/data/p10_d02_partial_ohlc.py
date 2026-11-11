"""Resolve the six missing-close mismatch-count exceptions from native rows."""
import argparse
import json
from pathlib import Path
from decimal import Decimal
import pandas as pd
import pyarrow.parquet as pq


def run(native,work,output):
    receipts={p.stem:json.loads(p.read_text(encoding='utf-8')) for p in native.glob('*-*.json') if p.name.startswith(('CSMAR-','CNRDS-'))}
    records=[]
    for month in ['2020-10','2020-11']:
        frame=pd.read_csv(work/(month+'.csv'),dtype=str,keep_default_na=False)
        price=frame.loc[frame.field.isin(['open','high','low','close'])]
        groups=price.groupby(['date','symbol']).field.agg(set)
        keys=set(groups[groups.map(lambda f:f=={'open','high','low'})].index)
        selected=price.loc[[tuple(r) in keys for r in price[['date','symbol']].itertuples(index=False,name=None)]]
        needed=set(selected.left_locator)|set(selected.right_locator)
        lookup={}
        for name,receipt in receipts.items():
            path=native/'monthly'/name/(month+'.parquet')
            if not path.exists():
                continue
            data=pq.read_table(path,columns=['date','code','sheet','row','open','high','low','close','volume','amount','filling','native_record']).to_pandas()
            prefix=receipt['archive']+'/'+receipt['member']+':'
            for row in data.itertuples(index=False):
                locator=prefix+row.sheet+':'+row.row
                if locator in needed:
                    lookup[locator]=(name,row)
        for entry in selected.loc[selected.field.eq('open')].itertuples(index=False):
            left_name,left=lookup[entry.left_locator]
            right_name,right=lookup[entry.right_locator]
            records.append(dict(date=entry.date,symbol=entry.symbol,left_locator=entry.left_locator,right_locator=entry.right_locator,left_sha256=receipts[left_name]['sha256'],right_sha256=receipts[right_name]['sha256'],left_filling=left.filling,volume_zero=Decimal(left.volume)==Decimal(right.volume)==0,close_exact_equal=Decimal(left.close)==Decimal(right.close),CSMAR_OHLC_flat=len({Decimal(getattr(left,f)) for f in ['open','high','low','close']})==1,CNRDS_OHLC_flat=len({Decimal(getattr(right,f)) for f in ['open','high','low','close']})==1,feature='SUSPENSION_FILL_PRICE_SHAPE_DIFFERENCE_WITH_EQUAL_CLOSE'))
    output.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(records,ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native',required=True,type=Path)
    p.add_argument('--work',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    a=p.parse_args()
    run(a.native,a.work,a.output)
