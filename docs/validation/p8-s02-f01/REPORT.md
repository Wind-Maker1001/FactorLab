# FactorLab P8-S02-F01：Binance 永续合约自动研究、优化与 Champion 选择报告

> **安全边界**：`NO LIVE MONEY` · 纯离线研究、Champion 选择与 S04 原生协议接线，不操作 Azure，不提交真实 Binance Demo 订单，`financial_admission = FORMAL_ADMISSION_BLOCKED` 保持不变。

## 0. 五大核心问题明确回答

1. **自动研究功能是否真实运行？**
   **是。** 在现有 S02 唯一编排与选择框架（`factorlab.research.automatic.run` / `replay` + `ChampionSelector` + `ExperimentTracker` + `tools/research/select_champion.py`）上完成 Binance USD-M 扩展，并已基于修复后的代码与最新 `origin/main`（`2529fa6`，含 PR #246 S04）重跑完整两类实证：(a) 合成协议端到端验收及全部 9 项独立审查缺陷 + OOS 新鲜度回归测试（`tests/research/test_binance_champion_research.py`、`tests/integrations/test_binance_champion_s04_integration.py`）；(b) 基于本地已审计 193 天真实 `NVDAUSDT` 1h 行情（4,632 根真实 OHLCV K 线，零失真导出）与完整 593 条历史资金费结算记录的真实 6 大家族 25 组候选 + 既有 `FIXED_GRID` incumbent 完整自动研究、风险压力测试、独立回放与 S03/S04 发布校验（`tools/research/run_binance_champion_research.py`，研究 ID `p8-s02-f01-nvda-real-v2`）。

2. **Binance 执行及资金费口径是否完整？**
   **离线研究第一阶段口径完整、零残差对账且修复了全部已复现执行/核算缺陷，但明确不冒充真实交易所撮合深度。** 严格限定 `NVDAUSDT`、单向持仓（`ONE_WAY_LONG_ONLY`）、`LIMIT` 挂单、1x 全额抵押风险口径，复用现有 `PaperBroker` + `TradingController` + `execution_candidate` + R09 F11/F08 生命周期护栏与 R10 修复后的同时间戳资金费结算规则（`fill_time < funding_time`），并补齐：(a) 风险触发 `halted` 后立即撤除全部未成交/部分成交非只减仓 `BUY` 挂单并保留合法只减仓 `SELL`（支持显式撤单成交竞态假设）；(b) 退出事件生命周期与订单尝试次数严格与每小时轮询检查解耦；(c) 压力测试异常与真实 `PaperBroker` 账本爆仓严格分类；(d) 导出数据完整保留真实 `open` 与浮点 `volume` 且缺失资金费分段一律 fail-closed。

3. **是否产生真实合格 Challenger？**
   **否（真实历史研究结论仍为 `INCONCLUSIVE`，`promoted = false`，原样保留 incumbent `FIXED_GRID` `c-7eefa330d4d7fa022db5d1ff`，版本保持 `v0`）。** 在真实 193 天 NVDAUSDT 数据（`train: 2026-05-14..2026-07-13`，`validation: 2026-07-14..2026-09-11`，`test: 2026-09-12..2026-11-22`）上：
   - 全部网格类候选（`FIXED_GRID_ALT`、`FUNDING_SENSITIVE_GRID` B1–B6、`VOL_ADAPTIVE_GRID` C1–C6）在验证期均为负收益（`-7.31%` 至 `-12.00%`）而止步验证阶段；
   - `CASH` 基准零成交零收益，被 `capacity_insufficient_or_unknown`、`net_return_below_floor` 与 `insufficient_trade_activity` 阻断；
   - 入围封闭测试的 2 组 S01 趋势目标候选 `DUAL_MA (5/48)`（`c-771426d69d072db5f561c7b1`）与 `CLOSE_MOMENTUM (lb=1)`（`c-ef5d445aa2f93fb3e13cfc27`）在 72 天下跌测试窗口中分别录得 `-12.53%` 与 `-25.89%` 净收益，触发 `net_return_below_floor`、`oos_window_not_fresh_unconsumed`（强制锁定 `<= 2026-11-22` 为历史复用伪 OOS）以及 25 候选族 Bonferroni 调整后 HAC/Bootstrap/Score 置信区间跨零。严格拒绝因"少亏"或点估计略高而放宽门槛。

