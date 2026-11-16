# MC-04 验证报告

**NO LIVE MONEY — 独立风险覆盖层估计，不是完整 Paper 回放或交易所强平成交。**

## 合成经济算术

冻结规则：1 contract = 1 base，初始价 100 USDT，观察时间为 2026-01-01 00:00、08:00、16:00 UTC。一般初始保证金率 10%，维持率 5%，档位 [0,1000000)，扣减为零；无杠杆对照初始率 100%。退出手续费率 0.04%、额外退出成本率 0.1%，只估计列示，不扣入权益。合约和规则完全合成，不是 Binance 参数。

|场景|预算|base 数量|终点 mark|累计资金费|终点账户权益|风险权益|初始保证金|维持保证金|可用保证金|
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
|unlevered|1000|10|90|0|900|900|900|45|0|
|positive_funding|1000|10|100|-20|980|980|100|50|880|
|negative_funding|1000|10|100|20|1020|1020|100|50|920|
|mark_crash|1000|50|79|-82|-132|-132|395|197.5|-527|
|isolated_crash|1000|10|80|0|800|-100|80|40|-180|
|same_exposure_small_budget|200|10|80|0|0|0|80|40|-80|
|short_positive|1000|-10|100|20|1020|1020|100|50|920|

资金费场景每期独立输入 ±1%，两次结算。mark_crash 价格路径 100→85→79，各次费用 -42.5、-39.5；第二期负权益没有被改成零。逐仓隔离初始抵押金 100，外部余额 900 不用于补仓；80 mark 时账户仍有 800，但隔离风险权益已为 -100。

mark_crash 在 85 时权益 207.5、维持保证金 212.5，已经触发情景强平阈值及保证金不足；79 时继续记录固定敞口的反事实风险，而不是假设已经成交退出。isolated_crash 在 90 时隔离风险权益为零，触发阈值；small_budget 在 80 时触发。负风险权益时使用率为 null，不输出误导性负比例。

同预算不同敞口：1000 预算的 10 base 初始名义 1000（1×），50 base 初始名义 5000（5×）。同敞口不同预算：10 base 对预算 1000/200 分别为 1×/5×，价格盈亏相同但缓冲不同。unlevered 与其余场景的初始保证金规则不同，不能把差异全归因于资金费。

逐期权益、资金费、占用、比例及风险事件见 [frozen/results.json](frozen/results.json) 和同目录逐场景 CSV；规则与原始帧见 [frozen/inputs.json](frozen/inputs.json)。金额使用 Decimal，JSON 序列化为字符串。

## 权益来源与证据

合成结果为预算 + 未实现盈亏 + 累计结算资金费，既无原 Paper NAV，也不虚构成交费用。单独的 Paper bridge 定向测试使用已含 2 USDT 成本的原权益 1018、数量 10、参考 close 100、风险 mark 90，只加 -100 的估值调整，得到 918；不再扣 2，也不再加全部未实现盈亏。读取前后原 daily.csv 内容相同。

[证据矩阵](rule-evidence.json) 将现有当前档位/账户字段能力与历史强平所缺证据分开。所有合成模型为 SCENARIO_ASSUMPTION_ONLY，权威强平判断为 BLOCKED_RULE_EVIDENCE。当前接口不接受将其升级为已验证引擎的开关。公开资金费事件标签也不代表真实账户确认到账。

## 定向验证与复现

定向测试覆盖无杠杆基准、正负资金费、多空及零仓、多期结算、独立结算 mark、合约单位换算、快速下跌、负权益、阈值相等边界、档位切换、逐仓隔离、全仓组合共享、完整组合声明拒绝、重复事件和无精确观察时间拒绝、规则版本与非有限数拒绝、动态逐仓拒绝、Paper NAV 桥接、固定输入重复性以及小型 MonteCarloRunner 资产价格接入。

离线入口在 socket.connect/create_connection 禁止的测试环境运行全部七个场景。新增模块没有交易适配器、凭据或网络客户端，也不调用 POST/DELETE。费用与阈值纯计算不生成 OrderEvent。原交易、授权、额度、Grid 执行和生产对账文件均不需要修改。

```bash
PYTHONPATH=src python -m pytest -q tests/research/test_perp_risk.py tests/research/test_grid_funding.py tests/research/test_monte_carlo.py tests/integrations/test_binance_futures_trading.py
PYTHONPATH=src python examples/research/perp_risk_demo.py --output /new/output/directory
```

首次联合运行 79 passed、1 failed：既有 CPU-only 子进程未继承源码路径；显式设置 PYTHONPATH 后 80 passed，并成功生成冻结证据。之后增加单位换算、独立结算 mark 和逐仓固定敞口拒绝测试，并联合既有永续接口测试完成最终验证：**132 passed in 11.33s**。
