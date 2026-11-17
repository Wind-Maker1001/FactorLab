"""Paired TTL research summaries for complete MC-05-F04A account sets."""

from collections import Counter, defaultdict
from statistics import mean, median

import numpy as np
from scipy.stats import binomtest

from factorlab.research.monte_carlo import _loss_tail


TTLS = (48, 100, 168)
CONTRASTS = ((48, 100), (100, 168))
BASELINE_CONTRASTS = ((100, 48), (100, 168))
ALPHA = .05
MINIMUM_TAIL_PATHS = 20
PAIR_METRICS = (
    ('terminal_nav', 'NAV（USDT）', False),
    ('maximum_drawdown', '最大回撤（百分点）', True),
    ('capital_utilization_mean', '平均资金利用率（百分点）', True),
    ('fills', '成交次数', False),
    ('terminal_inventory', '期末库存（单位）', False),
    ('fifo_unrealized_pnl', '期末 FIFO 库存净损益（USDT）', False),
)


def _table(lines, headers, rows):
    lines.extend([
        '',
        '| ' + ' | '.join(headers) + ' |',
        '| ' + ' | '.join('---' for _ in headers) + ' |',
    ])
    for row in rows:
        lines.append('| ' + ' | '.join(map(str, row)) + ' |')


def _number(value):
    return f'{value:.6f}'


def _tuple_text(values, separator=' / '):
    return separator.join(map(str, values))


def _quantiles(values, probabilities=(.05, .5, .95)):
    return _tuple_text(map(_number, np.quantile(values, probabilities)))


def _means(rows, keys, format_value=_number):
    return tuple(format_value(mean(row[key] for row in rows)) for key in keys)


def _percentage(value):
    return f'{value:.3%}'


def _paired_trials(pairs, low, high):
    return [(pair[low], pair[high]) for pair in pairs]


def _fill_transitions(trials):
    return Counter((before['traded'], after['traded']) for before, after in trials)


def _participation_tests(by_model):
    tests = []
    for model, pairs in by_model.items():
        for low, high in CONTRASTS:
            trials = _paired_trials(pairs, low, high)
            changes = _fill_transitions(trials)
            up = changes[False, True]
            down = changes[True, False]
            per_seed = Counter({before['seed']: 0 for before, _ in trials})
            for before, after in trials:
                per_seed[before['seed']] += int(after['traded']) - int(before['traded'])
            direction = {int(np.sign(change)) for change in per_seed.values() if change}
            tests.append(dict(
                model=model, low=low, high=high, paths=len(trials),
                gained=up, lost=down,
                p=binomtest(up, up + down, .5).pvalue if up + down else 1.,
                consistent_seeds=len(direction) == 1 and sum(bool(change) for change in per_seed.values()) >= 2,
                seed_net_changes=dict(sorted(per_seed.items())),
            ))
    adjusted = 0.
    for index, test in enumerate(sorted(tests, key=lambda test: test['p'])):
        adjusted = max(adjusted, min(1., (len(tests) - index) * test['p']))
        test['holm_p'] = adjusted
        test['supports_direction'] = adjusted <= ALPHA and test['consistent_seeds']
    return tests


def _participation_line(identity, rows):
    first = [row['first_fill_hour'] for row in rows if row['traded']]
    held = sum(row['has_terminal_inventory'] for row in rows)
    fills = [row['fills'] for row in rows]
    return (*identity, len(rows), len(first), len(rows) - len(first), _percentage(len(first) / len(rows)),
            _quantiles(first) if first else '—',
            f'{mean(fills):.3f} / {median(fills):g}',
            f"{mean(row['holding_hours'] for row in rows):.3f}",
            f'{held}/{len(rows)}（{_percentage(held / len(rows))}）',
            _percentage(mean(row['terminal_inventory_value_fraction'] for row in rows)))


def _lifecycle_line(identity, rows):
    flat = [row for row in rows if row['zero_fills']]
    totals = tuple(sum(row[field] for row in rows) for field in (
        'order_submissions', 'order_cancellations', 'submission_rejections',
        'ttl_cancellations', 'breakout_cancellations', 'breakout_pauses',
    ))
    return (*identity, *totals, sum(row['first_touch_after_ttl_hour'] is not None for row in rows),
            f"{sum(row['buy_limit_touch_after_ttl'] for row in flat)}/{len(flat)}" if flat else '—')


