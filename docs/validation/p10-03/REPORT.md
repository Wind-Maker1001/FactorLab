# P10-03：Qlib V179、V182 策略原生移植及 V2 运行时接入验证报告

## 1. 任务范围与核心结论

- **任务边界**：完成外部 Qlib V179 与 V182 策略的 FactorLab 原生移植、月度 Ridge 离线训练/在线推断拆分、只读 `^SP500TR` 基准适配、V2 策略包封装、逐周全链路差分验证以及 Alpaca Paper 离线 HTTP 多策略共存验收。完整金融研究与参数/绩效评审由 P10-04 独立负责。
- **既有策略保护**：完整保留现有 `V69_CAUSAL` 原生实现与历史账本，不覆盖旧策略、不因版本号更大而自动晋升 V179/V182 为 Champion、不直接操作生产 Azure。
- **核心验证结果**：
  - **24 项中间指标与 179 个月度 Ridge 预测差分**（`v179/v182 × omit_sndk=False/True`，3,997 个交易日）：最大绝对差 `0.000e+00`。
  - **全部 23 个原始归档账户逐周全链路重放**（含 `target`、`dev_a`、`dev_b`、`dev_crisis`、`challenge` 与 20 年 `twenty`，总计 >32,000 笔成交）：目标权重最大绝对差 `9.714e-17`；请求订单股数最大绝对差 `1.455e-10`；`target` 窗口净值最大差 `1.979e-9` USD，20 年全历史窗口净值最大差 `0.0107` USD（唯一微差来源已定位为 Qlib `*.day.bin` 二进制 `float32` 收盘盯市精度，非策略逻辑差异）。
  - **零 Qlib / 零云端 ML 训练约束**：云端运行时仅依赖 `numpy` 与 `pandas`，在显式封禁 `sklearn` 的独立子进程中推断通过。
  - **Alpaca 离线 HTTP 三策略共存验收**：`V69_CAUSAL`、`V179_TREND`、`V182_RECOVERY` 在 `#260 SHARED_SAME_DIRECTION` 资金政策下同账户并行运行，各自独立资金、持仓、订单、检查点与 TQQQ 3x 经济敞口乘数；缺失 `^SP500TR` 仅阻止 V179/V182 进入 `PREPARATION_INCOMPLETE`，不影响 V69；重启、暂停/恢复、包升级均产生 `0` 次重复调仓；`$1,000` 小额模拟账户合法碎股下单通过。

---

## 2. 原始资料身份与完整性核验

| 原始归档文件 | 文件大小 (Bytes) | SHA-256 |
|---|---:|---|
| `Qlib_v182与v179_策略源码配置与回测复现.zip` | `10,842,371` | `21abc3eebaf816264b325525ce0997910e63d6abd72b47b96c21b5fffe91a79a` |
| `F02_v69_策略入口_依赖_配置_结果.zip` | `115,479` | `d3e7346c8100e4c5ea6d9a98f3a60a151405288198bb5c84a1f4e32bb9dfe97e` |

- 仓库冻结夹具位于 `tests/fixtures/v179_v182_original/`，包含全部 63 个原始策略源码、配置、`LICENSE-QLIB`、`fixtures/target/*.parquet`（3,997 个交易日，2011-01-03 至 2026-11-23）以及 `target/{normal,no_sndk}` 参考账本（含 `attribution.csv` 以支持 P10-04 完整 7 层验收），完整清单及逐文件 SHA-256 记录于 `tests/fixtures/v179_v182_original/source-manifest.json`，由单元测试自动核验。
- 原始参考结果（`verification.json`，30 万美元初始资金，主窗口 2023-01-03 至 2026-11-23）：
  - `v179/target/normal`：977 日、873 笔成交、净收益 `186.9815%`、最大回撤 `-15.2525%`、标普总回报基准 `98.7816%`。
  - `v182/target/normal`：977 日、874 笔成交、净收益 `177.1905%`、最大回撤 `-15.1230%`、标普总回报基准 `98.7816%`。
  - 全部参考 `targets.csv / fills.csv / daily.parquet` 仅作为差分预言机比对，绝不作为原生策略信号输入。

---

