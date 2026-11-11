"""Targeted native pilot recheck and frozen-evidence regression controls."""
import argparse
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import time
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('tri_source_market_audit', Path(__file__).with_name('tri_source_market_audit.py'))
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def run(native, output):
    started = time.perf_counter()
    receipts = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in native.glob('*-*.json') if p.name.startswith(('CSMAR-', 'CNRDS-'))}
    values = {s: [] for s in ['CSMAR', 'CNRDS']}
    for name, receipt in receipts.items():
        for path in sorted((native/'monthly'/name).glob('201[78]-??.parquet')):
            frame = pq.read_table(path, filters=[('code','=','601857')], columns=['date','code',*AUDIT.FIELDS[:6],'filling']).to_pandas()
            if receipt['source'] == 'CSMAR':
                frame = frame.loc[frame.filling.ne('2')]
            if len(frame):
                values[receipt['source']].append(frame)
    cs, cn = [pd.concat(values[s]).set_index('date').sort_index() for s in ['CSMAR','CNRDS']]
    if cs.index.has_duplicates or cn.index.has_duplicates:
        raise ValueError('pilot ambiguous date keys')
    common = cs.index.intersection(cn.index)
    matched, mismatched = 0, 0
    for date in common:
        for field in AUDIT.FIELDS[:6]:
            equal = Decimal(cs.at[date,field]) == Decimal(cn.at[date,field])
            matched += equal
            mismatched += not equal
    expected = ['2017-02-27','2017-05-03','2018-10-12','2018-12-13']
    bq_manifest, differences = [], []
    reader = AUDIT.BQReader('E:/BigQuant Raw/stock.zip', 'stock', bq_manifest)
    for date in expected:
        frame = reader.read('cn_stock_real_bar1d',date,date,['date','instrument','amount'])
        row = frame.loc[frame.instrument.eq('601857.SH')].iloc[0]
        if float(cs.at[date,'amount']) != float(row.amount):
            differences.append(dict(date=date,binary64_CSMAR_minus_BigQuant=float(cs.at[date,'amount'])-float(row.amount),CSMAR_CNRDS_exact_equal=Decimal(cs.at[date,'amount'])==Decimal(cn.at[date,'amount'])))
    prior_path = ROOT/'docs/validation/tri-source/market/summary.json'
    raw = prior_path.read_bytes()
    prior = json.loads(raw)
    fixed, csi = prior['metrics']['fixed24'],prior['metrics']['csi500']
    controls = [dict(name='601857_2017_2018',mode='NATIVE_CHECKPOINT_RECOMPUTED',keys=len(common),matched=matched,mismatched=mismatched,passed=len(common)==487 and matched==2922 and mismatched==0),
        dict(name='BigQuant_four_known_amount_days',mode='FOUR_ARCHIVE_MEMBERS_RECHECKED',cases=differences,passed=[r['date'] for r in differences]==expected,note='Only the four known dates rechecked; absence of additional days inherited from prior full pilot evidence. Binary64 description only; no semantic certification.'),
        dict(name='fixed24_native_filled_prepared_missing',mode='FROZEN_EVIDENCE_CONSISTENCY_REVIEW',native_filled=fixed['BQ_suspended_CS_native_filled_OHLC'],prepared_missing=fixed['BQ_suspended_CS_prepared_missing_OHLC'],native_missing=fixed['BQ_suspended_CS_missing_OHLC'],passed=fixed['BQ_suspended_CS_native_filled_OHLC']==fixed['BQ_suspended_CS_prepared_missing_OHLC']==216 and fixed['BQ_suspended_CS_missing_OHLC']==0),
        dict(name='CSI500_missing_keys_prices',mode='FROZEN_EVIDENCE_CONSISTENCY_REVIEW',missing_keys=csi['raw_missing_keys'],missing_OHLC=csi['raw_OHLC_missing_member_days'],passed=csi['raw_missing_keys']==292 and csi['raw_OHLC_missing_member_days']==564)]
    result = dict(controls=controls,prior_summary_sha256=hashlib.sha256(raw).hexdigest(),BigQuant_inputs=bq_manifest,seconds=time.perf_counter()-started)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if not all(c['passed'] for c in controls):
        raise ValueError('regression control changed')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    run(args.native,args.output)
