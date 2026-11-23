# FactorLab P8-R09：F11/F08 完整离线执行与尾部风险研究报告

> `NO LIVE MONEY`。本研究未触碰 Azure 在线交易实例，未修改线上授权与订单状态，未覆盖已冻结的 R08 原始证据（`docs/validation/p8-r08/` 保持不变）。小时 OHLC 收盘价与合成路径均非真实交易所盘口；所有离线成交情景均显式标记为条件假设。

## 1. 结论分级判定（严格区分工程、回放、条件风险与实证边界）

| 结论标签 | 状态 | 判定依据与严格边界 |
| --- | --- | --- |
| `ENGINEERING_ACCEPTED` | **PASS / CONFIRMED** | 已在既有 GridExecution、PaperBroker、TradingController、PaperStore 与 grid_fifo 框架上补齐 F11/F08/F10 离线生命周期；全量研究路径与自动化单元测试 FIFO 对账残差均为 0。 |
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

| 反事实情景（同 1284 根实测 1m K 线 + 同 8 笔实测资金费率，0 手续费） | 模型 | 执行假设 | 净盈亏 (USDT) | 7U 收益率 | 资金费 (USDT) | 最大回撤 | 持仓时长 (h) | 轮换次数 | 期末残仓 | 终态 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `MODEL_A_BASELINE_NO_F11_NO_F08` | `NO_F11_NO_F08_STRANDED_POSITION_BASELINE` | `IDEAL_LIMIT_FILL` | +0.037166 | +0.5309% | -0.097634 | 1.4043% | 11.08 | 0 | 0.04 | `PAUSED` |
| `MODEL_B_F11_F08_IDEAL_IMMEDIATE` | `F11_F08_FULL_LIFECYCLE` | `IDEAL_LIMIT_FILL` | +0.000000 | +0.0000% | +0.000000 | 0.0000% | 0.00 | 3 | 0.00 | `RUNNING` |
| `MODEL_B_F11_F08_LIMITED_CAPACITY` | `F11_F08_FULL_LIFECYCLE` | `LIMITED_CAPACITY_PARTIAL` | +0.018583 | +0.2655% | -0.048817 | 0.7087% | 11.08 | 0 | 0.02 | `PAUSED` |
| `MODEL_B_F11_F08_DELAYED_EXIT` | `F11_F08_FULL_LIFECYCLE` | `DELAYED_EXIT_FILL` | +0.000000 | +0.0000% | +0.000000 | 0.0057% | 0.03 | 3 | 0.00 | `RUNNING` |
| `MODEL_B_F11_F08_UNFILLED_TIMEOUT` | `F11_F08_FULL_LIFECYCLE` | `UNFILLED_EXIT_TIMEOUT` | +0.037166 | +0.5309% | -0.097634 | 1.4043% | 11.08 | 0 | 0.04 | `PAUSED` |

- **重要结构洞察**：在 `LIMITED_CAPACITY_PARTIAL`（每根 1m Bar 仅可成交 `0.02`）中，`05:19Z` 买单仅部分成交 `0.02`，次分钟 `05:20Z` 触发 `BREAKOUT` 撤消剩余 `0.02` 买单，留下 `0.02 NVDA`（名义额约 `2.59 < 5.00 USDT MIN_NOTIONAL`），导致 F11 被 `EXIT_MIN_NOTIONAL` 合法阻断并被迫持续承担资金费（`-0.048817 USDT`）直至窗末。

## 4. R09-03 数据来源与条件执行假设矩阵

- **历史 1h 行情与资金费**：R04B 认证快照 `BINANCE:USDM:NVDAUSDT`（`2026-05-14T00:00:00+00:00` 至 `2026-08-12T00:00:00+00:00`，共 `2160` 根 1h K 线，`bars_sha256=16aa66258880efda...`），配对 `279` 条历史资金费结算记录（`docs/validation/p7-r09/R04B-base-baseline-funding-settlements.csv`）。
- **合成路径参数**：锚定价 `132.89 USDT`，基线小时波动率 `0.445%/h`，主对比每情景 `200` 条路径（敏感性 `128` 条），每条 `168` 小时（7 天），种子 `seed=909`，费用 `3 bps`、滑点 `2 bps`；策略预算口径 `7.00 USDT`（R04B 高价历史窗采用 `10.00 USDT` 并单独标记 7U 上限越界）。
- **真实性声明**：所有 1h K 线收盘价与合成收盘路径均**不构成真实交易所可成交盘口或成交回执**。以下 5 组条件情景明确标注各自的流动性、延迟与故障假设：

| 执行假设情景 (`CONDITIONAL_EXECUTION_ASSUMPTION`) | 关键假设说明 |
| --- | --- |
| `IDEAL_LIMIT_FILL` | Conditional assumption: marketable F11 reduce-only SELL LIMIT at synthetic bid crosses top-of-book immediately within bar capacity (0.10 NVDA/bar or bar volume), with 3 bps fee and 2 bps slippage; all real exchange filters & Demo guardrails enforced. |
| `LIMITED_CAPACITY_PARTIAL` | Conditional assumption: executable liquidity capped at 0.02 NVDA/bar (50% of 0.04 lot), same entry price basis as baseline (3 bps fee, 2 bps slippage), forcing multi-bar partial fills and exercising F11 anti-dust / MIN_NOTIONAL protection on <5 USDT remainders. |
| `DELAYED_EXIT_FILL` | Conditional assumption: same entry basis as baseline, but passive F11 reduce-only SELL LIMIT rests in queue for >=2 bars (2h on 1h bars) before filling when bid >= limit; reprices downward after cooldown if price drops (up to EXIT_MAX_ATTEMPTS=5). |
| `UNFILLED_EXIT_TIMEOUT` | Conditional assumption: same entry basis as baseline, severe liquidity drought where F11 reduce-only SELL LIMIT orders cannot fill; exercises bounded repricing up to EXIT_MAX_ATTEMPTS=5 then EXIT_RETRY_LIMIT_EXCEEDED. |
| `GAP_AND_CANCEL_FAULT` | Conditional assumption: widened 10 bps half-spread, PERCENT_PRICE [0.97, 1.03] gap block when bar jump exceeds 3%, 1-bar passive exit wait, plus injected cancel UNKNOWN->RECONCILE fault. |

