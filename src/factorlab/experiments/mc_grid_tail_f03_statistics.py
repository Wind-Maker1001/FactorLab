"""Preregistered, nested path analysis for the MC-05-F03 offline Paper study."""

from collections import defaultdict
from itertools import pairwise

import numpy as np

from factorlab.experiments.mc_grid_tail_convergence import GROUPS, grouped_statistics


CONDITION_FIELDS = {
    'traded': 'traded',
    'ever_inventory': 'ever_inventory',
    'terminal_inventory': 'has_terminal_inventory',
}
LEVELS = (95, 99)
RATE_NAMES = ('loss', 'traded', 'zero_fills', 'ever_inventory', 'has_terminal_inventory')
PROTOCOL = {
    'interval_coverage': .95,
    'minimum_expected_tail_paths': 20,
    'minimum_kish_tail_paths': 20,
    'maximum_tail_interval_width': .02,
    'maximum_adjacent_tail_change': .005,
    'maximum_final_seed_tail_range': .005,
    'maximum_final_rate_interval_width': .20,
    'requires_all_prespecified_tiers_and_seeds': True,
    'requires_all_conditions_and_both_tail_levels': True,
    'precision_intervals': {
        'var': 'MC-02 binomial order-statistic interval',
        'cvar': 'lower VaR confidence bound and unbounded upper endpoint from observed losses',
        'cvar_empirical': 'MC-02 observed-support bootstrap, descriptive only',
        'rates': 'MC-02 Wilson score interval for path-level indicators',
    },
}


def _group_sample(rows, group):
    if group == 'unconditional':
        return rows
    return [row for row in rows if row[CONDITION_FIELDS[group]]]


def _distribution(values):
    values = np.asarray(values, dtype=float)
    probabilities = (0., .05, .25, .5, .75, .95, .99, 1.)
    quantiles = np.quantile(values, probabilities)
    return dict(mean=float(values.mean()),
                quantiles={str(p): float(q) for p, q in zip(probabilities, quantiles)})


def _interval_width(interval):
    lower, upper = interval['lower'], interval['upper']
    if lower is None or upper is None:
        return None
    return upper - lower


def _interval_is_precise(interval, limit):
    width = _interval_width(interval)
    return width is not None and width <= limit


def _fill_tail_support(tail, losses, level):
    n = len(losses)
    tail_hundredths = (100 - level) * n
    whole, hundredths = divmod(tail_hundredths, 100)
    remaining = hundredths / 100
    mass = tail_hundredths / 100
    contributors = whole + bool(hundredths)
    kish_paths = mass ** 2 / (whole + remaining ** 2)
    largest_weight = min(1., 1. / mass)
    var_interval = tail['var_interval']
    cvar_bound = dict(lower=var_interval['lower'], upper=None,
                      lower_unbounded=var_interval['lower_unbounded'], upper_unbounded=True,
                      coverage_at_least=PROTOCOL['interval_coverage'],
                      method='var_lower_confidence_bound_and_no_data_only_upper_bound')
    failures = []
    if (mass < PROTOCOL['minimum_expected_tail_paths']
            or kish_paths < PROTOCOL['minimum_kish_tail_paths']):
        failures.append('INSUFFICIENT_TAIL_SUPPORT')
    if not _interval_is_precise(var_interval, PROTOCOL['maximum_tail_interval_width']):
        failures.append('VAR_PRECISION_NOT_ESTABLISHED')
    if not _interval_is_precise(cvar_bound, PROTOCOL['maximum_tail_interval_width']):
        failures.append('CVAR_PRECISION_NOT_ESTABLISHED')
    tail.update(expected_tail_paths=mass, tail_paths_with_nonzero_weight=contributors,
                largest_path_tail_weight=largest_weight, kish_tail_paths=kish_paths,
                distinct_weighted_loss_values=len(set(losses[:contributors])),
                no_upper_tail_draw_probability=(level / 100) ** n,
                cvar_conservative_interval=cvar_bound, f03_failures=failures,
                f03_status=failures[0] if failures else 'TAIL_PRECISION_CRITERIA_MET')


