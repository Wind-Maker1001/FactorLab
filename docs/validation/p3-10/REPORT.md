# P3-10 实际持仓反馈与动态换手约束

基线：`e1ebe0dec9eb0b585972565167eff0c29a7f1d62`。

结论：**P310_ENGINEERING_ACCEPTED**。

最终定向测试：**74 passed（2.08s）**；确定性示例执行成功，内置数值断言全部通过。首次回归为 64 passed。扩展测试最初有一处 NaN 成交 score 的字典相等断言失败，改用 pandas 缺失值语义比较后通过；示例最初因严格 JSON 不接受 NaN 而退出，已将缺失 score 输出为 JSON null。原始失败证据分别保留在 `extended-tests-initial.txt` 和 `demo-initial.txt`。

## 接口与账本

`AShareExecutionBacktester.start_session(bars, benchmark, signal_date=...)` 创建收盘到下一开盘的前向会话；`snapshot` 读取不可变快照，`submit_targets()` 排入下一交易日，`advance()` 只处理下一交易日。批量与增量路径共用 `_iterate_selected()` 和 `_execute_order()`，没有历史回放或第二套账本、撮合、费用系统。

`ExecutionFeedback(session, optimizer).optimize(input)` 将快照证券权重注入 `previous_weights`。不允许调用者同时传入上期目标或其他 previous_weights。`FeedbackDecision` 留存优化前快照、信号/决策/下一执行时点与优化结果。

快照分别提供 `last_targets`、`last_fills`、`holdings.shares`、`holdings.sellable_shares`，以及现金、NAV、现金权重、估值来源时间、待执行目标与拒单。当日 T+1 不可售量不被误当作次日不可售量；次日可售量由同一 FIFO 账本查询。

## 换手与不能成交的目标

优化器 gross security turnover = 所有证券（含退出目标域的旧仓）的 `sum(abs(target_weight - actual_weight))`，不乘 1/2，不另加现金权重变化。证券权重相对含现金的 NAV；目标现金为 `1 - investment`，实际现金为 `cash / NAV`。

会话结果 `nav.gross_security_turnover` = `(buy_notional + sell_notional) / pretrade_NAV`。旧 `nav.turnover` 仍为 `max(buy_notional, sell_notional) / pretrade_NAV`。`optimizer_turnover` 独立保留优化预期值。价格漂移、拒单、费用及取整可使实际换手和目标不同；这里的限制是决策权重约束，不承诺未来成交价格下的金额换手必然等于预期。

`SaleRestriction` 必须给出已知时点、股数、覆盖日期和原因；符合下一执行日的锁仓量进入优化器 `minimum_weights`，并将同一禁售股数传入原执行器。即使下一开盘价格漂移导致目标股数降低，也不出售被锁股份；可售余量不足一个合法交易单位时不成交，记录 `sale_restriction`。被锁旧仓在目标域外、预算/权重上限冲突或换手不可行时直接失败，不放松条件。当前停牌不自动推断未来继续停牌。执行日才揭示的停牌、涨跌停和资金不足由现有执行器拒单，差异见 `target_comparison`。

## 时间与公司行为边界

会话时间使用无时区的 A 股交易所本地时间：收盘 15:00、开盘及成交入账 09:30。决策必须等于当前快照收盘；信号和风险输入不得晚于决策；拒绝混用有时区时间。`advance()` 以前不会读取下一交易日成交结果进入快照。估值缺失时沿用已有引擎可知历史 mark，快照保留原始估值时间。

现有 DividendExecutionLedger 需要证券级已验证事件计划和生命周期挂接，不能仅凭行情推导。本接口不接入无证据事件，不制造任何公司行为；`corporate_actions_status=UNKNOWN`。`require_verified_actions=True` 会阻断优化；证据合格公司行为接入保留 TODO。

## 研究流水线最小接入

