# P7-M01：统一策略与冻结模型 Paper 闭环

**P7_M01_STRATEGY_PAPER_INTEGRATED / P7_M01_MODEL_PAPER_INTEGRATED / P7_M01_FULL_ACCEPTED**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 执行链路与接口

`RealtimeAccount.closed_bar()` 接收已确认闭合的 UTC K 线，策略输出 `StrategySignal`。目标权重在下一可执行事件上依据账户权益、实际持仓和交易精度转换为差额 `OrderIntent`；未完成订单阻止叠加目标，UNKNOWN 或未完成对账阻止新单。`RealtimeAccount._submit()` 经 `TradingController._submit_in_transaction()` 完成统一风控与提交，`PaperBroker` 在更晚事件成交；信号、目标结果、订单确认与账户快照在现有 PaperStore 同一事务中提交。

- `trading/strategy.py`：`ClosedBar`、`StrategySignal`、`Strategy`、`DualMAStrategy`。信号保存策略版本、时间、水位、来源、目标、模型身份及幂等身份；区分正式信号、无信号、数据不足和拒绝。
- `trading/frozen_strategy.py`：四模型共用 `FrozenModelStrategy`，复用冻结包加载和 `predict()`。`FrozenDeploymentBinding` 将市场、品种、频率、特征顺序/定义、float64 类型及算法版本绑定到包内研究合同摘要；不能将 A 股包重新贴标签用于 Binance。`FixedScoreConversion` 独立转换分数。
- `trading/strategy_config.py`：`select_strategy()` 提供 threshold / dual-ma / frozen-model 选择。默认仍为原阈值策略；冻结部署配置只允许可信本地模型包。
- `trading/strategy_replay.py` 与 `examples/strategy_paper_evidence.py`：固定历史 Snapshot、训练冻结、预测评估和同路径 Paper 回放。
- `strategies/dual_ma.py`：旧研究函数与新适配器共享 `crossover_indicators()`；不复用 A 股执行规则。
- `trading/realtime.py`、`realtime_service.py`：接入策略和确认闭合的公共 Kline；逐笔成交仍是在线可执行事件，不将 OHLC 当作成交。
- `trading/control.py`、`paper.py`、`storage.py`：复用控制器事务提交、账本投影与已有事件表；没有第二套订单系统、Broker 或经济账本。
- `integrations/binance_futures_trading/{market,rules}.py`：确认闭合桶解析、两个数量过滤器的共同精度及剩余成交数量的风险校验。

信号与 clientOrderId 的关联、拒绝原因在原事件表中保存。恢复要求配置/版本/模型包身份一致。重放已有确认不重复记账；断线后必须显式恢复，K 线缺口先暂停并要求连续补齐。Kill Switch 保留。

## 接入范围

| 策略/模型 | 状态 |
| --- | --- |
| 原阈值策略 | 默认兼容，原回归通过 |
| DualMA MA5/MA20 | 真实 NVDAUSDT 小时线及日线 Paper 闭环通过；保留真实交叉确认 |
| Frozen Ridge | 真实 Binance 小时线训练、冻结、推理与 Paper 闭环通过 |
| FactorScore / LightGBM / XGBoost | 同一适配器的真实模型构造、冻结加载、预测及非法合同拒绝通过；合同测试数据为 SYNTHETIC |
| Grid | **未接入 / BLOCKED**：当前策略以 SYNTHETIC 整数现货库存、独立订单撮合和费用状态为运行合同，没有可直接交给 TradingController 的目标/成交反馈边界。复用其执行器会重复记账；抽走这些状态会改变原行为，因此本轮不建立替代网格引擎 |

TODO：Grid 若继续适配，应先为原策略提供保持行为不变的目标提案与执行反馈边界，再绑定 Binance 小数数量和现有账户状态。

## 真实数据与研究设定

复用 P6-D02 已有历史，不新增采集：NVDAUSDT 在 `[2026-08-12, 2026-11-10)` 的 2,160 根小时线、90 根日线。

- 小时线版本：`8f4861c8b3bef653b6f68cc778516e771ec7bd88678c00ad404b9111ac9ca607`。
- 日线版本：`34f804333867a581b2ab135249ca1e7a1b9acacfdec55896005f0be59fb4704d`。
- 特征：闭合小时桶的 1 桶收益、6 桶平均收益、24 桶收益波动；只使用截至信号水位的数据。
- Ridge：alpha=1，fit_intercept=True，seed=42；标准化/缺失处理仅拟合 Train。无搜索或 Test 调参。
- 预测目标：信号后下一开盘到再下一开盘的一小时收益。预测周期不等于强制平仓周期：目标策略每小时重新判断，分数 >0.0001 时持有 5% 权重，<-0.0001 时归零，中间 HOLD 保持原持仓。
- 时间切分：名义 60/20/20 边界向后对齐 UTC 日；Train 最后标签退出 `2026-10-05T23:00Z`，Validation 从 `2026-10-06T00:00Z` 开始，Test 从 `2026-10-24T00:00Z` 开始。跨边界标签剔除，行数分别为 1,294 / 431 / 407。
- 冻结包：ridge / fixed-ridge-v1；内容身份 `c4fd343330624fccc9cefff11f3ef5c98d88e690566a1a8d9931fd42d3e320eb`。