## 3. 原生策略语义与完整依赖链

原生实现复用 `factorlab.strategies.v69_native`（不改动 V69 原实现），在 `src/factorlab/strategies/v179_v182_native.py` 中完整移植原始继承链，未修改任何参数、阈值、股票池或训练方式：

```text
Band (V48/V50: MA28, 21日波动带 0.5*vol, QQQ>MA63 约束, 市场宽度>=2/3 放大1.10)
 └─ WeakExit (V51: 宽度<0.5 或 QQQ<=MA63 时跌破 MA28 即退出)
     └─ Scaled (V61: 全局 1.10 缩放)
         └─ NativeV69 (V69: 42日对 QQQ 残差波动率75分位高波动新仓两日连续突破确认)
             └─ NativeV100 (V100: giveback + ridge_confirm + dual_strength，逐条规则后 37.5% 上限)
                 └─ NativeV105 (V105: scale=4，总目标仓位 90% 上限)
                     └─ NativeV125 (V125 [进取组合]: recovery 市场回撤<-10% & 宽度<0.5 & 趋势<0.5 缩至0.65；个股相对强度 tilt 0.30)
                         └─ NativeV150 (V150: 新开仓目标减半)
                             └─ NativeV161 (V161: 63日下行半方差风险贡献 32% 单标的上界 + 非TQQQ<=22%/+4% 再分配)
                                 └─ NativeV165 (V165 [防御组合]: fixed 已投预算增配 10%，受 90% 上限及单股+4%/TQQQ+1.2% 约束)
                                     ├─ NativeV179 (trend: ^SP500TR>MA200 且 宽度>=50% -> 进取75%，否则25%)
                                     └─ NativeV182 (market_recovery: 10日宽度变化>+10pp 且 ^SP500TR>MA63 -> 进取75%，否则40%)
```

- **股票池与 TQQQ 规则**：固定保持 `AAPL, SNDK, BA, C, JPM, GE, TQQQ, QQQ`；原始基础份额为普通标的 `4%`、`TQQQ` 为 `4%/3`；执行计划登记 `economic_multipliers={'TQQQ': Decimal('3')}`。
- **SNDK 上市与预热**：2025-02-24 上市前及 28 个已完成交易日预热期内权重与成交严格为 0；`omit_sndk=True` 时从宽度、高波动分位与 Ridge 训练/推断池中完整排除。
- **周频调度与订单生成**：每周首个合法交易日开盘调仓，仅使用上一已完成交易日收盘数据；零目标忽略死区全额退出，非零目标遵循 `0.5%` 调仓死区；双边委托受上一交易日复权成交量 `0.1%` 限制；严格先卖后买。

---

## 4. 行情数据边界与离线训练 / 在线推断拆分

### 4.1 四类数据严格分离

1. **8 只可交易证券**：经 Alpaca `MarketDataService` 获取历史日线与实时执行报价（`adjustment_basis='yahoo_ratio'`，同源 `raw`/`all` 复权因子）。
2. **只读市场基准信号（`^SP500TR`）**：核查确认 Alpaca `/v2/stocks/*` 接口仅提供可交易美股/ETF，**不提供** `^SP500TR` 标普 500 总回报指数（记录于 `ALPACA_BENCHMARK_CAPABILITY`）。新增 `src/factorlab/integrations/alpaca/benchmark_data.py`（提供 `LocalBenchmarkProvider` 与可注入传输层的只读 `YahooSP500TRProvider`），保留来源、可用时间与总回报历史口径，显式拒绝 `SPY / QQQ / IVV / VOO / ^GSPC / SPX` 替代。当 `^SP500TR` 缺失或过期时，仅阻止依赖该基准的 V179/V182 实例进入 `PREPARATION_INCOMPLETE`，不影响 `V69_CAUSAL` 或其他策略。
3. **历史训练数据与月度 Ridge 模型工件**：`src/factorlab/strategies/qlib_blend_ridge.py` 将离线训练（`train_monthly_ridge_artifact`，惰性导入 `scikit-learn`）与在线因果推断（`predict_residual_from_artifact`，纯 NumPy/Pandas）彻底拆分。离线脚本 `tools/research/train_v179_v182_ridge.py` 生成带 `artifact_sha256` 校验的轻量 JSON 工件（179 个拟合月仅 `170.9 KB`）。每次推断强制校验 `train_start <= train_cutoff < effective_from_signal_date <= signal_date`；截断历史至 `2025-06-30` 重训所得模型参数与全量历史工件在 `<= 2025-06` 的全部月份误差 `< 1e-12`，证明零未来回填；超期月份在 `strict_model_month=True` 下显式抛出 `ModelArtifactStaleError` 阻断执行。
4. **实际原始股价与执行报价**：信号与 NAV 在复权研究单位计算后，通过 `to_adjusted_holdings` / `original_plan_quantities` 与当期复权因子换算为经纪商原始股数，交由既有 `PortfolioExecutor` 使用实际账户权益与实时原始报价执行。

