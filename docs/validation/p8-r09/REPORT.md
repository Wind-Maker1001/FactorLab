# FactorLab P8-R09：F11/F08 完整离线执行与尾部风险研究报告

> `NO LIVE MONEY`。本研究未触碰 Azure 在线交易实例，未修改线上授权与订单状态，未覆盖已冻结的 R08 原始证据（`docs/validation/p8-r08/` 保持不变）。小时 OHLC 收盘价与合成路径均非真实交易所盘口；所有离线成交情景均显式标记为条件假设。

## 1. 结论分级判定（严格区分工程、回放、条件风险与实证边界）

| 结论标签 | 状态 | 判定依据与严格边界 |
| --- | --- | --- |
| `ENGINEERING_ACCEPTED` | **PASS / CONFIRMED** | 已在既有 GridExecution、PaperBroker、TradingController、PaperStore 与 grid_fifo 框架上补齐 F11/F08/F10 离线生命周期，并汇总本次重跑的官方重放与各研究路径 FIFO 对账结果。 |
| `LIFECYCLE_REPLAY_VERIFIED` | **PASS / CONFIRMED** | 已在可观测范围内完整重建官方 Binance Demo Gen 1 → F11 平仓 → F08 轮换 → Gen 2 生命周期，期末权益 5000.00551376 USDT、FIFO 对账残差 0.00000000 USDT，并核实精确 BREAKOUT Bar。 |
| `CONDITIONAL_RISK_ESTIMATED` | **PASS / CONFIRMED** | 在 5 组显式条件执行假设与 3 档资金费情景下，完成同路径配对 Model A vs B 的 VaR95、CVaR95、最大回撤、持仓时长、退出延迟、尾部残仓、资金费侵蚀与轮换成功率估计。 |
| `EMPIRICAL_TAIL_RISK_UNVERIFIED` | **CONFIRMED (UNVERIFIED_EMPIRICALLY)** | 线上真实交易所尾部风险仍未获实证验证：官方 Demo 仅含 7.3 天与 1 笔平仓闭环，缺少崩盘期真实 L2 盘口深度、排队成交延迟分布与下行 EXIT_MIN_NOTIONAL / 撤单竞态实证。 |

## 2. R09-01 代码核查：线上 F11/F08/F10 与离线模型差异及补齐

核查 `GridExecution`、`DemoGridBridge`、`GridSupervisor`、`PaperBroker`、`FuturesAdapter`、`grid_fifo`、`MonteCarloRunner` 与 R08 研究脚本后，识别并最小化补齐以下真实离线缺口（不重构已有正确模块，不引入第二套交易引擎）：

