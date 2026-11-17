"""Economic diagnostics for MC-05-F03 close-quote Grid accounts."""

from collections import Counter, defaultdict
from math import ceil, exp, log
from statistics import mean, median

import numpy as np
import pandas as pd
from scipy.stats import binom

from factorlab.experiments.grid_paper import D
from factorlab.research.monte_carlo.reliability import wilson_interval


def _zero_fill_diagnostics(prices, nav, result, buy_orders):
    # A flat account under the frozen geometric Grid can only submit its initial
    # buys. Breakouts pause it, expiry never reseeds it, and reversals require fills.
    # Orders first become eligible on the next quote. The cancel bar is included
    # because Paper computes execution candidates before cancel confirmations.
    dates = {date: step for step, date in enumerate(pd.to_datetime(nav.date, utc=True))}
    cancellations = {
        event['order_id']: (dates[pd.Timestamp(event['time'])], event['cause'])
        for event in result['lifecycle'] if event['status'] == 'CANCELED'
    }
    slip_multiplier = 1 + D(result['execution']['slippage'])
    buy_quotes = [D(float(price)) * slip_multiplier for price in prices]
    active_touch = after_ttl = after_breakout = False
    for key, order in buy_orders.items():
        end, cause = cancellations.get(key, (len(prices) - 1, None))
        limit = D(order['intent']['limit_price'])
        active_touch |= any(quote <= limit for quote in buy_quotes[1:end + 1])
        if cause in ('TTL', 'BREAKOUT'):
            later_touch = any(quote <= limit for quote in buy_quotes[end + 1:])
            after_ttl |= cause == 'TTL' and later_touch
            after_breakout |= cause == 'BREAKOUT' and later_touch
    return active_touch, after_ttl, after_breakout


def capture_account_diagnostics(row, nav, result, latent, prices):
    """Retain compact observations before disposing of the authoritative account."""
    orders = result['orders']
    buy_orders = {
        key: order for key, order in orders.items()
        if order['purpose'] == 'GRID' and order['intent']['side'] == 'BUY'
        and order['status'] != 'REJECTED'
    }
    risk = result['risk']
    rejected = {event['order_id'] for event in risk if event['kind'] == 'SUBMISSION_REJECTED'}
    rejected.update(key for key, order in orders.items() if order['status'] == 'REJECTED')
    active_touch = after_ttl = after_breakout = None
    if row['zero_fills']:
        active_touch, after_ttl, after_breakout = _zero_fill_diagnostics(prices, nav, result, buy_orders)
    row.update(
        submitted_grid_buy_orders=len(buy_orders),
        submission_rejections=len(rejected),
        fill_admission_denials=sum(event['kind'] == 'FILL_ADMISSION_DENIED' for event in risk),
        collateral_denials=sum(event['kind'] == 'INSUFFICIENT_COLLATERAL' for event in risk),
        breakout_pauses=sum(event['kind'] == 'RISK_TRIGGER' and event['state'] == 'PAUSED'
                            and event['reason'] == 'BREAKOUT' for event in risk),
        ttl_cancellations=sum(event['cause'] == 'TTL' and event['status'] == 'CANCELED'
                              for event in result['lifecycle']),
        active_buy_limit_touch=active_touch,
        buy_limit_touch_after_ttl=after_ttl,
        buy_limit_touch_after_breakout=after_breakout,
        hourly_return_volatility=float(np.std(np.diff(prices) / prices[:-1], ddof=1)),
    )
    for event in latent or ():
        if 'step' in event:
            step = event['step']
            before = float(nav.inventory.iloc[step - 1])
            after = float(nav.inventory.iloc[step])
            jump_multiplier = exp(event['jump_log_sizes'][0])
            # Strip only the frozen multiplicative jump, holding the actual
            # background increment and pre-quote inventory fixed.
            row_price = float(prices[step])
            event.update(inventory_before=before, inventory_after=after,
                         fixed_jump_mark_effect=before * (row_price - row_price / jump_multiplier))


