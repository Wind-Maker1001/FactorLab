# 外部策略接入

普通 Python 策略、Qlib 导出的策略和带冻结模型的策略都可以交付为 `StrategyPackage`。入口类实现 `describe() -> StrategySpecV2` 和 `decide(event, state) -> DecisionV2`。`describe()` 必须与包 manifest 一致。`decide()` 使用事件内的数据和上一份 JSON checkpoint，返回目标仓位、目标数量、数量变化或显式订单，以及新的 JSON checkpoint。

## 包和运行声明

从 [示例构建器](../../examples/external_strategy/run.py) 的 `example_manifest` 开始。构建器复用现有 V2 字段，从 `describe()` 生成 `requirements`，把运行参数写入 `configuration`，并将入口及工件列于 `files`。通用打包调用依次是 `build_package(project_root, manifest, archive)` 和 `install_package(archive, release_root)`。后者按 `package_id` 安装不可变版本。

| 声明 | 内容 |
| --- | --- |
| `strategy_id`, `strategy_version`, `code_commit`, `entrypoint` | 稳定策略名、发布版本、完整源码提交和 `module:Class`。 |
| `market`, `account_id`, `symbols` | 市场、账户绑定和可交易标的。安装的包仍需显式绑定实例及资金分配。 |
| `requirements.data` | 交易标的数据类型、`history_limit`、`historical_observations`，以及可选的只读参考序列和账户投影。闭合 OHLCV 的 volume 可直接使用。 |
| `requirements.schedule` | `interval` 使用秒级间隔。Alpaca 也支持官方日历的 `session_open` 与 `natural_week_open`。策略可用 checkpoint 表达更低频的调仓，在中间事件返回空目标。 |
| `requirements.state` | `version` 标记 JSON 结构。`reset` 从空对象开始，`preserve` 要求结构版本相同，`migrate` 调用入口的 `migrate_state(state, from_version)`，并限制 `from_versions`。 |
| `requirements.capital`, `requirements.execution` | 币种、十进制字符串形式的资金需求、订单类型、有效期、做空、零股和 reduce-only 能力。市场和已有账户政策负责资格检查。 |
| `dependencies`, `runtime_profile` | 依赖以 `name==version` 固定，由目标环境预装。`lightweight` 跳过旧适配器所需的数值库预加载。 |
| `model`, `files`, `evidence`, `migration` | 可选冻结模型、带文件 SHA-256 的清单、独立证据引用、现有持仓切换政策。 |

新策略可用 `data.account_fields` 和 `data.allocation_fields` 选择传入 Python 的顶层投影。空列表明确表示无需该投影。Alpaca 提供账户 `market`、`account_id`，以及分配的 `allocation_id`、`currency`、`capital`、`cash`、`positions`。Binance 提供现有状态投影，资金分配可按需附带 `currency`、`cash` 和实例持仓 `positions`。遗漏字段会阻止当次决策。完整执行事件仍由宿主保留。

包内只交付可信 Python 源码和声明的线性 JSON 工件。入口如需同目录模块，设置现有的 `package: true`，使用相对导入，并将所有模块列入 `files`。账户凭证留在宿主，策略通过事件取得可用的只读信息。worker 以清理后的环境启动并限制外部 I/O，它的信任前提是部署方已接受包内 Python。策略项目不得引入私有交易所客户端。Qlib、XGBoost、LightGBM 和深度学习训练依赖留在研究环境。

## 参考数据

下面的声明为持有 SPY 的策略单独预热两根 IWM 日线。每个序列都带有来源，价格型序列还带口径和周期。

```json
{
  "references": {
    "IWM": {
      "closed_bar": {
        "source": "ALPACA:iex",
        "price_basis": "raw",
        "interval_seconds": 86400,
        "history_limit": 2
      }
    }
  }
}
```

事件的 `references[symbol]` 与交易行情使用相同的 `observations`、`history` 和 `watermark` 结构。调度主事件须是 `closed_bar`，每个参考序列须预热到声明长度。参考标的保持只读，目标、订单和撤单仍只允许 `symbols` 内的标的。生产适配器使用显式来源及接入的日历窗口。