---

## 5. 逐周差分验证与差异根因说明

完整机读证据见：
- [`differential-summary.json`](differential-summary.json)
- [`target-weight-differences.csv`](target-weight-differences.csv)
- [`order-intent-differences.csv`](order-intent-differences.csv)

| 校验维度 | 覆盖范围 | 最大绝对差 | 结论 |
|---|---|---:|---|
| 24 项指标 + 179 月度 Ridge 预测 | `v179/v182 × omit_sndk={False,True}`（3,997 日） | `0.000e+00` | PASS |
| V125 / V165 / 最终混合目标权重 | 全部 23 个归档账户 | `9.714e-17` | PASS |
| 订单方向、标的、顺序与请求股数 | 全部 23 个归档账户（>32,000 笔成交） | `1.455e-10` | PASS |
| `target` 窗口日净值（6 账户） | 2023-01-03 至 2026-11-23 | `1.979e-09` USD | PASS |
| 20 年/开发/挑战窗口日净值（17 账户） | 2001-01-02 至 2026-11-20 | `0.0107` USD | PASS（根因见下） |
| 252 交易日在线滑动窗口 vs 全历史 | `2025-05-09`、`2026-11-20` | `< 1e-12` | PASS |
| 边界与异常处理场景 | 缺失行情、池外持仓、死区、0.1% 成交量上限、节假日周二开盘、跨年 ISO 周、拆股复权因子、重启幂等、禁基准替代 | 全部断言通过 | PASS |

**差异定位说明（不掩盖、不盲扩容差）**：
1. 首次使用 Parquet `float64` 开盘价撮合时，观察到 `max_qty_diff = 6.685e-4`、`max_nav_diff_usd = 0.0997` USD。定位首个差异日（`2023-01-03` `JPM`）发现：Qlib 二进制数据源 `provider/features/*/open.day.bin` 以 `float32` 存储价格（`123.23028564453125` vs Parquet `float64` `123.23028662668908`，相对差约 `1e-7`）。按 Qlib 真实 `float32` 开盘成交价重放后，全 23 账户请求与成交股数最大误差收敛至 `1.455e-10`，`target` 6 账户净值最大误差收敛至 `1.979e-9` USD。
2. 20 年历史窗口（`history`）的 `0.0107` USD 净值尾差（相对约 250 万美元净值 `< 4.3e-9`）同样来源于 Qlib `close.day.bin` `float32` 盯市与 Parquet `float64` 收盘价的精度差，目标权重和下单股数无任何策略语义偏离。

---

## 6. Alpaca 离线 HTTP 全链路验收与三策略共存隔离报告

完整机读实测数据见 [`alpaca-acceptance-summary.json`](alpaca-acceptance-summary.json)。

