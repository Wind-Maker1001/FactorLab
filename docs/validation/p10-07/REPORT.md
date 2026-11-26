# P10-07 动态资金分配与多策略资本管理

基线为 `388a8ee0f28e4b90ee7dbb20403a29c7fe085898`，包含 #260 和 #261。实现分支为 `feat/p10-07-dynamic-capital`。

## 资金与执行

资本操作在现有 `PortfolioStore` 的 `BEGIN IMMEDIATE` 事务里重新对账原订单、计算可释放金额、写入分配差额并追加事件。分配表保留初始投入，追加表投影当前预算和累计现金流。账户 ID 与事件 ID 共同标识请求，原请求重放返回原回执，内容冲突拒绝执行。转移的两个分配变更和事件同时提交。SQLite 进程中断测试在第一侧写入后退出，重开数据库确认没有半笔转移，原事件重试可以完整提交。

共享快照提供官方权益、现金及可用金额，另外列出未分配预算、公共现金归属和可分配余额。Alpaca 的官方可用字段取券商 buying power，资本变更同时受现金和订单预留约束。Binance 从同一次对账读取钱包、可用余额、资金费及持仓保证金。撤回扣除已归属保证金、未结订单预留、退出费估计和未实现亏损，并保留既有全名义额度约束。未实现盈利不计入可撤现金。原有账户和标的风险上限、订单 ID、实例 ID、分配 ID 继续适用。

退出只结算持仓清零、订单已经确认终态的实例。回执分别记录释放的预算与扫回的现金，两者可能因已实现损益而不同。退出后，历史资金费、公司行为与订单仍沿用原归属，运行端按持久化退出状态跳过该实例。

## 运行中操作

`python -m factorlab.trading.capital_cli` 接收现有模拟账户的组合数据库路径、账户 ID 和命令 ID。CLI 将请求写入同一个 SQLite 数据库，由 Binance Demo 或 Alpaca Paper 的既有运行进程处理。命令 ID 标识提交与结果，事件 ID 标识最终记账。处理后用原命令 ID 查询结果。运行进程也可以直接使用 `PortfolioExecutor.capital` 或账户执行主机的 `capital` 接口。

```text
python -m factorlab.trading.capital_cli portfolio.sqlite3 ACCOUNT move-001 change transfer-001 100 --source A --destination B
python -m factorlab.trading.capital_cli portfolio.sqlite3 ACCOUNT move-001 result
python -m factorlab.trading.capital_cli portfolio.sqlite3 ACCOUNT view-001 snapshot
python -m factorlab.trading.capital_cli portfolio.sqlite3 ACCOUNT pnl-001 performance A
python -m factorlab.trading.capital_cli portfolio.sqlite3 ACCOUNT events-001 history
```

省略来源表示从公共池分配，省略去向表示撤回到公共池。`EXACT` 额度不足时返回 `INSUFFICIENT_CAPITAL`、当时可执行额和未完成金额。`STAGED` 先结算闲置现金，未完成部分保存在原请求下。需要卖出时，通过来源策略的现有减仓路径提交原身份订单，经确认成交后用独立的命令 ID 与步骤 ID 续办，可附上原减仓计划 ID 以校验归属和成交。后续部分成交可用新的步骤 ID 继续处理。撤单阶段保留资金，直到官方回执确认。`release` 在原实例完全退出后归还预算与现金。

```text
python -m factorlab.trading.capital_cli portfolio.sqlite3 ACCOUNT pull-001 change withdrawal-001 100 --source A --mode STAGED
python -m factorlab.trading.capital_cli portfolio.sqlite3 ACCOUNT pull-002 advance withdrawal-001 fill-001 --reduction-plan PLAN
python -m factorlab.trading.capital_cli portfolio.sqlite3 ACCOUNT close-001 release retirement-001 A
```

运行中空闲轮次只查询待处理命令的索引。资本操作沿用现有对账和 Decimal 账本，无额外执行进程；只有查询绩效或历史时返回相应历史。长账本上的对账时间仍随原执行历史增长。

## 绩效

每笔已应用的资本流在同一事务保存流前权益。初始权益、流前估值、流后权益与当前估值串接为流量调整后的净值，绝对损益扣除净资本流入。TWR 连接各持有期收益，XIRR 使用实际日期和 365 天年化，并要求可识别的唯一解。未记录初始日期的老分配仍可计算 TWR 和绝对损益。费用、资金费、公司行为现金流和资本利用率在策略层返回。原固定资本报表保留初始投入字段。

## 验证

| 路径 | 离线证据 |
| --- | --- |
| Binance Demo | 签名 Demo HTTP 路径中的真实适配器接收离线订单、部分成交、资金费、保证金和撤单回执。重启后复用原事件与订单身份，官方钱包、分配现金及公共池残差为零。 |
| Alpaca Paper | 通过真实 Paper HTTP 适配器，用未终结的撤单应答验证预留不释放。以同标的双策略验证仅卖出来源策略的库存，跨两次成交和 SQLite 重启完成转移；验证分红仍归属成交持有人。 |
| SQLite | 两连接竞争同一公共预算，资金撤回与订单预留竞争，进程在转移第一侧写入后中断，未完成命令在已记账后重启重放。 |
| 历史与绩效 | 固定资本、旧数据库和原历史导入测试；独立收益测试使用已知的 21% TWR 和 10% XIRR。 |

独立代码审查覆盖全名义保证、资金费截止、旧资格回放、退出实例重启、命令异常处置与排队中的过期决策。可信 CI 使用仓库的 `FactorLab PR validation` 工作流对 PR 合并候选执行检查。