- Alpaca 使用 `MarketDataService` 和 `MarketCache` 读取指定 feed 的 raw 日线或常规交易时段分钟聚合。参考资产只解析数据身份。SPY 与 IWM 的市场角色由各自的包声明决定。
- Binance Demo 利用已有公共 REST 和 SQLite 历史区间读取与决策间隔对齐的 raw K 线。参考数据可以声明 `funding_forecast` 或 `funding_settlement`，对应各自的来源记录与时间语义。预测允许结算目标在未来，已知时间必须在决策截止前。已结算资金费记录在取得后才可见。
- `DataHubReferences(hub)` 可用于历史回放，以及注入 Alpaca 实例宿主的外部来源。注册来源明确暴露 `strategy_<类型>`，可按同样的接口接入指数、行业、波动率、特征、预测等。适配器收到单一符号、查询窗口和 `knowledge_as_of`，返回包含 `symbol`、`observed_at`、`effective_at`、`payload` 的表，并提供精确的 `source_id` 和 `data_version`。适配器须提供可核验的历史可用时间。只有回顾性日期的现有 DataHub 来源不能满足该合同。

OHLCV、调整口径和基准身份由数据生产者负责。更换来源、复权方式或标的需要显式调整声明并构建新包。参考输入缺失时实例等待完整数据，其他独立实例照常接收自己的事件。

## 训练、推断和切换

训练侧保存特征定义、预处理参数、训练数据版本与截止时间、验证结果和模型身份。冻结线性模型声明使用 `model = {file, model_id, version, trained_through, effective_at}`。时间使用 UTC，生效时刻须晚于训练截止时刻。模型文件随包进入 `files`，格式为 `FACTORLAB_LINEAR_MODEL_V1`，包括有序的 `features`、同序有限数值的 `coefficients` 和 `intercept`。

入口接收由 worker 注入的 `model`，从可用历史增量计算特征，调用 `model.predict(features, event_time=event['event_time'])`。模型仅依赖 Python 标准库。预处理属于策略代码或其固定参数，且随包受版本控制。

发布新模型时保留原包，使用新的包和模型版本，并通过现有部署路径切换。模型决策收据记录工件版本、摘要与生效时间。活动实例只为推进时钟的新事件执行模型，重复输入返回已有收据，回滚恢复旧包的 checkpoint。P10-04 可将旧收据对应的包和原事件交给隔离 worker 重放。

## 本地运行和验证入口

从 checkout 根目录执行，使用相同环境中安装的 FactorLab，或把 `src` 加入 `PYTHONPATH`。

```sh
python -m examples.external_strategy.run a local-runs/p10-11
python -m examples.external_strategy.run b local-runs/p10-11
python -m examples.external_strategy.run c local-runs/p10-11
python -m examples.external_strategy.run c local-runs/p10-11 --model-version 2
```

A 使用已有 Alpaca 等权多资产示例，B 使用只读参考 ETF，C 从冻结工件推断。每条命令独立打包、安装并执行一次，输出包身份、目标和 checkpoint。C 的训练示例可以用 `python -m examples.external_strategy.train 1 local-runs/linear-1.json` 在研究环境重建。Linux 可用 `--measure` 获取分阶段耗时和 RSS。

P10-04 可使用 `strategy_package_identity(manifest)` 获取包与模型身份，从 `manifest_spec(manifest)` 获取数据依赖，调用 `LoadedStrategy.from_strategy_package(release, trusted=True).decide(event, state)`。这些入口既可供历史兼容回放、FactorLab 回测和蒙特卡洛情景使用，也可由现有官方模拟盘宿主消费。在各环境的事件和结果证据中记录行情来源、数据版本、时间与成交规则。示例使用合成输入。金融证据和官方模拟盘选定版本分别由独立验证和已有 Champion 部署合同决定。
