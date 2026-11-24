# FactorLab P8-S02-F01：Binance 永续合约自动研究、优化与 Champion 选择报告

> **安全边界**：`NO LIVE MONEY` · 纯离线研究与 Champion 选择流程，不操作 Azure，不提交真实 Binance Demo 订单，`financial_admission = FORMAL_ADMISSION_BLOCKED` 保持不变。

## 0. 五大核心问题明确回答

1. **自动研究功能是否真实运行？**
   **是。** 已在现有 S02 唯一编排与选择框架（`factorlab.research.automatic.run` / `replay` + `ChampionSelector` + `ExperimentTracker` + `tools/research/select_champion.py`）上完成 Binance USD-M 扩展，并执行了：(a) 完整合成协议端到端验收（`tests/research/test_binance_champion_research.py`）；(b) 基于本地已审计 193 天真实 `NVDAUSDT` 1h 行情（4,632 根 K 线）与 593 条历史资金费结算记录的真实 6 大家族 25 组候选 + 既有 `FIXED_GRID` incumbent 完整自动研究与独立重放（`tools/research/run_binance_champion_research.py`，研究 ID `p8-s02-f01-nvda-real-v1`）。

2. **Binance 执行及资金费口径是否完整？**
   **离线研究第一阶段口径完整且零残差对账，但明确不冒充真实交易所撮合深度。** 严格限定 `NVDAUSDT`、单向持仓（`ONE_WAY_LONG_ONLY`）、`LIMIT` 挂单、1x 全额抵押风险口径，复用现有 `PaperBroker` + `TradingController` + `execution_candidate` + R09 F11/F08 生命周期护栏（`DELAYED_EXIT_FILL` / `LIMITED_CAPACITY_PARTIAL` / `UNFILLED_EXIT_TIMEOUT`）与 R10 修复后的同时间戳资金费结算规则（`fill_time < funding_time`，每根 K 线撮合前先按成交前库存结算资金费，禁止成交后反算），逐候选通过资金费对账（残差 `< 1e-4 USDT`）与权益盯市对账（残差 `< 1e-6 USDT`）。同时在合同与报告中明确标注：离线 1h 成交收盘价 + 合成盘口不是真实交易所逐笔订单簿撮合证据，且历史高价期（126–243 USDT）单笔 `0.04 NVDAUSDT` 超过现行 Demo `7.00 USDT` 上限，故沿用 R08/R09/R10 的 `10.00 USDT` 1x 历史回放预算并显式记录 `demo_live_cap_usdt = 7.0`。

3. **是否产生真实合格 Challenger？**
   **否（真实历史研究结论为 `INCONCLUSIVE`，`promoted = false`，原样保留 incumbent `FIXED_GRID` `c-7eefa330d4d7fa022db5d1ff`，版本保持 `v0`）。** 在真实 193 天 NVDAUSDT 数据（`train: 2026-05-14..2026-07-13`，`validation: 2026-07-14..2026-09-11`，`test: 2026-09-12..2026-11-22`）上：
   - 全部网格类候选（`FIXED_GRID_ALT`、`FUNDING_SENSITIVE_GRID` B1–B6、`VOL_ADAPTIVE_GRID` C1–C6）在验证期均为负收益（`-7.31%` 至 `-12.00%`）而止步验证阶段；
   - `CASH` 基准零成交零收益，被 `capacity_insufficient_or_unknown`、`net_return_below_floor` 与 `insufficient_trade_activity` 阻断，不能充当正 Alpha 晋升候选；
   - 仅 2 组 S01 趋势目标候选入围封闭测试（`DUAL_MA (5/48)` 验证 `+1.80%`、`CLOSE_MOMENTUM (lb=1)` 验证 `+4.61%`），但在 72 天下跌测试窗口中分别录得 `-12.53%` 与 `-25.89%` 净收益（尽管点估计亏损小于 incumbent `FIXED_GRID` 的 `-38.27%`），同时触发 `net_return_below_floor`、`oos_window_not_fresh_unconsumed`（历史窗口已被 R04B/R07/R09/R10 复用）、以及 25 候选族 Bonferroni 调整后 HAC/Bootstrap/Score 置信区间跨零等多项否决条件。严格拒绝因"少亏"或点估计略高而放宽门槛。

