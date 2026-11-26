# V179 / V182 原生策略包、离线训练与 Alpaca Paper 多策略接入说明

## 1. 策略与运行边界

- **股票池（保持原口径）**：`AAPL, SNDK, BA, C, JPM, GE, TQQQ, QQQ`（8 只可交易美股/ETF）。
- **只读基准信号数据**：`^SP500TR`（标普 500 总回报指数），与可交易标的严格隔离，禁止用 `SPY`、`QQQ` 或价格指数 `^GSPC` 替代。
- **V179 (`V179_TREND_BLEND`)**：`blend_rule = 'trend'`（`^SP500TR > MA200` 且市场宽度 `>= 50%` 时进取组合 V125 占 75%，否则占 25%；防御组合 V165 占余量）。
- **V182 (`V182_RECOVERY_BLEND`)**：`blend_rule = 'market_recovery'`（市场宽度较 10 个交易日前增加 `> 10%` 且 `^SP500TR > MA63` 时进取组合 V125 占 75%，否则占 40%；防御组合 V165 占余量）。
- **云端零 ML 训练约束**：月度 `StandardScaler + Ridge(alpha=80)` 仅在研究端离线训练并生成轻量冻结工件（`v179_v182_ridge_target.json`），云端运行时仅加载冻结参数执行纯 NumPy/Pandas 因果推断，不导入 `scikit-learn`，不引入 `qlib`。
- **不覆盖 V69 / 不自动晋升 Champion**：V179 与 V182 以独立 V2 实例和独立资金槽位注册，与既有 `V69_CAUSAL` 在 `#260` `SHARED_SAME_DIRECTION` 公共资金政策下并行共存，不会自动替换当前云端运行策略。

## 2. 离线月度 Ridge 训练与策略打包

每月首个交易日开盘前（使用截至上一已完成交易日的冻结历史数据，满足 5 个交易日标签成熟期，杜绝未来信息泄漏）：

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

产物说明：
- `artifacts/v179_v182_ridge_target.json`：含 `artifact_sha256` 校验和与逐月 `train_start / train_cutoff / effective_from_signal_date / scaler_mean / scaler_scale / ridge_coef / ridge_intercept`（约 170 KB）。
- `v179_trend_blend.zip` / `v182_recovery_blend.zip`：标准 `FACTORLAB_DECISION_V2` 内容寻址策略包，内嵌 `strategy.py` 与 `model_artifact.py`，通过 `install_package` 安装并在隔离工作进程中执行。

## 3. 多策略共存配置示例

参见 [`multi_strategy_example_config.json`](multi_strategy_example_config.json)：
- `capital_policy.mode = "SHARED_SAME_DIRECTION"`（复用 `#260` 公共资金政策）。
- 三个实例各自独立绑定：`V69_CAUSAL`（`native_v69: true`，保留旧账户和旧账本）、`V179_TREND`（`allocation_id: "v179-usd"`）、`V182_RECOVERY`（`allocation_id: "v182-usd"`）。
- `benchmark_source` 提供只读 `^SP500TR` 数据快照；若该数据缺失或过期，仅阻止 `V179_TREND` 与 `V182_RECOVERY` 进入 `PREPARATION_INCOMPLETE`，不影响 `V69_CAUSAL` 正常运行。
