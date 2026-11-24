# 通用策略示例

这些示例使用 `factorlab.strategies.protocol`，只返回决策和可恢复的 JSON 状态。
账户、持仓、费用、风控、下单权限、成交确认和订单账本由执行适配器持有。

## 加载

```python
from factorlab.strategies.loader import LoadedStrategy

# 显式可信 Python 文件，不在执行进程中导入用户模块。
strategy = LoadedStrategy('examples/protocol_strategy/strategy.py', trusted=True,
    class_name='CloseMomentum', version='1.0.0',
    parameters={'symbol': 'NVDAUSDT', 'interval_seconds': 60, 'weight': '.01'})

# JSON 清单绑定入口类、版本与配置。
strategy = LoadedStrategy('examples/protocol_strategy/strategy.json', trusted=True)

# 版本包支持相对导入，哈希覆盖整个包中的 Python 文件。
strategy = LoadedStrategy('examples/protocol_strategy/close_momentum_v1', trusted=True)
print(strategy.binding)
```

可将 `strategy.code_sha256` 写入清单的 `sha256` 字段来固定代码身份。
加载后会使用内存中的源码快照；改变原文件不会悄悄切换已运行策略。
不同版本、配置、包内依赖或 FactorLab 运行时代码会产生不同部署绑定。
恢复时不允许在原部署中替换绑定；使用新部署并保留原账本。

## 决策接口

`describe()` 返回 `StrategySpec`：身份、版本、状态版本、`target` 或 `orders`、
标的、周期秒数、数据类型及所需历史长度。
`decide(event, state)` 返回 `Decision`，其中 `state` 必须是 JSON 对象。
工作进程可能在每次调用后退出，因此持久状态必须显式返回。

`target_weight=None` 表示维持原目标；数值使用 `[0, 1]` 内的 Decimal。
订单策略返回完整的既有 `OrderIntent`，以及待撤销的客户端订单 ID。
订单 ID 必须以 `event['deployment_id'] + '-'` 开头，且符合现有 Demo 长度限制。
`limit_once.json` 展示自定义订单策略；`grid.json`、`dual_ma.json` 展示原有引擎适配。
Grid 的规则和初始权益是示例参数，使用前必须与实际账户和观察到的交易规则一致。
Gateway 仍独立校验实际规则，不接受策略自行放宽交易限制。

订单事件含显式账户投影，不向工作进程传递账户对象或 Gateway。
Grid 的观察、部分成交、撤单、完全成交和反向挂单均由原 `GridExecution` 决策。

## 研究

`factorlab.research.workflow.run_protocol_research` 接受已加载策略、既有
`TradingController(PaperBroker, PaperStore, ...)` 和因果事件迭代器。
事件为 `('closed_bar', ClosedBar)`、`('quote', Quote)` 或
`('fill', {'order_id': ..., 'quantity': Decimal(...), 'price': Decimal(...)})`。
研究驱动方推进 controller/broker 的时钟，成交仍由 PaperBroker 记账。
默认 `source_kind='SYNTHETIC'`；真实候选输入须显式声明 `REAL_CANDIDATE`。
返回运行适配器，并把开始/终态回执与不可变账本导出保存到 ExperimentTracker。
研究和 Demo 使用同一个 `LoadedStrategy.decide`；成交假设由各自适配器决定。

完整可运行的离线例子和断言见 `tests/integrations/test_unified_strategy.py`。

## Demo Supervisor

在现有安全配置和授权流程下选择策略：

```text
python -m factorlab.integrations.binance_futures_trading.demo_strategy_supervisor start --strategy examples/protocol_strategy/strategy.json --interval 1m --seconds 30
```

默认 GET-only dry-run。现有 `--execute` 及持久授权仍是执行前提。
不传 `--strategy` 保持已有 DualMA 部署的身份、配置与恢复行为。
当前 Demo 支持范围由现有 feed 和 Gateway 决定，声明其他标的不会扩展交易权限。
原 Grid Supervisor 和 DemoGridBridge 可继续使用，原部署无需迁移。

## 可信代码边界

选择 Python 策略即显式信任该代码。加载、构造和决策都在独立 Python 进程执行：
清空继承环境、关闭继承句柄、使用临时工作目录，不传递密钥或执行对象。
工作进程的审计守卫禁止外部文件读写、socket、子进程和 ctypes 调用；
只允许读取已暂存源码和运行库。超时或无效输出不提交决策。
这是可信代码的进程隔离和 IO 防误用边界，不是面向恶意原生扩展的 OS 沙箱。
不可信策略应先在操作系统隔离环境审查，不属于本接口接受的代码范围。

当前实现适合有界研究和 Demo 策略：每次决策启动干净进程；Grid 适配状态使用
原生命令重放，保留现有账本容量限制和 4 MiB IPC 上限。长期高频 Grid 仍可使用
原有增量恢复链路。订单派单意图已持久化但账户未确认的中断会阻止重发，需先对账。