4. **输出能否被 S03/S04 正常消费？**
   **能。** 输出原生 `FACTORLAB_CHAMPION_SELECTION_V1`（`common.execution = binance_usdm_demo_nvda_limit_long_only`，`common.symbol = NVDAUSDT`）与 `package_mapping.json`，并已通过两类离线消费实证：
   - **真实历史未晋升结果**：经 `build_s03_release_envelope` 与 S03 `azure_strategy_deploy.cycle` 实际消费，返回 `accepted = false, reason = "RETAIN_CHAMPION"`（无需发布包、不触发现有部署状态表）；
   - **合成合格晋升结果**（`tests/research/test_binance_champion_research.py`）：端到端通过 S03 `build_package` → `install_package` → `champion_release_adapter.adapt_selection` → `strategy_worker.invoke(probe)` → `azure_strategy_deploy.cycle` 全链路，返回 `accepted = true`。
   - 同时在 `package_mapping.json` 中诚实标注兼容性边界：`CASH`、`DUAL_MA`、`CLOSE_MOMENTUM` 原生支持 S03 `factorlab.demo.evaluate/v1`；`FIXED_GRID` 对应既有 `LEGACY_DEMO_GRID_BRIDGE`；`FUNDING_SENSITIVE_GRID` 与 `VOL_ADAPTIVE_GRID` 需由 S04 补齐云端资金费/波动率上下文与多单网格运行桥接，绝不伪造部署能力。

5. **还缺哪些实际证据？**
   - **真正未消费的新 OOS 样本窗口**：现有已审计 193 天历史数据（截至 `2026-11-23T00:00:00Z`）均已被 R04B/R07/R09/R10 观察或参与研究，尚未累积足够长度（至少 `min_oos_days = 60` 个完整新 UTC 日）的全新未见样本；
   - **真实交易所历史订单簿/逐笔成交与实时规则快照证据**：离线回放采用 1h 成交收盘价与 `DELAYED_EXIT_FILL` 假设，缺少真实逐笔 L2 深度与历史每小时交易所过滤规则演变记录；
   - **R11 并行研究与 S04 云端发布控制器联调证据**：本任务按约定不等待并行的 R11/S04，已预留兼容接口；待 R11 新策略与 S04 部署接线完成后，可在新 OOS 窗口复跑并接入云端；
   - **Codex Review 复审记录**：用户确认当前 Codex Review 额度已耗尽，待用户后续自行复审。

---

## 1. 能力审计与架构复用结论

| 模块 | 现有能力（PR #234–#238） | P8-S02-F01 审计发现的缺口 | 最小充分接线实现 |
| --- | --- | --- | --- |
| **S02 核心选择器** (`src/factorlab/research/champion.py`) | `SelectionPolicy`、`ChampionSelector.select`、HAC + 移动块 Bootstrap + Bonferroni 族校正 + Score CI | 仅校验 `capacity_cny`，缺少 USDT 容量、最低交易活跃度（防空仓充 Alpha）、退出失败率上限与 `oos_eligibility` 新鲜度门槛 | 向后兼容扩展 `SelectionPolicy`（`min_capacity_usdt`、`min_trade_count`、`max_exit_failure_ratio`）、`oos_eligibility` 校验与 `classify_verdict`；保留唯一 `ChampionSelector` |
| **S02 自动编排** (`src/factorlab/research/automatic.py`) | `run`/`replay`、文件锁、`TEST_RESERVED` 中断保护、OOS 消费拦截、冷却期、`pre-run-state.json`、`ExperimentTracker` | 仅硬编码接受 `a_share_daily_long_only_next_open_t1` 与 `funding_bps == 0` | 保留全部 A 股路径不变，按 `common.execution` 分发至 `_run_binance`，复用同一状态机、锁、Tracker、Replay 与 `FACTORLAB_CHAMPION_SELECTION_V1` 输出 |
| **S01 策略协议** (`src/factorlab/strategies/protocol.py` 等) | `StrategySpec`、`Decision`、`UnifiedStrategy`、`DualMAAdapter`、`CloseMomentum` | 需将 6 大家族封装为哈希固定、参数规范化且同时对齐 S01/S03 契约的候选文件 | 新增 `src/factorlab/strategies/binance_usdm/` 下 6 个独立候选模块，均实现 `describe()`/`normalized_parameters()`，目标类同时提供无外部依赖的 `evaluate(context, state)` |
| **R09 完整离线执行** (`src/factorlab/experiments/grid_f11_f08_offline.py`) | `PaperBroker` + `TradingController` + `DemoGuardrails` + `ExecutionAssumptions` + F11 只减仓 LIMIT 退出 + F08 轮换 | 仅覆盖单一固定网格，未对接 S01 目标策略与 R10 资金费/波动率网格统一评估 | 在 `src/factorlab/research/binance_perpetual.py` 中基于同一 `PaperBroker`/`TradingController` 统一驱动网格族与目标族，并加入浮盈仓位只减仓分拆、负可用保证金安全投影与单次退出重试重置 |
| **R10 资金费结算与滤波** (`grid_funding_adaptive.py`, `grid_funding.py`) | `validate_hourly_timeline`、`FundingSensitiveGrid`、`VolatilityAdaptiveGrid`、`position_settlements`（`fill_time < funding_time`） | `FundingSensitiveGrid` 的 `_risk(exit_position=True)` 原产生 `MARKET` 单，不符合 Binance `LIMIT`-only 合同 | 子类 `_LimitOnlyFundingSensitiveGrid` 将资金费退出转为合法只减仓 `LIMIT` 单；每根 K 线撮合前先按成交前库存入账资金费，并以 `position_settlements` 独立核验零残差 |
| **S03 发布适配** (`champion_release_adapter.py`, `strategy_package.py`, `azure_strategy_deploy.py`) | 严格消费 `FACTORLAB_CHAMPION_SELECTION_V1` + `binance_usdm_demo_nvda_limit_long_only` | 需要将 S02 输出自动映射为 S03 `StrategyPackage` 与 `.candidate.json` 入箱信封 | 新增 `build_s03_release_envelope` 与 `package_mapping.json`，默认 `expected_active` 对齐 `strategy_deployment.LEGACY` (`legacy-grid`) |