4. **输出能否被 S03/S04 正常消费？**
   **能，且已全面切换至最新 `main`（PR #246）原生 `FACTORLAB_DECISION_V1` 协议。**
   - 删除了 `dual_ma_usdm.py`、`close_momentum_usdm.py`、`cash_benchmark.py` 中与研究不等价的固定 `0.04` 数量 `evaluate/v1` 逻辑；
   - `build_s03_release_envelope` 直接以原始冻结策略源码、策略类与参数构建原生 `FACTORLAB_DECISION_V1` `StrategyPackage`（携带 `strategy_id`、`strategy_version`、`state_version`、`execution_mode="target"`、`interval_seconds=3600`），并经 `adapt_selection`、`LoadedStrategy.from_strategy_package` 隔离 worker、以及 S04 `ReleaseConsumer` + `DemoStrategyRuntime` 真实 `closed_bar` → `quote` → 假网络 `POST /fapi/v1/order` 全链路实证（不再仅用 `probe`）；
   - 真实历史未晋升结果经 `azure_strategy_deploy.cycle` 离线消费，返回 `accepted = false, reason = "RETAIN_CHAMPION"`；
   - 网格族策略继续诚实标记兼容性边界：`FIXED_GRID` 对应 `LEGACY_DEMO_GRID_BRIDGE`，`FUNDING_SENSITIVE_GRID` 与 `VOL_ADAPTIVE_GRID` 明确记录待 S04/R11 补齐资金费/波动率上下文桥接。

5. **还缺哪些实际证据？**
   - **真正未消费的新 OOS 样本窗口**：现有已审计 193 天历史数据（截至 `2026-11-23T00:00:00Z`）已内置为不可伪造新鲜度的历史复用基线，需等待至少 `min_oos_days = 60` 个完整新 UTC 日的真实未见样本；
   - **真实交易所历史订单簿/逐笔成交与实时规则快照证据**：离线回放采用 1h 成交收盘价与 `DELAYED_EXIT_FILL` 假设，不代表真实 L2 盘口深度；
   - **并行 PR #251（R11）与 PR #252（PERF-01）合入后的后续联调**：本轮严格不引入未合入 PR 代码，已在第 8 节明确记录与 R11 生命周期边界对齐点；
   - **独立审查复核与 Codex Review**：本轮已修复原独立审查报告中全部 9 项缺陷 + OOS 新鲜度问题，待原独立审查代理复审（Codex Review 额度耗尽，用户后续自行复审）。

---

## 1. PR #253 独立审查缺陷修复总览（P1 × 3 + P2 × 6 + OOS 新鲜度）

> 同步最新 `origin/main` 基线：`2529fa61f5f8b862970ea430f797effe1c2f7bc6`（含 PR #246 `P8-S04: S01/S03 unified strategy protocol and Azure cloud execution acceptance`），零冲突合入原分支 `4704bcfbf4cb720b5050ac24cf8ed95ccc841265`，保留全部 S04 既有行为。