## 5. 历史 11 个 7 天独立窗口：新旧模型同窗对照（`CONDITIONAL_RISK_ESTIMATED`）

| 资金费情景 | 执行模型 | 执行假设 | 正收益窗 | 均值收益率 (10U) | 最差窗收益 | 均值/最差最大回撤 | 均值持仓 (h) | 均值退出延迟 (h) | 均值残仓 | 残仓窗数 | 轮换成功率 | 熔断窗数 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `EX_FUNDING` | `NO_F11_NO_F08_STRANDED_POSITION_BASELINE` | `IDEAL_LIMIT_FILL` | 2/11 | -1.06% | -6.34% | 2.81% / 8.18% | 81.9 | 81.4 | 0.0218 | 6/11 | 0.0% | 0 |
| `EX_FUNDING` | `F11_EXIT_ONLY_NO_ROTATION` | `IDEAL_LIMIT_FILL` | 0/11 | -0.04% | -0.08% | 0.04% / 0.08% | 0.0 | 0.0 | 0.0000 | 0/11 | 0.0% | 0 |
| `EX_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `IDEAL_LIMIT_FILL` | 0/11 | -0.45% | -0.86% | 0.45% / 0.86% | 0.0 | 0.0 | 0.0000 | 0/11 | 100.0% | 0 |
| `EX_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `LIMITED_CAPACITY_PARTIAL` | 5/11 | -0.47% | -3.17% | 2.45% / 4.13% | 139.0 | 106.0 | 0.0200 | 11/11 | 45.0% | 0 |
| `EX_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `DELAYED_EXIT_FILL` | 0/11 | -1.85% | -3.45% | 2.38% / 4.55% | 27.9 | 2.8 | 0.0036 | 1/11 | 98.4% | 0 |
| `EX_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `UNFILLED_EXIT_TIMEOUT` | 5/11 | -0.94% | -6.34% | 4.85% / 8.18% | 139.0 | 106.0 | 0.0400 | 11/11 | 45.0% | 0 |
| `EX_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `GAP_AND_CANCEL_FAULT` | 1/11 | -2.06% | -6.14% | 2.44% / 6.77% | 13.9 | 1.2 | 0.0000 | 0/11 | 99.2% | 0 |
| `HIST_R04B_FUNDING` | `NO_F11_NO_F08_STRANDED_POSITION_BASELINE` | `IDEAL_LIMIT_FILL` | 2/11 | -1.18% | -6.65% | 2.88% / 8.33% | 81.9 | 81.4 | 0.0218 | 6/11 | 0.0% | 0 |
| `HIST_R04B_FUNDING` | `F11_EXIT_ONLY_NO_ROTATION` | `IDEAL_LIMIT_FILL` | 0/11 | -0.04% | -0.08% | 0.04% / 0.08% | 0.0 | 0.0 | 0.0000 | 0/11 | 0.0% | 0 |
| `HIST_R04B_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `IDEAL_LIMIT_FILL` | 0/11 | -0.45% | -0.86% | 0.45% / 0.86% | 0.0 | 0.0 | 0.0000 | 0/11 | 100.0% | 0 |
| `HIST_R04B_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `LIMITED_CAPACITY_PARTIAL` | 5/11 | -0.54% | -3.32% | 2.49% / 4.21% | 139.0 | 106.0 | 0.0200 | 11/11 | 45.0% | 0 |
| `HIST_R04B_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `DELAYED_EXIT_FILL` | 0/11 | -1.87% | -3.49% | 2.39% / 4.55% | 27.9 | 2.8 | 0.0036 | 1/11 | 98.4% | 0 |
| `HIST_R04B_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `UNFILLED_EXIT_TIMEOUT` | 5/11 | -1.09% | -6.65% | 4.92% / 8.33% | 139.0 | 106.0 | 0.0400 | 11/11 | 45.0% | 0 |
| `HIST_R04B_FUNDING` | `F11_F08_FULL_LIFECYCLE` | `GAP_AND_CANCEL_FAULT` | 1/11 | -2.08% | -6.18% | 2.45% / 6.78% | 13.9 | 1.2 | 0.0000 | 0/11 | 99.2% | 0 |
| `DEMO_STRESS_22.97BPS` | `NO_F11_NO_F08_STRANDED_POSITION_BASELINE` | `IDEAL_LIMIT_FILL` | 0/11 | -16.79% | -40.72% | 16.89% / 40.72% | 81.9 | 81.4 | 0.0218 | 6/11 | 0.0% | 6 |
| `DEMO_STRESS_22.97BPS` | `F11_EXIT_ONLY_NO_ROTATION` | `IDEAL_LIMIT_FILL` | 0/11 | -0.04% | -0.08% | 0.04% / 0.08% | 0.0 | 0.0 | 0.0000 | 0/11 | 0.0% | 0 |
| `DEMO_STRESS_22.97BPS` | `F11_F08_FULL_LIFECYCLE` | `IDEAL_LIMIT_FILL` | 0/11 | -0.45% | -0.86% | 0.45% / 0.86% | 0.0 | 0.0 | 0.0000 | 0/11 | 100.0% | 0 |
| `DEMO_STRESS_22.97BPS` | `F11_F08_FULL_LIFECYCLE` | `LIMITED_CAPACITY_PARTIAL` | 0/11 | -13.77% | -20.36% | 13.84% / 20.36% | 139.0 | 106.0 | 0.0200 | 11/11 | 45.0% | 9 |
| `DEMO_STRESS_22.97BPS` | `F11_F08_FULL_LIFECYCLE` | `DELAYED_EXIT_FILL` | 0/11 | -7.63% | -18.96% | 7.78% / 19.81% | 31.3 | 4.3 | 0.0073 | 2/11 | 96.6% | 2 |
| `DEMO_STRESS_22.97BPS` | `F11_F08_FULL_LIFECYCLE` | `UNFILLED_EXIT_TIMEOUT` | 0/11 | -27.53% | -40.72% | 27.68% / 40.72% | 139.0 | 106.0 | 0.0400 | 11/11 | 45.0% | 10 |
| `DEMO_STRESS_22.97BPS` | `F11_F08_FULL_LIFECYCLE` | `GAP_AND_CANCEL_FAULT` | 0/11 | -4.92% | -12.54% | 5.11% / 13.39% | 16.0 | 1.4 | 0.0036 | 1/11 | 98.4% | 1 |

## 6. 随机路径同路径对照：Model A（旧残仓）vs Model B（F11/F08 完整生命周期）

所有模型在完全相同的 `200` 条路径/情景、同资金（`7.00 USDT` 预算 / `5000 USDT` 钱包）、同费用滑点（`3 bps / 2 bps`）及同资金费流下逐路径配对运行，全部路径 FIFO 对账残差严格为 `0`。

### 6.1 不含资金费（`EX_FUNDING`，0 bps/h）

| 随机情景 | 模型 / 执行假设 | 均值收益 | 亏损概率 (Wilson 95% CI) | VaR95 | CVaR95 (Boot 95% CI) | P95 最大回撤 | 均值持仓 (h) | 均值退出延迟 (h) | 尾部残仓率 | 资费侵蚀 (占7U) | 轮换成功率 | 相对 A ΔCVaR95 |
| --- | --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gbm_low_vol_0.5x` | A: 旧残仓 | +0.07% | 27.0% (`[21.3%, 33.5%]`) | 1.92% | 3.35% (`[2.36%, 4.27%]`) | 3.50% | 75.0 | 74.5 | 51.0% | 0.00% | 0.0% | **基准** |
| `gbm_low_vol_0.5x` | B0: 仅F11 | -0.03% | 51.0% (`[44.1%, 57.8%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-3.28 pp** |
| `gbm_low_vol_0.5x` | B: F11+F08 (IDEAL_LIMIT) | -0.18% | 97.0% (`[93.6%, 98.6%]`) | 0.33% | 0.38% (`[0.33%, 0.42%]`) | 0.33% | 0.0 | 0.0 | 0.0% | 0.00% | 98.9% | **-2.97 pp** |
| `gbm_low_vol_0.5x` | B: F11+F08 (DELAYED) | -0.65% | 94.0% (`[89.8%, 96.5%]`) | 1.72% | 2.87% (`[2.02%, 3.62%]`) | 1.96% | 14.7 | 2.8 | 13.0% | 0.00% | 96.9% | **-0.48 pp** |
| `gbm_base_vol_1.0x` | A: 旧残仓 | -0.13% | 26.5% (`[20.9%, 33.0%]`) | 6.12% | 7.12% (`[6.06%, 7.67%]`) | 7.85% | 82.3 | 81.8 | 51.0% | 0.00% | 0.0% | **基准** |
| `gbm_base_vol_1.0x` | B0: 仅F11 | -0.03% | 51.0% (`[44.1%, 57.8%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-7.05 pp** |
| `gbm_base_vol_1.0x` | B: F11+F08 (IDEAL_LIMIT) | -0.54% | 100.0% (`[98.1%, 100.0%]`) | 0.79% | 0.83% (`[0.80%, 0.87%]`) | 0.79% | 0.1 | 0.0 | 0.0% | 0.00% | 98.3% | **-6.28 pp** |
| `gbm_base_vol_1.0x` | B: F11+F08 (DELAYED) | -2.90% | 100.0% (`[98.1%, 100.0%]`) | 5.32% | 6.21% (`[5.61%, 6.63%]`) | 6.58% | 44.9 | 6.4 | 29.0% | 0.00% | 96.4% | **-0.91 pp** |
| `gbm_high_vol_2.0x` | A: 旧残仓 | -0.04% | 25.5% (`[20.0%, 32.0%]`) | 10.27% | 12.81% (`[10.55%, 14.85%]`) | 14.91% | 81.2 | 80.7 | 49.0% | 0.00% | 0.0% | **基准** |
| `gbm_high_vol_2.0x` | B0: 仅F11 | -0.03% | 49.0% (`[42.2%, 55.9%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-12.74 pp** |
| `gbm_high_vol_2.0x` | B: F11+F08 (IDEAL_LIMIT) | -0.95% | 100.0% (`[98.1%, 100.0%]`) | 1.44% | 2.07% (`[1.46%, 3.18%]`) | 1.44% | 0.8 | 0.1 | 0.5% | 0.00% | 98.6% | **-10.74 pp** |
| `gbm_high_vol_2.0x` | B: F11+F08 (DELAYED) | -7.51% | 99.0% (`[96.4%, 99.7%]`) | 13.52% | 15.57% (`[14.11%, 16.61%]`) | 16.13% | 70.6 | 6.4 | 44.5% | 0.00% | 97.2% | **+2.77 pp** |
| `moving_block_bootstrap_24h` | A: 旧残仓 | -0.48% | 30.0% (`[24.1%, 36.7%]`) | 5.49% | 7.62% (`[5.94%, 9.12%]`) | 7.97% | 73.2 | 72.8 | 47.5% | 0.00% | 0.0% | **基准** |
| `moving_block_bootstrap_24h` | B0: 仅F11 | -0.03% | 47.5% (`[40.7%, 54.4%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-7.55 pp** |
| `moving_block_bootstrap_24h` | B: F11+F08 (IDEAL_LIMIT) | -0.36% | 99.5% (`[97.2%, 99.9%]`) | 0.59% | 0.67% (`[0.62%, 0.72%]`) | 0.59% | 0.0 | 0.0 | 0.0% | 0.00% | 97.9% | **-6.94 pp** |
| `moving_block_bootstrap_24h` | B: F11+F08 (DELAYED) | -1.97% | 97.5% (`[94.3%, 98.9%]`) | 4.84% | 6.60% (`[5.34%, 7.54%]`) | 6.31% | 34.3 | 6.5 | 23.0% | 0.00% | 96.2% | **-1.02 pp** |
| `regime_bootstrap_24h` | A: 旧残仓 | +0.56% | 11.0% (`[7.4%, 16.1%]`) | 2.44% | 5.16% (`[2.99%, 7.39%]`) | 4.81% | 49.5 | 49.2 | 33.0% | 0.00% | 0.0% | **基准** |
| `regime_bootstrap_24h` | B0: 仅F11 | -0.02% | 33.0% (`[26.9%, 39.8%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-5.10 pp** |
| `regime_bootstrap_24h` | B: F11+F08 (IDEAL_LIMIT) | -0.31% | 99.5% (`[97.2%, 99.9%]`) | 0.53% | 0.58% (`[0.54%, 0.63%]`) | 0.53% | 0.0 | 0.0 | 0.0% | 0.00% | 99.1% | **-4.58 pp** |
| `regime_bootstrap_24h` | B: F11+F08 (DELAYED) | -1.46% | 96.5% (`[93.0%, 98.3%]`) | 3.55% | 5.37% (`[3.97%, 6.80%]`) | 4.34% | 23.3 | 3.6 | 17.0% | 0.00% | 97.6% | **+0.21 pp** |
| `jump_stress_downside` | A: 旧残仓 | -2.57% | 41.5% (`[34.9%, 48.4%]`) | 11.01% | 13.02% (`[11.66%, 14.23%]`) | 12.88% | 79.4 | 78.9 | 49.0% | 0.00% | 0.0% | **基准** |
| `jump_stress_downside` | B0: 仅F11 | -0.03% | 49.0% (`[42.2%, 55.9%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-12.96 pp** |
| `jump_stress_downside` | B: F11+F08 (IDEAL_LIMIT) | -0.49% | 99.5% (`[97.2%, 99.9%]`) | 0.75% | 1.82% (`[0.77%, 3.84%]`) | 0.75% | 0.8 | 0.2 | 0.5% | 0.00% | 95.8% | **-11.20 pp** |
| `jump_stress_downside` | B: F11+F08 (DELAYED) | -4.21% | 99.0% (`[96.4%, 99.7%]`) | 11.31% | 12.76% (`[11.48%, 13.78%]`) | 12.38% | 57.2 | 11.3 | 37.0% | 0.00% | 93.4% | **-0.26 pp** |

### 6.2 实测 Demo 高资金费压力（`DEMO_STRESS_22.97BPS`，22.9669 bps/h）

| 随机情景 | 模型 / 执行假设 | 均值收益 | 亏损概率 (Wilson 95% CI) | VaR95 | CVaR95 (Boot 95% CI) | P95 最大回撤 | 均值持仓 (h) | 均值退出延迟 (h) | 尾部残仓率 | 资费侵蚀 (占7U) | 轮换成功率 | 相对 A ΔCVaR95 |
| --- | --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gbm_low_vol_0.5x` | A: 旧残仓 | -12.88% | 51.0% (`[44.1%, 57.8%]`) | 29.07% | 29.88% (`[29.21%, 30.27%]`) | 29.07% | 75.0 | 74.5 | 51.0% | 12.95% | 0.0% | **基准** |
| `gbm_low_vol_0.5x` | B0: 仅F11 | -0.03% | 51.0% (`[44.1%, 57.8%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-29.81 pp** |
| `gbm_low_vol_0.5x` | B: F11+F08 (IDEAL_LIMIT) | -0.18% | 97.0% (`[93.6%, 98.6%]`) | 0.33% | 0.38% (`[0.33%, 0.42%]`) | 0.33% | 0.0 | 0.0 | 0.0% | 0.00% | 98.9% | **-29.50 pp** |
| `gbm_low_vol_0.5x` | B: F11+F08 (DELAYED) | -3.17% | 97.0% (`[93.6%, 98.6%]`) | 7.94% | 13.49% (`[9.40%, 17.39%]`) | 7.94% | 14.7 | 2.8 | 13.0% | 2.52% | 96.9% | **-16.38 pp** |
| `gbm_base_vol_1.0x` | A: 旧残仓 | -14.29% | 51.0% (`[44.1%, 57.8%]`) | 32.66% | 33.81% (`[32.98%, 34.37%]`) | 32.93% | 82.3 | 81.8 | 51.0% | 14.16% | 0.0% | **基准** |
| `gbm_base_vol_1.0x` | B0: 仅F11 | -0.03% | 51.0% (`[44.1%, 57.8%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-33.74 pp** |
| `gbm_base_vol_1.0x` | B: F11+F08 (IDEAL_LIMIT) | -0.55% | 100.0% (`[98.1%, 100.0%]`) | 0.79% | 0.95% (`[0.81%, 1.20%]`) | 0.79% | 0.1 | 0.0 | 0.0% | 0.01% | 98.3% | **-32.85 pp** |
| `gbm_base_vol_1.0x` | B: F11+F08 (DELAYED) | -10.63% | 100.0% (`[98.1%, 100.0%]`) | 22.14% | 27.44% (`[23.53%, 30.53%]`) | 22.15% | 45.4 | 7.0 | 29.5% | 7.76% | 96.0% | **-6.36 pp** |
| `gbm_high_vol_2.0x` | A: 旧残仓 | -13.89% | 49.0% (`[42.2%, 55.9%]`) | 36.29% | 38.63% (`[36.94%, 39.78%]`) | 37.51% | 81.2 | 80.7 | 49.0% | 13.85% | 0.0% | **基准** |
| `gbm_high_vol_2.0x` | B0: 仅F11 | -0.03% | 49.0% (`[42.2%, 55.9%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-38.56 pp** |
| `gbm_high_vol_2.0x` | B: F11+F08 (IDEAL_LIMIT) | -1.07% | 100.0% (`[98.1%, 100.0%]`) | 1.44% | 4.35% (`[1.48%, 8.80%]`) | 1.44% | 0.8 | 0.1 | 0.5% | 0.12% | 98.6% | **-34.28 pp** |
| `gbm_high_vol_2.0x` | B: F11+F08 (DELAYED) | -18.11% | 100.0% (`[98.1%, 100.0%]`) | 34.86% | 37.03% (`[34.16%, 38.85%]`) | 35.03% | 70.0 | 7.7 | 42.0% | 11.91% | 95.6% | **-1.59 pp** |
| `moving_block_bootstrap_24h` | A: 旧残仓 | -13.00% | 47.5% (`[40.7%, 54.4%]`) | 31.45% | 33.59% (`[31.98%, 35.37%]`) | 31.46% | 73.2 | 72.8 | 47.5% | 12.52% | 0.0% | **基准** |
| `moving_block_bootstrap_24h` | B0: 仅F11 | -0.03% | 47.5% (`[40.7%, 54.4%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-33.52 pp** |
| `moving_block_bootstrap_24h` | B: F11+F08 (IDEAL_LIMIT) | -0.36% | 99.5% (`[97.2%, 99.9%]`) | 0.59% | 0.67% (`[0.62%, 0.72%]`) | 0.60% | 0.0 | 0.0 | 0.0% | 0.00% | 97.9% | **-32.91 pp** |
| `moving_block_bootstrap_24h` | B: F11+F08 (DELAYED) | -7.76% | 99.5% (`[97.2%, 99.9%]`) | 22.42% | 26.40% (`[22.79%, 29.18%]`) | 22.69% | 34.3 | 7.0 | 23.0% | 5.80% | 96.1% | **-7.19 pp** |
| `regime_bootstrap_24h` | A: 旧残仓 | -8.08% | 33.0% (`[26.9%, 39.8%]`) | 29.25% | 31.75% (`[29.89%, 33.53%]`) | 29.42% | 49.5 | 49.2 | 33.0% | 8.64% | 0.0% | **基准** |
| `regime_bootstrap_24h` | B0: 仅F11 | -0.02% | 33.0% (`[26.9%, 39.8%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-31.68 pp** |
| `regime_bootstrap_24h` | B: F11+F08 (IDEAL_LIMIT) | -0.31% | 99.5% (`[97.2%, 99.9%]`) | 0.53% | 0.58% (`[0.54%, 0.63%]`) | 0.53% | 0.0 | 0.0 | 0.0% | 0.00% | 99.1% | **-31.17 pp** |
| `regime_bootstrap_24h` | B: F11+F08 (DELAYED) | -5.49% | 99.5% (`[97.2%, 99.9%]`) | 12.47% | 18.99% (`[13.98%, 23.48%]`) | 12.47% | 23.3 | 3.8 | 17.0% | 4.03% | 97.6% | **-12.76 pp** |
| `jump_stress_downside` | A: 旧残仓 | -15.65% | 49.0% (`[42.2%, 55.9%]`) | 37.05% | 38.76% (`[37.53%, 39.68%]`) | 37.12% | 79.4 | 78.9 | 49.0% | 13.08% | 0.0% | **基准** |
| `jump_stress_downside` | B0: 仅F11 | -0.03% | 49.0% (`[42.2%, 55.9%]`) | 0.07% | 0.07% (`[0.07%, 0.07%]`) | 0.07% | 0.0 | 0.0 | 0.0% | 0.00% | 0.0% | **-38.69 pp** |
| `jump_stress_downside` | B: F11+F08 (IDEAL_LIMIT) | -0.60% | 100.0% (`[98.1%, 100.0%]`) | 0.75% | 4.02% (`[0.77%, 10.43%]`) | 0.75% | 0.8 | 0.2 | 0.5% | 0.11% | 95.8% | **-34.74 pp** |
| `jump_stress_downside` | B: F11+F08 (DELAYED) | -13.59% | 100.0% (`[98.1%, 100.0%]`) | 33.68% | 35.48% (`[33.59%, 36.32%]`) | 34.37% | 57.5 | 12.1 | 38.0% | 9.41% | 93.2% | **-3.28 pp** |

## 7. 执行摩擦、撤单故障与资金费异常升高敏感性分析（`CONDITIONAL_RISK_ESTIMATED`）

在 3 类代表性随机情景（`gbm_base_vol_1.0x`、`moving_block_bootstrap_24h`、`jump_stress_downside`，每情景 `128` 条同路径）上，系统检验 5 组执行假设与 3 档资金费率（正常 `0.125 bps/h`、实测压力 `22.97 bps/h`、异常飙升 `50.0 bps/h`）：

| 情景 | 资金费档位 | 模型 / 执行假设 | 均值收益 | 亏损概率 | VaR95 | CVaR95 | P95 回撤 | 持仓 (h) | 退出延迟 (h) | 残仓率 | 熔断率 | 主要退出阻断 | 相对 A ΔCVaR95 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| `gbm_base_vol_1.0x` | `NORMAL_FUNDING_0.125BPS` | A: 旧残仓 | -0.16% | 25.0% | 6.09% | 7.13% | 7.64% | 82.0 | 81.5 | 50.8% | 0.0% | — | **基准** |
| `gbm_base_vol_1.0x` | `NORMAL_FUNDING_0.125BPS` | B: `IDEAL_LIMIT_FILL` | -0.52% | 100.0% | 0.73% | 0.79% | 0.74% | 0.1 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-6.34 pp** |
| `gbm_base_vol_1.0x` | `NORMAL_FUNDING_0.125BPS` | B: `DELAYED_EXIT_FILL` | -2.83% | 100.0% | 5.35% | 5.87% | 6.54% | 45.0 | 6.9 | 28.1% | 0.0% | `EXIT_RETRY_LIMIT_EXCEEDED` | **-1.26 pp** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -14.17% | 50.8% | 32.57% | 33.71% | 32.83% | 82.0 | 81.5 | 50.8% | 50.8% | — | **基准** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.54% | 100.0% | 0.74% | 0.98% | 0.74% | 0.1 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-32.73 pp** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -13.30% | 100.0% | 16.29% | 16.86% | 16.47% | 151.7 | 107.7 | 100.0% | 34.4% | `EXIT_MIN_NOTIONAL` | **-16.86 pp** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -10.50% | 100.0% | 21.92% | 27.40% | 22.06% | 45.5 | 7.7 | 28.1% | 15.6% | `HALTED` | **-6.31 pp** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -26.59% | 100.0% | 32.57% | 33.71% | 32.93% | 151.7 | 107.7 | 100.0% | 100.0% | `HALTED` | **+0.00 pp** |
| `gbm_base_vol_1.0x` | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -6.63% | 100.0% | 15.70% | 23.83% | 16.00% | 24.1 | 2.4 | 17.2% | 7.8% | `EXIT_MIN_NOTIONAL` | **-9.88 pp** |
| `gbm_base_vol_1.0x` | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | -30.75% | 50.8% | 65.12% | 65.94% | 65.12% | 82.0 | 81.5 | 50.8% | 50.8% | — | **基准** |
| `gbm_base_vol_1.0x` | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -0.56% | 100.0% | 0.74% | 1.34% | 0.74% | 0.1 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-64.60 pp** |
| `gbm_base_vol_1.0x` | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -19.74% | 100.0% | 44.99% | 56.98% | 45.04% | 46.3 | 8.6 | 34.4% | 64.8% | `HALTED` | **-8.96 pp** |
| `moving_block_bootstrap_24h` | `NORMAL_FUNDING_0.125BPS` | A: 旧残仓 | -0.50% | 32.0% | 6.28% | 8.43% | 8.25% | 78.7 | 78.2 | 50.8% | 0.0% | — | **基准** |
| `moving_block_bootstrap_24h` | `NORMAL_FUNDING_0.125BPS` | B: `IDEAL_LIMIT_FILL` | -0.37% | 100.0% | 0.59% | 0.68% | 0.59% | 0.0 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-7.76 pp** |
| `moving_block_bootstrap_24h` | `NORMAL_FUNDING_0.125BPS` | B: `DELAYED_EXIT_FILL` | -2.08% | 99.2% | 5.66% | 7.09% | 6.41% | 35.0 | 6.3 | 24.2% | 0.0% | `EXIT_MIN_NOTIONAL` | **-1.35 pp** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -13.89% | 50.8% | 31.22% | 34.27% | 31.29% | 78.7 | 78.2 | 50.8% | 50.8% | — | **基准** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.37% | 100.0% | 0.59% | 0.68% | 0.60% | 0.0 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-33.59 pp** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -12.51% | 100.0% | 15.87% | 17.40% | 15.89% | 143.9 | 104.2 | 100.0% | 29.7% | `EXIT_MIN_NOTIONAL` | **-16.87 pp** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -8.02% | 100.0% | 22.34% | 26.00% | 22.58% | 35.5 | 7.1 | 25.0% | 12.5% | `HALTED` | **-8.27 pp** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -25.02% | 100.0% | 31.74% | 34.80% | 31.78% | 143.9 | 104.2 | 100.0% | 97.7% | `HALTED` | **+0.53 pp** |
| `moving_block_bootstrap_24h` | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -4.86% | 98.4% | 10.77% | 17.67% | 11.29% | 17.1 | 2.1 | 10.9% | 3.1% | `EXIT_MIN_NOTIONAL` | **-16.60 pp** |
| `moving_block_bootstrap_24h` | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | -29.75% | 50.8% | 63.50% | 65.86% | 63.50% | 78.7 | 78.2 | 50.8% | 50.8% | — | **基准** |
| `moving_block_bootstrap_24h` | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -0.37% | 100.0% | 0.60% | 0.69% | 0.60% | 0.0 | 0.0 | 0.0% | 0.0% | `EXIT_MIN_NOTIONAL` | **-65.18 pp** |
| `moving_block_bootstrap_24h` | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -16.12% | 100.0% | 46.99% | 56.43% | 46.99% | 38.4 | 9.3 | 29.7% | 36.7% | `HALTED` | **-9.43 pp** |
| `jump_stress_downside` | `NORMAL_FUNDING_0.125BPS` | A: 旧残仓 | -2.61% | 42.2% | 11.42% | 13.26% | 12.89% | 79.7 | 79.2 | 49.2% | 1.6% | — | **基准** |
| `jump_stress_downside` | `NORMAL_FUNDING_0.125BPS` | B: `IDEAL_LIMIT_FILL` | -0.53% | 100.0% | 0.72% | 2.41% | 0.72% | 1.2 | 0.3 | 0.8% | 0.0% | `EXIT_MIN_NOTIONAL` | **-10.86 pp** |
| `jump_stress_downside` | `NORMAL_FUNDING_0.125BPS` | B: `DELAYED_EXIT_FILL` | -4.23% | 100.0% | 11.36% | 12.74% | 12.43% | 57.6 | 12.1 | 35.2% | 1.6% | `EXIT_MIN_NOTIONAL` | **-0.52 pp** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -15.69% | 49.2% | 37.08% | 38.88% | 37.27% | 79.7 | 79.2 | 49.2% | 49.2% | — | **基准** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.70% | 100.0% | 0.72% | 5.82% | 0.72% | 1.2 | 0.3 | 0.8% | 0.8% | `HALTED` | **-33.06 pp** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -15.46% | 100.0% | 19.09% | 19.68% | 19.13% | 153.7 | 108.0 | 100.0% | 71.9% | `EXIT_MIN_NOTIONAL` | **-19.20 pp** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -13.71% | 100.0% | 34.20% | 35.59% | 34.52% | 58.1 | 13.1 | 35.9% | 29.7% | `HALTED` | **-3.28 pp** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -30.92% | 100.0% | 38.19% | 39.35% | 38.24% | 153.7 | 108.0 | 100.0% | 100.0% | `HALTED` | **+0.47 pp** |
| `jump_stress_downside` | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -8.69% | 100.0% | 28.81% | 30.07% | 28.91% | 32.9 | 5.8 | 21.1% | 15.6% | `HALTED` | **-8.81 pp** |
| `jump_stress_downside` | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | -31.17% | 49.2% | 68.01% | 69.48% | 68.01% | 79.7 | 79.2 | 49.2% | 49.2% | — | **基准** |
| `jump_stress_downside` | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -0.90% | 100.0% | 0.72% | 9.87% | 0.72% | 1.2 | 0.3 | 0.8% | 0.8% | `HALTED` | **-59.62 pp** |
| `jump_stress_downside` | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -26.04% | 100.0% | 60.99% | 63.90% | 60.99% | 61.9 | 13.9 | 43.0% | 67.2% | `HALTED` | **-5.58 pp** |

## 8. 确定性极端压力路径对照（连续下跌、跳空、突发反转、资费飙升、退出无法成交）

下表展示实测 Demo 资金费压力（`DEMO_STRESS_22.97BPS`）与资金费飙升（`FUNDING_SPIKE_50BPS`）下，各确定性压力路径的 Model A 与 Model B 全执行假设对照（完整表见 `deterministic_stress_paired.csv`）：

| 压力路径 (价格变动) | 资金费档位 | 模型 / 执行假设 | 收益率 | 最大回撤 | 持仓 (h) | 退出延迟 (h) | 期末残仓 | 轮换数 | 撤改/故障 | 熔断原因 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -52.22% | 52.22% | 164.0 | 163.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.26% | 0.26% | 0.0 | 0.0 | 0.00 | 4 | 0/0 | — |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -26.11% | 26.11% | 164.0 | 163.0 | 0.02 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -52.22% | 52.22% | 164.0 | 163.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -52.22% | 52.22% | 164.0 | 163.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 5 | 0/0 | — |
| `continuous_decline` (-40.0%) | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | -78.95% | 78.95% | 164.0 | 163.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -0.26% | 0.26% | 0.0 | 0.0 | 0.00 | 4 | 0/0 | — |
| `continuous_decline` (-40.0%) | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -78.95% | 78.95% | 164.0 | 163.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `continuous_decline` (-40.0%) | `FUNDING_SPIKE_50BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -78.95% | 78.95% | 164.0 | 163.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -35.80% | 35.80% | 157.0 | 156.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.19% | 0.19% | 0.0 | 0.0 | 0.00 | 4 | 0/0 | — |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -17.90% | 17.90% | 157.0 | 156.0 | 0.02 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -35.80% | 35.80% | 157.0 | 156.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -35.80% | 35.80% | 157.0 | 156.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `slow_decline` (-15.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 5 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `low_volatility` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.79% | 0.79% | 0.0 | 0.0 | 0.00 | 23 | 0/0 | — |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -23.94% | 23.94% | 158.0 | 78.5 | 0.02 | 1 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -42.71% | 42.71% | 93.0 | 5.4 | 0.04 | 16 | 16/0 | `LOSS_BUDGET_EXHAUSTED` |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -47.88% | 47.88% | 158.0 | 78.5 | 0.04 | 1 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `range_then_gap` (-30.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -15.36% | 15.47% | 42.0 | 2.2 | 0.00 | 18 | 15/1 | `LOSS_BUDGET_EXHAUSTED` |
| `range_then_gap` (-30.0%) | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | +0.00% | 0.00% | 0.0 | 0.0 | 0.00 | 0 | 0/0 | — |
| `range_then_gap` (-30.0%) | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -0.79% | 0.79% | 0.0 | 0.0 | 0.00 | 23 | 0/0 | — |
| `range_then_gap` (-30.0%) | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -66.46% | 66.46% | 119.0 | 10.7 | 0.04 | 10 | 10/0 | `LOSS_BUDGET_EXHAUSTED` |
| `range_then_gap` (-30.0%) | `FUNDING_SPIKE_50BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -77.64% | 77.64% | 158.0 | 78.5 | 0.04 | 1 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -38.47% | 39.95% | 166.0 | 165.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.32% | 0.32% | 0.0 | 0.0 | 0.00 | 4 | 0/0 | — |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -19.24% | 19.97% | 166.0 | 165.0 | 0.02 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -38.47% | 39.95% | 166.0 | 165.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -38.47% | 39.95% | 166.0 | 165.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `insufficient_rebound` (-22.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -38.55% | 40.02% | 166.0 | 165.0 | 0.04 | 0 | 4/1 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | A: 旧残仓 | -27.33% | 27.33% | 163.0 | 162.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `IDEAL_LIMIT_FILL` | -0.82% | 0.82% | 0.0 | 0.0 | 0.00 | 25 | 0/0 | — |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `LIMITED_CAPACITY_PARTIAL` | -13.66% | 13.66% | 163.0 | 162.0 | 0.02 | 0 | 0/0 | — |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `DELAYED_EXIT_FILL` | -13.77% | 21.66% | 78.0 | 78.0 | 0.00 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -27.33% | 27.33% | 163.0 | 162.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `DEMO_STRESS_22.97BPS` | B: `GAP_AND_CANCEL_FAULT` | -21.32% | 22.54% | 144.0 | 28.6 | 0.04 | 4 | 2/1 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `FUNDING_SPIKE_50BPS` | A: 旧残仓 | -60.51% | 60.51% | 163.0 | 162.0 | 0.04 | 0 | 0/0 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `FUNDING_SPIKE_50BPS` | B: `IDEAL_LIMIT_FILL` | -0.82% | 0.82% | 0.0 | 0.0 | 0.00 | 25 | 0/0 | — |
| `fast_reversal` (+0.0%) | `FUNDING_SPIKE_50BPS` | B: `DELAYED_EXIT_FILL` | -28.49% | 34.88% | 78.0 | 78.0 | 0.00 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |
| `fast_reversal` (+0.0%) | `FUNDING_SPIKE_50BPS` | B: `UNFILLED_EXIT_TIMEOUT` | -60.51% | 60.51% | 163.0 | 162.0 | 0.04 | 0 | 4/0 | `LOSS_BUDGET_EXHAUSTED` |

## 9. 样本量收敛与误差稳定性核查

依据误差与稳定性决定必要样本量（不无限追加模拟），下表展示 `DEMO_STRESS_22.97BPS` 下从 `n=32 -> 64 -> 128 -> 200` 路径的 VaR95 / CVaR95 收敛轨迹：

| 情景 | 样本路径数 `n` | Model A VaR95 | Model A CVaR95 | Model B VaR95 | Model B CVaR95 | B CVaR95 Boot 95% CI 宽 | ΔCVaR95 (B-A) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gbm_base_vol_1.0x` | 32 | 32.62% | 33.54% | 0.78% | 1.54% | 1.24 pp | -32.01 pp |
| `gbm_base_vol_1.0x` | 64 | 31.79% | 32.98% | 0.78% | 1.19% | 1.15 pp | -31.79 pp |
| `gbm_base_vol_1.0x` | 128 | 32.57% | 33.71% | 0.74% | 0.98% | 0.63 pp | -32.73 pp |
| `gbm_base_vol_1.0x` | 200 | 32.66% | 33.81% | 0.79% | 0.95% | 0.39 pp | -32.85 pp |
| `moving_block_bootstrap_24h` | 32 | 29.25% | 34.31% | 0.62% | 0.66% | 0.08 pp | -33.66 pp |
| `moving_block_bootstrap_24h` | 64 | 33.07% | 34.81% | 0.59% | 0.67% | 0.12 pp | -34.14 pp |
| `moving_block_bootstrap_24h` | 128 | 31.22% | 34.27% | 0.59% | 0.68% | 0.12 pp | -33.59 pp |
| `moving_block_bootstrap_24h` | 200 | 31.45% | 33.59% | 0.59% | 0.67% | 0.10 pp | -32.91 pp |
| `jump_stress_downside` | 32 | 36.93% | 38.64% | 0.67% | 0.70% | 0.08 pp | -37.94 pp |
| `jump_stress_downside` | 64 | 37.10% | 38.87% | 0.67% | 10.73% | 30.10 pp | -28.14 pp |
| `jump_stress_downside` | 128 | 37.08% | 38.88% | 0.72% | 5.82% | 15.10 pp | -33.06 pp |
| `jump_stress_downside` | 200 | 37.05% | 38.76% | 0.75% | 4.02% | 9.66 pp | -34.74 pp |

## 10. 核心风险结论：F11/F08 是否真正改善尾部风险？

基于同路径配对对照（A 旧残仓 vs B0 仅 F11 vs B 完整 F11+F08）以及 5 组执行假设，得出 4 条清晰、非单向的量化结论：

1. **当 F11 限价退出能够顺利成交时（`IDEAL_LIMIT_FILL`），尾部风险与资金费侵蚀大幅下降**：在 `DEMO_STRESS_22.97BPS` 下，旧模型 A 因突破后永久滞留残仓导致 6 类随机情景 CVaR95 达 `29.88%–38.76%`、均值持仓 `49.5–82.3h`；完整模型 B（`IDEAL_LIMIT_FILL`）将 CVaR95 降至 `0.38%–4.35%`（改善 `-29.50 pp` 至 `-34.74 pp`），资费侵蚀降至 `<=0.12%`。但需注意：即便在理想流动性下，`gbm_high_vol_2.0x` 与 `jump_stress_downside` 仍有 `0.5%` 路径因价格跌破 `125.00 USDT` 触发 `EXIT_MIN_NOTIONAL` 导致残仓与 CVaR 跳升（`4.02%–4.35%`）。
2. **F11 与 F08 对尾部风险的方向并不相同（F08 引入再暴露与连续换手成本）**：同路径消融（`B0 仅 F11` vs `B 完整 F11+F08`）显示，**真正削减单次极端尾部损失的是 F11 止损退出**（`B0` 在全部 6 类情景下 CVaR95 均为 `0.07%`）；而 F08 将原本空仓暂停的账户反复轮换回市场：在 `EX_FUNDING` 下 `B (F11+F08)` 的亏损概率升至 `94%–100%`（因单层 `±1%` 网格在高频突破中连续止损吃手续费/滑点），在 `gbm_high_vol_2.0x (EX_FUNDING)` 与 `regime_bootstrap_24h (EX_FUNDING)` 的 `DELAYED_EXIT_FILL` 下，CVaR95 分别比旧模型 A **恶化 `+2.77 pp` 与 `+0.21 pp`**；在 `range_then_gap` 确定性压力中，旧模型 A 因早期空仓暂停仅损 `0.00%`，而模型 B 因 F08 轮换重新入场在跳空处被套，延迟/无法成交下亏损高达 `-42.71%` 至 `-47.88%`。
3. **一旦退出成交受阻（延迟、限容部分成交、无法成交或跳空越带），F11 的尾部保护会显著衰减甚至失效**：
   - `DELAYED_EXIT_FILL`（2h 被动排队 + 撤改）：在 `DEMO_STRESS_22.97BPS` 下，相对旧模型 A 的 CVaR95 改善缩窄至 `-16.38 pp` 至 `-1.59 pp`，高波动与跳跃情景下 CVaR95 仍高达 `35.48%–37.03%`；在单边下跌 `continuous_decline` / `slow_decline` 中，连续 4 次撤改仍无法成交最终触及 `LOSS_BUDGET_EXHAUSTED` 熔断，亏损与旧模型完全一致（`-52.22%` / `-35.80%`）；
   - `LIMITED_CAPACITY_PARTIAL`（每 bar 限容 `0.02`）：买单部分成交 `0.02` 后遇突破撤单留下碎仓（`< 5 USDT`），触发 **`EXIT_MIN_NOTIONAL`** 导致 3 类敏感性情景残仓率升至 `100.0%`（但因半仓暴露，CVaR95 约为旧模型一半 `16.86%–19.68%`）；
   - `UNFILLED_EXIT_TIMEOUT`（退出长时间无法成交）：F11 达 5 次撤改上限或 `1.00 USDT` 亏损熔断，叠加 F08 早期空仓轮换将所有路径最终拖入持仓被套，导致残仓率 `100.0%`、均值亏损翻倍（如 `gbm_base_vol_1.0x` `-26.59%` vs A `-14.17%`），CVaR95 与旧模型持平或更差（`+0.00 pp` 至 `+0.53 pp`）；
   - `GAP_AND_CANCEL_FAULT`（宽点差 + 3% 跳空越带 + 撤单 `UNKNOWN` 恢复）：在 `DEMO_STRESS_22.97BPS` 下保留部分改善（ΔCVaR95 `-8.81 pp` 至 `-16.60 pp`），但仍存在 `10.9%–21.1%` 尾部残仓与 `EXIT_MIN_NOTIONAL` / `HALTED` 阻断。
4. **实证边界声明（`EMPIRICAL_TAIL_RISK_UNVERIFIED`）**：尽管离线工程（`ENGINEERING_ACCEPTED`）与单次官方 Demo 生命周期重放（`LIFECYCLE_REPLAY_VERIFIED`）均已零残差验证，真实交易所下行突破时的 L2 盘口深度、排队成交概率、撤改滑点及极端资金费持续性尚无实证分布样本，因此上述 VaR95 / CVaR95 均属条件估计（`CONDITIONAL_RISK_ESTIMATED`），不得外推为实盘尾部风险保证。

## 11. 可复现配置与运行命令

- 随机种子：`seed=909`；主对比样本量：`n_paths=200`；敏感性样本量：`sensitivity_paths=128`；步长：`168h`；总耗时：`1231.47s`（`workers=20`）。
- 输出产物：`summary.json`、`demo_replay_counterfactuals.csv`、`historical_windows_paired.csv`、`historical_windows_summary.csv`、`stochastic_paired_comparison.csv`、`execution_profile_sensitivity.csv`、`deterministic_stress_paired.csv`、`sample_convergence.csv`、`REPORT.md`。

```powershell
python tools/research/run_p8_r09.py --cloud-snapshot "<local-r08-cloud-snapshot>" --windows-snapshot "<local-windows-snapshot>" --r04b-snapshot "<r04b-certified-snapshot>" --output-dir "docs/validation/p8-r09"
```