| 缺口维度 | R08 旧离线模型 (`grid_paper.py` / `demo_grid_performance.py`) | 线上 Demo 完整语义 (PR #223/#224/#225) | R09 最小补齐实现 |
| --- | --- | --- | --- |
| F11 突破退出命令 | `GridExecution` 仅支持外部 `propose_limit_exit`/`cancel_exit`，Paper 路径缺失；`_admit_paper_fill` 仅豁免 `MARKET` 平仓 | `BREAKOUT` 后禁 `BUY`，先撤在途买单，按新鲜 `bidPrice` 提 reduce-only `SELL LIMIT GTC`，支持有界撤改（180s/最多 5 次）、防碎仓、`PERCENT_PRICE` 预检 | `GridExecution` 新增 `propose_limit_exit_paper`、`cancel_exit_paper`、`withdraw_unsubmitted_paper` 及 `_admit_paper_fill` 对 F11 限价退出清算豁免 |
| 撤单故障与 `UNKNOWN` 恢复 | Paper 回放无 `UNKNOWN -> EXIT_RECONCILING -> reconcile` 闭环 | 写超时标 `UNKNOWN`，严禁替代下单直至 GET 对账恢复；已消费 `DELETE` 防重复撤单 | `GridExecution` 新增 `mark_unknown_paper`、`reconcile_paper`；`grid_f11_f08_offline.py` 接入 `GAP_AND_CANCEL_FAULT` 故障注入与恢复 |
| 持仓期间资金费结算 | 仅在 `daily.csv` 事后叠加（`_paper_funding_stress`），不进入 `PaperBroker` 账本与风控 | F10 通过 `FundingEvent` 写入 `PaperBroker.apply_funding`，直接影响钱包、权益、`max_loss` 熔断与 FIFO | `GridExecution` 新增 `apply_funding_paper`，全量资金费作为一等 `FundingEvent` 写入共享 `PaperStore`，由 `grid_fifo.attribute_events` 零残差对账 |
| F08 平仓后自动轮换 | `GridExecution._paper_command` 仅允许单代部署；突破后永久 `PAUSED` 保留残仓 | 确认平仓、零挂单、零 `UNKNOWN`、订单全终态且预算充足时，在共享账本上创建 `g-0000N` 并继续执行 | `GridExecution` 新增 fail-closed `rotate_paper`，校验平仓/终态/对账后在同账户 `PaperStore` 上原子交接至下一代并记录 `grid_rotation_history` |
| Demo 硬护栏与规则约束 | 离线未建模 `MIN_NOTIONAL=5`、`MAX_NOTIONAL=7`、`PERCENT_PRICE=[0.97,1.03]`、`max_loss=1U`、`total_orders=100` | 价格低于 `125` 或部分成交碎仓触发 `EXIT_MIN_NOTIONAL`；亏达 `1 USDT` 触发 `HALTED`；撤改超 5 次触发 `EXIT_RETRY_LIMIT_EXCEEDED` | `DemoGuardrails` 完整复现全部规则与预算约束，与线上 `DemoGridBridge._plan_limit_exit` / `DemoRuntime` 对齐 |

- Grid 与 F11 共用 Campaign POST 窗口。默认窗口允许 `4` 次提交，持续 `10800` 秒。F08 轮换继承窗口用量，窗口到期后自动续开，Campaign 累计订单数与名义金额延续。
- F11 即时退出用合成 bid 计算一次不利滑点，再向下取整得到可成交的下单限价。成交价使用当时经滑点调整的 bid。挂单只有在经滑点调整的 bid 达到原限价后才可成交，成交价始终不低于该限价。
- `constant_hourly_bps` 按两个收盘时点之间的实际持仓时长折算小时费率，通过 `FundingEvent` 入账。`historical_events` 使用原始资金费事件，官方重放使用冻结成交与资金费记录。

## 3. R09-02 官方 Demo 实证生命周期离线重建与对账（`LIFECYCLE_REPLAY_VERIFIED`）

基于只读 R08 官方快照，将连续 Grid 家族 `fl-d7345f8795464a65-g-00001`（`1284` 根 1m K 线）→ F11 平仓 → F08 轮换 → `fl-d7345f8795464a65-g-00002`（`20` 根 1m K 线）通过 `GridExecution` + `PaperBroker` + `TradingController` + `PaperStore` + `grid_fifo.attribute_events` 完整离线重放与对账：

| 核验维度 | 离线重建结果 | 官方 Demo 账本记录 | 对账状态 |
| --- | --- | --- | --- |
| Gen 1 期初钱包 (USDT) | `4999.97114760` | `4999.97114760` | `EXACT_MATCH` |
| Gen 1 买单分笔成交 | `0.03 + 0.01 @ 129.59` (`fee=0`) | `0.03 + 0.01 @ 129.59` (`Maker=True`) | `True` |
| 控制器实际 `BREAKOUT` 触发 Bar | `2026-11-22T05:20:00+00:00` (`close=129.54 < 129.59`) | `controller.sqlite3 command:624` (`2026-11-22T05:20:00Z`) | `True` |
| 后续首次突破上界 Bar | `2026-11-22T08:42:00+00:00` (`close=132.36 > 132.21`) | R08 记录上行越界时点 | `CONSISTENT` |
| 持仓期间 8 笔整点资金费 (USDT) | `-0.09763384` (`8` 笔) | `-0.09763384` (`8` 笔) | `True` |
| F11 reduce-only 限价平仓毛利 (USDT) | `0.1320000` (`0.04 @ 132.89`) | `0.13200000` | `True` |
| Grid 净收益 (USDT) | `0.03436616` | `0.03436616` | `True` |
| 期末钱包与账户权益 (USDT) | `5000.00551376` | `5000.00551376` (差值 `0E-8`) | `True` |
| 权威 FIFO 对账残差 (USDT) | `0E-8` | `0.00000000` | `True` |
| Gen 1 终态 & F08 轮换至 Gen 2 | `EXITED/BREAKOUT` → `fl-d7345f8795464a65-g-00002-00000001` (`BUY 0.04 @ 131.56000 SUBMITTED`) | `EXITED/BREAKOUT` → `g-00002-00000001` (`131.56 SUBMITTED`) | `True` |

> **关键实证核查发现**：通过精确重放官方 `controller.sqlite3` 命令流，离线引擎独立核实了 Gen 1 实际进入 `PAUSED/BREAKOUT` 的精确时点为 `2026-11-22T05:20:00+00:00`（`close=129.54 < lower=129.59`，即买单 `05:18:48Z` 成交后仅 2 分钟即发生下沿突破），早于 `08:42:00Z` 上行突破 `132.21` 约 `3.37` 小时。因当时 Windows 端未启用 F11，持仓滞留至 Azure C05 切换后 `16:24:30Z` 才执行 F11 平仓并承担了 `8` 笔资金费（`-0.09763384 USDT`）。

### 3.1 Gen 1 实测 1m K 线条件反事实对照（`CONDITIONAL_COUNTERFACTUAL_ON_OBSERVED_1M_BARS`）

| 反事实情景（同 1284 根实测 1m K 线、同 8 笔实测资金费率，手续费和滑点均为 0） | 模型 | 执行假设 | 净盈亏 (USDT) | 7U 收益率 | 资金费 (USDT) | 最大回撤 | 持仓时长 (h) | 轮换次数 | 期末残仓 | 终态 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `MODEL_A_BASELINE_NO_F11_NO_F08` | `NO_F11_NO_F08_STRANDED_POSITION_BASELINE` | `IDEAL_LIMIT_FILL` | +0.037166 | +0.5309% | -0.097634 | 1.4043% | 11.07 | 0 | 0.04 | `PAUSED` |
| `MODEL_B_F11_F08_IDEAL_IMMEDIATE` | `F11_F08_FULL_LIFECYCLE` | `IDEAL_LIMIT_FILL` | +0.000000 | +0.0000% | +0.000000 | 0.0000% | 0.00 | 3 | 0.00 | `RUNNING` |
| `MODEL_B_F11_F08_LIMITED_CAPACITY` | `F11_F08_FULL_LIFECYCLE` | `LIMITED_CAPACITY_PARTIAL` | +0.018583 | +0.2655% | -0.048817 | 0.7087% | 11.07 | 0 | 0.02 | `PAUSED` |
| `MODEL_B_F11_F08_DELAYED_EXIT` | `F11_F08_FULL_LIFECYCLE` | `DELAYED_EXIT_FILL` | +0.000000 | +0.0000% | +0.000000 | 0.0057% | 0.03 | 3 | 0.00 | `RUNNING` |
| `MODEL_B_F11_F08_UNFILLED_TIMEOUT` | `F11_F08_FULL_LIFECYCLE` | `UNFILLED_EXIT_TIMEOUT` | +0.037166 | +0.5309% | -0.097634 | 1.4043% | 11.07 | 0 | 0.04 | `PAUSED` |

- **重要结构洞察**：在 `LIMITED_CAPACITY_PARTIAL`（每根 1m Bar 仅可成交 `0.02`）中，`05:19Z` 买单仅部分成交 `0.02`，次分钟 `05:20Z` 触发 `BREAKOUT` 撤消剩余 `0.02` 买单，留下 `0.02 NVDA`（名义额约 `2.59 < 5.00 USDT MIN_NOTIONAL`），导致 F11 被 `EXIT_MIN_NOTIONAL` 合法阻断并被迫持续承担资金费（`-0.048817 USDT`）直至窗末。

## 4. R09-03 数据来源与条件执行假设矩阵

- **历史 1h 行情与资金费**：R04B 认证快照 `BINANCE:USDM:NVDAUSDT`（`2026-05-14T00:00:00+00:00` 至 `2026-08-12T00:00:00+00:00`，共 `2160` 根 1h K 线，`bars_sha256=16aa66258880efda...`），配对 `279` 条历史资金费结算记录（`docs/validation/p7-r09/R04B-base-baseline-funding-settlements.csv`）。
- **合成路径参数**：锚定价 `132.89 USDT`，基线小时波动率 `0.445%/h`，主对比每情景 `200` 条路径（敏感性 `128` 条），每条 `168` 小时（7 天），种子 `seed=909`，费用 `3 bps`、滑点 `2 bps`；策略预算口径 `7.00 USDT`（R04B 高价历史窗采用 `10.00 USDT` 并单独标记 7U 上限越界）。
- **真实性声明**：所有 1h K 线收盘价与合成收盘路径均**不构成真实交易所可成交盘口或成交回执**。以下 5 组条件情景明确标注各自的流动性、延迟与故障假设：

| 执行假设情景 (`CONDITIONAL_EXECUTION_ASSUMPTION`) | 关键假设说明 |
| --- | --- |
| `IDEAL_LIMIT_FILL` | Conditional assumption: marketable F11 reduce-only SELL LIMIT at the tick-rounded adverse bid fills immediately within bar capacity (0.10 NVDA/bar or bar volume), with 3 bps fee and 2 bps slippage; all real exchange filters & Demo guardrails enforced. |
| `LIMITED_CAPACITY_PARTIAL` | Conditional assumption: executable liquidity capped at 0.02 NVDA/bar (50% of 0.04 lot), same entry price basis as baseline (3 bps fee, 2 bps slippage), forcing multi-bar partial fills and exercising F11 anti-dust / MIN_NOTIONAL protection on <5 USDT remainders. |
| `DELAYED_EXIT_FILL` | Conditional assumption: same entry basis as baseline, but passive F11 reduce-only SELL LIMIT rests in queue for >=2 bars (2h on 1h bars) before filling at its limit when the adverse bid reaches it; reprices downward after cooldown if price drops (up to EXIT_MAX_ATTEMPTS=5). |
| `UNFILLED_EXIT_TIMEOUT` | Conditional assumption: same entry basis as baseline, severe liquidity drought where F11 reduce-only SELL LIMIT orders cannot fill; exercises bounded repricing up to EXIT_MAX_ATTEMPTS=5 then EXIT_RETRY_LIMIT_EXCEEDED. |
| `GAP_AND_CANCEL_FAULT` | Conditional assumption: widened 10 bps half-spread, PERCENT_PRICE [0.97, 1.03] gap block when bar jump exceeds 3%, 1-bar passive exit wait, plus injected cancel UNKNOWN->RECONCILE fault. |

## 5. 历史 11 个 7 天独立窗口：新旧模型同窗对照（`CONDITIONAL_RISK_ESTIMATED`）

| 资金费情景 | 执行模型 | 执行假设 | 正收益窗 | 均值收益率 (10U) | 最差窗收益 | 均值/最差最大回撤 | 均值持仓 (h) | 均值退出延迟 (h) | 均值残仓 | 残仓窗数 | 轮换成功率 | 熔断窗数 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `EX_FUNDING` | `NO_F11_NO_F08_STRANDED_POSITION_BASELINE` | `IDEAL_LIMIT_FILL` | 2/11 | -1.06% | -6.34% | 2.81% / 8.18% | 81.4 | 81.4 | 0.0218 | 6/11 | 0.0% | 0 |
| `EX_FUNDING` | `F11_EXIT_ONLY_NO_ROTATION` | `IDEAL_LIMIT_FILL` | 0/11 | -0.05% | -0.09% | 0.05% / 0.09% | 0.0 | 0.0 | 0.0000 | 0/11 | 0.0% | 0 |
| `EX_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `IDEAL_LIMIT_FILL` | 0/11 | -0.56% | -1.08% | 0.56% / 1.08% | 0.0 | 0.0 | 0.0000 | 0/11 | 100.0% | 0 |
| `EX_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `LIMITED_CAPACITY_PARTIAL` | 5/11 | -0.47% | -3.17% | 2.45% / 4.13% | 138.0 | 106.0 | 0.0200 | 11/11 | 45.0% | 0 |
| `EX_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `DELAYED_EXIT_FILL` | 0/11 | -1.85% | -3.45% | 2.38% / 4.55% | 28.2 | 2.8 | 0.0036 | 1/11 | 98.4% | 0 |
| `EX_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `UNFILLED_EXIT_TIMEOUT` | 5/11 | -0.94% | -6.34% | 4.85% / 8.18% | 138.0 | 106.0 | 0.0400 | 11/11 | 45.0% | 0 |
| `EX_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `GAP_AND_CANCEL_FAULT` | 1/11 | -2.18% | -6.14% | 2.54% / 6.77% | 14.6 | 1.3 | 0.0000 | 0/11 | 99.2% | 0 |
| `HIST_R04B_FUNDING` | `NO_F11_NO_F08_STRANDED_POSITION_BASELINE` | `IDEAL_LIMIT_FILL` | 2/11 | -1.18% | -6.65% | 2.88% / 8.33% | 81.4 | 81.4 | 0.0218 | 6/11 | 0.0% | 0 |
| `HIST_R04B_FUNDING` | `F11_EXIT_ONLY_NO_ROTATION` | `IDEAL_LIMIT_FILL` | 0/11 | -0.05% | -0.09% | 0.05% / 0.09% | 0.0 | 0.0 | 0.0000 | 0/11 | 0.0% | 0 |
| `HIST_R04B_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `IDEAL_LIMIT_FILL` | 0/11 | -0.56% | -1.08% | 0.56% / 1.08% | 0.0 | 0.0 | 0.0000 | 0/11 | 100.0% | 0 |
| `HIST_R04B_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `LIMITED_CAPACITY_PARTIAL` | 5/11 | -0.54% | -3.32% | 2.49% / 4.21% | 138.0 | 106.0 | 0.0200 | 11/11 | 45.0% | 0 |
| `HIST_R04B_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `DELAYED_EXIT_FILL` | 0/11 | -1.87% | -3.49% | 2.39% / 4.55% | 28.2 | 2.8 | 0.0036 | 1/11 | 98.4% | 0 |
| `HIST_R04B_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `UNFILLED_EXIT_TIMEOUT` | 5/11 | -1.09% | -6.65% | 4.92% / 8.33% | 138.0 | 106.0 | 0.0400 | 11/11 | 45.0% | 0 |
| `HIST_R04B_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `GAP_AND_CANCEL_FAULT` | 1/11 | -2.20% | -6.18% | 2.55% / 6.78% | 14.6 | 1.3 | 0.0000 | 0/11 | 99.2% | 0 |
| `DEMO_STRESS_22.97BPS` | `NO_F11_NO_F08_STRANDED_POSITION_BASELINE` | `IDEAL_LIMIT_FILL` | 0/11 | -16.68% | -40.50% | 16.80% / 40.50% | 81.4 | 81.4 | 0.0218 | 6/11 | 0.0% | 6 |
| `DEMO_STRESS_22.97BPS` | `F11_EXIT_ONLY_NO_ROTATION` | `IDEAL_LIMIT_FILL` | 0/11 | -0.05% | -0.09% | 0.05% / 0.09% | 0.0 | 0.0 | 0.0000 | 0/11 | 0.0% | 0 |
| `DEMO_STRESS_22.97BPS` | `F11_F08_FULL_LIFECYCLE` | `IDEAL_LIMIT_FILL` | 0/11 | -0.56% | -1.08% | 0.56% / 1.08% | 0.0 | 0.0 | 0.0000 | 0/11 | 100.0% | 0 |
| `DEMO_STRESS_22.97BPS` | `F11_F08_FULL_LIFECYCLE` | `LIMITED_CAPACITY_PARTIAL` | 0/11 | -13.67% | -20.25% | 13.77% / 20.25% | 138.0 | 106.0 | 0.0200 | 11/11 | 45.0% | 9 |
| `DEMO_STRESS_22.97BPS` | `F11_F08_FULL_LIFECYCLE` | `DELAYED_EXIT_FILL` | 0/11 | -6.87% | -10.26% | 7.00% / 10.26% | 27.0 | 3.0 | 0.0036 | 1/11 | 96.6% | 2 |
| `DEMO_STRESS_22.97BPS` | `F11_F08_FULL_LIFECYCLE` | `UNFILLED_EXIT_TIMEOUT` | 0/11 | -27.34% | -40.50% | 27.53% / 40.50% | 138.0 | 106.0 | 0.0400 | 11/11 | 45.0% | 10 |
| `DEMO_STRESS_22.97BPS` | `F11_F08_FULL_LIFECYCLE` | `GAP_AND_CANCEL_FAULT` | 0/11 | -5.14% | -12.35% | 5.31% / 13.20% | 16.6 | 1.4 | 0.0036 | 1/11 | 98.4% | 1 |

## 6. 随机路径同路径对照：Model A（旧残仓）vs Model B（F11/F08 完整生命周期）

所有模型在完全相同的 `200` 条路径/情景、同资金（`7.00 USDT` 预算 / `5000 USDT` 钱包）、同费用滑点（`3 bps / 2 bps`）及同资金费流下逐路径配对运行，全部路径 FIFO 对账残差严格为 `0`。

### 6.1 不含资金费（`EX_FUNDING`，0 bps/h）

| 随机情景 | 模型 / 执行假设 | 均值收益 | 亏损概率 (Wilson 95% CI) | VaR95 | CVaR95 (Boot 95% CI) | P95 最大回撤 | 均值持仓 (h) | 均值退出延迟 (h) | 尾部残仓率 | 资费侵蚀 (占7U) | 轮换成功率 | 相对 A ΔCVaR95 |
| --- | --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gbm_low_vol_0.5x` | A: 旧残仓 | +0.07% | 27.0% (`[21.3%, 33.5%]`) | 1.92% | 3.35% (`[2.36%, 4.27%]`) | 3.50% | 74.5 | 74.5 | 51.0% | 0.00% | 0.0% | **基准** |
| `gbm_low_vol_0.5x` | B0: 仅F11 | -0.04% | 51.0% (`[44.1%, 57.8%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-3.27 pp** |
| `gbm_low_vol_0.5x` | B: F11+F08 (IDEAL_LIMIT) | -0.22% | 97.0% (`[93.6%, 98.6%]`) | 0.40% | 0.46% (`[0.41%, 0.52%]`) | 0.40% | 0.0 | 0.0 | 0.0% | 0.00% | 98.9% | **-2.88 pp** |
| `gbm_low_vol_0.5x` | B: F11+F08 (DELAYED) | -0.66% | 94.0% (`[89.8%, 96.5%]`) | 1.77% | 2.92% (`[2.05%, 3.65%]`) | 2.12% | 15.0 | 2.9 | 13.5% | 0.00% | 96.8% | **-0.43 pp** |
| `gbm_base_vol_1.0x` | A: 旧残仓 | -0.13% | 26.5% (`[20.9%, 33.0%]`) | 6.12% | 7.12% (`[6.06%, 7.67%]`) | 7.85% | 81.8 | 81.8 | 51.0% | 0.00% | 0.0% | **基准** |
| `gbm_base_vol_1.0x` | B0: 仅F11 | -0.04% | 51.0% (`[44.1%, 57.8%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-7.04 pp** |
| `gbm_base_vol_1.0x` | B: F11+F08 (IDEAL_LIMIT) | -0.66% | 100.0% (`[98.1%, 100.0%]`) | 0.97% | 1.02% (`[0.98%, 1.06%]`) | 0.97% | 0.1 | 0.0 | 0.0% | 0.00% | 98.3% | **-6.09 pp** |
| `gbm_base_vol_1.0x` | B: F11+F08 (DELAYED) | -2.94% | 100.0% (`[98.1%, 100.0%]`) | 5.37% | 6.26% (`[5.67%, 6.63%]`) | 6.70% | 45.5 | 6.6 | 30.5% | 0.00% | 96.3% | **-0.86 pp** |
| `gbm_high_vol_2.0x` | A: 旧残仓 | -0.04% | 25.5% (`[20.0%, 32.0%]`) | 10.27% | 12.81% (`[10.55%, 14.85%]`) | 14.91% | 80.7 | 80.7 | 49.0% | 0.00% | 0.0% | **基准** |
| `gbm_high_vol_2.0x` | B0: 仅F11 | -0.04% | 49.0% (`[42.2%, 55.9%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-12.72 pp** |
| `gbm_high_vol_2.0x` | B: F11+F08 (IDEAL_LIMIT) | -1.15% | 100.0% (`[98.1%, 100.0%]`) | 1.77% | 2.40% (`[1.79%, 3.47%]`) | 1.77% | 0.8 | 0.1 | 0.5% | 0.00% | 98.6% | **-10.41 pp** |
| `gbm_high_vol_2.0x` | B: F11+F08 (DELAYED) | -7.50% | 99.0% (`[96.4%, 99.7%]`) | 13.52% | 15.57% (`[14.11%, 16.61%]`) | 16.13% | 70.5 | 6.4 | 44.5% | 0.00% | 97.2% | **+2.77 pp** |
| `moving_block_bootstrap_24h` | A: 旧残仓 | -0.48% | 30.0% (`[24.1%, 36.7%]`) | 5.49% | 7.62% (`[5.94%, 9.12%]`) | 7.97% | 72.8 | 72.8 | 47.5% | 0.00% | 0.0% | **基准** |
| `moving_block_bootstrap_24h` | B0: 仅F11 | -0.04% | 47.5% (`[40.7%, 54.4%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-7.54 pp** |
| `moving_block_bootstrap_24h` | B: F11+F08 (IDEAL_LIMIT) | -0.44% | 99.0% (`[96.4%, 99.7%]`) | 0.73% | 0.83% (`[0.76%, 0.88%]`) | 0.74% | 0.0 | 0.0 | 0.0% | 0.00% | 97.9% | **-6.79 pp** |
| `moving_block_bootstrap_24h` | B: F11+F08 (DELAYED) | -1.98% | 97.5% (`[94.3%, 98.9%]`) | 4.84% | 6.60% (`[5.34%, 7.54%]`) | 6.31% | 34.6 | 6.6 | 24.5% | 0.00% | 96.1% | **-1.02 pp** |
| `regime_bootstrap_24h` | A: 旧残仓 | +0.56% | 11.0% (`[7.4%, 16.1%]`) | 2.44% | 5.16% (`[2.99%, 7.39%]`) | 4.81% | 49.2 | 49.2 | 33.0% | 0.00% | 0.0% | **基准** |
| `regime_bootstrap_24h` | B0: 仅F11 | -0.03% | 33.0% (`[26.9%, 39.8%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-5.08 pp** |
| `regime_bootstrap_24h` | B: F11+F08 (IDEAL_LIMIT) | -0.38% | 99.5% (`[97.2%, 99.9%]`) | 0.65% | 0.72% (`[0.67%, 0.77%]`) | 0.65% | 0.0 | 0.0 | 0.0% | 0.00% | 99.1% | **-4.45 pp** |
| `regime_bootstrap_24h` | B: F11+F08 (DELAYED) | -1.46% | 96.5% (`[93.0%, 98.3%]`) | 3.58% | 5.37% (`[4.02%, 6.80%]`) | 4.47% | 23.4 | 3.6 | 17.5% | 0.00% | 97.6% | **+0.21 pp** |
| `jump_stress_downside` | A: 旧残仓 | -2.57% | 41.5% (`[34.9%, 48.4%]`) | 11.01% | 13.02% (`[11.66%, 14.23%]`) | 12.88% | 78.9 | 78.9 | 49.0% | 0.00% | 0.0% | **基准** |
| `jump_stress_downside` | B0: 仅F11 | -0.04% | 49.0% (`[42.2%, 55.9%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-12.94 pp** |
| `jump_stress_downside` | B: F11+F08 (IDEAL_LIMIT) | -0.62% | 99.5% (`[97.2%, 99.9%]`) | 0.95% | 2.72% (`[0.99%, 5.32%]`) | 0.97% | 1.1 | 0.2 | 1.0% | 0.00% | 95.8% | **-10.30 pp** |
| `jump_stress_downside` | B: F11+F08 (DELAYED) | -4.26% | 99.0% (`[96.4%, 99.7%]`) | 11.31% | 12.76% (`[11.48%, 13.78%]`) | 12.38% | 57.5 | 11.6 | 37.5% | 0.00% | 93.4% | **-0.26 pp** |

### 6.2 实测 Demo 高资金费压力（`DEMO_STRESS_22.97BPS`，22.9669 bps/h）

| 随机情景 | 模型 / 执行假设 | 均值收益 | 亏损概率 (Wilson 95% CI) | VaR95 | CVaR95 (Boot 95% CI) | P95 最大回撤 | 均值持仓 (h) | 均值退出延迟 (h) | 尾部残仓率 | 资费侵蚀 (占7U) | 轮换成功率 | 相对 A ΔCVaR95 |
| --- | --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gbm_low_vol_0.5x` | A: 旧残仓 | -12.79% | 51.0% (`[44.1%, 57.8%]`) | 28.90% | 29.70% (`[29.04%, 30.10%]`) | 28.90% | 74.5 | 74.5 | 51.0% | 12.86% | 0.0% | **基准** |
| `gbm_low_vol_0.5x` | B0: 仅F11 | -0.04% | 51.0% (`[44.1%, 57.8%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-29.62 pp** |
| `gbm_low_vol_0.5x` | B: F11+F08 (IDEAL_LIMIT) | -0.22% | 97.0% (`[93.6%, 98.6%]`) | 0.40% | 0.46% (`[0.41%, 0.52%]`) | 0.40% | 0.0 | 0.0 | 0.0% | 0.00% | 98.9% | **-29.24 pp** |
| `gbm_low_vol_0.5x` | B: F11+F08 (DELAYED) | -3.22% | 97.0% (`[93.6%, 98.6%]`) | 7.93% | 13.36% (`[9.38%, 17.25%]`) | 7.93% | 15.0 | 2.9 | 13.5% | 2.56% | 96.8% | **-16.35 pp** |
| `gbm_base_vol_1.0x` | A: 旧残仓 | -14.20% | 51.0% (`[44.1%, 57.8%]`) | 32.48% | 33.63% (`[32.81%, 34.19%]`) | 32.81% | 81.8 | 81.8 | 51.0% | 14.08% | 0.0% | **基准** |
| `gbm_base_vol_1.0x` | B0: 仅F11 | -0.04% | 51.0% (`[44.1%, 57.8%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-33.55 pp** |
| `gbm_base_vol_1.0x` | B: F11+F08 (IDEAL_LIMIT) | -0.68% | 100.0% (`[98.1%, 100.0%]`) | 0.97% | 1.19% (`[1.01%, 1.45%]`) | 0.97% | 0.1 | 0.0 | 0.0% | 0.01% | 98.3% | **-32.45 pp** |
| `gbm_base_vol_1.0x` | B: F11+F08 (DELAYED) | -10.76% | 100.0% (`[98.1%, 100.0%]`) | 22.52% | 27.73% (`[24.06%, 30.44%]`) | 22.75% | 45.9 | 7.1 | 30.5% | 7.85% | 95.8% | **-5.90 pp** |
| `gbm_high_vol_2.0x` | A: 旧残仓 | -13.81% | 49.0% (`[42.2%, 55.9%]`) | 36.12% | 38.45% (`[36.77%, 39.60%]`) | 37.41% | 80.7 | 80.7 | 49.0% | 13.77% | 0.0% | **基准** |
| `gbm_high_vol_2.0x` | B0: 仅F11 | -0.04% | 49.0% (`[42.2%, 55.9%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-38.37 pp** |
| `gbm_high_vol_2.0x` | B: F11+F08 (IDEAL_LIMIT) | -1.28% | 100.0% (`[98.1%, 100.0%]`) | 1.77% | 4.64% (`[1.82%, 9.00%]`) | 1.77% | 0.8 | 0.1 | 0.5% | 0.12% | 98.6% | **-33.81 pp** |
| `gbm_high_vol_2.0x` | B: F11+F08 (DELAYED) | -18.19% | 100.0% (`[98.1%, 100.0%]`) | 34.68% | 36.86% (`[33.98%, 38.68%]`) | 34.85% | 70.4 | 7.8 | 42.5% | 11.97% | 95.6% | **-1.59 pp** |
| `moving_block_bootstrap_24h` | A: 旧残仓 | -12.92% | 47.5% (`[40.7%, 54.4%]`) | 31.28% | 33.41% (`[31.81%, 35.19%]`) | 31.28% | 72.8 | 72.8 | 47.5% | 12.43% | 0.0% | **基准** |
| `moving_block_bootstrap_24h` | B0: 仅F11 | -0.04% | 47.5% (`[40.7%, 54.4%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-33.33 pp** |
| `moving_block_bootstrap_24h` | B: F11+F08 (IDEAL_LIMIT) | -0.44% | 99.5% (`[97.2%, 99.9%]`) | 0.74% | 0.85% (`[0.77%, 0.91%]`) | 0.74% | 0.0 | 0.0 | 0.0% | 0.00% | 97.9% | **-32.56 pp** |
| `moving_block_bootstrap_24h` | B: F11+F08 (DELAYED) | -7.81% | 99.5% (`[97.2%, 99.9%]`) | 22.25% | 26.23% (`[22.75%, 29.01%]`) | 22.53% | 34.5 | 7.4 | 24.5% | 5.85% | 96.0% | **-7.19 pp** |
| `regime_bootstrap_24h` | A: 旧残仓 | -8.02% | 33.0% (`[26.9%, 39.8%]`) | 29.08% | 31.58% (`[29.72%, 33.36%]`) | 29.36% | 49.2 | 49.2 | 33.0% | 8.58% | 0.0% | **基准** |
| `regime_bootstrap_24h` | B0: 仅F11 | -0.03% | 33.0% (`[26.9%, 39.8%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-31.50 pp** |
| `regime_bootstrap_24h` | B: F11+F08 (IDEAL_LIMIT) | -0.38% | 99.5% (`[97.2%, 99.9%]`) | 0.65% | 0.72% (`[0.67%, 0.77%]`) | 0.65% | 0.0 | 0.0 | 0.0% | 0.00% | 99.1% | **-30.86 pp** |
| `regime_bootstrap_24h` | B: F11+F08 (DELAYED) | -5.49% | 99.5% (`[97.2%, 99.9%]`) | 12.30% | 18.85% (`[13.98%, 23.33%]`) | 12.30% | 23.4 | 3.9 | 17.5% | 4.03% | 97.6% | **-12.73 pp** |
| `jump_stress_downside` | A: 旧残仓 | -15.56% | 49.0% (`[42.2%, 55.9%]`) | 36.88% | 38.59% (`[37.36%, 39.51%]`) | 37.06% | 78.9 | 78.9 | 49.0% | 12.99% | 0.0% | **基准** |
| `jump_stress_downside` | B0: 仅F11 | -0.04% | 49.0% (`[42.2%, 55.9%]`) | 0.08% | 0.08% (`[0.08%, 0.08%]`) | 0.08% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-38.50 pp** |
| `jump_stress_downside` | B: F11+F08 (IDEAL_LIMIT) | -0.78% | 100.0% (`[98.1%, 100.0%]`) | 0.97% | 5.72% (`[1.02%, 13.54%]`) | 0.97% | 1.1 | 0.2 | 1.0% | 0.16% | 95.8% | **-32.87 pp** |
| `jump_stress_downside` | B: F11+F08 (DELAYED) | -13.69% | 100.0% (`[98.1%, 100.0%]`) | 33.51% | 35.31% (`[33.63%, 36.15%]`) | 34.20% | 57.8 | 12.3 | 38.5% | 9.45% | 93.2% | **-3.28 pp** |

## 7. 执行摩擦、撤单故障与资金费异常升高敏感性分析（`CONDITIONAL_RISK_ESTIMATED`）

在 3 类代表性随机情景（`gbm_base_vol_1.0x`、`moving_block_bootstrap_24h`、`jump_stress_downside`，每情景 `128` 条同路径）上，系统检验 5 组执行假设与 3 档资金费率（正常 `0.125 bps/h`、实测压力 `22.97 bps/h`、异常飙升 `50.0 bps/h`）：

| 情景 | 资金费档位 | 模型 / 执行假设 | 均值收益 | 亏损概率 | VaR95 | CVaR95 | P95 回撤 | 持仓 (h) | 退出延迟 (h) | 残仓率 | 熔断率 | 主要退出阻断 | 相对 A ΔCVaR95 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| `gbm_base_vol_1.0x` | `NORMAL_FUNDING_0.125BPS` | A: 旧残仓 | -0.16% | 25.0% | 6.09% | 7.13% | 7.64% | 81.5 | 81.5 | 50.8% | 0.0% | — | **基准** |
| `gbm_base_vol_1.0x` | `NORMAL_FUNDING_0.125BPS` | B: `IDEAL_LIMIT_FILL` | -0.64% | 100.0% | 0.90% | 0.97% | 0.91% | 0.1 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-6.16 pp** |
| `gbm_base_vol_1.0x` | `NORMAL_FUNDING_0.125BPS` | B: `DELAYED_EXIT_FILL` | -2.87% | 100.0% | 5.35% | 5.95% | 6.54% | 45.1 | 7.0 | 28.9% | 0.0% | `EXIT_RETRY_LIMIT_EXCEEDED` | **-1.18 pp** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -14.09% | 50.8% | 32.40% | 33.54% | 32.66% | 81.5 | 81.5 | 50.8% | 50.8% | — | **基准** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.67% | 100.0% | 0.93% | 1.23% | 0.93% | 0.1 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-32.31 pp** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -13.21% | 100.0% | 16.20% | 16.77% | 16.38% | 150.7 | 107.7 | 100.0% | 33.6% | `EXIT_MIN_NOTIONAL` | **-16.77 pp** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -10.55% | 100.0% | 22.15% | 27.25% | 22.30% | 45.6 | 7.7 | 28.9% | 16.4% | `HALTED` | **-6.29 pp** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -26.42% | 100.0% | 32.40% | 33.54% | 32.76% | 150.7 | 107.7 | 100.0% | 100.0% | `HALTED` | **+0.00 pp** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -6.80% | 100.0% | 15.78% | 23.76% | 15.83% | 24.8 | 2.9 | 17.2% | 8.6% | `EXIT_MIN_NOTIONAL` | **-9.78 pp** |
| `gbm_base_vol_1.0x` | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | -30.56% | 50.8% | 64.74% | 65.56% | 64.74% | 81.5 | 81.5 | 50.8% | 50.8% | — | **基准** |
| `gbm_base_vol_1.0x` | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -0.69% | 100.0% | 0.93% | 1.71% | 0.93% | 0.1 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-63.85 pp** |
| `gbm_base_vol_1.0x` | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -18.90% | 100.0% | 44.62% | 56.61% | 44.68% | 44.2 | 8.5 | 31.2% | 65.6% | `HALTED` | **-8.95 pp** |
| `moving_block_bootstrap_24h` | `NORMAL_FUNDING_0.125BPS` | A: 旧残仓 | -0.50% | 32.0% | 6.28% | 8.43% | 8.25% | 78.2 | 78.2 | 50.8% | 0.0% | — | **基准** |
| `moving_block_bootstrap_24h` | `NORMAL_FUNDING_0.125BPS` | B: `IDEAL_LIMIT_FILL` | -0.44% | 99.2% | 0.73% | 0.83% | 0.73% | 0.0 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-7.60 pp** |
| `moving_block_bootstrap_24h` | `NORMAL_FUNDING_0.125BPS` | B: `DELAYED_EXIT_FILL` | -2.10% | 99.2% | 5.66% | 7.09% | 6.41% | 35.4 | 6.3 | 25.8% | 0.0% | `EXIT_MIN_NOTIONAL` | **-1.35 pp** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -13.80% | 50.8% | 31.05% | 34.10% | 31.13% | 78.2 | 78.2 | 50.8% | 50.8% | — | **基准** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.45% | 100.0% | 0.74% | 0.87% | 0.74% | 0.0 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-33.23 pp** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -12.42% | 100.0% | 15.79% | 17.32% | 15.85% | 142.9 | 104.2 | 100.0% | 27.3% | `EXIT_MIN_NOTIONAL` | **-16.78 pp** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -8.11% | 100.0% | 22.17% | 25.83% | 22.42% | 35.9 | 7.2 | 26.6% | 12.5% | `EXIT_RETRY_LIMIT_EXCEEDED` | **-8.26 pp** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -24.84% | 100.0% | 31.57% | 34.63% | 31.69% | 142.9 | 104.2 | 100.0% | 97.7% | `HALTED` | **+0.53 pp** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -5.04% | 98.4% | 11.45% | 18.59% | 11.46% | 18.1 | 2.2 | 12.5% | 3.9% | `EXIT_MIN_NOTIONAL` | **-15.50 pp** |
| `moving_block_bootstrap_24h` | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | -29.56% | 50.8% | 63.12% | 65.49% | 63.12% | 78.2 | 78.2 | 50.8% | 50.8% | — | **基准** |
| `moving_block_bootstrap_24h` | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -0.46% | 100.0% | 0.77% | 0.93% | 0.77% | 0.0 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-64.56 pp** |
| `moving_block_bootstrap_24h` | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -16.39% | 100.0% | 46.61% | 56.11% | 46.61% | 39.1 | 9.4 | 32.0% | 38.3% | `HALTED` | **-9.37 pp** |
| `jump_stress_downside` | `NORMAL_FUNDING_0.125BPS` | A: 旧残仓 | -2.61% | 42.2% | 11.42% | 13.26% | 12.89% | 79.2 | 79.2 | 49.2% | 1.6% | — | **基准** |
| `jump_stress_downside` | `NORMAL_FUNDING_0.125BPS` | B: `IDEAL_LIMIT_FILL` | -0.62% | 100.0% | 0.88% | 2.57% | 0.94% | 1.2 | 0.3 | 0.8% | 0.0% | `EXIT_MIN_NOTIONAL` | **-10.69 pp** |
| `jump_stress_downside` | `NORMAL_FUNDING_0.125BPS` | B: `DELAYED_EXIT_FILL` | -4.25% | 100.0% | 11.36% | 12.74% | 12.43% | 57.5 | 12.1 | 35.2% | 1.6% | `EXIT_MIN_NOTIONAL` | **-0.52 pp** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -15.61% | 49.2% | 36.91% | 38.71% | 37.21% | 79.2 | 79.2 | 49.2% | 49.2% | — | **基准** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.80% | 100.0% | 0.97% | 6.03% | 0.97% | 1.2 | 0.3 | 0.8% | 0.8% | `HALTED` | **-32.67 pp** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -15.37% | 100.0% | 19.01% | 19.59% | 19.07% | 152.7 | 108.0 | 100.0% | 71.1% | `EXIT_MIN_NOTIONAL` | **-19.12 pp** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -13.70% | 100.0% | 34.03% | 35.42% | 34.38% | 58.0 | 13.1 | 35.9% | 29.7% | `HALTED` | **-3.28 pp** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -30.74% | 100.0% | 38.02% | 39.18% | 38.11% | 152.7 | 108.0 | 100.0% | 100.0% | `HALTED` | **+0.47 pp** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -9.13% | 100.0% | 28.73% | 30.22% | 28.86% | 35.0 | 6.2 | 22.7% | 17.2% | `HALTED` | **-8.49 pp** |
| `jump_stress_downside` | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | -30.99% | 49.2% | 67.64% | 69.11% | 67.64% | 79.2 | 79.2 | 49.2% | 49.2% | — | **基准** |
| `jump_stress_downside` | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -1.02% | 100.0% | 0.97% | 10.20% | 0.97% | 1.2 | 0.3 | 0.8% | 0.8% | `HALTED` | **-58.91 pp** |
| `jump_stress_downside` | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -25.05% | 100.0% | 60.61% | 63.53% | 60.61% | 59.4 | 13.8 | 38.3% | 67.2% | `HALTED` | **-5.58 pp** |

## 8. 确定性极端压力路径对照（连续下跌、跳空、突发反转、资费飙升、退出无法成交）

下表展示实测 Demo 资金费压力（`DEMO_STRESS_22.97BPS`）与资金费飙升（`FUNDING_SPIKE_50BPS`）下，各确定性压力路径的 Model A 与 Model B 全执行假设对照（完整表见 `deterministic_stress_paired.csv`）：

| 压力路径 (价格变动) | 资金费档位 | 模型 / 执行假设 | 收益率 | 最大回撤 | 持仓 (h) | 退出延迟 (h) | 期末残仓 | 轮换数 | 撤改/故障 | 熔断原因 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -52.05% | 52.05% | 163.0 | 163.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.32% | 0.32% | 0.0 | 0.0 | 0.00 | 4 | 0/0 | — |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -26.02% | 26.02% | 163.0 | 163.0 | 0.02 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -52.05% | 52.05% | 163.0 | 163.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -52.05% | 52.05% | 163.0 | 163.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 5 | 0/0 | — |
| `continuous_decline` (-40.0%) | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | -78.58% | 78.58% | 163.0 | 163.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -0.32% | 0.32% | 0.0 | 0.0 | 0.00 | 4 | 0/0 | — |
| `continuous_decline` (-40.0%) | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -78.58% | 78.58% | 163.0 | 163.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `FUNDING_SPIKE_50BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -78.58% | 78.58% | 163.0 | 163.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -35.63% | 35.63% | 156.0 | 156.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.24% | 0.24% | 0.0 | 0.0 | 0.00 | 4 | 0/0 | — |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -17.81% | 17.81% | 156.0 | 156.0 | 0.02 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -35.63% | 35.63% | 156.0 | 156.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -35.63% | 35.63% | 156.0 | 156.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 5 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.97% | 0.97% | 0.0 | 0.0 | 0.00 | 23 | 0/0 | — |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -23.85% | 23.85% | 157.0 | 78.5 | 0.02 | 1 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -42.53% | 42.53% | 92.0 | 5.4 | 0.04 | 16 | 16/0 | `LOSS_BUDGET_EXHAUSTED` |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -47.70% | 47.70% | 157.0 | 78.5 | 0.04 | 1 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -15.35% | 15.35% | 42.0 | 2.2 | 0.00 | 18 | 15/1 | `LOSS_BUDGET_EXHAUSTED` |
| `range_then_gap` (-30.0%) | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `range_then_gap` (-30.0%) | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -0.97% | 0.97% | 0.0 | 0.0 | 0.00 | 23 | 0/0 | — |
| `range_then_gap` (-30.0%) | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -66.07% | 66.07% | 118.0 | 10.7 | 0.04 | 10 | 10/0 | `LOSS_BUDGET_EXHAUSTED` |
| `range_then_gap` (-30.0%) | `FUNDING_SPIKE_50BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -77.26% | 77.26% | 157.0 | 78.5 | 0.04 | 1 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -38.30% | 39.77% | 165.0 | 165.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.40% | 0.40% | 0.0 | 0.0 | 0.00 | 4 | 0/0 | — |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -19.15% | 19.89% | 165.0 | 165.0 | 0.02 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -38.30% | 39.77% | 165.0 | 165.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -38.30% | 39.77% | 165.0 | 165.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -38.37% | 39.85% | 165.0 | 165.0 | 0.04 | 0 | 4/1 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -27.15% | 27.15% | 162.0 | 162.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -1.00% | 1.00% | 0.0 | 0.0 | 0.00 | 25 | 0/0 | — |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -13.58% | 13.58% | 162.0 | 162.0 | 0.02 | 0 | 0/0 | — |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -13.77% | 21.49% | 78.0 | 78.0 | 0.00 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -27.15% | 27.15% | 162.0 | 162.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -21.16% | 22.50% | 143.0 | 28.6 | 0.04 | 4 | 2/1 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | -60.13% | 60.13% | 162.0 | 162.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -1.00% | 1.00% | 0.0 | 0.0 | 0.00 | 25 | 0/0 | — |
| `fast_reversal` (+0.0%) | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -28.48% | 34.50% | 78.0 | 78.0 | 0.00 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `FUNDING_SPIKE_50BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -60.13% | 60.13% | 162.0 | 162.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |

## 9. 样本量收敛与误差稳定性核查

下表按 `DEMO_STRESS_22.97BPS` 同路径样本的前缀展示 VaR95、CVaR95 和 Bootstrap 区间宽度。

| 情景 | 样本路径数 `n` | Model A VaR95 | Model A CVaR95 | Model B VaR95 | Model B CVaR95 | B CVaR95 Boot 95% CI 宽 | ΔCVaR95 (B-A) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gbm_base_vol_1.0x` | 32 | 32.45% | 33.37% | 0.96% | 1.68% | 1.19 pp | -31.69 pp |
| `gbm_base_vol_1.0x` | 64 | 31.62% | 32.80% | 0.98% | 1.50% | 1.11 pp | -31.30 pp |
| `gbm_base_vol_1.0x` | 128 | 32.40% | 33.54% | 0.93% | 1.23% | 0.72 pp | -32.31 pp |
| `gbm_base_vol_1.0x` | 200 | 32.48% | 33.63% | 0.97% | 1.19% | 0.45 pp | -32.45 pp |
| `moving_block_bootstrap_24h` | 32 | 29.08% | 34.14% | 0.80% | 0.90% | 0.25 pp | -33.24 pp |
| `moving_block_bootstrap_24h` | 64 | 32.90% | 34.64% | 0.79% | 0.88% | 0.23 pp | -33.76 pp |
| `moving_block_bootstrap_24h` | 128 | 31.05% | 34.10% | 0.74% | 0.87% | 0.16 pp | -33.23 pp |
| `moving_block_bootstrap_24h` | 200 | 31.28% | 33.41% | 0.74% | 0.85% | 0.14 pp | -32.56 pp |
| `jump_stress_downside` | 32 | 36.76% | 38.46% | 0.82% | 0.98% | 0.28 pp | -37.49 pp |
| `jump_stress_downside` | 64 | 36.92% | 38.70% | 0.83% | 10.99% | 29.86 pp | -27.71 pp |
| `jump_stress_downside` | 128 | 36.91% | 38.71% | 0.97% | 6.03% | 14.98 pp | -32.67 pp |
| `jump_stress_downside` | 200 | 36.88% | 38.59% | 0.97% | 5.72% | 12.52 pp | -32.87 pp |

- `gbm_base_vol_1.0x` 的 ΔCVaR95（B−A）在各样本前缀介于 `-32.45` 和 `-31.30 pp`。B 的 CVaR95 Bootstrap 区间宽度从 `n=32` 的 `1.19 pp` 变为 `n=200` 的 `0.45 pp`。
- `moving_block_bootstrap_24h` 的 ΔCVaR95（B−A）在各样本前缀介于 `-33.76` 和 `-32.56 pp`。B 的 CVaR95 Bootstrap 区间宽度从 `n=32` 的 `0.25 pp` 变为 `n=200` 的 `0.14 pp`。
- `jump_stress_downside` 的 ΔCVaR95（B−A）在各样本前缀介于 `-37.49` 和 `-27.71 pp`。B 的 CVaR95 Bootstrap 区间宽度从 `n=32` 的 `0.28 pp` 变为 `n=200` 的 `12.52 pp`。

## 10. 核心风险结论：F11/F08 是否真正改善尾部风险？

在 `DEMO_STRESS_22.97BPS` 的主样本中，旧模型 A 各情景的 CVaR95 为 `29.70 至 38.59%`，仅启用 F11 的 B0 为 `0.08 至 0.08%`，完整轮换模型 B 在 `IDEAL_LIMIT_FILL` 下为 `0.46 至 5.72%`。B 相对 A 的配对 ΔCVaR95 为 `-33.81 至 -29.24 pp`，均值持仓从 A 的 `49.17 至 81.78 h` 变为 B 的 `0.00 至 1.05 h`，B 的均值资金费侵蚀占 7U 预算的 `0.00 至 0.16%`。

F08 平仓后重新部署，网格因此再次暴露于价格变动，并继续承担手续费与滑点。`EX_FUNDING` 下，完整轮换模型 B 在理想成交情景的亏损概率为 `97.00 至 100.00%`。延迟退出时，相对 A 的 ΔCVaR95 介于 `-1.02 至 2.77 pp`。

在 `range_then_gap` 的确定性压力下，A 的收益率为 `+0.00%`。完整轮换模型 B 延迟退出的收益率为 `-42.53%`，无法退出时为 `-47.70%`。

在同一组资金费压力敏感性样本中，执行条件改变了尾部损失和残仓概率。部分成交可能使持仓低于最小名义金额，延迟和跳空则可能使原限价暂时无法成交。

| Model B 执行假设 | CVaR95 范围 | 配对 ΔCVaR95（B−A） | 残仓率范围 |
| --- | ---: | ---: | ---: |
| `IDEAL_LIMIT_FILL` | 0.87 至 6.03% | -33.23 至 -32.31 pp | 0.00 至 0.78% |
| `LIMITED_CAPACITY_PARTIAL` | 16.77 至 19.59% | -19.12 至 -16.77 pp | 100.00 至 100.00% |
| `DELAYED_EXIT_FILL` | 25.83 至 35.42% | -8.26 至 -3.28 pp | 26.56 至 35.94% |
| `UNFILLED_EXIT_TIMEOUT` | 33.54 至 39.18% | 0.00 至 0.53 pp | 100.00 至 100.00% |
| `GAP_AND_CANCEL_FAULT` | 18.59 至 30.22% | -15.50 至 -8.49 pp | 12.50 至 22.66% |

上述 VaR95 / CVaR95 是条件风险估计（`CONDITIONAL_RISK_ESTIMATED`）。官方 Demo 快照只提供一次完整平仓与轮换，真实下行 L2 深度、队列成交率、撤改滑点及极端资金费的持续性仍缺实证样本。

## 11. 可复现配置与运行命令

- 随机种子：`seed=909`；主对比样本量：`n_paths=200`；敏感性样本量：`sensitivity_paths=128`；步长：`168h`；总耗时：`1144.09s`（`workers=20`）。
- 输出产物：`summary.json`、`demo_replay_counterfactuals.csv`、`historical_windows_paired.csv`、`historical_windows_summary.csv`、`stochastic_paired_comparison.csv`、`execution_profile_sensitivity.csv`、`deterministic_stress_paired.csv`、`sample_convergence.csv`、`REPORT.md`。

```powershell
python tools/research/run_p8_r09.py --cloud-snapshot "<local-r08-cloud-snapshot>" --windows-snapshot "<local-windows-snapshot>" --r04b-snapshot "<r04b-certified-snapshot>" --output-dir "docs/validation/p8-r09"
```