def _touch_line(identity, rows):
    flat = [row for row in rows if row['zero_fills']]
    no_accepted_buy = sum(row['submitted_grid_buy_orders'] == 0 for row in flat)
    active_buy = sum(row['active_buy_limit_touch'] for row in flat)
    missing_limit = len(flat) - no_accepted_buy - active_buy
    return (*identity, len(flat), no_accepted_buy, missing_limit, active_buy,
            sum(row['buy_limit_touch_after_breakout'] for row in flat),
            *(sum(row[key] for row in rows) for key in (
                'active_limit_touch_unfilled_order_hours',
                'active_touch_capacity_blocked_order_hours',
                'active_touch_collateral_denied_order_hours',
                'active_touch_admission_denied_order_hours',
                'active_touch_other_order_hours',
            )))


def _transitions_line(identity, trials):
    changes = _fill_transitions(trials)
    both = [(before, after) for before, after in trials if before['traded'] and after['traded']]
    hours = [after['first_fill_hour'] - before['first_fill_hour'] for before, after in both]
    candidates = [(before, after) for before, after in trials
                  if before['zero_fills'] and before['buy_limit_touch_after_ttl']]
    gains = [after for _, after in candidates if after['traded']]
    return (*identity, *(changes[a, b] for a, b in ((False, False), (False, True), (True, False), (True, True))),
            f'{sum(hour < 0 for hour in hours)} / {hours.count(0)} / {sum(hour > 0 for hour in hours)}',
            _quantiles(hours) if hours else '—',
            f'{len(gains)}/{len(candidates)}' if candidates else '—',
            sum(before['fills'] != after['fills'] for before, after in trials),
            sum(before['order_submissions'] != after['order_submissions'] for before, after in trials),
            sum(before['inventory_path_sha256'] != after['inventory_path_sha256'] for before, after in trials))


def _render_participation(by_model, cohorts, tests, status):
    lines = [
        '# MC-05-F04A 成交参与率', '',
        f'{status}。NO LIVE MONEY。', '',
        '每个模型汇总的是同一模型下三个种子。时间从模拟起点计。首次成交分位数只使用有成交账户。'
        '持仓小时按小时估值时点有仓计数，期末库存市值比例以初始权益为分母。',
    ]
    _table(lines, ('模型', '种子', 'TTL（小时）', '路径数', '有成交', '零成交', '参与率',
                   '首次成交小时 P05/P50/P95', '成交次数均值/中位数', '平均持仓小时',
                   '期末有仓账户', '平均期末库存市值比例'),
           (_participation_line(identity, rows) for identity, rows in cohorts))
    lines.extend([
        '', '## 订单生命周期与后续触价', '',
        '成交、到期撤单和突破处理的时序见 [TTL 工程语义](./TTL_SEMANTICS.md)。'
        '到期后触价从撤单确认小时之后开始查找，按原订单方向应用成交滑点与限价比较。'
        '“TTL 后触价账户”列至少触及一个已到期的限价，包含已成交账户的其他订单。'
        '末列限定零成交账户及其买单，与 F03 的零成交诊断口径对应。'
        '突破撤单按确认回报的归因统计，突破暂停按策略事件统计。'
        '撤单后一笔报价只满足价格条件，实际成交还取决于是否有有效订单、可用现金、容量和控制准入。',
    ])
    _table(lines, ('模型', '种子', 'TTL', '提交', '取消确认', '拒绝提交', 'TTL 取消确认',
                   '突破取消确认', '突破暂停', 'TTL 后触价账户', 'TTL 后买单触价/零成交'),
           (_lifecycle_line(identity, rows) for identity, rows in cohorts))
    lines.extend([
        '', '三类零成交诊断按账户互斥。突破后的首次触价是额外的价格观察。'
        '右侧按订单×小时记录活跃限价满足报价且该小时无成交的情况，包含所有账户。'
        '“其他”表示现有记录未将该订单小时归因到前三种机制。',
    ])
    _table(lines, ('模型', '种子', 'TTL', '零成交账户', '无已接纳买单', '有买单但活跃时未触价',
                   '活跃买单触价', '突破后买单触价', '活跃未成交订单小时', '容量受限',
                   '抵押不足', '权威成交准入拒绝', '其他'),
           (_touch_line(identity, rows) for identity, rows in cohorts))
    all_transitions = []
    joint_transitions = []
    for model, pairs in by_model.items():
        seed_groups = defaultdict(list)
        for pair in pairs:
            seed_groups[pair[100]['seed']].append(pair)
        for seed, group in [('合计', pairs), *sorted(seed_groups.items())]:
            combinations = Counter(''.join(str(int(pair[ttl]['traded'])) for ttl in TTLS) for pair in group)
            joint_transitions.append((model, seed, len(group), *(combinations[f'{pattern:03b}'] for pattern in range(8))))
            for low, high in CONTRASTS:
                all_transitions.append(_transitions_line((model, seed, f'{low} → {high}'),
                                                         _paired_trials(group, low, high)))
    lines.extend([
        '', '## 同一路径上的交叉结果', '',
        '三位模式依次按 TTL 48、100、168 排列。1 表示至少一次成交，0 表示全程零成交。'
        '每条路径按实际模式进入一列。',
    ])
    _table(lines, ('模型', '种子', '完整路径数', '000', '001', '010', '011', '100', '101', '110', '111'),
           joint_transitions)
    lines.extend([
        '', '首次成交变化只比较两侧均成交的路径，小时差为延长后减延长前。'
        '“转为成交/原 TTL 后买单触价”限定较短 TTL 的零成交且到期后有买价触及的路径。'
        '持仓轨迹比较覆盖所有小时的净库存。',
    ])
    _table(lines, ('模型', '种子', 'TTL 延长', '无→无', '无→有', '有→无', '有→有',
                   '首笔提前/同小时/推迟', '首笔变化小时 P05/P50/P95', '转为成交/原 TTL 后买单触价',
                   '成交次数变化', '订单提交数变化', '持仓轨迹变化'), all_transitions)
    lines.extend([
        '', '## 预设方向性判据', '',
        '对六项模型内比较分别用双侧精确 McNemar 检验，样本为成交状态相异的路径对，'
        '再做 Holm 校正。整体方向判据要求校正后 p ≤ 0.05，至少两个种子的净变化非零，'
        '且任何非零净变化皆同向。推断条件是这三种冻结生成机制及其输入。',
    ])
    _table(lines, ('模型', 'TTL 延长', '路径数', '无→有', '有→无', '种子净变化', '精确 p', '校正后 p', '达到方向性判据'),
           ((test['model'], f"{test['low']} → {test['high']}", test['paths'], test['gained'], test['lost'],
             _tuple_text((f'{seed}={change:+d}' for seed, change in test['seed_net_changes'].items()), ', '),
             f"{test['p']:.6g}", f"{test['holm_p']:.6g}", '是' if test['supports_direction'] else '否')
            for test in tests))
    return '\n'.join(lines) + '\n'