- **测试场景**：`2026-10-05` 周一开盘（信号截止 `2026-10-02`），初始账户现金 `$40,000`，`AAPL` 注入非平凡复权因子 `0.5`，复用 `StrategyInstances`、`DeploymentStore`、`LoadedStrategy`（真实 `-I -B` 隔离工作进程）、`PortfolioExecutor` 与 `#260 CapitalPolicyMode.SHARED_SAME_DIRECTION`（预算 `$35,000`）。
- **Phase 1（缺失 `^SP500TR` 隔离）**：未配置基准源时，`V69_CAUSAL` 正常完成全部 4 笔买入并进入 `WEEK_COMPLETE`；`V179_TREND` 与 `V182_RECOVERY` 均进入 `PREPARATION_INCOMPLETE (MISSING_BENCHMARK_PROVIDER:^SP500TR)`，互不干扰。
- **Phase 2（三策略同账户并行）**：注入只读 `^SP500TR` 后，三策略全部完成当周计划（共 12 笔真实订单）：
  - `V69_CAUSAL`（分配 `$20,000`）：剩余现金 `$17,066.67`，持有 AAPL/QQQ/SNDK/TQQQ，`multipliers={'TQQQ': 3}`。
  - `V179_TREND`（分配 `$10,000`，`blend_share=0.75`，`ridge_month=2026-10`，`ridge_cutoff=2026-09-24`）：剩余现金 `$4,793.33`，4 笔合法碎股成交。
  - `V182_RECOVERY`（分配 `$5,000`，`blend_share=0.40`，`ridge_month=2026-10`，`ridge_cutoff=2026-09-24`）：剩余现金 `$2,858.67`，4 笔合法碎股成交。
  - 资金政策表确认 `AAPL` 同向共享归属 `['V179_TREND', 'V182_RECOVERY', 'V69_CAUSAL']`。
- **Phase 3（重启 / 暂停恢复 / 策略升级幂等）**：销毁并重建 `StrategyInstances`、对 `V179_TREND` 执行 `pause/resume`、对 `V182_RECOVERY` 请求 `wait_flat` 升级至新包版本，累计重复调仓与重复下单均为 `0`，周检查点与模型 SHA-256 完整恢复。
- **小额模拟账户碎股验收**：`$1,000` 账户运行 `V179_TREND_BLEND`，按实际权益缩放（总花费 `< $900`），成功提交合法碎股订单（如 `AAPL 0.504589955`、`SNDK 0.644596573`、`TQQQ 0.642718988`、`QQQ 0.215805861`）。

---

## 7. 实际运行依赖与资源消耗报告

完整机读数据见 [`runtime-dependencies-resources.json`](runtime-dependencies-resources.json)。

| 指标 | 实测值 | 说明 |
|---|---|---|
| 云端运行依赖 | `numpy`, `pandas` | `cloud_qlib_dependency=false`, `cloud_sklearn_dependency=false` |
| 研究端离线训练依赖 | `numpy`, `pandas`, `scikit-learn` | 仅在 `train_monthly_ridge_artifact` 内惰性导入 |
| 离线 Ridge 模型工件大小 | `178,804` Bytes (`~175 KB`) | 覆盖 2011–2026 共 179 个拟合月（含 `label_maturity_date`） |
| V2 策略包大小 (`v179.zip` / `v182.zip`) | `181,460` / `181,463` Bytes | 内含 `strategy.py` + `model_artifact.py` + `manifest.json`（`migration="artifact_refresh"`） |
| 在线单次 252 日推断延迟 (`V179` / `V182`) | `335.43 ms` / `325.81 ms` | 单核 Python 实测 |
| 在线单次 252 日峰值内存 (`V179` / `V182`) | `0.974 MiB` / `0.959 MiB` | `tracemalloc` 峰值统计（远低于 2 vCPU / 1 GiB 云端约束） |

---

## 8. 交付清单与面向 P10-04 的交接接口

### 8.1 交付物清单
- 原生策略与共享指标：`src/factorlab/strategies/{qlib_blend_ridge,v179_v182_native,v179_v182,v179_v182_runtime}.py`
- 只读基准与 Alpaca 运行时适配：`src/factorlab/integrations/alpaca/benchmark_data.py` 及 `strategy_data.py / strategy_execution.py / strategy_instances.py` 增量扩展
- 离线训练工具与验证脚本：`tools/research/{train_v179_v182_ridge,validate_v179_v182_migration}.py`
- 模型工件与示例配置：`examples/v179_v182_alpaca/{README.md,multi_strategy_example_config.json,artifacts/v179_v182_ridge_target*.json}`
- 测试与冻结夹具：`tests/research/test_v179_v182_equivalence.py`、`tests/integrations/test_alpaca_v179_v182_runtime.py`、`tests/fixtures/v179_v182_original/`
- 验证报告与证据：`docs/validation/p10-03/{REPORT.md,differential-summary.json,target-weight-differences.csv,order-intent-differences.csv,alpaca-acceptance-summary.json,runtime-dependencies-resources.json}`