def _summarize(rows, *, resamples, bootstrap_seed):
    groups = grouped_statistics(rows, resamples=resamples, bootstrap_seed=bootstrap_seed)
    for name, group in groups.items():
        sample = _group_sample(rows, name)
        if not sample:
            group['f03_status'] = 'INSUFFICIENT_TAIL_SUPPORT'
            continue
        nav = np.array([row['terminal_nav'] for row in sample])
        initial = np.array([row['initial_capital'] for row in sample])
        losses = 1. - nav / initial
        group['f03_distributions'] = {
            'terminal_nav': _distribution(nav),
            'terminal_return': _distribution(-losses),
            'terminal_loss': _distribution(losses),
            **{key: _distribution([row[key] for row in sample]) for key in (
                'maximum_drawdown', 'capital_utilization_mean', 'capital_utilization_peak',
                'residual_inventory_loss', 'terminal_mark_unrealized_pnl', 'fifo_unrealized_pnl',
            )},
        }
        ordered_losses = sorted(losses, reverse=True)
        for level in LEVELS:
            confidence = str(level / 100)
            _fill_tail_support(group['statistics']['tails'][confidence], ordered_losses, level)
        group['f03_status'] = next(
            (tail['f03_status'] for tail in group['statistics']['tails'].values()
             if tail['f03_failures']), 'TAIL_PRECISION_CRITERIA_MET')
    return groups


def _point_estimates(group):
    if not group['n']:
        return {}
    result = {}
    for level in LEVELS:
        tail = group['statistics']['tails'][str(level / 100)]
        for name in ('var', 'cvar'):
            result[f'{name}{level}'] = tail[name]
    for name in RATE_NAMES:
        result[name] = group['events'][name]['estimate']
    result['drawdown_median'] = group['statistics']['maximum_drawdown_quantiles']['0.5']['estimate']
    return result


def _spread(group_samples):
    estimates = [_point_estimates(group) for group in group_samples if group['n']]
    if not estimates:
        return {}
    result = {}
    for key in estimates[0]:
        values = [row[key] for row in estimates]
        result[key] = dict(minimum=min(values), maximum=max(values),
                           range=max(values) - min(values),
                           seed_sd=float(np.std(values, ddof=1)) if len(values) > 1 else None)
    return result


def analyze(rows, *, completed_by_model, models, seeds, sizes, resamples=999):
    """Consume admitted prefix counts. Interrupted rows never define a completed tier."""
    indexed = defaultdict(dict)
    for row in rows:
        key = row['model'], row['seed']
        path_id = row['path_id']
        if path_id in indexed[key]:
            raise ValueError(f'duplicate account identity: {key}, {path_id}')
        indexed[key][path_id] = row

    cells, pooled, dispersion, changes = [], [], [], []
    final_size = sizes[-1]
    failures = set()
    tail_keys = tuple(f'{name}{level}' for level in LEVELS for name in ('var', 'cvar'))

    for model in models:
        complete_sizes = [size for size in sizes if size <= completed_by_model.get(model, 0)]
        if complete_sizes != list(sizes):
            failures.add('PLANNED_TIERS_INCOMPLETE')
        model_cells = {}
        for size in complete_sizes:
            model_sample = []
            for seed in seeds:
                eligible = indexed[model, seed]
                sample = [eligible[path_id] for path_id in range(size)]
                model_sample.extend(sample)
                groups = _summarize(sample, resamples=resamples, bootstrap_seed=seed)
                model_cells[size, seed] = groups
                cells.append(dict(model=model, seed=seed, n_paths=size, groups=groups))
                if size == final_size:
                    for group in groups.values():
                        if not group['n']:
                            failures.add('INSUFFICIENT_TAIL_SUPPORT')
                            continue
                        for tail in group['statistics']['tails'].values():
                            failures.update(tail['f03_failures'])
                        if any(not _interval_is_precise(group['events'][name],
                                PROTOCOL['maximum_final_rate_interval_width']) for name in RATE_NAMES):
                            failures.add('RATE_PRECISION_NOT_ESTABLISHED')
            pooled.append(dict(model=model, n_paths=size, seeds=list(seeds),
                               groups=_summarize(model_sample, resamples=resamples, bootstrap_seed=0)))
            for name in GROUPS:
                samples = [model_cells[size, seed][name] for seed in seeds]
                spread = _spread(samples)
                seed_counts = {str(seed): group['n'] for seed, group in zip(seeds, samples)}
                if size == final_size:
                    if not all(seed_counts.values()) or any(spread[key]['range'] >
                            PROTOCOL['maximum_final_seed_tail_range'] for key in tail_keys):
                        failures.add('SEED_SPREAD_CRITERIA_NOT_MET')
                dispersion.append(dict(model=model, n_paths=size, group=name,
                                       seed_counts=seed_counts, metrics=spread))

        for lower, upper in pairwise(complete_sizes):
            for seed in seeds:
                for name in GROUPS:
                    previous = model_cells[lower, seed][name]
                    current = model_cells[upper, seed][name]
                    difference = {}
                    if previous['n'] and current['n']:
                        baseline = _point_estimates(previous)
                        difference = {key: value - baseline[key]
                                      for key, value in _point_estimates(current).items()}
                    if not difference or any(abs(difference[key]) >
                            PROTOCOL['maximum_adjacent_tail_change'] for key in tail_keys):
                        failures.add('TIER_CHANGE_CRITERIA_NOT_MET')
                    changes.append(dict(model=model, seed=seed, group=name, lower=lower, upper=upper,
                                        prior_n=previous['n'], new_n=current['n'] - previous['n'],
                                        current_n=current['n'], differences=difference))
    return dict(cells=cells, pooled=pooled, seed_dispersion=dispersion, tier_differences=changes,
                protocol=PROTOCOL, convergence_failures=sorted(failures),
                statistical_status='STATISTICALLY_UNSTABLE' if failures else 'STATISTICALLY_STABLE',
                convergence_status='CONVERGENCE_NOT_ESTABLISHED' if failures else 'CONVERGENCE_ESTABLISHED')


