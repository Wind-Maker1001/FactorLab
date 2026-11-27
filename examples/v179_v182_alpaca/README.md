# V179 / V182 原生策略包、离线训练与 Alpaca Paper 多策略接入说明

## 1. 策略与运行边界

- **股票池（保持原口径）**：`AAPL, SNDK, BA, C, JPM, GE, TQQQ, QQQ`（8 只可交易美股/ETF）。
- **只读基准信号数据**：`^SP500TR`（标普 500 总回报指数），与可交易标的严格隔离，禁止用 `SPY`、`QQQ` 或价格指数 `^GSPC` 替代。
- **V179 (`V179_TREND_BLEND`)**：`blend_rule = 'trend'`（`^SP500TR > MA200` 且市场宽度 `>= 50%` 时进取组合 V125 占 75%，否则占 25%；防御组合 V165 占余量）。
- **V182 (`V182_RECOVERY_BLEND`)**：`blend_rule = 'market_recovery'`（市场宽度较 10 个交易日前增加 `> 10%` 且 `^SP500TR > MA63` 时进取组合 V125 占 75%，否则占 40%；防御组合 V165 占余量）。
- **云端零 ML 训练约束**：月度 `StandardScaler + Ridge(alpha=80)` 仅在研究端离线训练并生成轻量冻结工件（`v179_v182_ridge_target.json`），云端运行时仅加载冻结参数执行纯 NumPy/Pandas 因果推断，不导入 `scikit-learn`，不引入 `qlib`。
- **不覆盖 V69 / 不自动晋升 Champion**：V179 与 V182 以独立 V2 实例和独立资金槽位注册，与既有 `V69_CAUSAL` 在 `#260` `SHARED_SAME_DIRECTION` 公共资金政策下并行共存，不会自动替换当前云端运行策略。

## 2. 离线月度 Ridge 训练时机、因果时间语义与带仓模型更新

### 2.1 严格的月度训练时机（原 5 交易日标签 / 508 交易日窗口 / Ridge `alpha=80`）

原始月度 Ridge 训练在当月首个合法交易日 `d_0 = calendar[i]` 上定义训练截止日 `train_cutoff = calendar[i - 5]`；该截止日的 5 个交易日前瞻残差标签在 **`d_0` 收盘后** 才成熟，且 `train_monthly_ridge_artifact()` 仅对输入日历中已包含且存在有效特征行的月份拟合模型。因此：

- **仅提供上月末历史数据（截至上月最后交易日收盘）不会生成当月模型**，且不得提前使用未成熟标签、关闭 `strict_model_month` 或在云端在线训练。
- **正确排程窗口**：在**当月首个合法交易日 `d_0` 完成收盘、且 8 只证券与 `^SP500TR` 收盘行情均已可用之后**，并在**首次使用当月信号的自然周开盘调仓之前**完成离线训练、工件校验与部署切换。
- **五个显式区分的时间点**：
  1. **训练样本截止日期 (`train_cutoff`)**：`calendar[i - 5]`（508 交易日回看窗口 `[calendar[max(0, i - 508)], calendar[i - 5]]` 的最后样本日）。
  2. **标签成熟时间 (`label_maturity_date`)**：`d_0 = calendar[i]` 收盘（`train_cutoff` 对应 5 个交易日前瞻标签的成熟时刻）。
  3. **模型生成时间 (`generated_at`)**：研究端离线任务在 `d_0` 收盘及行情/基准完整可用之后产出冻结工件的实际 UTC 时间。
  4. **模型生效信号日期 (`effective_from_signal_date`)**：`d_0 = calendar[i]`（当月首个可由该模型打分的已完成信号交易日）。
  5. **实际执行时间 (`execution_session`)**：首次使用当月信号的自然周首日开盘，其信号日 `previous >= d_0`，因此必然严格晚于 `d_0` 收盘。

典型日历场景：
- **月初即周一**（如 `2025-12-01` 周一）：`2025-12-01` 开盘调仓使用上月末信号 `2025-11-28`（沿用 `2025-11` 模型，不要求 `2025-12` 模型）；`2025-12-01` 收盘后训练并切换 `2025-12` 模型，用于 `2025-12-08` 开盘（信号 `2025-12-05`）。
- **月初为周中或周五**（如 `2025-08-01` 周五）：`2025-08-01` 开盘不调仓；`2025-08-01` 收盘后（周末窗口）训练并切换 `2025-08` 模型，用于 `2025-08-04` 周一开盘（信号 `2025-08-01`）。
- **跨年与节假日顺延**（如元旦休市 `2024-01-01`）：`2024-01-02` 周二为新年首个交易日兼当周首日，开盘使用 `2023-12-29` 信号（`2023-12` 模型）；`2024-01-02` 收盘后训练并切换 `2024-01` 模型，用于 `2024-01-08` 开盘（信号 `2024-01-05`）。

```bash
PYTHONPATH=src python tools/research/train_v179_v182_ridge.py \
  --market-dir tests/fixtures/v179_v182_original/fixtures/target \
  --benchmark tests/fixtures/v179_v182_original/fixtures/target/benchmark.parquet \
  --output examples/v179_v182_alpaca/artifacts/v179_v182_ridge_target.json \
  --build-packages-dir results/p10-03-packages \
  --account-id <YOUR_ALPACA_PAPER_ACCOUNT_ID> \
  --capital 10000 \
  --code-commit $(git rev-parse HEAD)
```

### 2.2 产物说明与保留持仓的月度模型更新 (`artifact_refresh`)

- `artifacts/v179_v182_ridge_target.json`：含 `artifact_sha256` 校验和与逐月 `train_start / train_cutoff / label_maturity_date / effective_from_signal_date / scaler_mean / scaler_scale / ridge_coef / ridge_intercept`（约 172 KB）。
- `v179_trend_blend.zip` / `v182_recovery_blend.zip`：标准 `FACTORLAB_DECISION_V2` 内容寻址策略包（默认 `migration="artifact_refresh"`，附带 `model_artifact` 冻结元数据），内嵌 `strategy.py` 与纯数据 `model_artifact.py`，通过 `install_package` 安装并在隔离工作进程中执行。
- **带仓兼容模型更新与回滚**：通过既有 `DeploymentStore.request(manifest, model_refresh_candidate(...))` 与 `DeploymentController.step()` 执行。状态机在先完成订单对账（无未确认、`UNKNOWN`、未终态或未结决策）的前提下，校验新旧包策略代码、配置、标的、执行合同与历史月份参数完全兼容且 `model_artifact.py` 仅包含纯冻结 JSON 负载，允许**保留当前持仓、现金、绩效账本与 checkpoint** 完成切换或兼容回滚，不触发强制清仓；普通非兼容策略变更仍严格遵循 `wait_flat` / `reduce_only`。

## 3. 多策略共存配置示例

参见 [`multi_strategy_example_config.json`](multi_strategy_example_config.json)：
- `capital_policy.mode = "SHARED_SAME_DIRECTION"`（复用 `#260` 公共资金政策）。
- 三个实例各自独立绑定：`V69_CAUSAL`（`native_v69: true`，保留旧账户和旧账本）、`V179_TREND`（`allocation_id: "v179-usd"`）、`V182_RECOVERY`（`allocation_id: "v182-usd"`）。
- `benchmark_source` 提供只读 `^SP500TR` 数据快照；若该数据缺失或过期，仅阻止 `V179_TREND` 与 `V182_RECOVERY` 进入 `PREPARATION_INCOMPLETE`，不影响 `V69_CAUSAL` 正常运行。