---

## 2. Binance 永续合约第一阶段研究合同

冻结配置位于 [p8_s02_f01_binance_nvda_frozen.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/configs/research/p8_s02_f01_binance_nvda_frozen.json)，核心合同参数如下：

- **执行合同标识**：`binance_usdm_demo_nvda_limit_long_only`（与 S03 `champion_release_adapter.EXECUTION` 精确一致）
- **标的 / 市场 / 周期 / 持仓 / 订单 / 杠杆**：`NVDAUSDT` / `BINANCE_USDM` / `1h` UTC / `ONE_WAY_LONG_ONLY` / `LIMIT` / `1x`（禁止加杠杆、做空、市价单或多标的扩展）
- **交易价格与假设边界**：1h 成交收盘价叠加不利滑点与 `DELAYED_EXIT_FILL` 被动限价排队退出（R09 主对比档位，退出限价单至少排队 2 根 K 线，超时改价最多 5 次）；显式声明不代表真实交易所逐笔盘口撮合证据
- **交易所过滤规则**：`min_notional = 5 USDT`、`step_size = 0.01`、`tick_size = 0.01`、`min_quantity = 0.01`、`quantity_unit = 0.04`、`max_quantity = 0.04`、`PERCENT_PRICE = [0.97, 1.03]`
- **成本与流动性**：手续费 `3.0 bps`、滑点 `2.0 bps`、成交参与度上限 `1.0%`（支持部分成交与未成交撤单）、目标策略限价缓冲 `10.0 bps`
- **账户风险预算与历史边界**：离线 1x 抵押预算 `10.00 USDT`（因 May–Nov 2026 历史价位 `126.19–242.61 USDT` 下 `0.04` 单位名义额为 `5.05–9.70 USDT`，超过现行 Demo `7.00 USDT` 上限；合同中显式记录 `demo_live_cap_usdt = 7.0` 与 `max_loss_ratio = 0.25`）
- **资金费结算规则**：严格复用 R10 `settlement_before_exact_tie_fill`（`fill_time < funding_time`），每根 K 线在撮合当期成交前先按成交前持仓结算 `(prev_close, now]` 内的资金费事件，并由 `grid_funding.position_settlements` 独立复核零残差。

---

## 3. 首批 6 大候选策略家族与兼容性矩阵

