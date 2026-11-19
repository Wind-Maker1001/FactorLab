"""Render the bounded MC-05-F04C-02 execution evidence for review."""
from decimal import Decimal
import json


CONDITIONAL = 'CONDITIONALLY_SELECTED_EXECUTION_CASE'


def _number(value):
    if value is None:
        return '无'
    number = Decimal(str(value))
    return format(number, 'f').rstrip('0').rstrip('.') if number % 1 else format(number, '.0f')


def _table(headers, rows):
    lines = ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |']
    lines.extend('| ' + ' | '.join(str(value).replace('|', '\\|') for value in row) + ' |' for row in rows)
    return '\n'.join(lines)


def _json(value):
    return '```json\n' + json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n```'


def _label(window):
    sample = '触价专项案例' if window['sample'] == CONDITIONAL else '独立历史窗口'
    return f"{window['id']}（{sample}）：[{window['start']}, {window['end_exclusive']}) UTC"


def _metrics(window, fields):
    a = window['accounts']['A']['metrics']
    b = window['accounts']['B']['metrics']
    rows = []
    for key, label in fields:
        rows.append((label, _number(a[key]), _number(b[key]), _number(Decimal(str(b[key])) - Decimal(str(a[key])))))
    return _table(('指标', 'A 小时收盘', 'B 分钟收盘', '配对差异 B−A'), rows)


FINANCIAL_FIELDS = (
    ('submitted_orders', '订单提交数'),
    ('fills', '确认成交事件数'),
    ('quantity', '成交数量（合约单位）'),
    ('notional', '成交名义额（USDT）'),
    ('fees', '手续费（USDT）'),
    ('slippage', '滑点成本（USDT）'),
    ('max_inventory', '最大库存（合约单位）'),
    ('average_hourly_inventory_notional', '平均小时持仓市值（USDT）'),
    ('holding_hours', '非零库存持有时间（小时）'),
    ('paper_unrealized', '期末 Paper 未实现盈亏（USDT）'),
    ('fifo_realized_net', 'FIFO 已实现净盈亏（USDT）'),
    ('fifo_unrealized_net', 'FIFO 期末残余净盈亏（USDT）'),
    ('terminal_equity', '期末权益（USDT）'),
    ('terminal_nav', '期末归一化 NAV'),
    ('max_drawdown', '共同小时收盘最大回撤（比例）'),
    ('terminal_cash', '期末 USDT 现金'),
    ('terminal_inventory', '期末残余库存（合约单位）'),
    ('average_hourly_committed', '平均小时总资金占用（USDT）'),
    ('max_hourly_committed', '最大小时总资金占用（USDT）'),
    ('max_hourly_inventory_notional', '最大小时持仓市值（USDT）'),
)


PARTICIPATION_FIELDS = (
    ('submitted_orders', '接纳订单数'),
    ('filled_orders', '至少部分成交订单数'),
    ('fill_rate', '订单参与率（至少部分成交／接纳）'),
    ('fills', '确认成交事件数'),
    ('quantity', '确认成交数量（合约单位）'),
    ('notional', '确认成交名义额（USDT）'),
    ('partial_fill_events', '部分成交事件数'),
    ('terminal_partial_orders', '期末部分成交订单数'),
    ('ttl_cancellations', 'TTL 撤销事件数'),
    ('cancellations', '最终撤销订单数'),
    ('submission_rejections', '提交拒绝次数'),
)


OBSERVATION_FIELDS = (
    ('quote_observations', '窗口内完成报价观察'),
    ('eligible_order_quotes', '有效订单 × 报价检查'),
    ('limit_touches', '收盘报价触及原始限价'),
    ('candidates', '模型可执行候选'),
    ('slippage_blocked_quotes', '滑点阻挡报价检查'),
    ('capacity_blocked_quotes', '容量阻挡报价检查'),
    ('slippage_blocked_quote_quantity', '滑点阻挡累计报价检查数量'),
    ('capacity_blocked_quote_quantity', '容量阻挡累计报价检查数量'),
)


def _financial(windows):
    lines = [
        '# MC-05-F04C-02 真实行情 Paper 库存与账务',
        '每个窗口的两账户从 1,000 USDT、零库存开始，独立记账。表中差异为 B−A，'
        'NAV 与最大回撤都在相同的小时收盘估值点比较。持有时间按实际 Paper 成交时间计算；'
        '平均持仓市值使用共同小时标记。手续费为 4 bps，方向性滑点为 6 bps，资金费为 `NOT_SIMULATED`。',
    ]
    for window in windows:
        lines.extend(['## ' + _label(window), _metrics(window, FINANCIAL_FIELDS)])
        a, b = (window['accounts'][arm]['metrics'] for arm in ('A', 'B'))
        delta = lambda key: Decimal(str(b[key])) - Decimal(str(a[key]))
        lines.append(
            f"本窗 B−A 的成交事件差为 {_number(delta('fills'))}、成交数量差为 {_number(delta('quantity'))}，"
            f"期末权益差为 {_number(delta('terminal_equity'))} USDT。"
            f"分钟执行使非零库存持有时间变化 {_number(delta('holding_hours'))} 小时，"
            f"平均小时持仓市值变化 {_number(delta('average_hourly_inventory_notional'))} USDT，"
            f"最大库存变化 {_number(delta('max_inventory'))} 单位。"
            f"手续费差为 {_number(delta('fees'))} USDT，滑点成本差为 {_number(delta('slippage'))} USDT。")
        reverse_counts = [sum(len(order['reverse_orders']) for order in
                             window['execution_cases']['arms'][arm]['orders'].values()) for arm in ('A', 'B')]
        lines.append(f"已生成的相邻反向订单关联数为 A={reverse_counts[0]}、B={reverse_counts[1]}；"
                     '每张订单的提交时间在执行案例中列出。反向单留到小时阶段生成，'
                     '先成交不会触发额外的分钟策略决策。')
        capacity = [window['accounts'][arm]['observations']['capacity_blocked_quotes'] for arm in ('A', 'B')]
        lines.append(f"容量阻挡报价检查 A={capacity[0]}、B={capacity[1]}。"
                     '这些阻挡直接来自固定的每小时 0.10 单位容量。成交价格与时间差先由报价观察产生；'
                     '本轮保持容量相同，未把这组差异归因于真实市场可用深度。')
        terminal = [window['execution_cases']['arms'][arm]['terminal_account'] for arm in ('A', 'B')]
        reserve_fields = (
            ('pending_reservations', '期末挂单资金预留（USDT）'),
            ('position_collateral', '期末持仓抵押占用（USDT）'),
            ('committed', '期末总资金占用（USDT）'),
            ('available', '期末可用资金（USDT）'),
        )
        lines.append(_table(('资金类别', 'A 小时收盘', 'B 分钟收盘', 'B−A'),
            [(label, _number(terminal[0][key]), _number(terminal[1][key]),
              _number(Decimal(str(terminal[1][key])) - Decimal(str(terminal[0][key]))))
             for key, label in reserve_fields]))
        lines.append('挂单资金预留与持仓抵押分别来自原生 Paper 账户投影；'
                     '持仓市值按期末和小时收盘标记单列，避免把订单资金占用解释为实际价格敞口。')
        a, b = (window['accounts'][arm]['metrics'] for arm in ('A', 'B'))
        if b['fills'] > a['fills']:
            relation = '下降' if Decimal(str(b['terminal_nav'])) < Decimal(str(a['terminal_nav'])) else '上升或持平'
            lines.append(f'B 的确认成交事件多于 A，期末 NAV 相对 A {relation}。'
                         '额外成交同时改变库存持有时段、交易成本和终值估值，成交数增加本身不构成策略改进。')
        else:
            lines.append('成交事件数与终值需要同时阅读：相同事件数仍可能在成交时点、价格、'
                         '反向订单和库存持有时段上产生差异。')
        for arm in ('A', 'B'):
            account = window['accounts'][arm]
            metrics = account['metrics']
            exposure = '空仓' if Decimal(str(metrics['terminal_inventory'])) == 0 else '保留库存'
            participation = '零成交账户' if metrics['fills'] == 0 else '非零成交账户'
            lines.append(f"**{arm}：{participation}，期末{exposure}。** "
                         f"期末未完成订单及剩余数量：`{json.dumps(metrics['terminal_open_orders'], ensure_ascii=False, default=str)}`。")
            exits = {key: dict(status=order['final_status'], filled=order['filled_quantity'], remaining=order['remaining_quantity'])
                     for key, order in account['orders'].items() if order['purpose'] == 'RISK_EXIT'}
            lines.append('风险退出订单：' + ('无。' if not exits else _json(exits)))
            lines.append('小时风险事件：' + ('无。' if not account['risk'] else _json(account['risk'])))
            residuals = account['reconciliation']['maximum_absolute_residual']
            rows = [(key, _number(value)) for key, value in residuals.items()]
            lines.append(f"账务核验 `{account['reconciliation']['status']}`，容差 `{account['reconciliation']['tolerance']}`。"
                         '从有序 Paper 事件重算现金流、库存、手续费和小时权益，再与 FIFO 及权威账户对账。')
            lines.append(_table(('对账项', '最大绝对残差'), rows))
    lines.append('滑点成本是实际成交价与该成交参考报价的差额，已经进入执行价格；'
                 '解释净损益时不再次扣减。FIFO 已实现净盈亏含被卖出批次的买卖费用，'
                 '残余批次保留其买入成本。Paper 的持仓成本与 FIFO 分批成本采用不同表示，'
                 '二者通过期末权益对账连接。')
    return '\n\n'.join(lines) + '\n'


def _participation(windows):
    lines = [
        '# MC-05-F04C-02 成交参与与容量约束',
        '两组每个小时共同适用 0.10 合约单位的账户内容量上限。A 在小时收盘观察一次，'
        'B 用同一小时的分钟收盘依次消耗剩余额度。Grid 只在小时决策阶段生成提案和反向订单，'
        'TTL 保持 100 次小时观察。报价候选经过 Paper 经济准入后才成为确认成交。',
    ]
    for window in windows:
        lines.extend(['## ' + _label(window), _metrics(window, PARTICIPATION_FIELDS)])
        observations = [window['accounts'][arm]['observations'] for arm in ('A', 'B')]
        lines.append(_table(('报价诊断', 'A 小时收盘', 'B 分钟收盘'),
                            [(label, _number(observations[0][key]), _number(observations[1][key]))
                             for key, label in OBSERVATION_FIELDS]))
        diagnostic = observations[0]
        lines.append(f"C 在 A 的有效订单路径上记录 {diagnostic['ohlc_order_minute_touches']} 个原始极值触价，"
                     f"其中小时收盘遗漏 {diagnostic['missed_order_minutes']} 个订单×分钟、"
                     f"{diagnostic['missed_order_hours']} 个订单×小时，涉及 "
                     f"{diagnostic['distinct_orders_with_missed_touches']} 张订单。")
        pairing = window['order_pairing']
        lines.append(f"按相同订单起源配对 {pairing['matched_orders']} 张订单；"
                     f"成交轨迹不同 {pairing['order_trajectories_with_different_fills']} 条，"
                     f"A 专有订单 {pairing['hourly_only_orders']} 张，B 专有订单 {pairing['minute_only_orders']} 张。"
                     '完整订单和第一处分歧见 [EXECUTION_CASES.md](EXECUTION_CASES.md)。')
    lines.append('阻挡数量按“订单 × 报价检查”累加，同一剩余数量可在多个分钟被重复检查，'
                 '因此不能把它解释为应得而未得的成交量。容量是冻结的合成假设，'
                 '小时内先触发的订单可能消耗后续订单的额度；本轮没有改变容量做参数敏感性搜索。')
    return '\n\n'.join(lines) + '\n'


def _coverage(audit, windows):
    lines = [
        '# MC-05-F04C-02 行情覆盖与准入',
        '窗口选择先于分钟采集和账户回放。独立窗口按 UTC 时间顺序固定，'
        '专项案例只按预注册的初始网格、有效期与小时高低价筛查；选择记录不读取收益或回撤。'
        '候选全表保留未选中和数据拒绝的窗口。',
    ]
    outcomes = {window['id']: window for window in audit['windows']}
    rows = []
    for candidate in audit['candidates']:
        outcome = outcomes.get(candidate['id'])
        state = outcome['status'] if outcome is not None else 'NOT_SELECTED'
        rows.append((candidate['id'], candidate['sample'], candidate['start'], candidate['end_exclusive'],
                     candidate['anchor_time'], _number(candidate['anchor_close']), candidate['selected'], state))
    lines.append(_table(('候选', '样本类型', '开始 UTC', '结束 UTC（不含）', '锚点小时起点 UTC',
                         '锚点收盘价', '选中', '数据状态'), rows))
    for candidate in audit['candidates']:
        if candidate['screening'] is not None:
            lines.extend(['### ' + candidate['id'] + ' 小时筛查', _json(candidate['screening'])])
    for window in audit['windows']:
        if window['status'] == 'MARKET_DATA_BLOCKED':
            lines.extend(['## ' + _label(window), '数据拒绝原因：' + window['rejection']])
    for window in windows:
        data = window['data']
        lines.extend(['## ' + _label(window),
            f"共 {data['minute_rows']} 根分钟线、{data['hourly_rows']} 根研究小时线、{data['daily_rows']} 根独立日线，"
            f"另有前置小时 `{data['seed']}`。锚点收盘为 {_number(window['anchor_close'])} USDT。",
            _table(('核对', 'OHLCV 一致桶数', '辅助字段差异'), (
                ('60 分钟 → 认证小时', data['minute_to_hour']['ohlcv_matches'],
                 json.dumps(data['minute_to_hour']['auxiliary_mismatches'], ensure_ascii=False)),
                ('24 小时 → 独立日线', data['hour_to_day']['ohlcv_matches'],
                 json.dumps(data['hour_to_day']['auxiliary_mismatches'], ensure_ascii=False)),
            )),
            f"R04B data version：`{data['hourly_source']['data_version']}`；"
            f"bars SHA-256：`{data['hourly_source']['bars_sha256']}`；"
            f"取得时间：`{data['hourly_source']['acquired_at']}`。",
            '实际引用的原始小时对象：',
            _table(('官方 URL', 'SHA-256', '取得时间 UTC'),
                   [(f"[小时 ZIP]({row['url']})", row['sha256'], row['acquired_at'])
                    for row in data['hourly_source']['raw_receipts']]),
            '分钟和日线来源记录（包括官方 CHECKSUM）：',
            _table(('对象', 'SHA-256', '字节数', '取得时间 UTC'),
                   [(f"[{row['name']}]({row['url']})", row['sha256'], row['bytes'], row['fetched_at'])
                    for row in data['archive_requests']]),
        ])
        if data['minute_to_hour']['examples'] or data['hour_to_day']['examples']:
            lines.append('辅助字段差异示例及语义记录：' + _json(dict(
                minute_to_hour=data['minute_to_hour']['examples'], hour_to_day=data['hour_to_day']['examples'])))
    lines.append('分钟数据按原生 UTC 顺序校验完整、闭合、无重复和缺桶；不补值。'
                 '价格 OHLC 与 base volume 精确比较，quote volume、交易笔数及主动买入字段按归档语义记录差异。'
                 '原始压缩行情直接读取，保留在仓库外；[AUDIT.json](AUDIT.json) 保存各对象 URL、摘要、'
                 '取得时点、长度、服务器时间与数据版本。原 F04C 窗口和证据保持冻结。')
    return '\n\n'.join(lines) + '\n'


def _compact(value):
    if value is None:
        return '无'
    if 'minute' in value:
        return (f"{value['minute']}；low={value['low']}，high={value['high']}，"
                f"close={value['close']}")
    return json.dumps(value, ensure_ascii=False, default=str, separators=(',', ':'))


def _cases(windows):
    lines = [
        '# MC-05-F04C-02 真实价格触及与执行案例',
        '这里依次区分真实分钟 OHLC 的价格触及、冻结滑点下的可执行收盘报价、Paper 确认成交。'
        'C 不记账，极值与收盘资格均仅在订单生效至终结的区间内计算。'
        '完整分钟触价列表和逐笔账户路径保存在 [AUDIT.json](AUDIT.json) 的 `execution_cases`。',
    ]
    for window in windows:
        cases = window['execution_cases']
        lines.append('## ' + _label(window))
        if window['sample'] == CONDITIONAL:
            lines.append('`CONDITIONALLY_SELECTED_EXECUTION_CASE`。这个样本按预先冻结的小时触价条件选出，'
                         '用来验证触价与账户路径；不纳入独立窗口的成交发生次数统计。')
        divergence = cases['first_divergence']
        if divergence is None:
            lines.append('A/B 没有订单或经济状态的首处分歧。即使成交相同，非零账务案例仍完整保留。')
        else:
            event = divergence['event']
            minute = divergence['minute_bar']
            hourly = divergence['hour_bar']
            identity = event['identity']
            lines.append(
                f"最早分歧在 `{event['event_time']}` UTC，对应 `{minute['time']}` 分钟。"
                f"共同订单限价为 {identity['limit_price']}，数量为 {identity['quantity']}，"
                f"提交时间为 `{identity['submitted_at']}`。该分钟 low={minute['low']}、"
                f"high={minute['high']}、close={minute['close']}，成交笔数为 {minute['trade_count']}；"
                f"对应小时收盘为 {hourly['close']}。"
                f"B 的事件为 `{_compact(event['minute'])}`，A 对应事件为 `{_compact(event['hourly'])}`。"
                '两账户在分歧前的余额一致；B 确认后即时持有库存并支付费用，'
                'A 在该时刻仍挂单，等待小时收盘。')
            lines.extend(['### 第一处分歧',
                '以下事件给出最早差异的 UTC 时间、对应真实分钟与小时收盘，以及两账户当时的有效订单、'
                '资金和库存。分钟先达到可执行报价时，B 可以先确认成交；A 继续等待小时收盘。'
                '成交立即改变 Paper 余额和库存，反向提案与策略风险仍在既定小时决策处理。',
                _json(divergence)])
        for arm in ('A', 'B'):
            evidence = cases['arms'][arm]
            lines.extend(['### ' + arm + ' 订单与触价', _json(evidence['summary'])])
            rows = []
            for order_id, order in evidence['orders'].items():
                rows.append((order_id, order['side'], _number(order['quantity']), _number(order['limit_price']),
                             _number(order['reservation_price']),
                             order['submitted_at'], order['effective_at'], order['eligible_until'],
                             _compact(order['first_raw_ohlc_touch']), _compact(order['first_slippage_ohlc_touch']),
                             _compact(order['first_raw_close']), _compact(order['first_slippage_close']),
                             order['final_status']))
            lines.append(_table(('订单', '方向', '数量', '限价', 'Paper 原始预留价', '提交 UTC', '生效 UTC', '有效至 UTC',
                                 '首次原始极值触价', '首次滑点极值触价', '首次原始分钟收盘触价',
                                 '首次滑点分钟收盘触价', '最终状态'), rows))
            for order_id, order in evidence['orders'].items():
                if not order['confirmed_fills']:
                    continue
                source = window['accounts'][arm]['orders'][order_id]
                cumulative = Decimal(0)
                rows = []
                for event in source['events']:
                    cumulative += Decimal(str(event['filled_quantity']))
                    rows.append((event['event_time'], event['status'], _number(event['fill_price']),
                                 _number(event['filled_quantity']), _number(cumulative), _number(event['fee'])))
                lines.extend([f'**{order_id}：确认成交、部分成交和终结事件**',
                    _table(('时间 UTC', '状态', '成交价格', '本次数量', '累计成交', '本次费用'), rows),
                    '模型报价资格：' + _compact(order['first_model_price_eligible_quote']),
                    '后续反向订单：' + _compact(order['reverse_orders'])])
            lines.append('从首笔成交开始的逐笔 Paper 现金、库存和费用确认路径见 '
                         f"[AUDIT.json](AUDIT.json) 的 `{window['id']}.execution_cases.arms.{arm}.first_fill_path`。"
                         '同分钟成交笔数和主动买入数量来自已取得的真实分钟字段，'
                         '不据此推断一分钟内完整成交顺序。')
    lines.append('交易所实际成交能力还需要对应历史时刻的最优买卖报价、可分配深度、'
                 '逐笔顺序、订单到达与撤销确认、队列位置及实际费用和规则。'
                 '公开逐笔记录可细化价格和数量证据，无法单独恢复本订单的排队位置。'
                 '本轮的三类研究证据均保留 `REAL_FILL_NOT_VERIFIED`。')
    return '\n\n'.join(lines) + '\n'


def _report(audit, windows):
    counts = audit['independent_counts']
    nonzero = any(window['accounts'][arm]['metrics']['fills'] for window in windows for arm in ('A', 'B'))
    changed = any(window['order_pairing']['order_trajectories_with_different_fills'] for window in windows)
    lines = [
        '# MC-05-F04C-02 真实行情非零成交与多粒度复验',
        ('已观察到真实历史价格触发的非零 Paper 成交。' if nonzero else
         '全部已准入的预定窗口中均未观察到非零 Paper 成交，保留这一正式结果。')
        + ('分钟报价观察改变了配对账户的成交轨迹，具体库存和 NAV 差异逐窗报告。' if changed else
           '本轮未观察到分钟报价观察改变成交轨迹。')
        + '研究从先前冻结的 F04C 执行规则出发，使用独立历史窗口与小时触价专项案例两组样本。',
        '## 独立窗口结果',
        _table(('计数', '全部预定独立窗口'), (
            ('事前预定窗口', counts['predeclared_windows']),
            ('行情准入窗口', counts['accepted_windows']),
            ('A 有效订单路径有真实极值触价窗口', counts['touch_windows']),
            ('产生 Paper 成交窗口', counts['paper_fill_windows']),
            ('A/B 成交轨迹不同窗口', counts['different_fill_windows']),
            ('期末 NAV 不同窗口', counts['different_nav_windows']),
        )),
        '每个独立窗口都保留，窗口选择未参考最终 NAV、回撤或执行差异。'
        '窗口数量不足以估计可靠的市场成交发生概率、VaR 或 CVaR。专项案例采用 '
        '`CONDITIONALLY_SELECTED_EXECUTION_CASE` 标记，只验证触价与账户路径。',
        '## 逐窗结果',
    ]
    rows = []
    for window in windows:
        a, b = (window['accounts'][arm]['metrics'] for arm in ('A', 'B'))
        rows.append((window['id'], window['sample'], a['fills'], b['fills'], _number(a['quantity']),
                     _number(b['quantity']), _number(a['terminal_nav']), _number(b['terminal_nav']),
                     _number(Decimal(str(b['terminal_nav'])) - Decimal(str(a['terminal_nav'])))))
    lines.append(_table(('窗口', '样本类型', 'A 成交', 'B 成交', 'A 数量', 'B 数量', 'A NAV', 'B NAV', 'NAV B−A'), rows))
    lines.extend([
        '分钟观察增加报价检查机会，同时可能更早持仓、消耗小时合成容量并改变后续反向单。'
        '额外手续费与滑点由实际确认成交计算，不能以成交率较高代替风险与净值评价。'
        '逐窗成本、库存、回撤、现金与 FIFO 见 [FINANCIAL.md](FINANCIAL.md)；'
        '首处分歧和真实订单时序见 [EXECUTION_CASES.md](EXECUTION_CASES.md)。',
        '本轮没有出现“更多最终成交笔数”的窗口：前两个独立窗口的成交数量相同，但成交时间和价格不同。'
        'I1 的分钟组更早承担库存，期末权益较小时组下降；I2 的分钟组更早成交且期末权益略高。'
        '因此本轮确认了账户路径对报价粒度敏感，未得到分钟观察必然增加最终成交量或改善净值的结论。',
        '\n'.join(f"窗口 {window['id']} [{window['start']}, {window['end_exclusive']}) "
                  f"拒绝准入：{window['rejection']}。保留 MARKET_DATA_BLOCKED，未替换候选。"
                  for window in audit['windows'] if window['status'] == 'MARKET_DATA_BLOCKED'),
        '## 工程与复现',
        '复用 `ResearchExecution`、既有官方归档准入、Grid、Paper、FIFO 和 NAV 审计。'
        '新增研究代码只调度预定窗口、筛查初始有效网格、整理价格与订单证据并生成报告。'
        '两组都每小时决策；B 在后续已完成分钟报价上撮合，反向单留到小时阶段。'
        '每小时容量保持 0.10，TTL 保持 100，资金费为 `NOT_SIMULATED`。',
        '同输入逐窗重复执行及冻结 F04C 对照：',
        _table(('窗口', '首次结果 SHA-256', '重复结果 SHA-256'),
               [(window['id'], window['result_sha256'], window['repeat_result_sha256']) for window in windows]),
        _json(audit['frozen_regression']),
        f"经济结果集合 SHA-256 为 `{audit['economic_results_sha256']}`。"
        f"包含执行案例的稳定审计 SHA-256 为 `{audit['result_sha256']}`。",
        '定向验证：', _json(audit['validation']),
        '资源实测：', _json(audit['resources']),
        '归档采集统计：',
        _json({key: value for key, value in audit['acquisition'].items() if key != 'head_requests'}),
        '每个正式窗口只有 A/B 两个账户；确定性复跑与旧窗口回归另列为验证账户，'
        '不增加正式样本或研究窗口。原始行情以压缩对象复用，不展开为重复 JSON 或 SQLite；'
        'SQLite 只用于小型 Paper 账户。',
        '离线复现（从仓库根目录执行；参数目录分别指向 R04B 快照、本轮压缩归档、原 F04C 压缩归档和新的输出目录）：',
        '```powershell\n$env:PYTHONPATH = "src"\npython tools/research/run_mc05_f04c_02.py '
        '--hourly-snapshot <R04B_SNAPSHOT> --archive-root <ARCHIVE_ROOT> '
        '--legacy-archive-root <F04C_ARCHIVE_ROOT> --accounts-root <NEW_ACCOUNT_ROOT> '
        '--output <NEW_REPORT_ROOT>\n```',
        '已有账户时，使用 `--resume` 并将 `--accounts-root` 指向缓存目录，从原生 PaperStore 重新审计。'
        '已有探测凭证可通过 `--probe-audit <PRIOR_AUDIT_JSON>` 复用。'
        '离线运行不发出 HTTP HEAD 或 GET，缺少历史凭证时记录 `NOT_RECHECKED_OFFLINE`。',
        '## 历史更细证据',
        _json(audit['finer_evidence']),
        '已有分钟线提供成交笔数和主动买入数量，能够说明该分钟内存在公开成交。'
        '对应日期的逐笔归档可用于进一步辨认价格顺序；历史买卖价、盘口深度、'
        '队列与订单确认共同决定实际订单是否成交。当前盘口不替代历史盘口。'
        '本轮结果验证的是冻结报价、滑点与容量下的 Paper 执行和账务，'
        '不构成一般收益结论。',
        '研究状态：', '\n'.join('- `' + state + '`' for state in audit['states']),
    ])
    return '\n\n'.join(lines) + '\n'


def render_reports(output, audit):
    """Write all five review documents from the same recorded study evidence."""
    windows = [window for window in audit['windows'] if window['status'] == 'MARKET_DATA_ACCEPTED']
    documents = {
        'DATA_COVERAGE.md': _coverage(audit, windows),
        'EXECUTION_CASES.md': _cases(windows),
        'PARTICIPATION.md': _participation(windows),
        'FINANCIAL.md': _financial(windows),
        'REPORT.md': _report(audit, windows),
    }
    for filename, content in documents.items():
        (output / filename).write_text(content, encoding='utf-8')