def _drawdown_text(values):
    return _tuple_text(map(_percentage, np.quantile(values, (.5, .95, 1.))))


def _outcomes_line(identity, rows):
    capital = rows[0]['initial_capital']
    nav = [row['terminal_nav'] for row in rows]
    losses = sum(row['loss'] for row in rows)
    return (*identity, len(rows), _number(mean(nav)), _quantiles(nav, (0., .05, .5, .95, 1.)),
            _percentage(mean(nav) / capital - 1),
            _tuple_text(map(_percentage, np.quantile(np.asarray(nav) / capital - 1, (.05, .5, .95)))),
            f'{losses}/{len(rows)}（{_percentage(losses / len(rows))}）',
            _drawdown_text([row['maximum_drawdown'] for row in rows]),
            _quantiles([row['fifo_realized_pnl'] for row in rows]),
            _quantiles([row['fifo_unrealized_pnl'] for row in rows]),
            *_means(rows, ('fifo_realized_pnl', 'fifo_unrealized_pnl')))


def _exposure_line(identity, rows):
    return (*identity, len(rows), *_means(rows, ('capital_utilization_mean', 'capital_utilization_peak'), _percentage),
            _percentage(max(row['capital_utilization_peak'] for row in rows)),
            *_means(rows, ('committed_cash_mean', 'committed_cash_peak', 'inventory_mean', 'max_inventory')),
            _number(max(row['max_inventory'] for row in rows)),
            f"{_number(mean(row['terminal_inventory'] for row in rows))} / "
            f"{_number(median(row['terminal_inventory'] for row in rows))}",
            *(_number(sum(row[field] for row in rows)) for field in ('fees', 'slippage')),
            sum(row['risk_exit'] for row in rows), sum(row['exit_triggers'] for row in rows),
            sum(row['incomplete_exit'] for row in rows))


