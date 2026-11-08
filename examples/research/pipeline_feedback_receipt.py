import copy
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd
from factorlab.dataset.identity import json_hash, file_hash
from factorlab.experiments import ExperimentTracker

root, destination = map(Path, sys.argv[1:])
tracker = ExperimentTracker(root/'experiments')
records = tracker.query(status='SUCCEEDED')
assert len(records) == 4
receipt = dict(summary=json.loads((root/'summary.json').read_text()), experiments=[], assertions={})
models, hashes = set(), set()
for record in records:
    experiment = tracker.get_experiment(record['experiment_id'])
    payload = experiment['payload']
    assert payload['parameters']['execution_mode'] == 'incremental_feedback'
    assert payload['parameters']['portfolio']['turnover_limit'] == .8
    alternate = copy.deepcopy(payload)
    alternate['parameters']['execution_mode'] = 'batch'
    assert json_hash(payload) != json_hash(alternate)
    assert payload['parameters']['execution_sources']
    assert len(payload['factors']) == 2
    models.add(payload['model'])
    hashes.add(experiment['sha256'])
    model = payload['model']
    decisions = pd.read_csv(root/model/'decisions.csv', parse_dates=['date', 'decision_time', 'execution_time'])
    positions = pd.read_csv(root/model/'positions.csv', parse_dates=['date'])
    nav = pd.read_csv(root/model/'nav.csv', parse_dates=['date']).set_index('date')
    signals = pd.read_csv(root/model/'targets.csv')
    deviations = pd.read_csv(root/model/'target_comparison.csv')
    actual_diff = False
    for index, row in decisions.iterrows():
        weights = json.loads(row.actual_weights)
        if index == 0:
            assert not weights and row.cash == 100000
        else:
            holdings = positions[positions.date == row.date]
            expected = dict(zip(holdings.symbol, holdings.market_value/nav.loc[row.date].portfolio_value))
            assert set(weights) == set(expected)
            for symbol in weights:
                np.testing.assert_allclose(weights[symbol], expected[symbol], rtol=1e-12)
        actual_diff |= weights != json.loads(row.previous_targets)
    assert actual_diff
    dates = pd.DatetimeIndex(nav.index)
    assert len(dates) == len(pd.bdate_range(dates.min(), dates.max()))
    assert len(dates) > len(decisions)
    assert deviations.weight_deviation.abs().gt(1e-8).any()
    receipt['experiments'].append(dict(model=model, experiment_id=experiment['id'],
        definition_hash=experiment['sha256'], execution_sources=payload['parameters']['execution_sources'],
        folds=receipt['summary']['models'][model]['folds'], decisions=len(decisions),
        nav_rows=len(nav), max_estimated_turnover=float(decisions.estimated_gross_security_turnover.max()),
        decision_sha256=file_hash(root/model/'decisions.csv'), nav_sha256=file_hash(root/model/'nav.csv')))
assert models == {'factor_score', 'ridge', 'lightgbm', 'xgboost'} and len(hashes) == 4
receipt['summary'].pop('reproduction')
receipt['assertions'] = dict(four_models=True, unique_identities=True, mode_changes_definition_hash=True,
    actual_weights_reconciled_every_decision=True, gap_sessions_preserved=True,
    target_deviations_observed=True, turnover_limit=.8)
destination.write_text(json.dumps(receipt, indent=2)+'\n', encoding='utf-8')
print(json.dumps(receipt['assertions']))