| 编号 | 级别 | 独立审查确认问题 | 根因定位与修复方案 | 修改文件 | 回归测试覆盖 |
| --- | --- | --- | --- | --- | --- |
| **F1** | **P1** | 研究与发布仓位语义不一致（发布侧 `evaluate()` 硬编码 `0.04` 忽略 `weight`/`max_weight`、权益与部分持仓） | 删除 `dual_ma_usdm.py` / `close_momentum_usdm.py` / `cash_benchmark.py` 的固定 `0.04` `evaluate/v1` 实现；`build_s03_release_envelope` 改为构建原生 S04 `FACTORLAB_DECISION_V1` 策略包（`execution_mode="target"`），经 `LoadedStrategy.from_strategy_package` + `DemoStrategyRuntime` 直接调用同一冻结策略 `decide()` 与 `target_quantity`；研究侧目标仓位计算同步对齐 `buy_limit`/`sell_limit` 与 `max_notional` 截断合同 | `src/factorlab/strategies/binance_usdm/{dual_ma_usdm,close_momentum_usdm,cash_benchmark}.py`, `src/factorlab/research/binance_perpetual.py`, `src/factorlab/research/automatic.py` | `test_f1_legacy_fixed_quantity_evaluate_removed_from_new_candidates`, `test_f1_research_and_s04_release_position_sizing_equivalence` (4 组参数化场景，覆盖 DualMA/CloseMomentum、不同权益、部分持仓、减仓、MIN_NOTIONAL 与假网络 `POST /fapi/v1/order`) |
| **F2** | **P1** | 冻结 `3600s` 策略可在 `60s` Feed 上运行并提交 `LIMIT BUY` | 策略包 manifest 显式绑定 `interval_seconds=3600` 并经 `LoadedStrategy.from_strategy_package` 与 `ReleaseConsumer._load_decision_runner` 严格校验 `manifest.interval_seconds == active_feed.seconds`；在 `DemoStrategyRuntime.closed_bar`（同时覆盖 `historical_bar`）补充 `bar.interval_seconds` 周期一致性守卫；不匹配时在激活/热切/重启/回补前直接拒绝并回滚，零 POST | `src/factorlab/research/binance_perpetual.py`, `src/factorlab/integrations/binance_futures_trading/strategy_runtime.py` | `test_f2_hourly_3600s_strategy_rejected_on_60s_feed_with_zero_posts` (覆盖 60s 激活拒绝、3600s 正常激活、未闭合/60s 历史与实时 bar/重复/乱序/缺口 bar、重启切换 60s 回滚，全程零 POST) |
| **F3** | **P1** | 风险 `halted=True` 后旧部分成交 `BUY` 挂单未撤销并在下根 K 线继续成交加仓 | 在 `simulate_binance_candidate` 的目标与网格双循环中，于资金费扣减后及亏损触发 `halted` 时无条件调用 `cancel_open_entry_buys_on_halt()`（不依赖 `decision.target_weight` 是否为 `None`），立即撤回未提交 `BUY` 提案并撤销全部活跃非只减仓 `BUY` 挂单；严格保留合法 `reduce_only=True` `SELL` 退出单；支持显式 `cancel_fill_race=True` 撤单成交竞态假设（真实记录竞态成交不伪造零成交）；`halted` 单调不可重置并阻断 F08 轮换 | `src/factorlab/research/binance_perpetual.py` | `test_f3_halt_cancels_partially_filled_open_buy_even_on_hold_signal` (含 MTM 停机、资金费停机与 `cancel_fill_race=True` 对照), `test_f3_halt_preserves_reduce_only_sell_and_never_resets_on_rebound` |
| **F4** | **P2** | 共享账户多标的持仓污染（旧 `evaluate()` 累加全部币种持仓） | 移除旧 `evaluate()` 后全面复用 S04 `DemoStrategyRuntime`（仅读取 `instrument_id == self.symbol` 的 `NVDAUSDT` 持仓，同时保持账户级共享 USDT 权益与可用余额约束） | `src/factorlab/strategies/binance_usdm/*.py`, S04 `strategy_runtime.py` | `test_f4_multi_symbol_shared_account_positions_do_not_contaminate_nvda` (假交易所 `/fapi/v3/account` & `/fapi/v3/positionRisk` 注入 `QQQUSDT=5.00`、`BTCUSDT=0.50` 时 `NVDAUSDT` 正确执行 `BUY`) |
| **F5** | **P2** | 首次激活丢弃历史预热，策略从空历史冷启动 | 切换原生 `FACTORLAB_DECISION_V1` 后，复用 S04 `ReleaseConsumer.historical_bar` + `history_watermark()` + `SignalAdapter`：`history_limit` 未满时处于显式 `INSUFFICIENT_DATA`（零 POST），通过历史 bar 预热补足窗口且零历史下单，首根可交易实时闭合 bar 与研究侧决策完全一致 | `src/factorlab/research/binance_perpetual.py`, S04 `demo_strategy_release.py` | `test_f5_first_activation_historical_warmup_without_historical_orders` (`DualMA(5, 20)` `history_limit=21` 预热前后对比与假网络下单) |
| **F6** | **P2** | 重复每小时轮询检查计入 `exit_attempts` / `exit_failures`，导致退出失败率随窗口时长畸变 | 拆分并独立记录 `exit_lifecycle_count`、`failed_exit_lifecycle_count`、`exit_order_attempt_count`、`failed_exit_order_attempt_count` 与 `exit_status_poll_checks`；`exit_failure_ratio = failed_exit_lifecycle_count / exit_lifecycle_count`，轮询检查永不进入分子或分母 | `src/factorlab/research/binance_perpetual.py` | `test_f6_exit_failure_metrics_do_not_inflate_with_window_duration` (`UNFILLED_EXIT_TIMEOUT` 48h vs 72h 退出生命周期、订单尝试与失败率完全一致，轮询计数严格分离) |
| **F7** | **P2** | 风险压力测试将任意程序/校验异常伪装为 `COLLATERAL_DEPLETED` 与 `-100%` 收益 | 引入 `CollateralDepletedError` 仅在权威 `PaperBroker` 账本证明 `equity <= 0` 时抛出；压力套件明确区分 `OK`、`RISK_HALTED`、`COLLATERAL_DEPLETED` 与 `SIMULATION_FAILED`（程序异常输出 `net_return=None`, `max_drawdown=None`, `cvar_95=None`, `error_type`, `detail`，绝不伪造 `-1.0`） | `src/factorlab/research/binance_perpetual.py`, `src/factorlab/research/automatic.py` | `test_f7_stress_suite_distinguishes_program_exceptions_from_collateral_depletion` (覆盖 `ValueError` checksum 异常与真实 `CollateralDepletedError`) |
| **F8** | **P2** | 缺少后 13 天资金费缓存文件时离线导出静默按 0 资金费继续 | 在 `export_audited_nvda_dataset` 与 `validate_funding_coverage` 中强制校验完整资金费时间覆盖：当导出 bar 越过 `2026-11-10Z` 且缺失 `nvdausdt_funding_20261110_20261123.json` 或首尾滞后 `> 8.05h` / 内部缺口 `> 24h` 时直接 fail-closed 报错，同时正确保留真实 `0.0` 费率事件（341 条） | `tools/research/run_binance_champion_research.py`, `src/factorlab/research/binance_perpetual.py`, `src/factorlab/research/automatic.py` | `test_f8_and_f9_funding_coverage_and_true_ohlcv_export_fidelity` (覆盖 0 费率合法通过、缺尾 13 天报错、内部缺口报错、缺缓存文件报错) |
| **F9** | **P2** | 导出的“已审计 OHLCV”用 `close` 覆盖真实 `open` 并截断 `volume` 为整数 | 在 `grid_empirical.py` 与 `grid_funding_adaptive.py` 保留源 `open` 与 `raw_volume`（保持原有 R02/R10 `volume` 整数接口兼容）；`export_audited_nvda_dataset` 严禁以 `close` 替代 `open`（缺失直接报错），完整保留真实 `open` 与浮点 `volume`；`load_binance_bars_and_funding` 保留浮点 `volume` | `src/factorlab/experiments/grid_empirical.py`, `src/factorlab/experiments/grid_funding_adaptive.py`, `tools/research/run_binance_champion_research.py`, `src/factorlab/research/binance_perpetual.py` | `test_f8_and_f9_funding_coverage_and_true_ohlcv_export_fidelity` + 真实 4,632 根导出回环校验（5 列最大绝对误差均为 `0.000000000000`） |
| **OOS 新鲜度** | **治理阻断** | 新建 `state_path` 并将历史窗口改标为 `FRESH_UNCONSUMED_OOS` 可绕过历史复用拦截 | (1) 内置权威已知历史消费下界 `KNOWN_CONSUMED_HISTORY_END_UTC[("BINANCE_USDM", "NVDAUSDT")] = "2026-11-22"`，任何 `REAL_CANDIDATE` 与之重叠（含部分重叠）均强制降级为 `HISTORICAL_REUSE_PSEUDO_OOS` 且永不晋升；(2) 增加带文件锁的跨研究共享消费台账 `oos-consumption-registry.json`（`TEST_RESERVED` 原子落盘，按 `(market, symbol, [test_start, test_end])` 拦截跨 `state_path`、换 `study_id`、重导出文件和部分重叠复用）；(3) `replay(audit=True)` 永不修改台账 | `src/factorlab/research/binance_perpetual.py`, `src/factorlab/research/automatic.py` | `test_oos_freshness_blocks_relabeling_known_history_and_cross_state_reuse` (覆盖已知历史改标、跨 `state_path` 部分重叠复用、`replay` 零变更) |