### 8.2 面向 P10-04 与 P10-11 的稳定接口与合同协同
- **研究层全链路分析接口**：`NativeV179` / `NativeV182`（含 `explain_targets(t, prices, valid_prices, held)` 输出全量中间层权重、市场状态、Ridge 月度截止点与预测值）及 `V179Planner` / `V182Planner`。
- **无状态检查点接口**：`V179Strategy` / `V182Strategy`。
- **离线模型训练与冻结工件合同（与 P10-11 对齐）**：`train_monthly_ridge_artifact(adjusted_close, market_index, omit_sndk=...)`、`validate_ridge_artifact`、`select_causal_month_model`、`predict_residual_from_artifact`。工件通过 `artifact_sha256`、逐月 `train_start <= train_cutoff < effective_from_signal_date` 与 `strict_model_month=True` 绑定，避免研究端与运行时出现两套模型时间口径。
- **只读外部参考数据时间语义（与 P10-11 及 `MarketDataService.prepare_history` 对齐）**：`LocalBenchmarkProvider` 与 `YahooSP500TRProvider` 显式区分四类时间戳：`signal_cutoff`（有效时间截止，必须等于上一已完成交易日收盘）、`acquired_at`（I/O 或 HTTP 完成后实测获取时间）、`knowledge_cutoff`（实时模式等于 `acquired_at`，历史 PIT 回放模式由调用方显式指定且不伪造 `observed_at`）以及最终决策 `event_time`（`StrategyData.event()` 在行情与基准准备完成后读取，满足 `signal_cutoff < acquired_at <= observed_horizon <= event_time`）。
- **V2 运行时与打包接口**：`V179AlpacaStrategy` / `V182AlpacaStrategy`、`build_v179_package` / `build_v182_package`。
- **复现命令**：
  ```bash
  PYTHONPATH=src python -m pytest tests/research/test_v179_v182_equivalence.py tests/integrations/test_alpaca_v179_v182_runtime.py -v
  PYTHONPATH=src python tools/research/validate_v179_v182_migration.py
  ```

---

## 9. PR #262 审查修复与复验记录（P1 / P2 / 打包基线）

- **P1（基准获取时间与决策时间解耦）**：
  - 同时修复 `LocalBenchmarkProvider.history()`、`YahooSP500TRProvider.history()`、`StrategyData.event()` 与 `_AlpacaBlendStrategyV2._extract_benchmark()`。
  - 实时路径不再在数据读取/HTTP 请求前传入过期 `now=self.clock()`，而是对齐 `MarketDataService.prepare_history()` 语义，在获取完成后取 `acquired_at = self.clock()` 作为实时知识时间截止（`knowledge_cutoff`），并在 `StrategyData.event()` 中以后获取时钟 `observed_horizon` 及 `signal_cutoff` 双向校验每条记录。
  - 完整保留 `observed_at` 校验、历史 PIT 显式 `knowledge_cutoff` 回放（支持带 `received_at` 的修订版本选择，未带 `received_at` 的实时快照对过去 `knowledge_cutoff` 严格 fail-closed）、未来 `received_at` / 盘前 `received_at` / 未来 `signal_cutoff` 拒绝。
  - 新增 `test_benchmark_advancing_clock_http_delay_explicit_received_at_pit_and_isolation` 覆盖真实推进时钟、模拟 HTTP 1.5s 延迟、本地与 Yahoo HTTP、显式 `received_at`、历史 PIT 回放、未来数据拒绝及 Yahoo 503 仅阻塞 V179/V182 而不影响 V69；原有三策略共存与小额碎股用例也升级为推进时钟并通过。