| 家族 | 策略文件 | 候选数量 | 预声明参数空间 | 离线研究状态 | S03 发布兼容性 |
| --- | --- | ---: | --- | --- | --- |
| **A. `CASH`** | [cash_benchmark.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/cash_benchmark.py) | 1 | 现金空仓基准 | 已运行（基准对照；不可作为正 Alpha 晋升） | 原生 `cash_benchmark:evaluate`（仅作研究基准/空仓保持） |
| **B. `FIXED_GRID`** | [fixed_grid.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/fixed_grid.py) | 1 incumbent + 2 alt | Incumbent `half_width=1.0%`；Challenger `[1.5%, 2.0%]` | 已运行 | `LEGACY_DEMO_GRID_BRIDGE`（现有 Demo 网格控制组） |
| **C. `FUNDING_SENSITIVE_GRID`** | [funding_sensitive_grid.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/funding_sensitive_grid.py) | 6 (`B1..B6`) | 复用 R10 预声明 `B1..B6` 因果资金费门控与退出参数 | 已运行（转 `LIMIT` 只减仓退出） | 需 S04 补齐云端已结算资金费事件上下文与网格桥接 |
| **D. `VOL_ADAPTIVE_GRID`** | [vol_adaptive_grid.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/vol_adaptive_grid.py) | 6 (`C1..C6`) | 复用 R10 预声明 `C1..C6`，`sigma_ref` 仅在训练窗拟合冻结 | 已运行 | 需 S04 补齐云端滚动波动率状态与网格桥接 |
| **E. `DUAL_MA`** | [dual_ma_usdm.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/dual_ma_usdm.py) | 4 | `fast=[5, 12] × slow=[20, 48]`，`max_weight=0.90` | 已运行（S01 `UnifiedStrategy`） | **原生兼容** `dual_ma_usdm:evaluate`（已通过 S03 worker 隔离验证） |
| **F. `CLOSE_MOMENTUM`** | [close_momentum_usdm.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/src/factorlab/strategies/binance_usdm/close_momentum_usdm.py) | 6 | `lookback=[1, 6, 24] × threshold_bps=[0, 25]`，`weight=0.90` | 已运行（S01 `UnifiedStrategy`） | **原生兼容** `close_momentum_usdm:evaluate`（已通过 S03 worker 隔离验证） |

---

## 4. 真实 NVDAUSDT 193 天历史研究结果（Section 9B）

### 4.1 数据审计与窗口划分

- **数据来源**：认证 R04B 快照（`2026-05-14` → `2026-08-12`，2,160h）+ 核验 R07/Post-R07（`2026-08-12` → `2026-11-23`，2,472h），合计 `4,632` 根连续无缺失 1h K 线（`0` 缺失、`0` 重复、`0` 异常 OHLCV），配对 `593` 条主网真实资金费结算事件（详见 [data_audit.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/data_audit.json)）。
- **窗口划分**：
  - `train`: `2026-05-14` → `2026-07-13`（61 天 / 1,464h，仅用于 `VOL_ADAPTIVE_GRID` 拟合 `sigma_ref`）
  - `validation`: `2026-07-14` → `2026-09-11`（60 天 / 1,440h，25 组候选筛选与冻结入围清单 [frozen-search.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/frozen-search.json)）
  - `test`: `2026-09-12` → `2026-11-22`（72 天 / 1,728h，封闭对比测试）
- **数据独立性标记**：`oos_eligibility = HISTORICAL_REUSE_PSEUDO_OOS`，显式登记重叠历史研究 `["P7-R04B", "P7-R09", "P8-R07", "P8-R08", "P8-R09", "P8-R10"]`，**不宣称未见新样本**。

### 4.2 完整候选金融评价表（含全部成本、资金费与残仓盯市）