---

## 2. 真实 NVDAUSDT 研究重跑前后对比（旧 HEAD `4704bcf` vs 修复后 `v2`）

### 2.1 数据导出与资金费覆盖前后对比

| 审计项 | 修复前 (`4704bcf` / `p8-s02-f01-nvda-real-v1`) | 修复后 (`v2` / `p8-s02-f01-nvda-real-v2`) | 变化说明 |
| --- | --- | --- | --- |
| 导出 K 线数量 / 连续性 | 4,632（0 缺失） | 4,632（0 缺失，新增 `interval="1h"` 列） | 完整连续 |
| 真实 `open != close` 根数 | **0 / 4,632**（全部被 `close` 覆盖） | **4,576 / 4,632**（与原始 R04B + R07/Post-R07 差值 `0.0`） | F9 彻底修复 |
| 浮点 `volume` 保留根数 | **0 / 4,632**（全部截断为整数） | **4,585 / 4,632**（与原始源最大差值 `0.0`） | F9 彻底修复 |
| 资金费覆盖事件与校验 | 593 条（缺后 13 天缓存时静默退化为 554 条） | **593 条**（`2026-05-14T00:00Z` → `2026-11-22T16:00Z`，最大间隔 `8.0h`，`341` 条真实 `0.0` 费率，缺缓存强制报错） | F8 彻底修复 |
| `bars_sha256` | `e7a86b3a...`（旧失真文件） | 见最新 [p8_s02_f01_binance_nvda_frozen.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/configs/research/p8_s02_f01_binance_nvda_frozen.json) | 真实 OHLCV 哈希重新冻结 |

