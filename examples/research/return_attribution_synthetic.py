"""P3-R14: reuse published predictions, execute once, attribute observed accounts."""
import argparse
from dataclasses import asdict, replace
from importlib.metadata import version
from io import BytesIO
import json
from pathlib import Path
import platform
import subprocess
from types import SimpleNamespace

import numpy as np
import pandas as pd

from examples.research.alpha_robustness_synthetic import OPTIONS, save, fold_receipts
from examples.research.signal_propagation_synthetic import fixture
from factorlab.application.alpha_robustness import calendar_paired_inference
from factorlab.application.signal_propagation import prediction_targets, pairwise_transmission
from factorlab.backtest import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeSchedule
from factorlab.dataset.identity import file_hash, frame_hash
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation, paired_inference, reprice_costs
from factorlab.evaluation.return_attribution import attribute_execution, paired_daily_difference
from factorlab.experiments import ExperimentTracker
from factorlab.experiments.provenance import capture_provenance

ROOT = Path(__file__).resolve().parents[2]
HISTORY = '99dafc24d6290164e33fadfef67559404173e660'
MODELS = ('factor_score', 'ridge', 'lightgbm', 'xgboost')
PROTOCOL = dict(boundary='SYNTHETIC / SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE',
    financial_admission='FORMAL_ADMISSION_BLOCKED', historical_predictions=HISTORY,
    objectives=['equal', 'minimum_variance', 'score'], investment=.8, max_weight=.15,
    random_seeds=[17,29,43], lag_sessions=5, initial_cash=100000,
    costs=['base','slippage20','commission15'], inference=asdict(OPTIONS),
    inference_families='P309 four score portfolios minus equal; separate four-model daily RankIC minus factor_score',
    negative_controls='P312 published complete 13-contrast family retained, not pooled with P309',
    factor_supplement='FactorScore only: baseline14/fixed64/Train-only selected10; fixed parameters; descriptive',
    supplemental_risk='latest 40 close-known complete cross-universe return observations; no price fill',
    counterfactual='stress fees explicitly re-executed; fixed-fill repricing separately labeled')


def historical(name):
    return subprocess.check_output(['git','show',f'{HISTORY}:docs/validation/p3-09/evidence/{name}'], cwd=ROOT)


def historical_csv(name):
    frame = pd.read_csv(BytesIO(historical(name)))
    for col in ('date','signal_date','execution_date','exit_date'):
        if col in frame:
            frame[col] = pd.to_datetime(frame[col])
    return frame


def decision_turnover(target, result):
    rows = []
    for date, group in target.groupby('date'):
        prior = result.positions.loc[result.positions.date.eq(date)].set_index('symbol').weight
        weights = group.set_index('symbol').target_weight
        a,b = weights.align(prior, fill_value=0)
        rows.append(dict(signal_date=date, target_gross_turnover=float((a-b).abs().sum())))
    return pd.DataFrame(rows)


