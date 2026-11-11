"""Targeted dictionary identity and third-source numeric diagnostics."""
import argparse
from collections import Counter
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]

def module(name, filename):
    spec = importlib.util.spec_from_file_location(name,Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

PROFILE = module('profile','profile_market_differences.py')
AUDIT = module('audit','tri_source_market_audit.py')


def run(native, work, output):
    receipts = {p.stem:json.loads(p.read_text(encoding='utf-8')) for p in native.glob('*-*.json') if p.name.startswith(('CSMAR-','CNRDS-'))}
    baseline = json.loads((ROOT/'docs/validation/tri-source/market/input-manifest.json').read_text(encoding='utf-8'))
    expected = {(r['source'],r['archive'],r['member']):r for r in baseline['native']}
    for receipt in receipts.values():
        reference = expected[(receipt['source'],receipt['archive'],receipt['member'])]
        for field in ['sha256','dictionary_sha256','code_sha256']:
            if receipt[field] != reference[field]:
                raise ValueError('checkpoint receipt differs from merged manifest: '+field)
    dictionary_checks=[]
    for source, year in [('CSMAR',None),('CNRDS','2019'),('CNRDS','2018')]:
        receipt=next(r for r in receipts.values() if r['source']==source and (year is None or r['member'].startswith(year+'/')))
        base=Path('E:/CSMAR Raw' if source=='CSMAR' else 'E:/CNRDS Raw')
        with zipfile.ZipFile(base/receipt['archive']) as archive:
            raw=archive.read(receipt['dictionary_member'])
        actual=hashlib.sha256(raw).hexdigest()
        if actual!=receipt['dictionary_sha256']:
            raise ValueError('dictionary identity changed')
        text=raw.decode('utf-8-sig')
        fields = ['Opnprc','Hiprc','Loprc','Clsprc','Dnvaltrd','Filling'] if source=='CSMAR' else ['Dopnprc','Dhiprc','Dloprc','Dclsprc','Dtrdamt','Adclsprc']
        lines=[line for line in text.splitlines() if line.split('[')[0].strip() in fields]
        dictionary_checks.append(dict(source=source,member=receipt['dictionary_member'],sha256=actual,field_definitions=lines))
    bq_manifest=[]
    reader=AUDIT.BQReader('E:/BigQuant Raw/stock.zip','stock',bq_manifest)
    observations=[]
    for month in ['2018-12','2019-05']:
        index=pd.read_csv(work/(month+'.csv'),dtype=str,keep_default_na=False)
        data=PROFILE.load_month(native,month,receipts)
        lookup={}
        for source,frame in data.items():
            frame['locator']=frame.receipt.map(lambda n:receipts[n]['archive']+'/'+receipts[n]['member'])+':'+frame.sheet+':'+frame.row
            lookup[source]={r.locator:r for r in frame.itertuples(index=False)}
        bq=reader.read('cn_stock_real_bar1d',month+'-01',month+'-31',['date','instrument','open','high','low','close','amount','volume']).set_index(['date','instrument'])
        for field in ['amount','open']:
            counts=Counter()
            for entry in index.loc[index.field.eq(field)].itertuples(index=False):
                key=(entry.date,entry.symbol)
                if key not in bq.index:
                    counts['BigQuant_absent']+=1
                    continue
                cs,cn=lookup['CSMAR'][entry.left_locator],lookup['CNRDS'][entry.right_locator]
                left,right=Decimal(getattr(cs,field)),Decimal(getattr(cn,field))
                third=bq.at[key,field]
                if pd.isna(third):
                    counts['BigQuant_null']+=1
                    continue
                third=Decimal.from_float(float(third))
                counts['compared']+=1
                counts['CSMAR_binary64_equals_BigQuant']+=int(float(left)==float(third))
                counts['CNRDS_binary64_equals_BigQuant']+=int(float(right)==float(third))
                if field=='amount':
                    counts['CSMAR_rounds_BigQuant_to_integer']+=int(left==third.quantize(Decimal(1),rounding=ROUND_HALF_UP))
                    counts['CNRDS_truncates_BigQuant_to_integer']+=int(right==third.quantize(Decimal(1),rounding=ROUND_DOWN))
                    counts['joint_round_truncate_relation']+=int(left==third.quantize(Decimal(1),rounding=ROUND_HALF_UP) and right==third.quantize(Decimal(1),rounding=ROUND_DOWN))
            observations.append(dict(month=month,field=field,counts=counts,status='DESCRIPTIVE_NUMERIC_RELATION_ONLY; BIGQUANT_UNIT_AND_PRECISION_CONTRACT_OPEN'))
    output.write_text(json.dumps(dict(receipt_identities_match_merged_manifest=len(receipts),dictionary_checks=dictionary_checks,third_source_diagnostics=observations,BigQuant_inputs=bq_manifest),ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(observations,ensure_ascii=False,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native',required=True,type=Path)
    parser.add_argument('--work',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    run(args.native,args.work,args.output)
