# P5-E01：现金分红与历史收益核算

## 核算接口与修复

`run_target_positions(..., dividend_plan=plan)` 继续复用 `DividendExecutionLedger` 和 PositionBook。新增返回表 `return_accounting`，由 `dividend_return_accounting(nav, payments, recognitions, initial_cash=...)` 从执行结果派生，不重放成交、不创建第二个账户。研究入口可以直接消费该表；ResearchEvaluation 原有组合评价不改变口径。

登记日开盘成交后的收盘持仓确定权益；除息日确认应收；仅日期级支付证据在当日开盘订单之后入账，下一观察开盘可用。非交易日支付保留实际日期，并于下一观察开盘前入账。未知税率支付进入受限现金，不进入可交易现金。调整价执行明确拒绝。

离线 `cash_dividend_ledger` 的缺口是税后状态延迟到支付日才判未知，且缺少税后应收及净值。现在从除息日开始将未知税率反映为不可计算，新增 `receivable_net` 和 `equity_net`，已知税率按净应收计价；保留原有税前列及离线对账用途，不把离线税前现金作为执行资金授权。

## 收益语义

所有收益以初始资本为分母，无外部申赎。`price_pnl` 为税前权益减初始资本减累计已确认税前股利，包含交易费用；`price_return` 为其初始资本收益率。这里是实际策略路径的价格损益，不是证券买入持有价格涨幅。

`total_return_gross` 使用 `equity_before_tax_charges`，在同一真实执行路径上加回已计税额，不是假设免税后重新投资的另一策略。`total_return_net` 使用 `equity_net`；未知税率权益行输出空值及 `NOT_COMPUTABLE_TAX_UNKNOWN`。不填零税率，也不以税前资产冒充精确税后财富。

`dividend_income_gross` 是累计确认收入；`dividend_received_gross` 是累计已支付税前金额；`dividend_received_available` 是累计进入可用现金的净额。未支付权益见 `receivable_gross`，已确认净应收见 `recognized_receivables`，支付但税额未知的资产见 `restricted_cash`。支付是资产转移，不再次确认收益。

三个残差应为零（浮点执行按数值容差核对）：

- 可用现金 − 初始资金 − 累计交易现金流 − 累计股利可用现金流。
- 累计确认税前股利 − 累计已支付税前股利 − 税前应收。
- 税前权益 − 可用现金 − 受限现金 − 市值 − 已确认应收 − 累计税额。

## 手算参考

初始10000元，100元买100股，登记日持仓100股，每股股利2元；除息后原价98元，支付日在下一周一：

|阶段|市值|可用现金（10%税）|净应收|税前总收益|税后总收益|
|---|---:|---:|---:|---:|---:|
|登记日|10000|0|0|0|0|
|除息日|9800|0|180|0|-0.2%|
|支付日收盘|9800|180|0|0|-0.2%|

价格损益为−200元，价格收益−2%；税前股利收入200元，税额20元。支付日开盘仍不能使用当天仅日期级到账款。税率未知时，除息日税后收益不可计算；支付后200元为受限现金。若支付日在观察期以后，200元仍为应收而非现金。

## 事件与历史证据边界

重复事件和同身份多修订必须先显式解决；完整范围声明还需绑定事件修订清单及证据，不仅检查 COMPLETE 字样。送股、转增、配股、拆并股、复合事件和代码变更沿用 UNSUPPORTED 拒绝。未来股数变更仍需独立定义成本基础、股数、交易限制和现金流合同。

复用 [P3-D03](../../data/P3_D03_ACTION_EVIDENCE.md)、[P3-D05](../p3-d05/REPORT.md)、[P4-D01](../p4-d01/REPORT.md)。601857.SH 2017—2018 四次已观察现金事件的条款不等于全部价格影响事件的完整集合；正文级类别覆盖、历史首次公开/分发及修订撤销链仍未闭合，2017官方PDF证据亦未升级。本轮不以合成证据补真实覆盖，不运行正式真实总收益认证。

TODO：真实账户税务（含持股期补扣）与到账证据合同闭合后再扩展现有税务接口；完整事件覆盖及历史修订证明闭合前保持真实历史执行阻塞。金融门禁、PIT/HFQ认证、DataHub单位、信号、训练和CI不变。

## 定向复现

```sh
python -I -X utf8 -c "import sys; sys.path[:0]=['src','.']; import pytest; sys.exit(pytest.main(['-q','-p','no:cacheprovider','tests/research/test_dividend_execution.py','tests/research/test_corporate_action_ledger.py','tests/research/test_execution_backtest_phase3.py','tests/research/test_portfolio_execution.py']))"
```

分红与离线账本定向结果：Python 3.12.7，59 passed；既有 `test_execution_backtest_phase3.py` 和 `test_portfolio_execution.py` 执行回归29 passed，共88 passed。Python 3.14 的首次启动因缺 pytest 未运行测试；未安装或修改依赖。

新增测试在原执行夹具和离线对账工具上验证已知/未知税率除息日净值、收益分解、三项残差、无分红基准和期末未付应收；原测试覆盖登记日权益变动、除息日买入、支付资金时点、重复修订、覆盖不足与不支持事件拒绝。