| 策略 | 参数摘要 | 阶段 | 净收益 | vs 现金 | vs Incumbent | MaxDD | VaR95 / CVaR95 | Sharpe / Sortino | 日亏损率 | 成交率 (笔数) | 换手 | 资金费侵蚀 | 持仓时长 (均值) | 资金利用率 | 期末残仓 | 退出失败率 | 状态 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `FIXED_GRID` | `hw=0.01, ttl=100` (Incumbent) | `test` | -38.27% | -38.27% | +0.00% | 42.19% | 5.17% / 7.40% | -5.60 / -5.80 | 41.7% | 40.6% (65) | 55.65x | 0.509% | 538h (16.3h) | 22.4% | 0.04 | 77.8% | `INCUMBENT_EVALUATED` |
| `CASH` | 现金基准 | `test` | +0.00% | +0.00% | +38.27% | 0.00% | 0.00% / 0.00% | N/A / N/A | 0.0% | 0.0% (0) | 0.00x | 0.000% | 0h (0.0h) | 0.0% | 0.00 | 0.0% | `BENCHMARK_CASH_EVALUATED` |
| `FIXED_GRID_ALT` | `hw=0.015` | `validation` | -10.95% | — | — | 11.48% | 1.75% / 2.16% | -6.16 / -6.03 | 28.3% | 43.3% (42) | 35.83x | 0.045% | 140h (6.7h) | 8.3% | 0.00 | 46.2% | `VALIDATION_REJECTED` |
| `FIXED_GRID_ALT` | `hw=0.02` | `validation` | -7.31% | — | — | 7.58% | 0.98% / 1.83% | -5.58 / -5.40 | 21.7% | 37.0% (20) | 17.01x | 0.061% | 67h (6.7h) | 3.8% | 0.00 | 37.5% | `VALIDATION_REJECTED` |
| `FUNDING_SENSITIVE_GRID_B1` | `entry<=0.25, exit=none` | `validation` | -11.73% | — | — | 11.73% | 1.66% / 2.00% | -7.20 / -6.89 | 31.7% | 37.5% (42) | 35.02x | 0.004% | 147h (7.0h) | 8.6% | 0.00 | 46.9% | `VALIDATION_REJECTED` |
| `FUNDING_SENSITIVE_GRID_B2` | `entry<=0.50, exit=none` | `validation` | -11.41% | — | — | 11.93% | 1.66% / 1.99% | -6.96 / -6.71 | 31.7% | 38.6% (44) | 36.70x | 0.009% | 151h (6.9h) | 8.8% | 0.00 | 45.9% | `VALIDATION_REJECTED` |
| `FUNDING_SENSITIVE_GRID_B3` | `entry<=1.00, exit=none` | `validation` | -11.41% | — | — | 11.93% | 1.66% / 1.99% | -6.96 / -6.71 | 31.7% | 38.6% (44) | 36.70x | 0.009% | 151h (6.9h) | 8.8% | 0.00 | 45.9% | `VALIDATION_REJECTED` |
| `FUNDING_SENSITIVE_GRID_B4` | `entry<=0.25, bleed/72h` | `validation` | -11.73% | — | — | 11.73% | 1.66% / 2.00% | -7.20 / -6.89 | 31.7% | 37.5% (42) | 35.02x | 0.004% | 147h (7.0h) | 8.6% | 0.00 | 46.9% | `VALIDATION_REJECTED` |
| `FUNDING_SENSITIVE_GRID_B5` | `entry<=0.50, bleed/72h` | `validation` | -11.41% | — | — | 11.93% | 1.66% / 1.99% | -6.96 / -6.71 | 31.7% | 38.6% (44) | 36.70x | 0.009% | 151h (6.9h) | 8.8% | 0.00 | 45.9% | `VALIDATION_REJECTED` |
| `FUNDING_SENSITIVE_GRID_B6` | `net_margin/96h` | `validation` | -11.41% | — | — | 11.93% | 1.66% / 1.99% | -6.96 / -6.71 | 31.7% | 38.6% (44) | 36.70x | 0.009% | 151h (6.9h) | 8.8% | 0.00 | 45.9% | `VALIDATION_REJECTED` |
| `VOL_ADAPTIVE_GRID_C1` | `24h RV static` | `validation` | -11.80% | — | — | 12.76% | 1.67% / 2.30% | -6.76 / -6.51 | 31.7% | 37.3% (38) | 31.87x | 0.004% | 154h (8.1h) | 9.0% | 0.00 | 46.4% | `VALIDATION_REJECTED` |
| `VOL_ADAPTIVE_GRID_C2` | `48h RV static` | `validation` | -11.28% | — | — | 12.12% | 1.73% / 2.08% | -6.70 / -6.46 | 30.0% | 36.8% (32) | 26.89x | 0.004% | 133h (8.3h) | 7.8% | 0.00 | 50.0% | `VALIDATION_REJECTED` |
| `VOL_ADAPTIVE_GRID_C3` | `72h RV static` | `validation` | -11.13% | — | — | 12.11% | 1.34% / 2.07% | -6.90 / -6.63 | 33.3% | 40.4% (38) | 32.12x | -0.081% | 140h (7.4h) | 8.2% | 0.00 | 48.9% | `VALIDATION_REJECTED` |
| `VOL_ADAPTIVE_GRID_C4` | `24h RV + 2.0x guard` | `validation` | -12.00% | — | — | 13.12% | 1.68% / 2.30% | -6.90 / -6.62 | 36.7% | 34.8% (46) | 38.58x | -0.013% | 168h (7.3h) | 9.8% | 0.00 | 43.7% | `VALIDATION_REJECTED` |
| `VOL_ADAPTIVE_GRID_C5` | `48h RV + 2.0x guard` | `validation` | -12.00% | — | — | 12.77% | 1.74% / 2.09% | -7.05 / -6.75 | 31.7% | 31.3% (36) | 30.19x | -0.013% | 143h (7.9h) | 8.4% | 0.00 | 47.3% | `VALIDATION_REJECTED` |
| `VOL_ADAPTIVE_GRID_C6` | `72h RV + 1.8x guard` | `validation` | -10.23% | — | — | 11.02% | 1.15% / 2.08% | -6.69 / -6.46 | 35.0% | 34.4% (42) | 35.53x | 0.004% | 146h (7.0h) | 8.6% | 0.00 | 46.2% | `VALIDATION_REJECTED` |
| `DUAL_MA` | `fast=5, slow=20` | `validation` | -0.39% | — | — | 9.26% | 1.44% / 3.86% | +0.03 / +0.04 | 38.3% | 81.4% (57) | 46.96x | 0.230% | 807h (28.2h) | 46.8% | 0.04 | 0.0% | `VALIDATION_REJECTED` |
| `DUAL_MA` | `fast=5, slow=48` (入围) | `validation` | +1.80% | — | — | 8.06% | 2.20% / 4.04% | +0.55 / +0.73 | 30.0% | 80.0% (24) | 20.22x | 0.192% | 741h (60.2h) | 42.6% | 0.00 | 0.0% | `OOS_REJECTED` |
| **`DUAL_MA`** | **`fast=5, slow=48` (测试)** | **`test`** | **-12.53%** | **-12.53%** | **+25.75%** | **17.87%** | **2.74% / 3.82%** | **-2.90 / -3.34** | **40.3%** | **84.8% (39)** | **30.25x** | **0.536%** | **802h (39.5h)** | **37.2%** | **0.04** | **0.0%** | **`OOS_REJECTED`** |
| `DUAL_MA` | `fast=12, slow=20` | `validation` | -1.30% | — | — | 11.07% | 1.74% / 3.86% | -0.18 / -0.23 | 43.3% | 78.3% (47) | 38.84x | 0.107% | 923h (38.9h) | 53.1% | 0.03 | 0.0% | `VALIDATION_REJECTED` |
| `DUAL_MA` | `fast=12, slow=48` | `validation` | -1.02% | — | — | 8.63% | 2.44% / 3.67% | -0.08 / -0.10 | 48.3% | 76.0% (19) | 16.39x | 0.183% | 1085h (108.4h) | 65.0% | 0.04 | 0.0% | `VALIDATION_REJECTED` |
| `CLOSE_MOMENTUM` | `lb=1, th=0` (入围) | `validation` | +4.61% | — | — | 9.77% | 2.65% / 3.89% | +1.00 / +1.42 | 48.3% | 1.6% (11) | 9.35x | 0.342% | 1428h (237.8h) | 85.5% | 0.04 | 1.9% | `OOS_REJECTED` |
| **`CLOSE_MOMENTUM`** | **`lb=1, th=0` (测试)** | **`test`** | **-25.89%** | **-25.89%** | **+12.38%** | **33.47%** | **3.44% / 5.15%** | **-4.25 / -4.67** | **54.2%** | **2.4% (17)** | **14.54x** | **1.173%** | **1710h (172.4h)** | **68.7%** | **0.03** | **18.5%** | **`OOS_REJECTED`** |
| `CLOSE_MOMENTUM` | `lb=1, th=25bps` | `validation` | -3.32% | — | — | 5.47% | 1.55% / 1.97% | -1.19 / -1.67 | 46.7% | 32.2% (112) | 90.73x | 0.105% | 484h (8.7h) | 27.2% | 0.00 | 22.6% | `VALIDATION_REJECTED` |
| `CLOSE_MOMENTUM` | `lb=6, th=0` | `validation` | -3.76% | — | — | 11.80% | 2.74% / 3.92% | -0.68 / -0.91 | 55.0% | 41.6% (148) | 114.23x | 0.234% | 1076h (14.6h) | 57.9% | 0.00 | 12.1% | `VALIDATION_REJECTED` |
| `CLOSE_MOMENTUM` | `lb=6, th=25bps` | `validation` | -0.65% | — | — | 10.41% | 1.98% / 4.01% | -0.02 / -0.03 | 51.7% | 47.3% (150) | 125.65x | 0.259% | 619h (8.2h) | 35.6% | 0.00 | 20.2% | `VALIDATION_REJECTED` |
| `CLOSE_MOMENTUM` | `lb=24, th=0` | `validation` | -6.31% | — | — | 10.00% | 1.78% / 3.51% | -1.50 / -1.92 | 53.3% | 46.1% (70) | 54.65x | 0.159% | 970h (26.8h) | 51.3% | 0.00 | 39.1% | `VALIDATION_REJECTED` |
| `CLOSE_MOMENTUM` | `lb=24, th=25bps` | `validation` | -2.42% | — | — | 10.49% | 1.90% / 3.34% | -0.54 / -0.72 | 45.0% | 41.9% (72) | 57.37x | 0.164% | 752h (21.0h) | 42.0% | 0.00 | 0.0% | `VALIDATION_REJECTED` |

