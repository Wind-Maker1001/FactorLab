"""Restricted JSON configuration for the synthetic research workflow."""
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import re

import pandas as pd

from examples.research.research_pipeline_synthetic import configured
from factorlab.backtest.fees import FeeSchedule
from factorlab.dataset import DatasetBuilder
from factorlab.dataset.identity import frame_hash
from factorlab.validation import ExpandingWalkForwardSplitter, WalkForwardRunner
from factorlab.validation.factor_selection import TrainFactorSelector, FactorSelectionConfig

MODELS = ('factor_score', 'ridge', 'lightgbm', 'xgboost')
DATA = dict(generator='make_synthetic_data-v1', seed=20261014, sessions=270,
            securities=24, version='seed-20261014-v1')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False)


def identity(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def fields(value, names, where):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError(f'{where}: required fields exactly {names}')


def number(value, lower, upper, where, *, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f'{where}: expected finite {"integer" if integer else "number"} in [{lower}, {upper}]')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate configuration field: {key}')
        result[key] = value
    return result


def load_config(path):
    document = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    validate(document)
    return document


def validate(c):
    fields(c, 'schema_version experiment_id source_kind data factors models research split label portfolio execution costs', 'config')
    if type(c['schema_version']) is not int or c['schema_version'] != 1:
        raise ValueError('schema_version must be 1')
    if c['source_kind'] != 'SYNTHETIC':
        raise ValueError('REAL_CANDIDATE: FORMAL_ADMISSION_BLOCKED; only SYNTHETIC is supported')
    if type(c['experiment_id']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,65}', c['experiment_id']):
        raise ValueError('invalid experiment_id (1..65 ASCII identifier characters)')
    fields(c['data'], 'generator seed sessions securities version', 'data')
    if canonical(c['data']) != canonical(DATA):
        raise ValueError('data identity differs from the supported fixed synthetic fixture')
    if type(c['factors']) is not list or not c['factors']:
        raise ValueError('explicit nonempty versioned factors required')
    for factor in c['factors']:
        fields(factor, 'name version', 'factor')
        if type(factor['name']) is not str or type(factor['version']) is not str:
            raise ValueError('factor name/version must be strings')
    if len({f['name'] for f in c['factors']}) != len(c['factors']):
        raise ValueError('duplicate factor')
    fields(c['models'], ' '.join(MODELS), 'models')
    for name, model in c['models'].items():
        parameters = {'factor_score': '', 'ridge': 'alpha', 'lightgbm': 'n_estimators num_leaves min_child_samples',
                      'xgboost': 'n_estimators max_depth'}[name]
        fields(model, 'version seed parameters', name)
        if model['version'] != '1':
            raise ValueError('model adapter version must be 1')
        number(model['seed'], 0, 2**31 - 1, 'model seed', integer=True)
        fields(model['parameters'], parameters, name + ' parameters')
        for key, value in model['parameters'].items():
            if key == 'alpha':
                number(value, 1e-12, 1e6, key)
            else:
                number(value, 2 if key == 'num_leaves' else 1,
                       64 if key == 'n_estimators' else 32 if key == 'max_depth' else 256, key, integer=True)
    fields(c['research'], 'start end risk_lookback rebalance_sessions', 'research')
    for key in ('start', 'end'):
        value = c['research'][key]
        if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('research dates require YYYY-MM-DD')
        pd.Timestamp(value)
    number(c['research']['risk_lookback'], 2, 65, 'risk_lookback', integer=True)
    number(c['research']['rebalance_sessions'], 1, 60, 'rebalance_sessions', integer=True)
    fields(c['split'], 'initial_train_months validation_months test_months step_months purge_days embargo_days', 'split')
    for key, value in c['split'].items():
        number(value, 0 if key in ('purge_days', 'embargo_days') else 1, 60, key, integer=True)
    if c['split']['step_months'] < c['split']['test_months']:
        raise ValueError('overlapping Test windows are not supported')
    fields(c['label'], 'holding_days entry_price', 'label')
    number(c['label']['holding_days'], 1, 10, 'holding_days', integer=True)
    if c['label']['entry_price'] != 'next_open':
        raise ValueError('close-known factors require next_open; future information is forbidden')
    if min(c['split']['purge_days'], c['split']['embargo_days']) < c['label']['holding_days']:
        raise ValueError('purge/embargo must cover holding_days')
    fields(c['portfolio'], 'objective investment max_weight shrinkage turnover_limit', 'portfolio')
    p = c['portfolio']
    if p['objective'] not in ('minimum_variance', 'equal', 'score'):
        raise ValueError('objective requires a supported allocation; scores are not expected returns')
    number(p['investment'], 1e-6, 1, 'investment')
    number(p['max_weight'], 1e-6, 1, 'max_weight')
    number(p['shrinkage'], 0, 1, 'shrinkage')
    if p['max_weight'] * 24 < p['investment']:
        raise ValueError('infeasible position budget')
    fields(c['execution'], 'mode initial_cash', 'execution')
    if c['execution']['mode'] not in ('batch', 'incremental_feedback'):
        raise ValueError('unknown execution mode')
    number(c['execution']['initial_cash'], 1, 1e12, 'initial_cash')
    if p['turnover_limit'] is not None:
        number(p['turnover_limit'], p['investment'], 2, 'turnover_limit')
        if c['execution']['mode'] != 'incremental_feedback':
            raise ValueError('turnover_limit requires actual incremental_feedback holdings')
    fields(c['costs'], 'commission_bps minimum_commission_cny slippage_bps', 'costs')
    for key, value in c['costs'].items():
        number(value, 0, 10000, key)


def configure(c, output):
    """Reuse the fixed provider and existing builders, selector and model runner."""
    validate(c)
    base, hub, provider = configured(output)
    snapshot = base.builder.snapshot
    refs = tuple(snapshot.get(f['name'], f['version']).ref for f in c['factors'])
    builder = DatasetBuilder(replace(base.builder.options, **c['label']), snapshot=snapshot,
        factor_refs=refs, data_context=base.builder.data_context,
        data_version=base.builder.data_version, calendar_id=base.builder.calendar_id)
    selector = TrainFactorSelector(snapshot, refs, FactorSelectionConfig(k=len(refs),
        minimum_cross_section=5, minimum_dates=10, minimum_abs_rank_ic=0., minimum_direction_consistency=0.))
    models = {}
    for name in MODELS:
        spec = c['models'][name]
        settings = dict(random_seed=spec['seed'], factor_selector=selector)
        if name == 'factor_score':
            settings['dynamic_factor_score_weighting'] = 'equal'
        elif name == 'ridge':
            settings['ridge_alphas'] = (spec['parameters']['alpha'],)
        else:
            settings[name + '_candidates'] = [spec['parameters']]
        models[name] = settings
    splitter = ExpandingWalkForwardSplitter(**c['split'])
    for name, settings in models.items():
        WalkForwardRunner(splitter, model_name=name, feature_names=builder.factor_names,
                          minimum_cross_section=5, **settings)
    dates = provider.tables['calendar']
    start, end = map(pd.Timestamp, (c['research']['start'], c['research']['end']))
    if start not in dates or end not in dates or start < dates[65] or start >= end or end > dates[-c['label']['holding_days'] - 2]:
        raise ValueError('invalid research interval: preserve warmup and future label/execution calendar')
    fees = FeeSchedule(replace(rule, **c['costs']) for rule in base.fees.rules)
    config = replace(base, experiment_id=c['experiment_id'], output=Path(output), builder=builder,
        models=models, splitter=splitter, optimizer=replace(base.optimizer, **c['portfolio']),
        execution=replace(base.execution, initial_cash=c['execution']['initial_cash']),
        execution_mode=c['execution']['mode'], fees=fees,
        research_start=c['research']['start'], research_end=c['research']['end'],
        risk_lookback=c['research']['risk_lookback'], rebalance_sessions=c['research']['rebalance_sessions'])
    data_identity = {name: frame_hash(frame) if isinstance(frame, pd.DataFrame)
                     else identity(list(frame.strftime('%Y-%m-%d')))
                     for name, frame in provider.tables.items()}
    return config, hub, data_identity