- **P2（跨月空预测窗口不误要求新月份模型）**：
  - 在 `predict_residual_from_artifact()` 中先提取当月 `score_now = panel.loc[days[0]:days[-1]]`，为空则跳过，仅在存在实际预测行时以 `score_now.index.min()` 调用 `select_causal_month_model(..., strict_month_coverage=strict_month_coverage)`；同时在 `NativeV100.targets()` 中对真实信号日 `previous` 保留 `select_causal_month_model` 校验，确保实际信号月缺少模型时（无论是否开启诊断）仍严格抛出 `ModelArtifactStaleError`。
  - 新增 `test_month_boundary_empty_prediction_month_crossyear_holiday_and_restart`（覆盖 V179 与 V182），验证月初首个交易日执行上月末信号（`2025-12-01` 执行 `2025-11-28` 信号）、空新月份跳过、实际新月份缺模型拒绝、跨年且节假日顺延周首日调仓（`2024-01-02` 周二执行 `2023-12-29` 信号）及重启幂等一致性。
- **分发打包与归档防篡改扫描**：
  - 按既有 `v69_original` 先例，在 `tools/migration/scan_distribution.py` 绑定 `tests/fixtures/v179_v182_original/source-manifest.json` 全部 13 个冻结 Parquet 精确路径与 SHA-256，并在 `tools/migration/check_distribution_rejections.py` 新增 `unbound_v179_v182` 与 `changed_v179_v182` 两项归档篡改拒绝用例。
  - 本地 Python 3.14 完整通过 `python -m build`、`inspect_distributions.py`（509 模块，0 违规）、`check_distribution_rejections.py`（23 项归档攻击全部拒绝）与 `packaging_ci.py`（wheel/sdist 独立 core-only venv 安装、导入、CLI、demo 与 0 findings 扫描）。
- **联合复验结果**：
  - Python 3.13.9：`test_v179_v182_equivalence.py` **25 passed**、`test_alpaca_v179_v182_runtime.py` **5 passed**；全量 V69 + V2 部署/切换/恢复 + Alpaca 集成套件零回归。
  - 23 归档账户全链路重放再次确认：`max_weight_diff = 9.714e-17`、`max_requested_qty_diff = 1.455e-10`、`max_nav_diff_usd = 0.0107` USD。

---

## 10. PR #262 第二轮审查修复记录（带仓月度模型更新与月度训练时机）

- **P1（保留持仓的兼容月度模型工件更新与回滚）**：
  - 完整复用既有 `StrategyPackage`、`DeploymentStore`、`DeploymentController` 与 `AlpacaDeployment` 状态机，不引入第二套部署状态机，并与 P10-11 冻结模型身份合同对齐：
    - `src/factorlab/strategies/package.py`：在 V2 manifest 中新增结构化 `model_artifact` 元数据（`kind / schema_version / file / artifact_sha256 / covered_start / covered_end / omit_sndk / fitted_months`）、显式 `migration="artifact_refresh"` 策略与 `FACTORLAB_MODEL_REFRESH_V2`（`model_refresh_candidate`）请求凭证（无需伪造 Champion 晋升）。
    - `src/factorlab/strategies/qlib_blend_ridge.py`：新增零执行纯 AST 解析器 `extract_frozen_artifact_from_module_source`（杜绝经由 `model_artifact.py` 夹带任意可执行代码）与历史月份兼容性校验 `verify_ridge_artifact_compatibility`（确保已拟合历史月份参数不可被回填改写，升级覆盖期单调不收缩）。
    - `src/factorlab/strategies/deployment.py`：`DeploymentStore.request()` 与 `DeploymentController._step()` 双重门禁校验新旧包除 `model_artifact.py` 与相关版本/摘要/证据外，逻辑策略、实例、账户、`allocation_id`、标的、执行合同、配置、依赖、状态 schema 及全部策略源码字节完全一致；严格保留未确认/`UNKNOWN`/未追踪/未终态订单与未结决策对账门禁，绝不绕过；仅在全部订单终态且兼容性通过时保留持仓、现金、绩效账本、checkpoint 与水位完成切换或兼容回滚（普通非兼容变更继续严格遵循 `wait_flat`/`reduce_only`）。
    - `src/factorlab/integrations/alpaca/strategy_deployment.py`：修复带仓切换事件日志序列化时 `Decimal` 持仓数量改为精确字符串 `str(quantity)`，避免 `TypeError`；`v179_v182_runtime.py` 统一以 UTF-8 LF 写入策略包成员保证跨平台 `package_id` 一致。
  - 新增 `test_position_preserving_monthly_model_refresh_rollback_recovery_and_guards` 真实 Alpaca 离线 HTTP 全链路验证：持有多只证券时完成月度模型更新（零新增交易、持仓/资金/checkpoint/水位完全保留）、新月份周调仓使用新模型、旧决策与订单保留原模型身份、外部未追踪挂单及未终态子单严格阻塞、激活回调中断后重启恢复、篡改历史月份参数与夹带可执行代码均自动失败回滚、兼容模型回滚不强制清仓、修改 `strategy.py`/配置与普通 `wait_flat` 升级均不得绕过约束。
