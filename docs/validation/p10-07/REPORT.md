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

PR 为 [#263](https://github.com/Wind-Maker1001/FactorLab/pull/263)，对应检查以 PR 当前提交记录为准。

本地 Windows Python 3.13 的两市场、资本迁移、命令与实例回归在两个逻辑 CPU 上通过 25 项，测试主进程峰值工作集为 136.2 MiB。进程中断回滚测试通过独立运行。Alpaca 实例重启与云运行回归通过 21 项。扩大回归通过 475 项，余下一项在源码修订时触发策略包源码身份变更，冻结源码后单独复测通过。资源数字来自离线定向运行。

## 待执行决定的资金保护

Binance 的 Demo outbox 仍负责记录已接受的冻结订单及原客户订单 ID。接受决定时持有组合账本的写锁，实际写入只发生在 Demo 账本。资本快照持有同一写锁，读取仍有效的冻结买单并按原 ID 匹配组合账本。正式计划事务提交之前使用冻结数量、限价和费率计算预留，提交之后由组合订单的剩余未成交数量、费用和状态计算预留。中断时，已经提交的 Demo 回执仍可重放。组合计划事务回滚后，冻结决定继续提供预留。两套 SQLite 账本各自提交。

资金或额度不足的未派发决定使用既有拒绝终态。拒绝原因和以原客户订单 ID 记录的本地 `REJECTED` 事件在 Demo 账本同一事务提交，策略可以通过现有事件流清理自己的订单状态。已经进入正式计划或协议账本的订单继续通过原身份恢复。部分成交、未确认撤单和 UNKNOWN 的剩余预留沿用正式订单规则。

Alpaca 的部署账本也可能比组合计划先提交决定。对未确认回执，账户主机在原计划正常恢复并确认前，将该策略可撤额度置零并保留退出请求需要的资金。目标型回执可能尚无可用于计算预留的限价。其他策略仍可按各自可用资金操作，资本追加和绩效查询也沿用原执行路径。

回归覆盖行情不可用时排队的撤资、跨策略转移、真实子进程在写入正式计划后且提交前退出、重开两个 SQLite 账本、冻结预留转换、部分成交后的撤单超时与 UNKNOWN，以及资金或额度不足时独立策略在同一调度轮次继续下单。Alpaca 回归从持久化未确认回执重启，经原决定恢复后验证可撤额度恢复。

本次定向及相邻回归在 Python 3.13 下通过 119 项，在 Python 3.14 下通过 9 项。

## 与 P10-11 的联合验证

将 PR #264 的主分支合并点 `59dbcadf86d583b717c63e3f365e9839d1fab1af` 合入原分支。Alpaca 的包加载继续注入参考数据提供者并隔离包故障，包资格使用原始资本，运行事件使用当前资金分配。部署切换仍按声明检查行情来源。Binance 宿主沿用外部参考行情及冻结模型版本合同，资本命令仍保护待执行决定和原客户订单。

Python 3.14 联合回归通过 26 项。联合用例从未确认决定重启，验证保护资金，在原计划恢复后撤资，再次重启后完成版本切换。新版本使用原分配 ID 和当前 30 USD，原执行计划、另一策略的 40 USD 及官方账户现金继续按原归属记录。套件同时覆盖外部参考行情、模型版本切换、Binance 部分成交及跨账本恢复。