### 4.3 同窗口配对统计推断（`family_size = 25`，Bonferroni 调整，6,000 次移动块 Bootstrap）

详见 [paired_statistics.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/paired_statistics.json)：

| 入围候选 vs `FIXED_GRID` | 日均差 HAC 调整区间 | 移动块 Bootstrap 调整区间 | 目标分数差 CI | 块胜率 | 尾部有效重采样 | 否决原因 |
| --- | --- | --- | --- | ---: | ---: | --- |
| `DUAL_MA (5/48)` (`c-f13e00ea46ee334f54d722a6`) | `[-0.172%, +1.101%]` | `[-0.025%, +1.107%]` | `[-0.013, +4.610]` | 64.3% | `6.0 >= 5` | `net_return_below_floor`, `oos_window_not_fresh_unconsumed`, `oos_advantage_statistically_uncertain`, `risk_adjusted_advantage_statistically_uncertain` |
| `CLOSE_MOMENTUM (lb=1)` (`c-b97c8b599be80d00b44fcc8e`) | `[-0.079%, +0.567%]` | `[-0.047%, +0.575%]` | `[-0.063, +2.246]` | 71.4% | `6.0 >= 5` | `max_drawdown_breach_or_unknown`, `daily_cvar_breach_or_unknown`, `net_return_below_floor`, `oos_window_not_fresh_unconsumed`, `oos_advantage_statistically_uncertain`, `risk_adjusted_advantage_statistically_uncertain` |

