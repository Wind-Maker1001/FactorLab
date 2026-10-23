"""Compare outputs, preserving failures and reporting model drift explicitly."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd


def compare(left, right):
    result = []
    names = ['factors.parquet','dataset.parquet']
    for model in ('factor_score','ridge','lightgbm','xgboost'):
        names += [model+'/'+name+'.parquet' for name in
                  ('predictions','daily','fast_nav','fast_trades','execution_nav','execution_trades','execution_rejections')]
    for name in names:
        a, b = pd.read_parquet(left/name), pd.read_parquet(right/name)
        row = {'artifact':name,'left_shape':list(a.shape),'right_shape':list(b.shape)}
        try:
            pd.testing.assert_frame_equal(a,b,check_exact=True,check_dtype=True)
            row['exact'] = True
        except AssertionError:
            row['exact'] = False
        try:
            pd.testing.assert_frame_equal(a,b,check_exact=False,check_dtype=True,rtol=1e-12,atol=1e-12)
            row['status'] = 'PASS'
        except AssertionError as exc:
            row.update(status='FAIL',reason=str(exc)[:1200])
        numeric = {}
        if a.shape == b.shape and list(a.columns)==list(b.columns):
            for column in a.select_dtypes(include='number').columns:
                av,bv = a[column].to_numpy(dtype=float),b[column].to_numpy(dtype=float)
                valid = np.isfinite(av)&np.isfinite(bv)
                difference = np.abs(av[valid]-bv[valid])
                numeric[column] = {'max_absolute_difference':float(difference.max()) if len(difference) else None,
                                   'mean_absolute_difference':float(difference.mean()) if len(difference) else None,
                                   'missing_mask_equal':bool(np.array_equal(np.isnan(av),np.isnan(bv)))}
        row['numeric'] = numeric
        result.append(row)
    return {'schema':'FACTORLAB_NUMERICAL_COMPARISON_V1','tolerance':{'rtol':1e-12,'atol':1e-12},
        'baseline':str(left),'candidate':str(right),'status':'PASS' if all(x['status']=='PASS' for x in result) else 'FAIL',
        'artifacts':result,'safety':'SYNTHETIC / SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE'}


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('left',type=Path); p.add_argument('right',type=Path); p.add_argument('output',type=Path)
    args=p.parse_args()
    report=compare(args.left,args.right)
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(report['status'])
    for item in report['artifacts']:
        print(item['artifact'],item['status'], 'exact='+str(item['exact']))