def _percent(value):
    return '—' if value is None else f'{100 * value:.3f}%'


def _percentage_points(value):
    return '—' if value is None else f'{100 * value:.3f} pp'


def _tail_values(group):
    if not group['n']:
        return [None] * 4
    tails = group['statistics']['tails']
    return [tails[str(level / 100)][name] for level in LEVELS for name in ('var', 'cvar')]


def _interval_text(interval):
    lower, upper = interval['lower'], interval['upper']
    return f"{_percent(lower) if lower is not None else '−∞'} 至 {_percent(upper) if upper is not None else '+∞'}"


def _table(lines, headers, rows):
    lines.extend(['', '| ' + ' | '.join(headers) + ' |',
                  '| ' + ' | '.join('---' for _ in headers) + ' |'])
    lines.extend('| ' + ' | '.join(str(value) for value in row) + ' |' for row in rows)


def render_report(result):
    """Render a human-readable companion to the complete machine-readable analysis."""
    lines = [
        '# MC-05-F03 统计收敛', '',
        f"**{result['convergence_status']} / {result['statistical_status']}**", '',
        '预先固定的判据要求完成全部计划档位。每个模型的三个种子分别接受评估，'
        '覆盖四个人群及 95% 和 99% 两个尾部。两次相邻扩档的 VaR、CVaR 变化绝对值'
        '不得超过 0.5 个百分点，最终档三种子极差不得超过 0.5 个百分点。'
        '最终档每尾名义质量和按权重计算的 Kish 有效路径数均须达到 20。'
        'VaR 和 CVaR 的逐项 95% 区间须有界且宽度不超过 2 个百分点。'
        '最终档损失及参与率的逐项 95% Wilson 区间宽度须不超过 20 个百分点。'
        '在 1,000 USDT 初始资金下，0.5 与 2 个百分点分别对应 5 与 20 USDT 的损失差。'
        '20 条名义尾部路径把单条路径在经验 CVaR 中的权重限制在 5%。', '',
        '路径损失为 1 − 期末 NAV ÷ 初始资金。VaR 使用线性经验分位数，'
        'CVaR 对损失最大的样本按精确分数质量加权。VaR 区间采用二项分布秩，'
        '样本量不足时保留无界端点。CVaR 的保守区间利用总体 CVaR ≥ VaR，'
        '上界在仅依靠已观察损失时为 +∞。经验 bootstrap 区间刻画现有样本内的模拟不确定性，'
        '重采样次数不增加独立模拟路径，也不参与 CVaR 精度验收。'
        '这些逐项区间固定了校准参数和输入历史。三种子的点估计极差衡量模拟重复之间的差异。'
        '模型校准不确定性另需研究。', '',
        '## 各模型的无条件损失', '',
        '每行合并同一个价格模型的三个完整种子前缀。种子和条件组的指标列于下方。',
    ]
    _table(lines, ('模型', '档位', '有效账户', 'VaR95', 'CVaR95', 'VaR99', 'CVaR99', '收敛状态'), (
        (cell['model'], cell['n_paths'], group['n'], *map(_percent, _tail_values(group)),
         result['convergence_status'])
        for cell in result['pooled'] for group in (cell['groups']['unconditional'],)
    ))
    if result['convergence_failures']:
        lines.extend(['', '未满足的判据包括 '
                      + '、'.join(f'\u0060{reason}\u0060' for reason in result['convergence_failures']) + '。'])

    lines.extend(['', '## 实际参与与尾部样本', '',
                  '档位是每种子的计划分母。条件组的纳入条件是有成交、曾持仓、期末持仓。'
                  '条件风险的概率分母只包括满足条件的账户，样本比例的分母包括该档全部账户。'
                  'Kish 路径数衡量经验尾部权重集中度。未抽中上尾概率按各独立路径进入固定概率质量尾部计算。'])
    participation = []
    rates = []
    intervals = []
    for cell in result['cells'] + result['pooled']:
        seed = cell.get('seed', '同模型三种子')
        for name, group in cell['groups'].items():
            identity = (cell['model'], cell['n_paths'], seed, name)
            fraction = group['population_fraction']
            participation.append((*identity, f"{group['n']}/{group['population_n']}",
                                  _percent(fraction['estimate']), _interval_text(fraction),
                                  group['f03_status']))
            if not group['n']:
                continue
            rates.append((*identity, *(_percent(group['events'][field]['estimate']) for field in RATE_NAMES),
                _percent(group['f03_distributions']['capital_utilization_mean']['mean']),
                _percent(group['f03_distributions']['capital_utilization_peak']['mean']),
                _percent(group['f03_distributions']['capital_utilization_peak']['quantiles']['1.0'])))
            for level in LEVELS:
                tail = group['statistics']['tails'][str(level / 100)]
                intervals.append((*identity, level, f"{tail['expected_tail_paths']:.2f}",
                    tail['tail_paths_with_nonzero_weight'], f"{tail['kish_tail_paths']:.2f}",
                    tail['distinct_weighted_loss_values'], _percent(tail['largest_path_tail_weight']),
                    _percent(tail['no_upper_tail_draw_probability']),
                    _percent(tail['var']), _interval_text(tail['var_interval']),
                    _percent(tail['cvar']), _interval_text(tail['cvar_interval']),
                    _interval_text(tail['cvar_conservative_interval']), tail['f03_status']))
    _table(lines, ('模型', '档位', '种子', '条件', '实际/总体', '占比', '95% Wilson 区间', '状态'), participation)
    _table(lines, ('模型', '档位', '种子', '条件', '损失率', '成交率', '零成交率',
                   '曾持仓率', '期末持仓率', '平均利用率', '账户峰值的均值', '账户峰值的最大值'), rates)
    _table(lines, ('模型', '档位', '种子', '条件', '尾部', '名义质量', '加权路径数', 'Kish 路径数',
                   '不同加权损失值', '最大权重', '未抽中上尾概率', 'VaR', '95% VaR 区间',
                   'CVaR', '95% 经验 bootstrap 范围', '95% 保守 CVaR 区间', '状态'), intervals)
    lines.extend(['', '## 相邻档与种子之间的差异', '',
                  '相邻档差值按同一模型、同一种子计算，较大档位包含较小档位的路径。'
                  '条件组新增样本数仅统计新增前缀中符合条件的账户。'
                  '下面的差值和极差是点估计，单位为百分点。'])
    keys = tuple(f'{name}{level}' for level in LEVELS for name in ('var', 'cvar'))
    _table(lines, ('模型', '种子', '条件', '档位', '原样本+新增',
                   'ΔVaR95', 'ΔCVaR95', 'ΔVaR99', 'ΔCVaR99'), (
        (row['model'], row['seed'], row['group'], f"{row['lower']} → {row['upper']}",
         f"{row['prior_n']} + {row['new_n']}",
         *(_percentage_points(row['differences'].get(key)) for key in keys))
        for row in result['tier_differences']
    ))
    _table(lines, ('模型', '档位', '条件', '各种子样本数',
                   'VaR95 极差', 'CVaR95 极差', 'VaR99 极差', 'CVaR99 极差'), (
        (row['model'], row['n_paths'], row['group'],
         ', '.join(f'{seed}={n}' for seed, n in row['seed_counts'].items()),
         *(_percentage_points(row['metrics'].get(key, {}).get('range')) for key in keys))
        for row in result['seed_dispersion']
    ))
    lines.extend(['', '结构化统计还保留逐个单元的期末 NAV、收益、损失、最大回撤、'
                  '资金利用率和库存盈亏分布，损失及参与率区间，以及各指标的种子标准差。'])
    return '\n'.join(lines) + '\n'