### 4.4 风险压力测试（`RISK_STRESS_ONLY_NOT_OOS_EVIDENCE`，不替代 OOS 资格）

详见 [risk_stress.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/risk_stress.json)：

| 策略 | `mainnet_historical_full` (72d) | `mainnet_5x_funding_full` (72d) | `demo_high_funding_22_97bps_h_168h` (7d) | `funding_regime_switch_48h_168h` (7d) |
| --- | --- | --- | --- | --- |
| `FIXED_GRID` | 净收益 `-38.27%`, DD `42.19%`, 资费 `-0.0509 U` | 净收益 `-40.31%`, DD `43.97%`, 资费 `-0.2547 U` | 净收益 `-24.55%`, DD `25.72%`, 资费 `-1.4866 U` | 净收益 `-20.37%`, DD `21.69%`, 资费 `-1.0688 U` |
| `DUAL_MA (5/48)` | 净收益 `-12.53%`, DD `17.87%`, 资费 `-0.0536 U` | 净收益 `-14.65%`, DD `19.18%`, 资费 `-0.2680 U` | 净收益 `-4.86%`, DD `5.82%`, 资费 `-0.3810 U` | 净收益 `-4.86%`, DD `5.82%`, 资费 `-0.3810 U` |
| `CLOSE_MOMENTUM (lb=1)` | 净收益 `-25.89%`, DD `33.47%`, 资费 `-0.1173 U` | 净收益 `-30.79%`, DD `36.41%`, 资费 `-0.5644 U` | 净收益 `-32.56%`, DD `33.68%`, 资费 `-2.2179 U` | 净收益 `-26.05%`, DD `27.33%`, 资费 `-1.5676 U` |
| `CASH` | `0.00%` / DD `0.00%` | `0.00%` / DD `0.00%` | `0.00%` / DD `0.00%` | `0.00%` / DD `0.00%` |

---

## 5. 工程协议端到端验收（Section 9A，合成数据）

> 合成晋升仅验证软件行为与治理逻辑，不代表真实金融优势。

由 [test_binance_champion_research.py](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/tests/research/test_binance_champion_research.py) 覆盖全部 9 项协议验收场景：