- **P2（修正月度训练时机与五类时间点语义）**：
  - 保持原始 5 交易日前瞻标签、508 交易日回看窗口、Ridge `alpha=80` 及 `strict_model_month=True` 因果语义，在每个拟合月记录 `label_maturity_date` 并于 `validate_ridge_artifact` 强制校验 `train_start <= train_cutoff < label_maturity_date <= effective_from_signal_date`。
  - 明确区分五个时间点：训练样本截止日 `train_cutoff`（`calendar[i-5]`）、标签成熟时间 `label_maturity_date`（当月首交易日 `d_0 = calendar[i]` 收盘）、模型生成时间 `generated_at`（`d_0` 收盘且全部行情与 `^SP500TR` 可用后）、模型生效信号日 `effective_from_signal_date`（`d_0`）与实际执行日 `execution_session`（严格晚于 `d_0` 的首个当月信号自然周开盘）。
  - 更新 `examples/v179_v182_alpaca/README.md` 与 `tools/research/train_v179_v182_ridge.py`，覆盖月初周一（`2025-12-01`）、月初周五（`2025-08-01`）与跨年节假日顺延（`2024-01-02` 周二）场景，并新增参数化截断历史回归 `test_monthly_training_timing_truncated_history_matches_full_after_first_session` 验证仅用上月末数据不生成当月模型、加入当月首日后与完整历史模型误差 `< 1e-12`。

---

## 11. 同步 `origin/main` (`9f02ebf`) 与 P10-11 / P10-04 联合验收记录

- **合并整合（含 `#264` P10-11、`#267` P10-01、`#269` P10-04）**：
  - 逐文件解决 `package.py`、`loader.py`、`strategy_data.py`、`strategy_instances.py` 全部 4 处冲突（未使用 `ours`/`theirs` 覆盖），完整保留：
    1. **P10-11 全部能力**：外部策略协议（`references` / `runtime_profile` / `LinearModel` / `DataHubReferences`）、无 TOCTOU 的 `read_package()` 源码校验加载、`StrategyPackageError` / `StrategyAssetIneligible` 隔离以及 `StrategyData.qualify()`。
    2. **P10-03 全部能力与已通过 Codex 复审的五条 P1/P2 修复**：V179/V182 原生金融逻辑与月度 Ridge 工件、`^SP500TR` 后获取时间语义与 PIT 回放、跨月空预测行跳过、带仓兼容 `artifact_refresh` 模型更新与回滚、加载前零执行纯 AST 工件校验（统一复用 P10-11 `read_package()` 实现 `load_package_sources()`）以及当月首交易日收盘后月度训练时机合同。