def _participation_line(model, accounts, rows):
    traded = [row for row in rows if row['traded']]
    ever_inventory = sum(row['ever_inventory'] for row in rows)
    terminal_inventory = sum(row['has_terminal_inventory'] for row in rows)
    utilization = mean(row['capital_utilization_mean'] for row in rows)
    traded_utilization = f"{mean(row['capital_utilization_mean'] for row in traded):.3%}" if traded else '—'
    peak_utilization = max(row['capital_utilization_peak'] for row in rows)
    fifo_net = sum(row['fifo_unrealized_pnl'] for row in rows)
    mark_pnl = sum(row['terminal_mark_unrealized_pnl'] for row in rows)
    average_nav = mean(row['terminal_nav'] for row in rows)
    median_drawdown = median(row['maximum_drawdown'] for row in rows)
    traded_drawdown = f"{median(row['maximum_drawdown'] for row in traded):.3%}" if traded else '—'
    max_drawdown = max(row['maximum_drawdown'] for row in rows)
    return (f'| {model} | {accounts} | {len(traded)}/{len(rows)}（{len(traded)/len(rows):.2%}） | '
            f'{ever_inventory} | {terminal_inventory} | {utilization:.3%} | {traded_utilization} | '
            f'{peak_utilization:.3%} | {average_nav:.4f} | {median_drawdown:.3%} / {traded_drawdown} | '
            f'{max_drawdown:.3%} | {fifo_net:.4f} | {mark_pnl:.4f} |')


def _nonparticipation_line(model, rows):
    flat = [row for row in rows if row['zero_fills']]
    counts = (
        sum(row['submitted_grid_buy_orders'] == 0 for row in flat),
        sum(row['submitted_grid_buy_orders'] > 0 and not row['active_buy_limit_touch'] for row in flat),
        sum(row['active_buy_limit_touch'] for row in flat),
        sum(row['buy_limit_touch_after_ttl'] for row in flat),
        sum(row['buy_limit_touch_after_breakout'] for row in flat),
        sum(row['submission_rejections'] > 0 for row in flat),
        sum(row['fill_admission_denials'] + row['collateral_denials'] > 0 for row in flat),
        sum(row['breakout_pauses'] > 0 for row in flat),
    )
    values = ' | '.join(str(count) for count in counts)
    return f'| {model} | {len(flat)}（{len(flat)/len(rows):.2%}） | {values} |'


def _jump_lines(rows, evidence):
    cohorts = defaultdict(list)
    events = []
    tail = []
    for row in rows:
        path = evidence[(row['model'], row['seed'], row['path_id'])]
        effects = sum(event['fixed_jump_mark_effect'] for event in path)
        cohorts[min(len(path), 2)].append(row)
        events.extend(path)
        tail.append((row, len(path), effects))
    lines = [
        '## Jump-down 库存冲击',
        '',
        'Jump-down 在冻结高斯背景上叠加独立的每小时 Bernoulli 压力事件，概率为 1/168，触发时令价格乘 0.7。事件频率和幅度是场景假设。',
        '',
        '| 路径跳跃次数 | 路径数 | 已成交 | 期末有仓 | 期末 NAV 均值 | 期末 FIFO 库存净损益合计（USDT） |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
    ]
    for count, selected in sorted(cohorts.items()):
        label = '≥2' if count == 2 else str(count)
        lines.append(f"| {label} | {len(selected)} | {sum(row['traded'] for row in selected)} | "
                     f"{sum(row['has_terminal_inventory'] for row in selected)} | "
                     f"{mean(row['terminal_nav'] for row in selected):.4f} | "
                     f"{sum(row['fifo_unrealized_pnl'] for row in selected):.4f} |")
    preheld = sum(event['inventory_before'] > 0 for event in events)
    added = sum(event['inventory_after'] > event['inventory_before'] for event in events)
    shock = sum(event['fixed_jump_mark_effect'] for event in events)
    nav_change = sum(event['nav_after'] - event['nav_before'] for event in events)
    terminal_loss = sum(-row['fifo_unrealized_pnl'] for row in rows if row['fifo_unrealized_pnl'] < 0)
    loss_after_jump = sum(-row['fifo_unrealized_pnl'] for row, count, _ in tail
                          if count and row['fifo_unrealized_pnl'] < 0)
    lines.extend([
        '',
        f'共 {len(events)} 次事件，其中 {preheld} 次跳前有库存，{added} 次事件小时的净库存增加。'
        f'保持实际背景收益与跳前库存固定，逐事件剥离乘数 0.7 后，跳跃造成的当小时存量标记冲击合计 {shock:.4f} USDT，'
        f'账户在相同小时的 NAV 变化合计 {nav_change:.4f} USDT。后者还包含背景价格变化与当小时交易成本。',
        f'期末 FIFO 库存亏损的正值合计 {terminal_loss:.4f} USDT，其中 {loss_after_jump:.4f} USDT '
        '出现在至少一次跳跃的路径。先看无跳跃组的基线，再用跳前有仓事件区分已有仓位遭打击和跌价时开仓。',
        '',
        '| 最差期末路径（seed/path） | 跳跃次数 | 期末 NAV | FIFO 已实现净盈亏 | FIFO 期末库存净盈亏 | 跳前库存固定跳幅冲击 |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
    ])
    for row, count, effects in sorted(tail, key=lambda entry: entry[0]['terminal_nav'])[:5]:
        lines.append(f"| {row['seed']}/{row['path_id']} | {count} | {row['terminal_nav']:.4f} | "
                     f"{row['fifo_realized_pnl']:.4f} | {row['fifo_unrealized_pnl']:.4f} | {effects:.4f} |")
    lines.extend(['', '购入时点、随后跳跃和跳后价格路径共同决定期末库存损益。逐事件存量冲击只归因当小时的标记变化，路径表可用于对照其与期末损益的差距。', ''])
    return lines


