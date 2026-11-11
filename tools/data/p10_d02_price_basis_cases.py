"""Test a next-distribution multiplicative price-basis hypothesis on fixed cases."""
import argparse
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import pandas as pd
import pyarrow.parquet as pq

FIELDS=['open','high','low','close']


def rows(native,source,code,months,receipts):
    pieces=[]
    for name,r in receipts.items():
        if r['source']!=source:
            continue
        for month in months:
            path=native/'monthly'/name/(month+'.parquet')
            if path.exists():
                frame=pq.read_table(path,filters=[('code','=',code)]).to_pandas()
                if len(frame):
                    frame['receipt']=name
                    pieces.append(frame)
    result=pd.concat(pieces,ignore_index=True)
    if source=='CSMAR':
        result=result.loc[result.filling.ne('2')]
    return result.set_index('date').sort_index()


def run(native,work,output):
    receipts={p.stem:json.loads(p.read_text(encoding='utf-8')) for p in native.glob('*-*.json') if p.name.startswith(('CSMAR-','CNRDS-'))}
    events=json.loads((work/'targeted-distribution-native-rows.json').read_text(encoding='utf-8'))
    cases=[]
    for event in events:
        record=event['record']
        code,exdate=record['Stkcd'],record['Exdistdt']
        target_month={'300682':'2018-12','600565':'2019-05','600578':'2019-07'}[code]
        months=sorted({target_month,exdate[:7]})
        cs=rows(native,'CSMAR',code,months,receipts)
        cn=rows(native,'CNRDS',code,months,receipts)
        previous=cs.loc[cs.index<exdate].iloc[-1]
        previous_date=cs.loc[cs.index<exdate].index[-1]
        dividend=Decimal(record['Amount'])
        factor=(Decimal(previous.close)-dividend)/Decimal(previous.close)
        common=cs.index.intersection(cn.index)
        selected=[d for d in common if d.startswith(target_month) and d<exdate]
        tested=matched=raw_equal=0
        for day in selected:
            for field in FIELDS:
                original=Decimal(cs.at[day,field])
                target=Decimal(cn.at[day,field])
                tested+=1
                matched+=int((original*factor).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)==target)
                raw_equal+=int(original==target)
        cases.append(dict(symbol=code,window=target_month,event_date=exdate,event_type=record['Disttyp'],distribution_sheet=event['sheet'],distribution_row=event['row'],pre_event_date=previous_date,pre_event_price_sha256=receipts[previous.receipt]['sha256'],pre_event_price_sheet=previous.sheet,pre_event_price_row=previous.row,tested_fields=tested,matched_next_cash_distribution_formula=matched,raw_equal_fields=raw_equal,
            tested_formula='round_half_up(CSMAR_raw_OHLC * (1 - next_cash_dividend / CSMAR_close_before_exdate), 0.01)',confidence='EXACT_FORMULA_TEST_ON_FIXED_CASES; NOT_SUPPLIER_PIPELINE_OR_FULL_SCOPE_CERTIFICATION'))
    output.write_text(json.dumps(cases,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(cases,ensure_ascii=False,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native',required=True,type=Path)
    parser.add_argument('--work',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    run(args.native,args.work,args.output)