def _exposure_spread_line(identity, rows):
    return (*identity, len(rows),
            *(_tuple_text(map(_percentage, np.quantile([row[field] for row in rows], (.05, .5, .95))))
              for field in ('capital_utilization_mean', 'capital_utilization_peak')),
            *(_quantiles([row[field] for row in rows])
              for field in ('inventory_mean', 'max_inventory', 'terminal_inventory')))


def _tails_line(identity, rows, confidence):
    n = len(rows)
    losses = 1 - np.asarray([row['terminal_nav'] / row['initial_capital'] for row in rows])
    mass = (1 - confidence) * n
    var, cvar = _loss_tail(losses, confidence)
    return (*identity, n, _percentage(confidence), _percentage(var), _percentage(cvar), f'{mass:.2f}',
            int(np.ceil(mass)), 'INSUFFICIENT_TAIL_SUPPORT' if mass < MINIMUM_TAIL_PATHS else '—')


def _delta_lines(model, label, trials):
    for field, name, rate in PAIR_METRICS:
        scale = 100 if rate else 1
        differences = defaultdict(list)
        for before, after in trials:
            differences[before['seed']].append((after[field] - before[field]) * scale)
        combined = [value for values in differences.values() for value in values]
        seed_means = {seed: mean(values) for seed, values in sorted(differences.items())}
        yield (model, label, name, len(combined), _number(mean(combined)), _quantiles(combined),
               _tuple_text((f'{seed}={_number(value)}' for seed, value in seed_means.items()), ', '),
               _number(max(seed_means.values()) - min(seed_means.values())))


def _trade_risk_line(model, label, trials):
    higher = [(before, after) for before, after in trials if after['fills'] > before['fills']]
    gained = [(before, after) for before, after in higher if before['zero_fills']]
    losses = sum(after['terminal_nav'] < before['terminal_nav'] for before, after in higher)
    drawdowns = sum(after['maximum_drawdown'] > before['maximum_drawdown'] for before, after in higher)
    both = sum(after['terminal_nav'] < before['terminal_nav'] and after['maximum_drawdown'] > before['maximum_drawdown']
               for before, after in higher)
    return (model, label, len(higher), len(gained), losses, drawdowns, both,
            _quantiles([after['terminal_nav'] - before['terminal_nav'] for before, after in gained]) if gained else '—',
            _percentage(mean(after['capital_utilization_mean'] - before['capital_utilization_mean']
                             for before, after in gained)) if gained else '—',
            _number(mean(after['terminal_inventory'] - before['terminal_inventory']
                         for before, after in gained)) if gained else '—')


def _jump_lines(pairs):
    lines = [
        '', '## Jump-down 跳跃与库存', '',
        '有跳和无跳按价格路径的冻结潜在事件划组，所有 TTL 使用相同的路径。'
        '跳前库存冲击以实际跳前库存乘事件导致的价格变动计算，使用未含跳跃的同小时背景价格作为对照。'
        '该量刻画跳前已持仓部分，跳价当小时才购入的库存及后续变化体现在 NAV 和 FIFO 期末库存损益中。',
    ]
    accounts = []
    paired = []
    for group, label in ((False, '无跳跃'), (True, '至少一次跳跃')):
        selected = [pair for pair in pairs if bool(pair[100]['jump_events']) == group]
        if not selected:
            continue
        for ttl in TTLS:
            rows = [pair[ttl] for pair in selected]
            accounts.append((label, ttl, len(rows), sum(row['traded'] for row in rows),
                             sum(row['has_terminal_inventory'] for row in rows),
                             sum(row['jump_events'] for row in rows),
                             sum(row['jumps_with_prior_inventory'] for row in rows),
                             _number(sum(row['fixed_jump_mark_effect'] for row in rows)),
                             _number(mean(row['terminal_nav'] for row in rows)),
                             _number(min(row['terminal_nav'] for row in rows)),
                             _number(mean(row['fifo_unrealized_pnl'] for row in rows))))
        for base, alt in BASELINE_CONTRASTS:
            trials = _paired_trials(selected, base, alt)
            nav_differences = [after['terminal_nav'] - before['terminal_nav'] for before, after in trials]
            paired.append((label, f'{alt} − {base}', len(trials), _number(mean(nav_differences)),
                           _number(min(nav_differences)),
                           _number(mean(after['fifo_unrealized_pnl'] - before['fifo_unrealized_pnl']
                                        for before, after in trials)),
                           _number(mean(after['fixed_jump_mark_effect'] - before['fixed_jump_mark_effect']
                                        for before, after in trials))))
    _table(lines, ('路径组', 'TTL', '路径数', '成交账户', '期末有仓', '跳跃次数', '跳前有仓次数',
                   '跳前库存冲击合计', '期末 NAV 均值', '最小期末 NAV', '期末 FIFO 库存净盈亏均值'), accounts)
    _table(lines, ('路径组', 'TTL 差', '匹配路径数', 'ΔNAV 均值', '最差路径 ΔNAV',
                   'Δ库存净损益均值', 'Δ跳前库存冲击均值'), paired)
    lines.extend([
        '', '按每条路径三档中最低的期末 NAV 排序，列出最差五条作为尾部个例。'
        '每个金额单元按 TTL 48 / 100 / 168 排列，使用该行的同一条价格路径。',
    ])
    extremes = sorted(pairs, key=lambda pair: min(pair[ttl]['terminal_nav'] for ttl in TTLS))[:5]
    _table(lines, ('seed/path', '跳跃次数', '期末 NAV', '期末库存 FIFO', '跳前库存冲击'),
           ((f"{pair[100]['seed']}/{pair[100]['path_id']}", pair[100]['jump_events'],
             *(_tuple_text(_number(pair[ttl][field]) for ttl in TTLS)
               for field in ('terminal_nav', 'fifo_unrealized_pnl', 'fixed_jump_mark_effect')))
            for pair in extremes))
    return lines


