# P7-M19-F06 Demo 初始化与执行输入验收

对象为 Binance USD-M Demo `NVDAUSDT`。开发基线是 `cbf4891d5cbcd6f77ffd42dfc127d9a4c46a166d`，即 [PR #218](https://github.com/Wind-Maker1001/FactorLab/pull/218) 合并后的 `origin/main`。

## 启动调用链

基线中新建 Grid 时，`open_grid_session` 先构建 `DemoSession`。构造器使用临时适配器校准时钟，读取并校验账户绑定、钱包初值，随后用 `observe_demo_rules` 读取 `exchangeInfo` 和 `leverageBracket`。`grid_factory.create` 接着通过 `session.recover → DemoExecutionGateway.recover → TradingController.reconcile → FuturesAdapter.reconcile` 再读取整组账户，才确认本地订单记录与交易所挂单一致并导入钱包和仓位。自动续期路径还会为 Campaign 单独触发一次启动对账。Worker 的每轮 `GridConsumer.before_poll → DemoGridBridge.recover` 是另一阶段，负责当前账户恢复与盘口读取，然后调用 `DemoMarketFeed.poll`。

一组账户读取包含 v3 账户和仓位风险、持仓模式、账户级挂单、合约杠杆与保证金设置、交易限制状态。只有 v3 缺少 `canTrade` 时才读 v2 账户。凭证绑定使用已保存的 Demo 环境及凭证账户标识。单向持仓、钱包和可用金、仓位与风险数量、杠杆及保证金配置参与启动事实校验。交易权限和量化交易锁仍由既有订单准入判定。

F06 将规则和时钟校准放在账户组之前。钱包初值来自持久化适配器读取的 `AccountFacts`。当协议账本和 Controller 都没有订单时，启动调用把同一组事实交给现有的 `TradingController.reconcile`。适配器在确认身份、读集归属、账本与挂单一致后，才提交对账状态。账户组从首个读取开始计时，沿用五秒窗口，并以单调时钟限制实际耗时，避免校准变化掩盖过期。启动借用作用域随对账调用结束。出现历史或待恢复订单时，进入原有的逐单查询、成交和手续费重放及最终对账。

`open_grid_session` 负责新部署与自动窗口需要的启动对账，Grid 工厂使用该结果构造部署。常规 Worker 恢复仍通过 `session.recover`。实际提交前仍重新读取账户与盘口，并在分派时检查五秒新鲜度。复用路径位于会话对外暴露和写授权使用之前。现有预算、STOP/HALTED、未知结果防重发、LIMIT 价格与数量及 SELL `reduceOnly` 继续由原有实现处理。

## 代码与测试范围

| 文件 | 变更 |
| --- | --- |
| `account.py`、`adapter.py` | 记录读集的单调时钟起点，在同一五秒窗口内完成组装及启动对账。 |
| `execution.py`、`demo_session.py` | 先读取规则，再获取钱包与账户事实；将启动读集限于空账本的一次 Controller 对账。 |
| `demo_runtime.py`、`demo_grid_supervisor.py` | 由会话入口完成新部署和自动窗口的启动对账，Grid 工厂读取结果。 |
| `tools/measure_demo_get_recovery.py` | 在 F04 测量入口增加启动完成事件和每轮账户读集年龄。 |
| `test_demo_get_recovery.py`、`test_demo_get_recovery_window.py` | 检验 4.999 秒与 5.001 秒、钱包种子在 Controller 初始化期间过期、启动只读一组，以及真实 `Feed.poll` 的盘口、预热 Bar 和新鲜闭合 Bar。 |

历史证据沿用 [F01](../p7-m19-f01/REPORT.md) 的冻结账户与预算、[F02](../p7-m19-f02/REPORT.md) 的 Bar 时钟、[F03](../p7-m19-f03/REPORT.md) 的订单/成交/费用证据链，以及 [F05](../p7-m19-f05/REPORT.md) 的独立 Execute 验收范围。

## 初始化请求比较

[F04 官方记录](../p7-m19-f04/REPORT.md) 在启动阶段发出 16 次 GET，耗时 16.625486 秒。首组账户读取成功，第二组从 9.054824 到 16.624425 秒，共约 7.570 秒，其中 v2 账户 GET 耗时 4.251803 秒。第二组在 `apiTradingStatus` 之前触发 `STALE_ACCOUNT`，当时尚未启动行情采集。

按同一成功启动路径计数，空账本可省 6 次账户 GET，缺少 v3 `canTrade` 时省 7 次。常规初始校准 1 次、规则 2 次、账户 6 或 7 次、Grid 初始盘口 1 次，F06 合计 10 或 11 次，F04 代码的相同成功路径为 16 或 18 次。实际遇到时钟重试、传输失败或提前停止时，以窗口观测计数为准。

## 离线回归

Windows 本机 Python 3.12.7 共通过 331 项定向测试。项目支持 Python 3.13 和 3.14，本机 3.14 环境的依赖下载在本轮验证时尚未完成。

- 初始化、五秒边界、签名 GET 及 GET-only Preview 共 72 项。覆盖 `CLOCK_SKEW` 校准、单端点有限重试、传输超时、真实 `Feed.poll` 路径、采集线程停止、SQLite 关闭和进程锁释放。测试在受控分钟边界前后验证预热与新鲜 Bar 的资格差异。
- Grid/Execute、Campaign、网络、观测、ACK/partial/filled 和费用恢复共 198 项。包括独立进程重开原协议与 Controller 数据库、UNKNOWN 后恢复、重复确认防重复记账、STOP/HALTED、累计预算及真实参数传递。两组均将未处理的线程异常警告视为测试错误。
- M14 对账 11 项，通用 Futures 适配器 50 项。后者在本机 pytest 7.4.4 上需使用 `-p no:nose`，避免旧插件把测试辅助函数 `setup` 当成模块钩子。首次运行的 50 个 setup 错误均在测试体之前发生。

## 官方 Demo GET-only 窗口

2026-11-21 22:13:42.037 UTC+8 使用 Windows Credential Manager 中已有的 Binance USD-M Demo 绑定。进程在预检后持有原执行状态目录及已有 Preview 目录的进程锁，复用 F04 观测入口和独立 Preview 本地状态。155 秒 Worker 窗口从启动后开始，总观测耗时 **163.282 秒**。共准入并完成 **130 GET、0 POST、0 DELETE**，130 个 HTTP 响应均为 200。观察在本机 Python 3.12.7 上完成。原始脱敏证据由本机保留。

| 阶段 | F04 | F06 |
| --- | --- | --- |
| 启动结束 | 16.625 秒、16 GET，`STALE_ACCOUNT` | 8.141 秒、11 GET，账户已对账、UNKNOWN 为 0 |
| 账户读集 | 第二组约 7.570 秒，v2 单次约 4.252 秒 | 唯一启动组 4.375 秒，v2 单次约 0.890 秒 |
| 首次 `Feed.poll` | 未到达 | 14.125 秒，交付 1 根预热 Bar 和 1 次报价 |
| 首次新鲜闭合 Bar | 未到达 | 22.625 秒，交付 1 根新鲜 Bar 和 1 次报价，`live_bar=true` |

F06 从规则请求到完成规则用时约 1.953 秒，4.375 秒账户组后进入本地对账，随后用 0.620 秒获取初始盘口。启动报告在初始盘口和部署落盘之后产生，账户组当时已 5.078 秒。周期恢复重新读取账户。采集器在窗口内取得 52 份交易所盘口，Consumer 收到 3 次报价和 2 根闭合 Bar。新鲜 Bar 的交易所闭合时间是 22:14:00 UTC+8，Kline 响应在窗口第 22.500 秒完成。

活跃轮次的账户读集最接近上限的一轮在约 5.000 秒收齐，后续盘口和行情传递使新鲜 Bar 的报告时点达到 **6.453 秒**账户年龄。10 轮中 3 轮 `RUNNING`，6 轮因账户读集耗时 **5.016–5.844 秒**暂停，最后 1 轮为 `STALE_OR_FUTURE_QUOTE`。六次账户过期发生在 `symbolConfig` 两次、v2 `account` 一次、`apiTradingStatus` 三次。读集仍由串行签名 GET 组成，`symbolConfig` 最慢 2.851 秒，个别 `positionRisk`、`positionSide/dual`、`openOrders` 和交易权限读取约 1.5–1.7 秒。各端点延迟相加即能越过五秒。响应均为 HTTP 200，记录中没有 `CLOCK_SKEW` 拒绝或重试。

**`M19_EXECUTION_INPUT_READY` 仍为 `BLOCKED`。** 真实账户启动、权威对账、盘口采集和新鲜闭合 Bar 交付均已观测到。窗口内没有观测到 Consumer 收到新鲜 Bar 时同时拥有五秒内的账户组，随后的周期恢复也未能持续满足时限。Preview 通道无写入授权。Execute 下单与分派仍分别要求当前账户和盘口证据。F05 的下单及生命周期验收仍待独立官方窗口。