1. **研究合同与搜索边界**：拒绝非 `NVDAUSDT`、杠杆 `!= 1`、做空、`MARKET` 单、非 R10 资金费规则、缺少 `prior_studies` 的伪 OOS 及超限搜索网格；
2. **R10 同时间戳资金费规则**：精确构造与 BUY/SELL 成交同微秒的资金费结算，验证 BUY 同点收 0 资金费、SELL 同点按成交前持仓扣费，残差 `< 1e-7`；
3. **部分成交与退出失败核算**：验证 `LIMITED_CAPACITY_PARTIAL` 触发真实部分成交计数与 `UNFILLED_EXIT_TIMEOUT` 残仓盯市零残差；
4. **合成合格 Challenger 晋升 + Tracker + Replay + S03 消费**：合成趋势下 `PROMOTE_CHALLENGER`（`version = 1`），`ExperimentTracker.reproduction(verify_artifacts=True)` 全绿，独立 `replay` 逐字段一致，S03 `build_package` → `install_package` → `adapt_selection` → `strategy_worker.invoke(probe)` → `azure_strategy_deploy.cycle` 返回 `accepted = true`；
5. **高资金费空仓拒充正 Alpha**：在极端资费下验证空仓避险收益 `0%` 被 `net_return_below_floor` 与 `insufficient_trade_activity` 拒绝；
6. **冷却期保持**：365 天冷却期内合格候选被 `promotion_cooldown` 阻断，保留原 Champion 版本 `v3`；
7. **中断恢复与防重放**：测试阶段注入 `KeyboardInterrupt` 后状态保留 `TEST_RESERVED`、incumbent 不变、OOS 窗口已不可逆消费；
8. **旧 OOS 重复消费拒绝**：同窗口重跑明确抛出 `OOS window already consumed`；
9. **测试集扰动零泄露 & 历史复用伪 OOS 阻断**：测试窗口价格 `+30%` 扰动对 `frozen-search.json` 与验证指标零影响；`HISTORICAL_REUSE_PSEUDO_OOS` 强制阻断晋升并输出 `INCONCLUSIVE`。

---

## 6. 独立重放、策略包映射与交付工件

- **冻结研究配置**：[configs/research/p8_s02_f01_binance_nvda_frozen.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/configs/research/p8_s02_f01_binance_nvda_frozen.json)
- **原生 Champion 决策结果**（`FACTORLAB_CHAMPION_SELECTION_V1`）：[docs/validation/p8-s02-f01/selection.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/selection.json)
- **完整候选研究结果**：[docs/validation/p8-s02-f01/candidates.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/candidates.json)
- **冻结入围清单**：[docs/validation/p8-s02-f01/frozen-search.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/frozen-search.json)
- **配对统计报告**：[docs/validation/p8-s02-f01/paired_statistics.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/paired_statistics.json)
- **风险压力报告**：[docs/validation/p8-s02-f01/risk_stress.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/risk_stress.json)
- **策略包身份映射证据**：[docs/validation/p8-s02-f01/package_mapping.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/package_mapping.json)
- **独立重放与 S03 消费核验收据**（11/11 重放一致性检查全绿）：[docs/validation/p8-s02-f01/replay_and_s03_verification.json](file:///D:/Code/FactorLab-worktrees/p8-s02-f01-binance-champion/docs/validation/p8-s02-f01/replay_and_s03_verification.json)
- **可执行入口**：
  - 统一 S02 CLI：`python tools/research/select_champion.py --config <cfg> --output <dir> --store <tracker> --state <state>` / `--replay-from <run> --audit-dir <audit>`
  - Binance 真实研究复现脚本：`python tools/research/run_binance_champion_research.py --r04b-snapshot <r04b> --cache-dir <cache> --work-dir <work>`

---

## 7. 定向与关联回归及审查状态

- **定向与关联测试**（`111 passed, 0 failed`）：
  - `tests/research/test_binance_champion_research.py`（7 passed：Binance 合同校验、R10 同时间戳资金费规则、部分成交与退出失败核算、合成合格晋升/独立重放/防重放/S03 消费、高资金费空仓拒充 Alpha、冷却期/中断恢复/历史复用伪 OOS 拦截、测试集零泄露）
  - `tests/research/test_champion_selector.py`（15 passed：既有 A 股 S02 全流程零回归）
  - `tests/integrations/test_strategy_deployment_s03.py`（39 passed：S03 发布适配、包校验与部署控制器）
  - `tests/research/test_grid_r10_review_regressions.py`（22 passed：R10 资金费 tie 规则与小时时间轴校验）
  - `tests/research/test_p8_r09_funding.py`（6 passed：R09 资金费入账核算）
  - `tests/integrations/test_unified_strategy.py`（22 passed：S01 统一策略协议与隔离 worker）
- **Codex Review**：用户确认当前 Codex Review 额度已耗尽，待用户后续自行复审；本地已完成全量 diff 自查与全部关联回归验证。