def _render_financial(by_model, model_cohorts):
    lines = [
        '# MC-05-F04A 账户收益、敞口与库存', '',
        'STATISTICAL_CONVERGENCE_NOT_ESTABLISHED。FINANCIAL_NOT_ESTABLISHED。NO LIVE MONEY。', '',
        'NAV、FIFO 损益、现金冻结、手续费与滑点均以 USDT 计。最大回撤是小时 NAV 相对于先前最高值的最大跌幅。'
        '已实现 FIFO 净损益合并正常卖出与风险退出。期末 FIFO 库存净损益含剩余批次的买入费用。',
        '各模型及 TTL 单独列出全部、已成交和零成交账户的实际分母。行情路径相同的三种 TTL 在配对章节以逐路径差计算。',
    ]
    conditions = []
    for identity, rows in model_cohorts:
        conditions.append(((*identity, '全部'), rows))
        for flag, label in (('traded', '已成交'), ('zero_fills', '零成交')):
            group = [row for row in rows if row[flag]]
            conditions.append(((*identity, label), group))
    _table(lines, ('模型', 'TTL', '条件', '路径数', '期末 NAV 均值', '期末 NAV 最小/P05/P50/P95/最大',
                   '收益率均值', '收益率 P05/P50/P95', '期末损失概率', '最大回撤 P50/P95/最大',
                   '已实现 FIFO P05/P50/P95', '期末库存 FIFO P05/P50/P95', '已实现 FIFO 均值', '期末库存 FIFO 均值'),
           (_outcomes_line(identity, rows) if rows else (*identity, 0, *('—',) * 10)
            for identity, rows in conditions))
    lines.extend([
        '', '利用率是持仓市值除以初始权益。下面分别列出各账户的全时段平均值与峰值在同组中的均值，'
        '并额外列出账户峰值中的最大值。冻结现金取未完成订单对应的权威现金预留。'
        '库存列采用交易资产数量。退出未完成按期末风险退出仍挂单或仍处于 EXITING 且有库存计。',
    ])
    _table(lines, ('模型', 'TTL', '条件', '路径数', '平均利用率均值', '峰值利用率均值', '最高利用率',
                   '平均冻结现金均值', '峰值冻结现金均值', '平均库存均值', '峰值库存均值', '最大库存',
                   '期末库存均值/中位数', '手续费合计', '滑点合计', '退出触发账户', '退出触发事件', '未完成退出账户'),
           (_exposure_line(identity, rows) if rows else (*identity, 0, *('—',) * 14)
            for identity, rows in conditions))
    lines.extend(['', '以下 P05/P50/P95 描述组内账户间的资金利用率和库存分布。'])
    _table(lines, ('模型', 'TTL', '条件', '路径数', '平均利用率', '峰值利用率',
                   '平均库存', '峰值库存', '期末库存'),
           (_exposure_spread_line(identity, rows) if rows else (*identity, 0, *('—',) * 5)
            for identity, rows in conditions))
    lines.extend([
        '', '## 模型内经验尾部', '',
        '以 1 − 期末 NAV / 初始权益度量损失。VaR 用线性经验分位数，CVaR 用最大损失起始的分数质量加权均值。'
        '有效名义尾部质量是各组路径数乘尾部概率，加权路径数含边界分数路径。'
        f'本轮每模型每 TTL 最多 48 条路径，两种置信水平均低于 F03 的 {MINIMUM_TAIL_PATHS} 条尾部质量门槛。',
    ])
    _table(lines, ('模型', 'TTL', '条件', '路径数', '置信水平', '经验 VaR', '经验 CVaR',
                   '名义尾部质量', '加权路径数', '支持'),
           (_tails_line(identity, rows, confidence)
            for identity, rows in conditions if rows for confidence in (.95, .99)))
    lines.extend([
        '', '## 以 TTL 100 为基准的逐路径差异', '',
        '差值是同一路径下对照 TTL 减 TTL 100。百分比指标的差采用百分点。'
        'P05/P50/P95 显示各条路径之间的描述性分布。'
        '种子均值及其极差描述固定生成机制下三个独立随机种子的离散程度。这些范围是描述性不确定性区间。',
    ])
    _table(lines, ('模型', 'TTL 差', '指标', '配对路径数', '均值', 'P05/P50/P95',
                   '各固定种子均值', '种子均值极差'),
           (row for model, pairs in by_model.items() for base, alt in BASELINE_CONTRASTS
            for row in _delta_lines(model, f'{alt} − {base}', _paired_trials(pairs, base, alt))))
    lines.extend([
        '', '## 更多成交时的风险变化', '',
        '下表中间三列都以成交次数增加的配对路径数为分母。'
        '总 NAV 下降且最大回撤上升给出收益与回撤同向恶化的路径数。'
        '新增参与组按较短 TTL 零成交、延长后有成交划分。'
        '收益与回撤同时恶化的计数是逐路径比较，模型整体的风险调整后收益还取决于完整的联合分布。'
        '本轮回放用于描述 NAV 与风险的联动，金融结论需独立样本和尾部精度。',
    ])
    _table(lines, ('模型', 'TTL 延长', '成交次数增加', '其中新增参与', 'NAV 下降', '回撤上升',
                   'NAV 下降且回撤上升', '新增参与 ΔNAV P05/P50/P95', '新增参与 Δ平均利用率',
                   '新增参与 Δ期末库存均值'),
           (_trade_risk_line(model, f'{low} → {high}', _paired_trials(pairs, low, high))
            for model, pairs in by_model.items() for low, high in CONTRASTS))
    lines.extend(_jump_lines(by_model['jump_down']))
    lines.extend([
        '', '小时收盘回放只能观察冻结撮合规则使用的小时报价。'
        '小时内是否曾触及限价、排队顺序和实盘流动性需要更细粒度的独立价格与成交数据。'
        '这三组分布使用各自的模型假设和相同历史校准窗口。',
    ])
    return '\n'.join(lines) + '\n'