### 2.2 核心候选测试阶段前后指标对比（`test: 2026-09-12..2026-11-22`，72d）

| 策略 | 版本 | 净收益 | MaxDD | 资金费 PnL | 成交笔数 | 退出失败率（新旧口径对比） | 轮询检查次数 | 停机标志 |
| --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| `FIXED_GRID` (Incumbent) | 旧 `4704bcf` | -38.27% | 42.19% | -0.0509 U | 65 | `336 / 432` (**77.8%**, 受轮询畸变) | 未分离 | `1.0` |
| `FIXED_GRID` (Incumbent) | **新 `v2`** | **-38.27%** | **42.19%** | **-0.0509 U** | **65** | 生命周期 `1 / 33` (**3.0%**)；订单尝试 `65 / 97` | **378** | **`1.0`** |
| `DUAL_MA (5/48)` | 旧 `4704bcf` | -12.53% | 17.87% | -0.0536 U | 39 | `0 / 24` (0.0%) | 未分离 | `0.0` |
| `DUAL_MA (5/48)` | **新 `v2`** | **-12.53%** | **17.87%** | **-0.0536 U** | **39** | 生命周期 `0 / 24` (**0.0%**)；订单尝试 `5 / 24` | **0** | **`0.0`** |
| `CLOSE_MOMENTUM (lb=1)` | 旧 `4704bcf` | -25.89% | 33.47% | -0.1173 U | 17 | `157 / 850` (18.5%) | 未分离 | `1.0` |
| `CLOSE_MOMENTUM (lb=1)` | **新 `v2`** | **-25.89%** | **33.47%** | **-0.1173 U** | **17** | 生命周期 `78 / 427` (**18.3%**)；订单尝试 `763 / 771` | **167** | **`1.0`** |
| `CASH` | 旧 / **新 `v2`** | 0.00% | 0.00% | 0.0000 U | 0 | `0 / 0` (0.0%) | 0 | `0.0` |

