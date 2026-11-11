"""Check historical distribution dates against targeted OHLC difference episodes."""
import argparse
from collections import defaultdict
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import pandas as pd
import zipfile

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('audit',Path(__file__).with_name('tri_source_market_audit.py'))
AUDIT=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def run(work,output):
    catalog=json.loads(Path('E:/FactorLabData/candidates/csmar-601857-2018-20261105/inventory/catalog.json').read_text(encoding='utf-8'))
    source=next(s for s in catalog['sources'] if any(m.get('family')=='TRDNEW_Cptl' for m in s['members']))
    member=next(m for m in source['members'] if m.get('family')=='TRDNEW_Cptl')
    with zipfile.ZipFile(Path('E:/CSMAR Raw')/source['name']) as archive:
        raw=archive.read(member['name'])
        dictionary=archive.read('TRDNEW_Cptl[DES][xlsx].txt')
    if hashlib.sha256(raw).hexdigest()!=member['sha256'] or hashlib.sha256(dictionary).hexdigest()!=AUDIT.PROFILES['TRDNEW_Cptl'][0]:
        raise ValueError('distribution member/dictionary identity mismatch')
    affected=defaultdict(set)
    for path in sorted(work.glob('2019-??.csv')):
        frame=pd.read_csv(path,dtype=str,keep_default_na=False)
        for row in frame.loc[frame.field.eq('open')].itertuples(index=False):
            affected[row.symbol].add(row.date)
    events=defaultdict(list)
    headers={}
    native_samples=[]
    affected_codes={s[:6] for s in affected}
    for sheet,row,values in AUDIT.workbook_rows(raw):
        if row==1:
            headers[sheet]=values
            continue
        record=dict(zip(headers[sheet],values))
        if record.get('Stkcd') not in affected_codes or not record.get('Exdistdt','').startswith('2019'):
            continue
        events[record['Stkcd']].append(record)
        if record['Stkcd'] in {'600565','600578','300682'}:
            native_samples.append(dict(sheet=sheet,row=row,record=record))
    observations=[]
    for symbol,days in sorted(affected.items()):
        last=max(days)
        later=[e for e in events[symbol[:6]] if 0 <= (date.fromisoformat(e['Exdistdt'])-date.fromisoformat(last)).days <= 10]
        observations.append(dict(symbol=symbol,first_difference=min(days),last_difference=last,difference_days=len(days),distribution_within_10_calendar_days_after_last_difference=bool(later),event_dates='|'.join(sorted({e['Exdistdt'] for e in later})),event_types='|'.join(sorted({e['Disttyp'] for e in later}))))
    pd.DataFrame(observations).to_csv(output.with_name('ohlc-corporate-action-associations.csv'),index=False)
    (work/'targeted-distribution-native-rows.json').write_text(json.dumps(native_samples,ensure_ascii=False,indent=2),encoding='utf-8')
    result=dict(archive=source['name'],member=member['name'],sha256=member['sha256'],dictionary_sha256=hashlib.sha256(dictionary).hexdigest(),selection='2019 open-difference securities; 2019 distribution records; last difference to ex-date interval [0,10] calendar days',affected_securities=len(affected),securities_with_boundary_distribution=sum(r['distribution_within_10_calendar_days_after_last_difference'] for r in observations),status='ASSOCIATION_ONLY; NOT_PROOF_OF_ADJUSTMENT_FORMULA_OR_HISTORICAL_ISSUER_IDENTITY',representative_events=[dict(symbol=r['record']['Stkcd'],ex_date=r['record']['Exdistdt'],type=r['record']['Disttyp'],sheet=r['sheet'],row=r['row']) for r in native_samples])
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    run(args.work,args.output)