def write_reports(output, rows):
    """Render summaries after replay has validated the whole paired plan."""
    by_identity = defaultdict(dict)
    for row in rows:
        by_identity[row['model'], row['seed'], row['path_id']][row['ttl_hours']] = row

    by_model = defaultdict(list)
    for identity, pair in sorted(by_identity.items()):
        by_model[identity[0]].append(pair)

    cohorts = []
    model_cohorts = []
    for model, pairs in by_model.items():
        rows_by_seed_ttl = defaultdict(list)
        for pair in pairs:
            for ttl in TTLS:
                rows_by_seed_ttl[pair[ttl]['seed'], ttl].append(pair[ttl])
        for ttl in TTLS:
            selected = [pair[ttl] for pair in pairs]
            cohorts.append(((model, '合计', ttl), selected))
            model_cohorts.append(((model, ttl), selected))
        cohorts.extend(((model, *identity), selected) for identity, selected in sorted(rows_by_seed_ttl.items()))

    tests = _participation_tests(by_model)
    status = ('TTL_SENSITIVITY_ESTABLISHED' if any(test['supports_direction'] for test in tests)
              else 'TTL_SENSITIVITY_INCONCLUSIVE')
    (output / 'PARTICIPATION.md').write_text(_render_participation(by_model, cohorts, tests, status), encoding='utf-8')
    (output / 'FINANCIAL.md').write_text(_render_financial(by_model, model_cohorts), encoding='utf-8')
    return dict(ttl_sensitivity_status=status, participation_tests=tests)