def _regime_lines(rows, evidence):
    groups = defaultdict(list)
    starts = Counter()
    traded_paths = Counter()
    for row in rows:
        blocks = evidence[(row['model'], row['seed'], row['path_id'])]
        starts[blocks[0]['state']] += 1
        traded_paths[blocks[0]['state']] += row['traded']
        previous = None
        for block in blocks:
            context = 'first' if previous is None else 'stay' if block['state'] == previous else 'switch'
            groups[block['state'], context].append(block)
            previous = block['state']
    lines = [
        '## Regime historical 状态与成交',
        '',
        '生成器按预定 24 小时区块切换状态。同一状态分别列出首块、状态保持、从其他状态切入时的暴露与成交。',
        '',
        '| 状态 | 入块方式 | 块数 | 成交小时/状态小时 | 持仓小时/状态小时 | 块 NAV 变化中位数（USDT） |',
        '| --- | --- | ---: | ---: | ---: | ---: |',
    ]
    labels = {'first': '首块', 'stay': '保持', 'switch': '切入'}
    for (state, context), blocks in sorted(groups.items()):
        hours = sum(block['size'] for block in blocks)
        traded_hours = sum(block['traded_hours'] for block in blocks)
        held_hours = sum(block['inventory_hours'] for block in blocks)
        nav_change = median(block['nav_change'] for block in blocks)
        lines.append(f'| {state} | {labels[context]} | {len(blocks)} | {traded_hours}/{hours} | '
                     f'{held_hours}/{hours} | {nav_change:.4f} |')
    support = '、'.join(f'{state} {traded_paths[state]}/{starts[state]}' for state in sorted(starts))
    lines.extend(['', f'按初始状态观察的全路径成交账户数占比分别为 {support}。'
                  '前序区块可留下订单及库存，且 TTL 造成成交机会随路径时间改变。相关分布同时受状态、切换时点和策略历史影响。', ''])
    return lines


def _bootstrap_lines(rows, historical_returns):
    historical_returns = np.asarray(historical_returns)
    log_windows = np.lib.stride_tricks.sliding_window_view(np.log1p(historical_returns), 24)
    worst_24h = float(np.expm1(log_windows.sum(axis=1).min()))
    squared = historical_returns ** 2
    return [
        '## Moving-block bootstrap 历史特征',
        '',
        '| 来源 | 小时收益标准差 | lag1 收益相关 | lag1 平方收益相关 | 最差小时 | 最差连续 24 小时 |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
        f'| 冻结历史整段 | {historical_returns.std(ddof=1):.4%} | '
        f'{np.corrcoef(historical_returns[:-1], historical_returns[1:])[0, 1]:.4f} | '
        f'{np.corrcoef(squared[:-1], squared[1:])[0, 1]:.4f} | '
        f'{historical_returns.min():.4%} | {worst_24h:.4%} |',
        f"| 模拟路径 | {mean(row['hourly_return_volatility'] for row in rows):.4%} | "
        f"{mean(row['return_autocorrelation_1'] for row in rows):.4f} | "
        f"{mean(row['squared_return_autocorrelation_1'] for row in rows):.4f} | "
        f"{min(row['worst_hour_return'] for row in rows):.4%} | {min(row['worst_24h_return'] for row in rows):.4%} |",
        '',
        '模拟标准差和相关系数按独立路径等权平均，极端值取全部模拟路径中的最差观察。块内按原顺序保留波动和极端小时，'
        '两个抽样块的拼接会重置跨块依赖并形成新的 24 小时复合片段。重复抽样反映冻结历史经验分布。',
        '',
    ]