本次离线训练和冻结后回放历史留出窗口，不将其写成模型在该历史时点已经部署的业绩。

### 预测质量，与执行收益分开

| 窗口 | RMSE | MAE | 方向命中率 | 零收益基线 RMSE |
| --- | ---: | ---: | ---: | ---: |
| Train | 0.00322535 | 0.00178113 | 50.93% | 0.00323035 |
| Validation | 0.00344545 | 0.00221168 | 50.35% | 0.00344891 |
| Test | 0.00556016 | 0.00331441 | 45.21% | 0.00554027 |

Test 的误差劣于零收益基线，方向命中率低于 50%；这次结果证明软件执行接入，不构成预测优势。

## Paper 订单、成交与账户

**REAL_BINANCE_HISTORY / HYPOTHETICAL_PAPER_EXECUTION**。初始资金各 1,000 USDT，目标权重 5%，合成费率 0.1%，滑点 0.01%。回放在下一桶开盘 +1ms 提交、+2ms 提供假设流动性以触发后续事件成交。该流动性不是交易所逐笔成交；历史开盘是 Paper mark 代理；使用快照规则而非历史 PIT 规则，不模拟资金费率或杠杆。没有 A 股 T+1、整手或涨跌停语义。

| 链路 | 信号 | 订单/成交 | 拒绝 | 费用 USDT | 期末现金 USDT | 期末权益 USDT | 仓位 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 小时 DualMA | 27 | 27 / 27 | 0 | 1.31704330237 | 996.85698532763 | 996.85215286763 | 0.27 NVDAUSDT |
| 小时 Frozen Ridge | 239 | 55 / 55 | 27 | 2.58438378588 | 991.87083513412 | 991.62297837412 | 0.27 NVDAUSDT |
| 日线 DualMA | 5 | 4 / 4 | 0 | 0.19270730467 | 997.74132198533 | 997.74132198533 | 空仓 |

小时 DualMA 与 Ridge 的 Paper 收益分别为 **-0.314785% / -0.837702%**；日线 DualMA 为 -0.225868%。Ridge 的 27 次拒绝是目标差额未满足交易过滤器；其余正式信号包含目标已满足的情况，不等同于订单。

可追踪成交示例：

- DualMA：`sgf16360a0325556dad337e0be35be6d`，BUY 0.23，成交价 211.521150000，费用 0.04864986450000 USDT，`2026-10-24T15:00:00.002Z`，FILLED。
- Ridge：`sgd24d81a5470459ca2c8734dcf3fff5`，BUY 0.23，成交价 211.211119000，费用 0.04857855737000 USDT，`2026-10-24T01:00:00.002Z`，FILLED。

[evidence.json](evidence.json) 保留全部订单、成交确认、费用、期末账户及恢复结果。三个真实运行账户均已重新打开并完成本地对账；逐一重放全部已有确认，新增经济记账为 0，现金和持仓保持不变，无未完成订单。对应事件数为 490 / 601 / 102。原 SQLite 经济账本保存在运行输出中，不提交复制账本。

## 定向验证与复现

**336 passed，40.64 秒**；[JUnit](targeted-tests.xml)。覆盖四模型合法推理及特征/来源/市场/版本漂移拒绝、未闭合桶、因果特征、下一事件执行、真实公共读取器分派、目标与成交精度、减仓、小额平仓、部分成交、自己的预留保证金释放、幂等、事务失败回滚、UNKNOWN、断线显式恢复、Kill Switch 以及 Demo/Production 网络准入回归。SYNTHETIC 测试数据不计入上面的真实历史研究结果。

现有 Python 环境中运行：

```bash
PYTHONPATH=src python examples/strategy_paper_evidence.py \
  --hourly-snapshot <P6-D02-hourly-generation> \
  --daily-snapshot <P6-D02-daily-generation> \
  --output <new-output-directory>

PYTHONPATH=src python -m factorlab.trading.realtime_service \
  --state-dir <paper-state> --strategy dual-ma --bar-seconds 3600 \
  --public-market-data --paper --paper-simulation --refresh-rules --seconds 60
```

公共会话只读取行情，冷启动历史不足时不生成正式信号；闭合桶缓存在账户中，重启后继续积累。FrozenModel 改用 `--strategy frozen-model --strategy-config <output>/deployment.json --bar-seconds 3600`。实时和回放共享信号/目标/提交路径；回放假设开盘流动性，实时等待实际公共成交事件，不能将两者的成交条件混称。

## 后续官方 Demo 最小接点

保持策略、模型、信号和 OrderIntent 不变：在账户构造边界注入已有 `DemoExecutionGateway`/控制器组合；Demo 模式停止本地 `_fills()`，改由已有订单确认和 `recover(controller)` 更新同一经济账本。继续使用现有 DemoExecutionPlan、一次性网络授权和控制器准入绑定，并将真实账户资格与 PaperSimulationPolicy 分开。本轮不启用这些权限，也不新增网络 POST/DELETE。