> **关键变化说明**：
> - 修复 F6 后，`FIXED_GRID` 在 72 天测试窗口的真实退出生命周期失败率从畸变的 `77.8% (336/432)` 还原为 `3.0% (1/33)`，378 次每小时状态轮询检查被单独记录于 `exit_status_poll_checks`；
> - 验证期 25 组候选的入围集合保持完全一致（仍为 `DUAL_MA (5/48)` 与 `CLOSE_MOMENTUM (lb=1)`），两者的 `candidate_id` 因切换至原生 `FACTORLAB_DECISION_V1` 策略源码哈希而同步更新（`DUAL_MA`: `c-771426d69d072db5f561c7b1`，`CLOSE_MOMENTUM`: `c-ef5d445aa2f93fb3e13cfc27`）；
> - 最终 Champion 结论保持不变：`verdict = INCONCLUSIVE`、`promoted = false`、`selected = FIXED_GRID (c-7eefa330d4d7fa022db5d1ff)`、`version = v0`、`financial_admission = FORMAL_ADMISSION_BLOCKED`。

### 2.3 风险压力测试前后对比（`RISK_STRESS_ONLY_NOT_OOS_EVIDENCE`）

详见最新 [risk_stress.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/risk_stress.json)（总计 `16` 组：`ok_runs = 9`、`risk_halted_runs = 7`、`collateral_depleted_runs = 0`、`simulation_failed_runs = 0`）：

| 策略 | `mainnet_historical_full` (72d) | `mainnet_5x_funding_full` (72d) | `demo_high_funding_22_97bps_h_168h` (7d) | `funding_regime_switch_48h_168h` (7d) |
| --- | --- | --- | --- | --- |
| `FIXED_GRID` | `RISK_HALTED` `-38.27%`, DD `42.19%`, 资费 `-0.0509 U` | `RISK_HALTED` `-40.31%`, DD `43.97%`, 资费 `-0.2547 U` | `RISK_HALTED` `-24.55%`, DD `25.72%`, 资费 `-1.4866 U` | `OK` `-20.37%`, DD `21.69%`, 资费 `-1.0688 U` |
| `DUAL_MA (5/48)` | `OK` `-12.53%`, DD `17.87%`, 资费 `-0.0536 U` | `OK` `-14.65%`, DD `19.18%`, 资费 `-0.2680 U` | `OK` `-4.86%`, DD `5.82%`, 资费 `-0.3810 U` | `OK` `-4.86%`, DD `5.82%`, 资费 `-0.3810 U` |
| `CLOSE_MOMENTUM (lb=1)` | `RISK_HALTED` `-25.89%`, DD `33.47%`, 资费 `-0.1173 U` | `RISK_HALTED` **`-29.52%`** (旧 `-30.79%`), DD **`35.98%`** (旧 `36.41%`), 资费 `-0.5778 U` | `RISK_HALTED` `-32.56%`, DD `33.68%`, 资费 `-2.2179 U` | `RISK_HALTED` `-26.05%`, DD `27.33%`, 资费 `-1.5676 U` |
| `CASH` | `OK` `0.00%` / DD `0.00%` | `OK` `0.00%` / DD `0.00%` | `OK` `0.00%` / DD `0.00%` | `OK` `0.00%` / DD `0.00%` |

> `CLOSE_MOMENTUM` 在 `mainnet_5x_funding_full` 下净收益由 `-30.79%` 改善至 `-29.52%`（最大回撤由 `36.41%` 降至 `35.98%`），直接源于 F3 修复后在停机瞬间及时撤销了未成交入场 `BUY` 挂单。

---

## 3. Binance 永续合约第一阶段研究合同与首批 6 大家族兼容性矩阵

冻结配置位于 [p8_s02_f01_binance_nvda_frozen.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/configs/research/p8_s02_f01_binance_nvda_frozen.json)：