def _paths_for_tail(probability, minimum_tail_support, coverage=.95):
    # With this upper bound the expected count is at least twice the target;
    # the multiplicative Chernoff bound then controls the lower-tail miss rate.
    left = minimum_tail_support
    right = ceil(max(2 * left, 8 * log(1 / (1 - coverage))) / probability)
    while left < right:
        middle = (left + right) // 2
        if binom.sf(minimum_tail_support - 1, middle, probability) >= coverage:
            right = middle
        else:
            left = middle + 1
    return left


def _research_size_lines(groups, calibration, cost_safety_factor, minimum_tail_support):
    lines = [
        '## 后续研究的独立路径与成本',
        '',
        f'每个模型、种子、条件组达到名义尾部质量 {minimum_tail_support}，'
        f'至少需要 {20 * minimum_tail_support:,} 个有效账户支持 95% 尾部，'
        f'至少需要 {100 * minimum_tail_support:,} 个有效账户支持 99% 尾部。'
        '下面用二项模型规划更有余量的规模，使实际观测达到目标尾部条数的概率至少为 95%。'
        '条件组用三个种子中最小的单独 95% Wilson 参与率下限，再乘以 5% 或 1% 作为每条独立路径的尾部命中概率。'
        '成本按每模型在相同并行度下的固定账户预校准速率估算，包含三种子和预校准安全系数。'
        'Jump-down 用包含进程池冷启动的首批速率外推，长批次的墙钟和 CPU 估算偏保守。'
        'CPU 是全进程树总时长。证据量按预校准的每账户观测值外推。',
        '',
        '| 模型 | 条件组 | 参与率规划下限 | 95% 尾部每种子原始路径 | 三种子墙钟/CPU（小时） | 99% 尾部每种子原始路径 | 三种子墙钟/CPU（小时） | 99% 证据 MiB |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    startup = calibration['performance']['startup_wall_seconds']
    startup_cpu = calibration['performance']['startup_tree_cpu_seconds']
    evidence_per_account = calibration['evidence_bytes_per_account']
    conditions = (('Unconditional', None), ('Traded', 'traded'),
                  ('Ever inventory', 'ever_inventory'), ('Terminal inventory', 'has_terminal_inventory'))
    for model, rows in groups.items():
        by_seed = defaultdict(list)
        for row in rows:
            by_seed[row['seed']].append(row)
        measured = calibration['models'][model]
        wall_per_account = measured['wall_seconds'] / measured['accounts']
        cpu_per_account = measured['tree_cpu_seconds'] / measured['accounts']
        for condition, flag in conditions:
            rates = [1.] if flag is None else []
            if flag is not None:
                for selected in by_seed.values():
                    count = sum(row[flag] for row in selected)
                    rates.append(wilson_interval(count, len(selected))['lower'] if count else 0.)
            minimum_rate = min(rates)
            if minimum_rate == 0:
                lines.append(f'| {model} | {condition} | 0% | — | — | — | — | — |')
                continue
            columns = [f'| {model}', condition, f'{minimum_rate:.2%}']
            for tail_probability in (.05, .01):
                per_seed = _paths_for_tail(minimum_rate * tail_probability, minimum_tail_support)
                total = per_seed * len(by_seed)
                wall_hours = (startup + total * wall_per_account * cost_safety_factor) / 3600
                cpu_hours = (startup_cpu + total * cpu_per_account * cost_safety_factor) / 3600
                columns.extend((f'{per_seed:,}', f'{wall_hours:.2f} / {cpu_hours:.2f}'))
            columns.append(f'{total * evidence_per_account / (1024 ** 2):.1f} |')
            lines.append(' | '.join(columns))
    lines.extend([
        '',
        '参与率下限为零时，按本次证据无法给出有界的条件样本成本。表中数量只对应尾部条数目标。'
        '跨档稳定、跨种子离散和尾部区间精度仍需分别达标。极端尾部均值的有界区间需要额外可论证的损失支持或尾部假设。'
        '扩大同一冻结模型的模拟样本可以降低随机模拟误差。市场风险推断还需要独立行情窗口、点时可用性证据，'
        '以及对跳跃频率和幅度、状态阈值和转移、区块长度及成交与资金费假设的预先登记敏感性研究。',
        '',
    ])
    return lines


def render_financial_report(rows, path_evidence, *, historical_returns, calibration, steps=168,
                            cost_safety_factor=1.2, minimum_tail_support=20):
    """Describe completed independent accounts at each model's largest admitted tier."""
    evidence = {(entry['model'], entry['seed'], entry['path_id']): entry['events']
                for entry in path_evidence}
    groups = defaultdict(list)
    for row in rows:
        groups[row['model']].append(row)
    lines = [
        '# MC-05-F03 金融解释',
        '',
        'FINANCIAL_NOT_ESTABLISHED。NO LIVE MONEY。',
        '',
        f'以下诊断只使用每个模型最后一个已完成的完整档位，每个 Paper 账户覆盖 {steps} 小时。'
        '各模型风险和成交分别计算。期末 NAV 与库存金额以 USDT 计。',
        '',
    ]
    if not groups:
        return '\n'.join(lines + ['资源准入后没有完整的正式模型组可用于金融诊断。', ''])
    lines.extend([
        '## 参与率、资金利用与回撤',
        '',
        '| 模型 | 每种子路径 | 成交账户 | 曾持仓数 | 末持仓数 | 平均利用率 | 成交账户平均利用率 | 利用率峰值 | 期末 NAV 均值 | 最大回撤中位数（全部 / 已成交） | 最大回撤峰值 | FIFO 期末库存净盈亏合计 | 账务未实现盈亏合计 |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |',
    ])
    for model, selected in groups.items():
        per_seed = len(selected) // len({row['seed'] for row in selected})
        lines.append(_participation_line(model, per_seed, selected))
    lines.extend([
        '',
        '利用率以持仓市值除以初始资金，挂单冻结金额另有现金账务记录。无成交账户从零库存开始，NAV 维持在初始现金。'
        '这些账户为无条件风险分布增加一个零损失质量点，也拉低全体平均利用率。'
        '比较已成交与全部账户的利用率和各条件组风险，可判断低敞口对小回撤的影响。',
        '',
        '| 模型 | 零成交账户 | 无已接纳买限单 | 有限单但活跃时未触价 | 活跃时触价 | TTL 后触价 | 突破暂停撤单后触价 | 提交被拒 | 成交准入或抵押不足 | 突破暂停 |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |',
    ])
    lines.extend(_nonparticipation_line(model, selected) for model, selected in groups.items())
    lines.extend([
        '',
        '零成交账户中，接纳的买单来自初始几何网格。该配置到期不重挂，突破会暂停并撤单，反向订单要等首笔成交。'
        '触价按晚于提交的小时收盘价加实际买方滑点与限价比较，撤单确认当小时仍属于报价候选检查范围。'
        '表中前三个原因列互斥，其他列可重叠。活跃时未触价支持报价未满足成交条件，TTL 后触价反映期限删去了后来可能的报价机会。'
        '突破与拒绝列给出策略或控制侧事件。撤单后的触价只描述随后价格，成交候选还受容量、最小名义金额与权威准入影响。',
        '',
        '期末 NAV 含按最后小时收盘价标记的全部未平仓头寸。FIFO 库存净损益把未平仓买入费用分配到剩余批次，'
        '账务未实现盈亏使用平均入场价。两列都是跨独立账户的金额加总。完成闭环的收益与库存标记变化须结合 FIFO 归因阅读。',
        '',
    ])
    if 'jump_down' in groups:
        lines.extend(_jump_lines(groups['jump_down'], evidence))
    if 'regime_historical' in groups:
        lines.extend(_regime_lines(groups['regime_historical'], evidence))
    if 'moving_block_bootstrap' in groups:
        lines.extend(_bootstrap_lines(groups['moving_block_bootstrap'], historical_returns))
    lines.extend([
        '## 研究使用范围',
        '',
        '本轮可以比较固定 Grid 规则在三种指定的价格生成机制下的参与率、敞口和已标记库存损失。'
        '参与组是在成交或持仓后形成的子总体。跨模型风险差异同时包含价格机制和敞口差异。'
        '把样本扩档、逐种子离散和各条件组尾部支持结合起来，才能评价模拟误差。',
        '',
        '实盘 168 小时损失概率、可交易容量和资金配置需要独立市场证据。小时收盘价与固定合成容量的 Paper 实验'
        '只覆盖本文的成交模型，资金费未模拟。现有路径依赖同一市场历史校准，模型内重复模拟无法缩小模型与校准不确定性。',
        '',
    ])
    lines.extend(_research_size_lines(groups, calibration, cost_safety_factor, minimum_tail_support))
    return '\n'.join(lines)