- **对接 P10-04 独立验证框架（明确区分 `SIGNAL_EQUIVALENCE` 与 `EXECUTION_EQUIVALENCE`）**：
  - 在 `src/factorlab/strategies/v179_v182_native.py` 暴露 `NativeBlendSignalAdapter` 与 `build_signal_provider`（`signal_source = "NATIVE_FL"`），并在 P10-04 的 `replay.py` 预留挂载钩子接入原生 V179/V182，同时在 `evaluation.py` 中显式分离 `SIGNAL_EQUIVALENCE` 与 `EXECUTION_EQUIVALENCE` 状态维度。
  - **独立差分实测结果**：
    - **`SIGNAL_EQUIVALENCE`（原生信号 vs 冻结参考信号，独立于撮合精度）**：`v179/v182` 原生 `NATIVE_FL` 相对 `ExternalPackageSignalAdapter` 与参考 `targets.csv` 在全部 **23 / 23** 归档账户上目标权重最大绝对差 **`9.714e-17`**（`layer2_targets_exact = True`），且在 `qlib_float32` 与 `float64` 两种撮合精度下均精确通过。
    - **`EXECUTION_EQUIVALENCE`（P10-04 独立回测引擎 7 层 fail-closed 16 项核验）**：在 `qlib_float32` 参考兼容模式下全部 **23 / 23** 归档账户精确通过（`exact_reference_match = True`，`failed_checks = []`，日净值最大差 **`2.794e-09` USD**）；在 `float64` 高精度模式下，原生 `NATIVE_FL` 与冻结参考信号在同一 FL 独立引擎下日净值最大差 **`<= 1e-9` USD**。
  - **治理与金融结论保留**：严格保留 P10-04 的 **`INCONCLUSIVE`（`overall_promoted = False`、`retained_incumbent = "v69"`、`FORMAL_ADMISSION_BLOCKED`）** 金融结论，绝不因原生移植及信号/执行等价通过而自动将 V179/V182 晋升为 Champion，不操作生产 VM。
- **合并后联合回归结果**：
  - Python 3.13.9 全量套件（`test_external_strategy`、`test_s04_package_failures`、`test_alpaca_v179_v182_runtime`、`test_alpaca_instance_switch`、`test_strategy_management_v2`、`test_strategy_management_recovery`、`test_v179_v182_equivalence`、`test_p10_04_external_validation`、`test_v69_equivalence`）：**116 passed, 0 failed**。
  - Python 3.14 分发构建、清单巡检与 23 项归档攻击拒绝扫描全部通过。

## 12. P10-07 动态资本联合集成

集成基线：P10-03 `fc5acdb1982730122720c3ab1834b8239d048b21`，合入 main 的 P10-07 合并提交 `5a1be4d34f6222c186937bea3ac7c90b9d675a14`。保留原生策略、静态工件校验与兼容带仓月度更新，以及动态资本、计划级承诺、订单预留与重启恢复。

带仓模型更新联合用例已通过（Python 3.13，1 passed）：追加 1,000 USD、撤回 500 USD，切换及激活中断重启后当前资本保持 10,500 USD，原 allocation_id、持仓、资金事件与流量调整绩效保持正确，V69 的资金和持仓不变。未生成子单的买入计划在切换前阻止撤资，部署对账将其明确持久终止，保留原请求且不新增订单；未确认／部分成交订单继续阻塞切换。新增用例首次在测试断言处失败：`allocation_basis` 按合同保留初始 10,000 USD，而非当前 10,500 USD；修正断言后复验通过，分别验证不可变资格基准和当前运行资本。扩大 Alpaca 相邻回归已通过（Python 3.13，22 passed），覆盖 V179/V182 与 V69 共存、动态资本、待执行资金承诺、部署切换与资本管理；新增联合场景另行复验通过。

已完成：Python 3.14 的 Alpaca 待执行承诺、策略执行与资本命令恢复套件 12 passed；Python 3.13 的原生信号／独立执行等价、无状态重启及跨月边界定向套件 8 passed。原生信号与独立执行等价仅对受影响路径复验，不无条件重跑全部历史研究。

本地独立代码审查已完成，未报告可确认的问题。审查对象为 P10-03 `fc5acdb1982730122720c3ab1834b8239d048b21` 合入 main `5a1be4d34f6222c186937bea3ac7c90b9d675a14` 后的未提交工作树；审查标识 `aa67659f036d069bf`（high）。范围涵盖三个 Alpaca 集成文件、包加载与部署的兼容工件更新门禁、初始资本资格与当前资本执行的区分，以及资本／会计／执行和 Binance 宿主相关删除行为。特别检查终态订单门禁、计划级承诺、正式订单预留、恢复及实例隔离。此记录为本地独立审查，不是远端 Codex 回执；最终提交身份将在提交后关联。

TODO: 在远端 Codex 额度恢复后，对本轮最终 HEAD 补充远端复审。