- **执行合同**：`binance_usdm_demo_nvda_limit_long_only`（`NVDAUSDT` / `BINANCE_USDM` / `1h` UTC / `ONE_WAY_LONG_ONLY` / `LIMIT` / `1x`）
- **过滤与成本**：`min_notional = 5 USDT`、`step_size = 0.01`、`tick_size = 0.01`、`quantity_unit = 0.04`、`max_quantity = 0.04`、`PERCENT_PRICE = [0.97, 1.03]`、手续费 `3.0 bps`、滑点 `2.0 bps`、参与度上限 `1.0%`、离线 1x 抵押预算 `10.00 USDT`（显式记录 `demo_live_cap_usdt = 7.0`）
- **资金费规则**：严格复用 R10 `settlement_before_exact_tie_fill`（`fill_time < funding_time`），逐候选资金费与权益盯市对账残差 `< 1e-6`

| 家族 | 策略文件 | 候选数 | 离线研究状态 | S03/S04 发布兼容性（已升级至最新 `main` S04） |
| --- | --- | ---: | --- | --- |
| **A. `CASH`** | [cash_benchmark.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/cash_benchmark.py) | 1 | 已运行（仅对照基准，不可充当正 Alpha） | 原生 `FACTORLAB_DECISION_V1` (`cash_benchmark:CashBenchmarkStrategy`, `target`, `3600s`) |
| **B. `FIXED_GRID`** | [fixed_grid.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/fixed_grid.py) | 1 incumbent + 2 alt | 已运行 | `LEGACY_DEMO_GRID_BRIDGE`（现有 Demo 网格控制组） |
| **C. `FUNDING_SENSITIVE_GRID`** | [funding_sensitive_grid.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/funding_sensitive_grid.py) | 6 (`B1..B6`) | 已运行（转 `LIMIT` 只减仓退出） | 需 S04/R11 补齐云端资金费事件上下文与网格桥接（禁止伪造兼容） |
| **D. `VOL_ADAPTIVE_GRID`** | [vol_adaptive_grid.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/vol_adaptive_grid.py) | 6 (`C1..C6`) | 已运行（`sigma_ref` 仅在训练窗拟合） | 需 S04/R11 补齐云端滚动波动率状态与网格桥接（禁止伪造兼容） |
| **E. `DUAL_MA`** | [dual_ma_usdm.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/dual_ma_usdm.py) | 4 | 已运行 | **原生兼容 `FACTORLAB_DECISION_V1`** (`dual_ma_usdm:DualMAUsdmStrategy`, `target`, `3600s`，已通过 S04 假网络闭环下单等价验证) |
| **F. `CLOSE_MOMENTUM`** | [close_momentum_usdm.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/close_momentum_usdm.py) | 6 | 已运行 | **原生兼容 `FACTORLAB_DECISION_V1`** (`close_momentum_usdm:CloseMomentumUsdmStrategy`, `target`, `3600s`，已通过 S04 假网络闭环下单等价验证) |

---

## 4. 配对统计推断结果（`p8-s02-f01-nvda-real-v2`，`family_size = 25`，Bonferroni 调整）

详见 [paired_statistics.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/paired_statistics.json)：

| 入围候选 vs `FIXED_GRID` | 日均差 HAC 调整区间 | 移动块 Bootstrap 调整区间 | 目标分数差 CI | 块胜率 | 否决原因 |
| --- | --- | --- | --- | ---: | --- |
| `DUAL_MA (5/48)` (`c-771426d69d072db5f561c7b1`) | `[-0.172%, +1.101%]` | `[-0.025%, +1.107%]` | `[-0.013, +4.610]` | 64.3% | `net_return_below_floor`, `oos_window_not_fresh_unconsumed`, `oos_advantage_statistically_uncertain`, `risk_adjusted_advantage_statistically_uncertain` |
| `CLOSE_MOMENTUM (lb=1)` (`c-ef5d445aa2f93fb3e13cfc27`) | `[-0.079%, +0.567%]` | `[-0.047%, +0.575%]` | `[-0.063, +2.246]` | 71.4% | `max_drawdown_breach_or_unknown`, `daily_cvar_breach_or_unknown`, `net_return_below_floor`, `oos_window_not_fresh_unconsumed`, `oos_advantage_statistically_uncertain`, `risk_adjusted_advantage_statistically_uncertain` |

---

## 5. 与未合入 PR #251 (R11) 生命周期边界说明（Section 12）