def factor_supplement(output):
    from examples.research.factor_candidates_demo import experiment, prepare, splitter
    from examples.research.dataset_registry_v2_demo import synthetic_inputs
    from factorlab.validation import WalkForwardRunner
    from factorlab.validation.ablation import _OneFold
    builder, baseline, selector = experiment()
    dates, bars, benchmark, membership, status, listings = synthetic_inputs(sessions=300, symbols=24)
    data = builder.build(bars, benchmark, membership, status, listings, dates)
    calendar = membership.date.drop_duplicates()
    prepared, folds, selections, plans, *_ = prepare(data, calendar, selector)
    save(output / 'factor-plans.json', plans)
    predictions, receipt_walk = {}, None
    for group, fixed in dict(baseline14=baseline, fixed64=selector.feature_names, selected10=None).items():
        frames, summaries = [], []
        for fold in folds:
            selected = selections[fold.fold]
            names = selected.feature_names if fixed is None else fixed
            local = selected.transform(prepared) if fixed is None else prepared.copy()
            local.attrs.clear()
            walk = WalkForwardRunner(_OneFold(splitter(), fold), model_name='factor_score',
                feature_names=names, minimum_cross_section=12, top_k=5, random_seed=42,
                factor_weights=dict.fromkeys(names,1/len(names)), normalize_by_absolute_weight=False).run(local, calendar=calendar)
            frames.append(walk.predictions)
            summaries.append(walk.fold_summary)
        predictions[group] = pd.concat(frames, ignore_index=True).sort_values(['date','symbol']).reset_index(drop=True)
        receipt_walk = SimpleNamespace(fold_summary=pd.concat(summaries, ignore_index=True))
        predictions[group].to_csv(output/f'factor-{group}-predictions.csv',index=False)
        receipt_walk.fold_summary.to_csv(output/f'factor-{group}-folds.csv',index=False)
    identity = predictions['baseline14'][['date','symbol','fold','EXCESS_RET5']]
    for p in predictions.values():
        pd.testing.assert_frame_equal(identity, p[identity.columns])
    risk = bars.pivot(index='date', columns='symbol', values='raw_close').pct_change(fill_method=None).dropna(how='any')
    context = dict(dataset_id=frame_hash(data), universe_id=frame_hash(membership), label='EXCESS_RET5',
        label_id='EXCESS_RET5:next_open:5', benchmark_id=frame_hash(benchmark), holding_days=5)
    pins = {}
    for group in predictions:
        names = baseline if group=='baseline14' else selector.feature_names if group=='fixed64' else sorted({n for p in plans for n in p['selected_features']})
        pins[group] = [asdict(selector.snapshot.get(n).ref) for n in names]
    return predictions, receipt_walk, dates, bars, benchmark, status, risk, context, pins


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    save(output/'protocol.json', PROTOCOL)
    from examples.research.return_attribution_deterministic import run_cases
    save(output/'deterministic.json', run_cases())
    source_files = [ROOT/'docs/validation'/part for part in (
        'p3-09/evidence/summary.json','p3-11/results/daily.csv','p3-11/results/manifest.json',
        'p3-12/results.json','p3-12/receipt.json','p3-13/receipt.json')]
    inputs = {str(p.relative_to(ROOT)):file_hash(p) for p in source_files}
    inputs.update({str(p.relative_to(ROOT)):file_hash(p) for p in (
        Path(__file__), ROOT/'examples/research/return_attribution_deterministic.py',
        ROOT/'src/factorlab/evaluation/return_attribution.py', ROOT/'tests/research/test_return_attribution.py')})
    provenance = capture_provenance(ROOT)
    bars, benchmark, dates, membership, status, listings = fixture()
    predictions = {m:historical_csv(f'score/{m}/predictions.csv').sort_values(['date','symbol']).reset_index(drop=True) for m in MODELS}
    identity = predictions['factor_score'][['date','symbol','fold','EXCESS_RET5']]
    for p in predictions.values():
        pd.testing.assert_frame_equal(identity, p[identity.columns])
    historical_hashes = {m:frame_hash(p) for m,p in predictions.items()}
    folds = historical_csv('score/factor_score/folds.csv')
    receipts = fold_receipts(SimpleNamespace(fold_summary=folds))
    receipts['train_start'] = pd.to_datetime(folds.train_window_start)
    from factorlab.factors.registry_v2 import FactorRegistryV2
    snapshot=FactorRegistryV2.with_builtins().freeze()
    pins={m:[asdict(snapshot.get(n,'1').ref) for n in ('MOM20','REV1')] for m in MODELS}
    risk = bars.pivot(index='date', columns='symbol', values='raw_close').pct_change(fill_method=None).iloc[1:]
    context = dict(dataset_id='P309:'+HISTORY, universe_id=frame_hash(membership), label='EXCESS_RET5',
        label_id='EXCESS_RET5:next_open:5', benchmark_id=frame_hash(benchmark), holding_days=5)
    config = ExecutionBacktestConfig(initial_cash=100000, minimum_cross_section=5)
    summaries, prediction_metrics, contrasts, transmissions, cost_sensitivity = [], [], [], {}, []
    all_daily, all_execution, all_paired = [], [], []
    tracker = ExperimentTracker(output/'experiments')

    def execute_family(family, arms, receipts, dates, bars, benchmark, status, risk, context, objectives, pins):
        first,last = arms[next(iter(arms))].date.agg(['min','max'])
        execution_dates = dates[(dates>=first)&(dates<=dates[dates.get_loc(last)+1])]
        pred_calendar = tuple(sorted(arms[next(iter(arms))].date.dt.strftime('%Y-%m-%d').unique()))
        pe = ResearchEvaluation(EvaluationContext(calendar=pred_calendar, **context))
        evaluator = ResearchEvaluation(EvaluationContext(calendar=tuple(execution_dates[1:].strftime('%Y-%m-%d')), **context))
        evaluated_predictions=[]
        for model,p in arms.items():
            e = pe.predictions(model,p,folds=receipts,minimum_cross_section=5)
            evaluated_predictions.append(e)
            prediction_metrics.append(dict(family=family,model=model,**{k:v.value for k,v in e.metrics.items()},
                mean_coverage=float(e.tables['daily'].coverage.mean()), prediction_identity=frame_hash(p)))
        targets, books, evaluations, attrs = {}, {}, {}, {}
        execution_bars=bars.loc[bars.date.isin(execution_dates)].merge(status,on=['date','symbol'],validate='one_to_one')
        for objective in objectives:
            targets[objective], books[objective], evaluations[objective], attrs[objective] = {}, {}, [], {}
            for model,p in arms.items():
                target, decisions, ranks = prediction_targets(p,risk,objective=objective)
                result=AShareExecutionBacktester(config).run_target_weights(target,execution_bars,benchmark.loc[benchmark.date.isin(execution_dates)])
                a=attribute_execution(result,initial_cash=100000,calendar=execution_dates[1:])
                key=f'{family}-{objective}-{model}'
                targets[objective][model],books[objective][model],attrs[objective][model]=target,result,a
                evaluations[objective].append(evaluator.portfolio(model,result.nav,trades=result.trades,positions=result.positions))
                daily=a.daily.assign(family=family,objective=objective,model=model)
                execution=a.execution.merge(decision_turnover(target,result),on='signal_date',validate='one_to_one')
                execution['decision_time']=execution.signal_date+pd.Timedelta(hours=15)
                execution['execution_time']=execution.execution_date+pd.Timedelta(hours=9,minutes=30)
                execution=execution.assign(family=family,objective=objective,model=model)
                all_daily.append(daily); all_execution.append(execution)
                summaries.append({**a.summary, 'family':family,'objective':objective,'model':model,
                    'target_turnover_status':'OK: same-date decision close marked actual holdings',
                    'target_gross_turnover':float(execution.target_gross_turnover.sum()),
                    'target_identity':frame_hash(target), 'prediction_identity':frame_hash(p)})
                record=output/(key+'.json')
                save(record,dict(summary=summaries[-1],risk_receipts=decisions.to_dict('records')))
                tracker.create_experiment(key, dict(model=model,parameters=dict(protocol=PROTOCOL, family=family,objective=objective),
                    factors=pins[model],
                    dataset=dict(identity=context['dataset_id']),label=dict(definition=context['label_id']),
                    windows={part:[str(pd.to_datetime(receipts[start]).min()),str(pd.to_datetime(receipts[end]).max())]
                        for part,start,end in [('train','train_start','train_end'),('validation','validation_start','validation_end'),('test','test_start','test_end')]},
                    walk_forward=dict(source='published fold receipts' if family=='p309' else 'P311 frozen Train selection',
                        folds=frame_hash(receipts)),costs=asdict(config)))
                tracker.append_run(key,key,**provenance,command=['python','-m','examples.research.return_attribution_synthetic','--output',str(output)],
                    status='SUCCEEDED',qualification='SYNTHETIC',artifacts=dict(summary=dict(path=str(record.resolve()),sha256=file_hash(record))),
                    prediction_metrics={},portfolio_metrics=dict(net_return=a.summary['net_return']),validation=dict(boundary=PROTOCOL['boundary']))
                if family=='p309' and objective=='score':
                    for scenario,schedule in dict(slippage20=FeeSchedule.a_share_default(slippage_bps=20),
                            commission15=FeeSchedule.a_share_default(commission_bps=15)).items():
                        stressed=AShareExecutionBacktester(config,schedule).run_target_weights(target,execution_bars,benchmark.loc[benchmark.date.isin(execution_dates)])
                        stress_a=attribute_execution(stressed,initial_cash=100000,calendar=execution_dates[1:])
                        repriced=reprice_costs(result.trades,{scenario:schedule})
                        cost_sensitivity.append(dict(model=model,scenario=scenario,net_return=stress_a.summary['net_return'],
                            costs=stress_a.summary['costs'],fixed_fill_cost_delta=float(repriced.cost_delta.sum()),
                            base_fills_identity=frame_hash(result.trades),stress_fills_identity=frame_hash(stressed.trades)))
            events,rates=pairwise_transmission(targets[objective],books[objective],predictions=arms)
            events=events.rename(columns={'orders_changed':'rounded_desired_positions_changed'})
            events.to_csv(output/f'{family}-{objective}-transmission.csv',index=False)
            transmissions[f'{family}-{objective}']={k.replace('orders_changed','rounded_desired_positions_changed'):v for k,v in rates.items()}
        if family=='p309':
            base=attrs['equal']['factor_score'].daily
            equal_eval=replace(evaluations['equal'][0],name='equal')
            for model,a in attrs['score'].items():
                all_paired.append(paired_daily_difference(a.daily,base).assign(family=family,model=model,baseline='equal'))
            contrasts.extend(paired_inference([equal_eval,*evaluations['score']],baseline='equal',metric='strategy_return',options=OPTIONS).assign(family='p309-return').to_dict('records'))
            contrasts.extend(calendar_paired_inference(evaluated_predictions,calendar=execution_dates[:-1],baseline='factor_score',metric='prediction_rank_ic',options=OPTIONS).assign(family='p309-rankic').to_dict('records'))
        else:
            base=attrs['score']['baseline14'].daily
            for model,a in attrs['score'].items():
                all_paired.append(paired_daily_difference(a.daily,base).assign(family=family,model=model,baseline='baseline14'))
        return books

    # Equal baseline needs a distinct name in the paired inference family.
    books=execute_family('p309',predictions,receipts,dates,bars,benchmark,status,risk,context,PROTOCOL['objectives'],pins)
    original=json.loads((ROOT/'docs/validation/p3-09/evidence/summary.json').read_text())
    for objective in PROTOCOL['objectives']:
        for model in MODELS:
            np.testing.assert_allclose(books[objective][model].nav.iloc[-1].strategy_nav,
                original['results'][objective][model]['final_nav'],rtol=0,atol=1e-8)
    supplemental=factor_supplement(output)
    arms,walk,fd,fb,fbench,fs,fr,fc,fp=supplemental
    factor_receipts=fold_receipts(walk)
    factor_receipts['train_start']=pd.to_datetime(walk.fold_summary.train_window_start)
    execute_family('p311-supplement',arms,factor_receipts,fd,fb,fbench,fs,fr,fc,['score'],fp)
    controls=json.loads((ROOT/'docs/validation/p3-12/results.json').read_text())
    pd.DataFrame([dict(arm=k,**v) for k,v in controls['portfolios'].items()]).to_csv(output/'negative-control-results.csv',index=False)
    pd.DataFrame(controls['portfolio_inference']).to_csv(output/'negative-control-return-inference.csv',index=False)
    pd.DataFrame(controls['prediction_inference']).to_csv(output/'negative-control-prediction-inference.csv',index=False)
    pd.DataFrame(summaries).to_csv(output/'portfolio-summary.csv',index=False)
    pd.DataFrame(prediction_metrics).to_csv(output/'prediction-comparison.csv',index=False)
    pd.concat(all_daily).to_csv(output/'daily-attribution.csv',index=False)
    pd.concat(all_execution).to_csv(output/'execution-deviation.csv',index=False)
    pd.concat(all_paired).to_csv(output/'paired-returns.csv',index=False)
    pd.DataFrame(contrasts).to_csv(output/'inference.csv',index=False)
    pd.DataFrame(cost_sensitivity).to_csv(output/'cost-sensitivity.csv',index=False)
    save(output/'transmission-summary.json',transmissions)
    save(output/'receipt.json',dict(protocol=PROTOCOL,inputs=inputs,historical_prediction_hashes=historical_hashes,
        environment=dict(python=platform.python_version(),packages={k:version(k) for k in ('numpy','pandas','scipy','scikit-learn')}),
        provenance=provenance,rows=len(summaries),
        negative_control_path_status='NOT_COMPUTABLE: P312 retains summary/inference, not fills; no invented shuffled path',
        artifacts={p.name:file_hash(p) for p in output.iterdir() if p.suffix in ('.csv','.json') and p.name!='receipt.json'}))
    print(json.dumps(dict(portfolios=len(summaries),boundary=PROTOCOL['boundary'])))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=PROTOCOL['boundary'])
    parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args().output)
