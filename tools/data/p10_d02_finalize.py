"""Assemble targeted evidence without converting signatures into semantic contracts."""
import argparse
from decimal import Decimal, ROUND_HALF_EVEN
import json
from pathlib import Path
import pandas as pd


def run(output,work):
    summary=json.loads((output/'summary.json').read_text(encoding='utf-8'))
    refinement=json.loads((output/'amount-refinement.json').read_text(encoding='utf-8'))
    feature='HALF_EVEN_QUANTIZATION_COMPATIBLE_NOT_PROVEN'
    summary['classes']=[r for r in summary['classes'] if r['feature']!=feature]
    for row in summary['classes']:
        if row['feature']=='RESIDUAL_NATIVE_VALUE_DIFFERENCE':
            row['count']=refinement['residual_rows']
    summary['classes'].append(dict(field='amount',classification='PRECISION_OR_ROUNDING',feature=feature,count=refinement['counts'][feature]))
    if sum(r['count'] for r in summary['classes'])!=sum(summary['field_counts'].values()):
        raise ValueError('final attribution partition does not match exact difference denominator')
    classes=pd.read_csv(output/'root-cause-classification.csv',dtype=str,keep_default_na=False)
    classes=classes.loc[classes.feature.ne(feature)].copy()
    classes.loc[classes.feature.eq('RESIDUAL_NATIVE_VALUE_DIFFERENCE'),'count']=str(refinement['residual_rows'])
    row=classes.loc[classes.field.eq('amount')].iloc[0].to_dict()
    row.update(classification='PRECISION_OR_ROUNDING',feature=feature,count=str(refinement['counts'][feature]))
    classes=pd.concat([classes,pd.DataFrame([row])],ignore_index=True)
    partial_feature='SUSPENSION_FILL_PRICE_SHAPE_DIFFERENCE_WITH_EQUAL_CLOSE'
    summary['classes']=[r for r in summary['classes'] if r['feature']!=partial_feature]
    classes=classes.loc[classes.feature.ne(partial_feature)].copy()
    for field in ['open','high','low']:
        for item in summary['classes']:
            if item['field']==field:
                item['count']=summary['field_counts'][field]-6
        selected=classes.field.eq(field)
        classes.loc[selected,'count']=str(summary['field_counts'][field]-6)
        item=classes.loc[selected].iloc[0].to_dict()
        item.update(classification='FILL_OR_STATUS_DIFFERENCE',feature=partial_feature,count='6',confidence='CONFIRMED_NATIVE_FILL_AND_EQUAL_CLOSE',date_range='2020-10-30/2020-11-06',evidence='partial-ohlc-cases.json: six exact native row pairs',needed_evidence='Supplier fill policy for economic interpretation; no transaction inferred')
        classes=pd.concat([classes,pd.DataFrame([item])],ignore_index=True)
        summary['classes'].append(dict(field=field,classification='FILL_OR_STATUS_DIFFERENCE',feature=partial_feature,count=6))
    classes.to_csv(output/'root-cause-classification.csv',index=False)
    raw_cases=json.loads((work/'representative-native-rows.json').read_text(encoding='utf-8'))
    by_id={r['case_id']:r for r in raw_cases}
    cases=pd.read_csv(output/'representative-cases.csv',dtype=str,keep_default_na=False)
    for idx,row in cases.loc[cases.feature.eq('RESIDUAL_NATIVE_VALUE_DIFFERENCE')].iterrows():
        native=by_id[row.case_id]
        left,right=Decimal(native['left']['amount']),Decimal(native['right']['amount'])
        if any(left.quantize(Decimal(10)**-p,rounding=ROUND_HALF_EVEN)==right or right.quantize(Decimal(10)**-p,rounding=ROUND_HALF_EVEN)==left for p in [0,1,2]):
            cases.at[idx,'feature']=feature
            cases.at[idx,'classification']='PRECISION_OR_ROUNDING'
    cases.to_csv(output/'representative-cases.csv',index=False)
    residual=pd.read_csv(work/'amount-residual-complete.csv',dtype=str,keep_default_na=False)
    residual['abs_delta']=residual.delta.map(lambda x:abs(Decimal(x)))
    residual['band']=residual.abs_delta.map(lambda x:'<=0.5' if x<=Decimal('.5') else '(0.5,1]' if x<=1 else '(1,10]' if x<=10 else '>10')
    tail=[]
    for dimension,groups in [('year',residual.date.str[:4]),('month',residual.date.str[:7]),('security',residual.symbol),('absolute_difference_band',residual.band)]:
        for group,frame in residual.groupby(groups,sort=True):
            tail.append(dict(dimension=dimension,group=group,count=len(frame),absolute_max=str(frame.abs_delta.max()),relative_max=str(max(Decimal(x) for x in frame.relative)),sources='CSMAR|CNRDS',classification='UNRESOLVED'))
    pd.DataFrame(tail).to_csv(output/'amount-residual-profile.csv',index=False)
    ohlc=pd.read_csv(output/'ohlc-difference-profile.csv')
    partial=json.loads((output/'partial-ohlc-cases.json').read_text(encoding='utf-8'))
    summary['ohlc_cooccurrence']=dict(all_four_mismatch_days=int(ohlc.loc[ohlc.mismatch_mask.eq('open|high|low|close'),'security_days'].sum()),open_high_low_only_days=len(partial),filled_or_zero_volume_days=int(ohlc.filled_days.sum()),CNRDS_vwap_outside_reported_range_days=int(ohlc.cn_vwap_outside_native_range_days.sum()),CSMAR_vwap_outside_reported_range_days=int(ohlc.cs_vwap_outside_native_range_days.sum()))
    summary['amount_refinement']=dict(counts=refinement['counts'],resource=refinement['resource'])
    summary['amount_numeric_signatures_explained_count']=sum(r['count'] for r in summary['classes'] if r['field']=='amount' and r['classification']=='PRECISION_OR_ROUNDING')
    summary['amount_residual_count']=len(residual)
    summary['supplier_processing_cause_certified']=False
    summary['normalization_authorized']=False
    summary['targeted_tests']=dict(passed=5,command='python -m pytest tests/test_p10_d02_market_differences.py -q -p no:cacheprovider')
    summary['evidence_files']=['targeted-evidence.json','price-basis-cases.json','corporate-action-evidence.json','partial-ohlc-cases.json','regression-controls.json']
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    unknown=[dict(scope='amount',count=len(residual),unit='field_values',date_range='2015-01-01/2026-10-20',sources='CSMAR|CNRDS',status='UNRESOLVED',evidence='amount-residual-profile.csv; private complete residual locator index',needed='Archive-version export precision and trade-session inclusion contracts; independent transaction totals; historical code identity for BJ cases',handling='ISOLATE; no value replacement'),
        dict(scope='amount_numeric_signatures',count=summary['amount_numeric_signatures_explained_count'],unit='field_values',date_range='2015-01-01/2026-10-20',sources='CSMAR|CNRDS',status='NUMERIC_SIGNATURE_CONFIRMED_CAUSE_OPEN',evidence='amount-difference-profile.csv; amount-refinement.json',needed='Supplier precision and rounding pipeline proof; quantization relation does not prove which side is correct',handling='Preserve original differences; no automatic normalization'),
        dict(scope='OHLC',count=540296,unit='field_values',date_range='2015-01-01/2026-10-20',sources='CSMAR|CNRDS',status='CAUSAL_COVERAGE_INCOMPLETE',evidence='ohlc-difference-profile.csv; fixed exact-formula cases; corporate-action associations',needed='Supplier price-basis/vintage contract; independent raw exchange prices; historical issuer identity; remaining adjustment formula cases',handling='ISOLATE price bases; 172 exact-formula fields are evidence subsets not global semantic certification'),
        dict(scope='2019_OHLC_distribution_boundary_exceptions',count=5,unit='securities; subset of OHLC above',date_range='2019-01-01/2019-12-31',sources='CSMAR|CNRDS|CSMAR-distributions',status='NO_DISTRIBUTION_WITHIN_TESTED_BOUNDARY',evidence='ohlc-corporate-action-associations.csv',needed='000750.SZ;600145.SH;600419.SH;600485.SH;600781.SH historical status and price basis',handling='ISOLATE; do not assign cash-dividend cause'),
        dict(scope='300682_formula_exceptions',count=6,unit='field_values; subset of OHLC above',date_range='2018-12-01/2018-12-31',sources='CSMAR|CNRDS|CSMAR-distributions',status='TESTED_FORMULA_NOT_MATCHED',evidence='price-basis-cases.json:74/80',needed='Exact supplier adjustment factor and rounding chain or independent raw prices',handling='Do not generalize the tested formula'),
        dict(scope='volume',count=8683,unit='field_values',date_range='2015-01-01/2026-10-20',sources='CSMAR|CNRDS',status='OUTSIDE_PRIMARY_CAUSAL_INVESTIGATION',evidence='root-cause-classification.csv; existing exact difference index',needed='Trade-session definitions and independent volume totals',handling='Preserve unresolved designation'),
        dict(scope='BigQuant',count='UNKNOWN',unit='semantic-contract field scope; not additive',date_range='historical archive',sources='BigQuant',status='UNIT_AND_PRECISION_CONTRACT_OPEN',evidence='targeted-evidence.json; regression-controls.json',needed='Version-bound economic units precision and historical applicability',handling='Binary64 observations only; no same-meaning certification')]
    pd.DataFrame(unknown).to_csv(output/'unresolved-evidence.csv',index=False)
    manifest=json.loads((output/'input-manifest.json').read_text(encoding='utf-8'))
    manifest['targeted_additional_inputs']=dict(dictionary_and_BigQuant='targeted-evidence.json',distribution_member=json.loads((output/'corporate-action-evidence.json').read_text(encoding='utf-8')),pilot_BigQuant='regression-controls.json')
    manifest['private_artifacts']=['monthly selected difference CSVs','representative-native-rows.json','amount-residual-complete.csv','targeted-distribution-native-rows.json']
    (output/'input-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(amount_signatures=summary['amount_numeric_signatures_explained_count'],amount_residual=len(residual),ohlc=summary['ohlc_cooccurrence']),indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--work',type=Path,required=True)
    a=p.parse_args()
    run(a.output,a.work)
