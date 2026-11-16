# MC-04：USD-M 永续风险情景覆盖层

**NO LIVE MONEY。结果为 INDEPENDENT_RISK_OVERLAY，不是完整 Paper 账户回放，也不是交易所强平成交。**

## 已有能力与缺口

`AccountFacts` 已区分钱包、可用余额、保证金余额、初始/维持保证金、报告强平价和真实 FUNDING_FEE；`RuleObservation` 保存当前过滤器、档位和来源，提供下单准入。二者没有提供可认证的历史强平引擎。当前 observed_at 档位不代表过去或未来有效。

现有 USD-M Paper 在成交时记已实现盈亏与手续费，持仓使用共享 `Position` 的带符号 base 数量、入场价和标记价。Grid 导出保存已含费用和滑点的权益。R05 `cashflow` 已定义资金费符号与覆盖层；R10 的价格路径、压力路径与 `funding=NOT_SIMULATED` 保持原样。新增模块只复用这些语义，不引入订单、持久账本或 TradingController，不修改 Demo 协议、授权、额度或生产对账。

缺口由 `research/perp_risk.py` 的纯计算覆盖层填补；`experiments/perp_risk.py` 连接冻结路径、MonteCarloRunner.asset_paths 与只读 Paper CSV。

## 规则与经济口径

`mc-perp-risk/v1` 显式输入线性 USDT 合约的 base 合约单位、来源、有效时间、初始/维持保证金档位、累计扣减和退出费用率。合约数量先乘单位转换成共享 Position.quantity；之后资金费和盈亏均使用 base 数量，不再乘一次单位。名义仓位为 `abs(base quantity) × mark`，由持仓和路径逐步报告。

合成档位采用 `IM = notional × initial_rate`、`MM = notional × maintenance_rate − deduction`，档位下界包含、上界不含。档位必须从零起连续覆盖，维持保证金在边界连续；超过给定覆盖范围直接拒绝。这是版本化合成模型，不是 Binance 已验证公式。

- 未实现盈亏：`Σ quantity × (mark − entry)`。
- 当期资金费：复用 `−quantity × settlement_mark × rate`；正值收款，负值付款。费率、结算时间与结算标记价独立于价格收益输入。
- 合成账户：`equity = budget + unrealized + cumulative_funding`。
- Paper 覆盖：`equity = original_NAV + Σ quantity × (risk_mark − exported_close) + cumulative_funding`。原 NAV 已含成交成本，不能再加全部未实现盈亏或扣原手续费。
- 全仓风险权益为完整账户权益；必须显式声明组合完整，并输入共享订单保证金。各仓位初始/维持保证金加总，盈亏共享。
- 逐仓 v1 为单个固定 endowed 持仓：风险权益为隔离抵押金加该仓盈亏和资金费，外部余额单列；不向外部余额自动补保证金。抵押金不得超过账户预算。动态逐仓 Paper 缺隔离分账历史，直接拒绝。
- 可用保证金为风险权益减初始保证金与订单占用；使用率为初始保证金加订单占用除以风险权益。非正权益时比例为 null，保留负余额及事件。

成交价/入场价、原 NAV 的估值参考 close、风险 mark、资金费结算 mark 是不同字段。模拟路径用作 mark 仅为情景假设，不代表可成交价格。`hypothetical_exit_cost` 是名义仓位乘手续费率及额外退出成本率的估计，只列示，不从权益扣除，也不生成成交。

## 时间、证据与拒绝语义

观察时间必须严格递增且为 UTC，结算必须有精确同刻持仓观察；不对结算时点插值。Frame.positions 约定为严格早于同刻成交的持仓；同刻资金费先于成交，调用者须按此准备帧。每个事件有独立 identity、来源及 kind，重复 identity 拒绝。Regular/Special 等不同真实事件须保留不同 identity。

`ASSUMED_SETTLEMENT` 表示情景中已经结算的假设费用；`OBSERVED_PUBLIC_SETTLEMENT` 表示公开历史事件，不等于已确认到账的账户 FUNDING_FEE。本接口不把预测费率当真实结算，也不生成未来真实费率。

所有场景规则为 SCENARIO_ASSUMPTION_ONLY；权威强平结论固定 BLOCKED_RULE_EVIDENCE。没有通过简单布尔开关升级为交易所认证的入口。矩阵见 [rule-evidence.json](../validation/mc-04/rule-evidence.json)。TODO：只有具备适用历史档位、账户系数、完整结算语义、标记价格及经独立验证的交易所强平规则后，才能增加真实规则认证模式。

`risk_equity <= maintenance_margin` 只产生 SCENARIO_LIQUIDATION_THRESHOLD。之后继续给定敞口的反事实轨迹，不平仓、不重设权益、不钳制负数、不制造已成交状态。Paper 覆盖不会重新执行资金约束、策略或退出。

`paper_frames` 仅读原 daily.csv 的完成小时权益，保留原导出浮点精度；需要每个桶末 UTC 时点的显式风险 mark。其 entry 来自导出 unrealized 的反推，未实现盈亏仅具原导出精度。适用输入是未包含资金费的既有 Grid Paper 导出，不能再次覆盖已加资金费的研究 NAV。资金费落在非小时观察时刻时，须提供对应持仓和权威权益帧，不能把小时桶起点当成结算持仓。

## 对照与复现

冻结输入及逐期结果见 [MC-04](../validation/mc-04/REPORT.md)。同预算 1000 的 10 base 与 50 base 是敞口变化；同 10 base、预算 1000 与 200 是资本缓冲变化；仅修改 initial_rate 不会改变持仓盈亏或资金费，只改变保证金占用。

```bash
PYTHONPATH=src python -m pytest -q tests/research/test_perp_risk.py tests/research/test_grid_funding.py tests/research/test_monte_carlo.py
PYTHONPATH=src python examples/research/perp_risk_demo.py --output /new/output/directory
```

输出目录必须不存在；冻结合成参数在生成器内显式列出，同时导出 inputs.json、results.json 和逐场景 CSV。Monte Carlo 接入必须启用 save_full_paths，取资产价格而不是 fixed-weight 参考 NAV，并显式提供 UTC start/step。接入生成路径不改变 mark 或资金费的假设证据等级。