本轮严格不引入尚未合入 `main` 的 PR #251（R11）与 PR #252（PERF-01）代码，同时确保 `src/factorlab/research/binance_perpetual.py` 中的 F3（风险停机撤单）与 F6（退出生命周期核算）与 R11 保持清晰的后续统一接口：

- **可统一复用的状态与边界**：
  1. `cancel_open_entry_buys_on_halt()` 与 `cancel_fill_race` 竞态模型：统一以 `controller.account().open_orders` 为权威挂单源，按 `side == BUY and not reduce_only` 过滤撤销，严格保留 `reduce_only=True` 的 `SELL` 退出单；
  2. `exit_lifecycle_count` / `failed_exit_lifecycle_count` / `exit_order_attempt_count` / `failed_exit_order_attempt_count` / `exit_status_poll_checks` 五项指标契约：后续 R11 合入后可直接对接同一退出生命周期状态机与遥测字段名，避免两套退出失败率口径漂移；
  3. 跨研究 OOS 消费台账 `FACTORLAB_OOS_CONSUMPTION_REGISTRY_V1` 与 `KNOWN_CONSUMED_HISTORY_END_UTC`：R11 新策略接入 S02 时自动受同一历史基线（`<= 2026-11-22`）与跨 `state_path` 防重复消费保护。

---

## 6. 交付工件与独立回放核验

- **冻结研究配置**：[configs/research/p8_s02_f01_binance_nvda_frozen.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/configs/research/p8_s02_f01_binance_nvda_frozen.json)
- **原生 Champion 决策结果**（`FACTORLAB_CHAMPION_SELECTION_V1`）：[docs/validation/p8-s02-f01/selection.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/selection.json)
- **完整候选研究结果**：[docs/validation/p8-s02-f01/candidates.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/candidates.json)
- **冻结入围清单**：[docs/validation/p8-s02-f01/frozen-search.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/frozen-search.json)
- **配对统计报告**：[docs/validation/p8-s02-f01/paired_statistics.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/paired_statistics.json)
- **风险压力报告**：[docs/validation/p8-s02-f01/risk_stress.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/risk_stress.json)
- **策略包身份映射证据**（含原生 `FACTORLAB_DECISION_V1` 与 `StrategySpec`）：[docs/validation/p8-s02-f01/package_mapping.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/package_mapping.json)
- **数据审计证据**（含 `funding_coverage` 与 `ohlcv_export_fidelity`）：[docs/validation/p8-s02-f01/data_audit.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/data_audit.json)
- **独立重放与 S03/S04 消费核验收据**（11/11 重放一致性检查全绿）：[docs/validation/p8-s02-f01/replay_and_s03_verification.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/replay_and_s03_verification.json)

---

## 7. 定向与关联回归验证状态

全部验证均在合并最新 `origin/main`（`2529fa6`）后的工作树中实际执行：

- `python -m compileall -q src tests tools`：通过（0 错误）
- `git diff --check`：通过（0 格式问题）
- 定向与关联 pytest 套件（合计 `135 passed, 0 failed`）：
  - `tests/research/test_binance_champion_research.py`（13 passed：含 F3/F6/F7/F8/F9、OOS 新鲜度跨 `state_path` 阻断、R10 tie 规则、合成晋升 + 原生 `FACTORLAB_DECISION_V1` S04 策略包加载与回放）
  - `tests/integrations/test_binance_champion_s04_integration.py`（8 passed：含 F1 仓位等价与真实 `closed_bar`→假网络 `POST /fapi/v1/order`、F2 3600s/60s 周期阻断、F4 多标的隔离、F5 首次激活历史预热）
  - `tests/research/test_champion_selector.py`（15 passed）
  - `tests/integrations/test_strategy_deployment_s03.py`（39 passed）
  - `tests/integrations/test_unified_cloud_strategy_s04.py`（13 passed）
  - `tests/integrations/test_s04_history_recovery.py`（13 passed）
  - `tests/integrations/test_s04_package_failures.py`（6 passed）
  - `tests/research/test_grid_r10_review_regressions.py`（22 passed）
  - `tests/research/test_p8_r09_funding.py`（6 passed）
- 独立审查 worktree（`D:/Code/FactorLab-worktrees/pr253-independent-review`）及原始复现脚本均保持原样未修改。
