# P8-S01 通用自定义策略接入与统一执行接口

日期：2026-11-23。独立分支：`feat/p8-s01-strategy-protocol`。

## main 能力核查

开发基线为 `22b24957e22703bd75dbd4fe2e71979c3e752c06`（#232）。
交付前同步 main 到 `a14254d47ee1276b6300ea32407fed41c1c0daab`（#233）；
新增内容仅为 P8-C08 验证文档，没有交易代码变化。

| 组件 | main 的真实能力 | 本次接入方式 |
|---|---|---|
| `strategies.custom.StrategyProtocol` | 本地可信单文件、构造参数检查、源码哈希、日线 `generate_targets`；当前进程导入，单标的多头、下一开盘执行 | 保留旧研究接口；新增决策协议，不把日线假设套入 Demo |
| `trading.strategy.Strategy` | `describe/signal`；已收盘 K 线、目标权重、固定 DualMA 计算，缺少通用订单与显式状态接口 | `SignalAdapter` 把目标决策送入原运行时；DualMAAdapter 复用原计算 |
| `GridExecution` | 已有 observe/confirm/complete_bar、订单提议、撤单、部分成交聚合、反向单、未知状态与原生恢复 | GridAdapter 重放原生命令并返回完整意图，不转换成 BUY/SELL/HOLD |
| `DemoStrategyRuntime` | 专用 Demo Gateway、因果目标信号、下一有效报价、预算/过滤器、账户对账、账本与暂停/停止恢复；当前 NVDAUSDT 范围 | 保留；目标策略复用它，订单策略使用薄控制器适配器 |
| `Research Workflow` | 日线 custom_daily/account_batch、配置/输入身份、生命周期回执、不可变报告和 replay/verify | 新增 `run_protocol_research` API，复用 PaperBroker/TradingController/ExperimentTracker |
| `ExperimentTracker` | 不可变 JSON 回执、定义与运行校验、可重建 SQLite 索引、工件哈希 | 保存策略绑定、开始/终态、样本/状态摘要及可检查的完整账本导出 |
| `DemoExecutionGateway` | 已有离线注入与网络 Demo 权限分离、执行/规则/账户校验、UNKNOWN 与 GET 恢复、统一经济记账 | 直接复用；策略不能获得 Gateway、凭据或提交函数 |
| `demo_strategy_supervisor` | 原先直接构造 DualMAStrategy；Grid 另有 GridSupervisor/GridConsumer/Bridge | 新增 `--strategy`，按能力选择执行适配器；原默认部署与 Grid 链路保留 |

## 实现

统一协议在 `strategies/protocol.py`：StrategySpec 声明身份、版本、状态版本、
配置绑定、标的、周期、数据类型、历史长度和决策形态；Decision 分别承载目标权重
或原生订单/撤单，以及显式 JSON 恢复状态。未声明的数据需求和不匹配的输出被拒绝。

`LoadedStrategy` 支持显式 Python 文件、JSON 清单和带相对导入的版本化目录包。
代码摘要覆盖所选择的所有 Python 源码，另绑定 FactorLab 运行时摘要；包依赖变化、
版本变化、配置变化或运行时代码变化不能继续使用原部署。用户代码不在执行进程导入。

DecisionJournal 复用 PaperStore 元数据事务，保存部署绑定、原始观察/账户投影、
决策、状态版本、校验和和每次恢复记录。记录分片遵守原有单事件大小限制。
同一观察重放使用原决策，不重复调用策略；变化的观察不能复用同一身份。
订单派单前保存 outbox；未确认的派单中断阻止自动重发。只有原控制器/会话可以提交
和撤销订单，Gateway、账户风控、执行预算、持久授权与经济账本继续生效。

数据适配按声明与现有 feed 能力取交集。沿用 DemoMarketFeed 的 SYMBOL/INTERVALS，
不新建交易品种和周期白名单。当前 Gateway 的 NVDAUSDT/单向 LIMIT 限制仍有效。
研究可选择现有控制器支持的标的和周期；研究成交由显式 quote/fill 输入驱动。
研究与 Demo 使用相同加载器、策略 decide 与状态，允许不同执行适配器。

示例覆盖 CloseMomentum 目标策略、LimitOnce 订单策略、DualMA 与 Grid 原生适配，
以及 CloseMomentum 的版本包。用法见 `examples/protocol_strategy/README.md`。

## 隔离边界

所有自定义代码的导入、构造、describe 和 decide 在独立的干净 Python 子进程进行。
子进程使用隔离解释器模式、临时目录、关闭继承句柄和环境白名单；不继承 Demo/API
密钥环境，不传递凭据、客户端、账户控制器或持久授权对象。
审计守卫阻止范围外文件读写、socket、进程启动、注册表及 ctypes IO，工作进程超时
或输出不合法时不保存新决策。测试使用哨兵凭据，确认 PID 不同、环境无密钥、文件
读取/网络/子进程被拒绝。全部测试仅使用离线 REST 注入，无真实 Demo 下单。

接受范围是可信 Python。该守卫并非隔离恶意原生代码的 OS 安全沙箱，不声称支持
不可信插件。每次启动干净进程有开销；Grid 的通用适配采用原生命令重放，适合有界
回归和 Demo，不替代已有 Grid 增量恢复优化。IPC 上限 4 MiB，账本容量仍由原存储
控制。需要更大容量时使用既有显式容量选项，达到上限则停止，不丢弃身份历史。

## 离线验证

运行环境：Windows、Python 3.13.9、pandas 2.3.3。

新增协议回归：**19 passed**，见 `protocol-tests.txt` 与 `protocol-tests.xml`。
原有相邻回归：**214 passed**，见 `existing-tests.txt`。
共 **233** 项通过；`compileall` 与 `git diff --check` 通过。

新增断言包括：

- 自定义目标策略研究成交、Tracker 回执、Demo 成交、重启去重与状态/身份绑定。
- DualMA 决策和原计算一致，并完成研究与 Demo 下单。
- Grid 研究买卖闭环；Demo 部分成交、重启、余量聚合、反向限价单、撤单和超时恢复。
- 任意订单策略经能力工厂接入，预算变化拒绝恢复；交易 kill switch 仍由控制器阻止。
- outbox 已有意图但无账户确认时禁止重发；重复报价不重复产生经济事件。
- 订单事件的策略状态与待执行意图在同一事务提交；注入中断后两者一起回滚。
- 文件/版本包加载、包内相对导入、源码摘要、参数与能力不匹配、无效决策回滚、超时。
- 无凭据工作进程以及文件、网络、子进程访问拦截。
- Supervisor 的自定义策略 dry-run/execute 两种模式，离线注入验证现有持久授权和预算。

复现（仓库根目录，使用已安装依赖的 Python 3.13）：

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m pytest tests/integrations/test_unified_strategy.py -q
python -m pytest tests/integrations/test_binance_demo_strategy.py tests/integrations/test_binance_demo_strategy_session.py tests/integrations/test_demo_strategy_supervisor.py tests/integrations/test_binance_demo_grid_bridge.py tests/research/test_grid_execution.py tests/research/test_custom_strategy.py tests/research/test_research_workflow.py tests/trading/test_strategy_pipeline.py -q
git diff --check
```

本任务交付为独立 PR，不自动合并；没有变更生产交易、账户记账或现有 Demo 授权流程。