不修改 P3-07B 编排器。其决策日期循环只需：一次创建 session；以 `session.snapshot.as_of` 构造风险及信号输入；调用 feedback.optimize；调用 feedback.advance 处理下一交易日；下一轮再次读取 snapshot。无信号日仅 advance。成交、拒单、目标偏差和 NAV 从 session.result 获取；该属性按需组装输出，不在每日推进时复制整个历史。

```python
session = executor.start_session(bars, benchmark, signal_date=first_signal_date)
feedback = ExecutionFeedback(session, optimizer)
# 每个决策日，以当前 snapshot.as_of 创建 OptimizationInput：
decision = feedback.optimize(optimization_input, signal_time=signal_observed_at)
snapshot = feedback.advance()
```

## 确定性结果与覆盖

示例初始资金 10,000 元，A/B 价格 10/11 元，每笔佣金 5 元；两次目标投资比例均为 50%，换手上限均为 50%。

| 决策与随后执行 | 优化输入实际权重 A/B | 目标权重 A/B | 实际持股 A/B | 现金 | NAV | 预计 gross 换手 | 实际 gross 换手 |
|---|---|---|---|---:|---:|---:|---:|
| 01-02 收盘 → 01-03 开盘 | 0 / 0 | .25 / .25 | 200 / 200 | 5,790 | 9,990 | .5 | .42 |
| 01-03 收盘 → 01-04 开盘 | .2002002002 / .2202202202 | 0 / .5 | 0 / 400 | 5,580 | 9,980 | .4799799800 | .4204204204 |

第二次换手不是按上期目标 .25/.25 得出的 .5，而是按真实持仓得到 .47997998。两期 NAV 均等于现金加实际持仓市值。

定向案例覆盖：

- 空仓建仓、连续两期及目标/实际权重差异：`test_empty_then_two_decisions_use_actual_not_target`。
- 部分成交、完全拒买、费用资金差：`test_partial_cash_fill_and_next_turnover_uses_fill`、`test_rejected_first_buy_stays_all_cash`。
- T+1 同日禁止出售：原执行器 `test_position_book_enforces_same_day_t_plus_one`、`test_executor_rejects_same_day_sell`；反馈案例同时断言收盘不可售、次日可售。
- 停牌无法退出旧仓并导致新买资金不足：`test_unforeseen_suspension_and_cash_rejection_are_not_relaxed`。
- 整手与禁售余股：`test_known_share_lock_survives_next_open_price_drift` 分别覆盖 450/500 股被锁，避免执行不足整手的 50 股。
- 换手 .5 恰好可行、.499 不可行：`test_turnover_boundary_and_infeasibility`。
- 已知锁仓导致基线目标不可行、风险目标有下界、锁仓域外不可行：`test_known_locked_holding_is_solver_floor`、`test_locked_outside_universe_is_infeasible`。
- 有真实持仓的下一期决策不受未来成交价变化影响：`test_future_fill_does_not_change_real_holdings_second_decision`；另覆盖首次建仓、未来信号/风险/禁售证据及混合时区输入。
- 逐期现金、持仓、NAV 和 gross 成交金额对账：`assert_reconciled`。
- 旧目标权重 API 与增量路径的 NAV、成交、持仓、拒单、信号及偏差逐字段一致：`test_session_and_legacy_batch_exact_execution_parity`；同时运行原优化器、目标执行和 Top-K 执行回归。

## 复现

在本工作树根目录，以指定 Python 并设置 `PYTHONPATH=src`：

```sh
D:/Environments/factorlab-py313/Scripts/python.exe -m pytest tests/research/test_execution_feedback.py tests/research/test_portfolio_execution.py tests/research/test_portfolio_optimizer.py tests/research/test_execution_backtest_phase3.py -q
D:/Environments/factorlab-py313/Scripts/python.exe examples/research/execution_feedback_synthetic_demo.py
```

证据：`pytest.txt`、`numerical.json`。示例内置确定性数值断言。

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED
