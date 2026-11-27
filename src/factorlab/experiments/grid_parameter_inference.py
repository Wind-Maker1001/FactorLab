"""Stage-level grid selection and independent-seed validation inference."""
import argparse
import json
from pathlib import Path

import numpy as np
from scipy.stats import t


MODELS = ('gbm', 'moving_block_bootstrap', 'regime_historical', 'jump_down')
PLANNED_SEEDS = 5


def _cells(root):
    cells = []
    for path in sorted(Path(root).glob('*/status.json')):
        cell = json.loads(path.read_text(encoding='utf-8'))
        if cell['status'] == 'COMPLETE':
            cells.append(cell)
    if not cells:
        raise ValueError('no COMPLETE stage cells')
    if len({cell['stage'] for cell in cells}) != 1:
        raise ValueError('root must contain exactly one stage')
    identities = [(cell['model'], cell['replicate']) for cell in cells]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicate model/replicate cell')
    return cells


def _average(values):
    if isinstance(values[0], dict):
        return {key: _average([value[key] for value in values]) for key in values[0]}
    if any(value is None for value in values):
        return None
    return float(np.mean(values))


def _summaries(cells):
    ids = set(cells[0]['summaries'])
    if any(set(cell['summaries']) != ids for cell in cells):
        raise ValueError('complete cells must contain the same configurations')
    models = sorted({cell['model'] for cell in cells})
    return {key: _average([_average([cell['summaries'][key] for cell in cells
                                   if cell['model'] == model]) for model in models])
            for key in sorted(ids)}


def summarize_stage(root):
    """Equal model weights and, within each model, equal complete-seed weights.

    Quantiles and tail summaries are averages of cell statistics, not pooled-path
    quantiles. Only status.json is read; path arrays are unnecessary.
    """
    return _summaries(_cells(root))


def _candidate(key):
    return key not in {'baseline', 'arithmetic-baseline', 'cash'} and not key.endswith(('-hold', '-endowed'))


def _objectives(summaries, ids):
    return np.asarray([[-summaries[key]['net_return']['mean'], summaries[key]['cvar95'],
                        summaries[key]['cvar99'],
                        summaries[key]['maximum_drawdown']['quantiles']['0.99'],
                        summaries[key]['loss_probability'],
                        summaries[key]['maximum_committed']['mean'],
                        -summaries[key]['fills']['mean']] for key in ids])


def _front(vectors):
    return [i for i, row in enumerate(vectors)
            if not np.any(np.all(vectors <= row, axis=1) & np.any(vectors < row, axis=1))]


def _representatives(summaries, count, *, first_front=False):
    if count < 0:
        raise ValueError('count must be nonnegative')
    ids = sorted(summaries)
    if not ids or count == 0:
        return []
    vectors = _objectives(summaries, ids)
    span = np.ptp(vectors, axis=0)
    scaled = np.divide(vectors - vectors.min(axis=0), span,
                       out=np.zeros_like(vectors), where=span > 0)
    chosen, remaining = [], list(range(len(ids)))
    while remaining and len(chosen) < count:
        layer = [remaining[i] for i in _front(vectors[remaining])]
        if len(chosen) + len(layer) <= count:
            chosen.extend(layer)
        else:
            anchors = dict.fromkeys(layer[int(np.argmin(vectors[layer, j]))]
                                    for j in range(vectors.shape[1]))
            for index in anchors:
                if len(chosen) < count:
                    chosen.append(index)
            pool = [i for i in layer if i not in chosen]
            while pool and len(chosen) < count:
                distances = [np.min(np.sum((scaled[chosen] - scaled[i]) ** 2, axis=1))
                             for i in pool]
                chosen.append(pool.pop(int(np.argmax(distances))))
        if first_front:
            break
        remaining = [i for i in remaining if i not in layer]
    return [ids[i] for i in chosen]


def select_refinement(root, count=20):
    """Select Pareto layers with normalized farthest-point objective coverage."""
    return _representatives({key: value for key, value in summarize_stage(root).items()
                             if _candidate(key)}, count)


def _interval(values, confidence, comparisons=1):
    values = np.asarray(values, dtype=float)
    standard_error = float(values.std(ddof=1) / np.sqrt(len(values)))
    critical = float(t.ppf(1 - (1 - confidence) / (2 * comparisons), len(values) - 1))
    mean = float(values.mean())
    return {'mean': mean, 'lower': mean - critical * standard_error,
            'upper': mean + critical * standard_error, 'seed_count': len(values),
            'degrees_of_freedom': len(values) - 1, 'seed_standard_error': standard_error,
            'critical_value': critical, 'comparisons': comparisons}


def _paired(cell, key, control, metric):
    if control == 'baseline':
        return cell['paired_vs_baseline'][key][metric]['mean_difference']
    paired = cell.get('paired_vs_controls', {}).get(key, {}).get(control, {})
    if metric in paired:
        return paired[metric]['mean_difference']
    other = 'cash' if control == 'cash' else key + '-' + control
    return cell['summaries'][key][metric]['mean'] - cell['summaries'][other][metric]['mean']


def assess_validation(root, selected_ids, searched=500, confidence=.95):
    """Two-sided seed-t intervals; cash claims use searched x four Bonferroni tests."""
    if searched < 1 or not 0 < confidence < 1:
        raise ValueError('searched must be positive and confidence in (0, 1)')
    selected_ids = list(dict.fromkeys(selected_ids))
    if any(not _candidate(key) for key in selected_ids):
        raise ValueError('selected_ids must exclude controls and baselines')
    if len(selected_ids) > searched:
        raise ValueError('searched must include every selected configuration')
    cells = _cells(root)
    expected = {(model, replicate) for model in MODELS for replicate in range(PLANNED_SEEDS)}
    if {(cell['model'], cell['replicate']) for cell in cells} != expected:
        raise ValueError('validation requires all four models and exactly five seeds (replicates 0..4)')
    for model in MODELS:
        seeds = [cell['seed'] for cell in cells if cell['model'] == model]
        if len(set(seeds)) != PLANNED_SEEDS:
            raise ValueError('validation requires five distinct independent seeds per model')
    summaries = _summaries(cells)
    configurations, passed = {}, {}
    for key in selected_ids:
        model_results = {}
        for model in MODELS:
            group = sorted((cell for cell in cells if cell['model'] == model),
                           key=lambda cell: cell['replicate'])
            metrics = group[0]['metrics']
            intervals = {metric: _interval([cell['summaries'][key][metric]['mean']
                                           for cell in group], confidence) for metric in metrics}
            paired = {control: {metric: _interval([_paired(cell, key, control, metric)
                                                   for cell in group], confidence,
                                                  searched * len(MODELS)
                                                  if control == 'cash' and metric == 'net_return' else 1)
                                for metric in metrics}
                      for control in ('cash', 'baseline', 'hold', 'endowed')}
            active = all(cell['summaries'][key]['fills']['mean'] > 0 for cell in group)
            risk_intervals = {metric: _interval([cell['summaries'][key][metric]
                                                 for cell in group], confidence)
                              for metric in ('cvar95', 'cvar99', 'loss_probability', 'fill_probability')}
            model_results[model] = {'metrics': intervals, 'risk_metrics': risk_intervals,
                                    'paired_vs_controls': paired,
                                    'active': active, 'shortlist_gate': model != 'jump_down',
                                    'passes': paired['cash']['net_return']['lower'] > 0 and active}
        passes = all(model_results[model]['passes'] for model in MODELS if model != 'jump_down')
        configurations[key] = {'models': model_results, 'passes': passes}
        if passes:
            passed[key] = summaries[key]
    return {'configurations': configurations,
            'final_ids': _representatives(passed, 10, first_front=True),
            'confidence': confidence, 'searched': searched, 'planned_seed_count': PLANNED_SEEDS,
            'expected_degrees_of_freedom': 4,
            'cash_family_comparisons': searched * len(MODELS),
            'interval_method': 'two-sided Student t on independent seed means; Bonferroni for cash net-return claims',
            'candidate_rule': 'research-supported profitability: corrected cash-difference return lower bound > 0 and positive fills in every seed in each of the three nonstress models',
            'shortlist_meaning': 'evidence-supported profitability for research, not deployment approval',
            'stress_role': 'jump_down is an adverse scenario, not a calibrated probability forecast; its profitability and activity do not gate the shortlist',
            'interpretation': [
                'Seed means are assumed independent and approximately normal; five seeds do not establish universal robustness.',
                'Path standard errors are not independent-seed uncertainty and are not used.',
                'Search selection bias is addressed for cash return claims over the searched configurations and four models; other intervals are descriptive, unadjusted and selection-sensitive.',
                'Baseline, hold and endowed contrasts describe strategy packages; differing initial exposure can confound strategy attribution, and these intervals do not remove that uncertainty.',
                'Final choices retain non-dominated economic objectives and diverse representation; no unique optimum is claimed.',
                'Failure to enter the research shortlist is not a universal claim of unprofitability; no stress profitability or drawdown acceptance threshold is imposed.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('summarize', 'select', 'validate'))
    parser.add_argument('root', type=Path, help='single-stage root containing model/replicate cell directories')
    parser.add_argument('--selected-ids', nargs='+', default=[])
    parser.add_argument('--count', type=int, default=20)
    parser.add_argument('--searched', type=int, default=500)
    parser.add_argument('--confidence', type=float, default=.95)
    args = parser.parse_args(argv)
    if args.action == 'summarize':
        result = summarize_stage(args.root)
    elif args.action == 'select':
        result = select_refinement(args.root, args.count)
    else:
        result = assess_validation(args.root, args.selected_ids, args.searched, args.confidence)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
