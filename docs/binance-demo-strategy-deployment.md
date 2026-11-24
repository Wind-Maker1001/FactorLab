# Demo 策略发布接口

沿用原 Execute、state-root、凭据及授权。控制器没有初始化账户、恢复旧账本或扩大预算功能。

```powershell
python tools/deploy_demo_strategy.py --host 85.211.193.242 --user azureuser --key D:/Environments/factorlab-demo-01_key.pem --commit <完整SHA> --repo .
```

构建固定 Git 归档；上传校验后安装版本目录，短暂重启原服务。失败只恢复旧入口，交易账本保持当前位置。主机 release 下保存 deployment-result.json。

## 独立策略与 S01 通用协议适配

策略包同时支持两类接口协议（保留 `legacy-grid` 回退路径）：

1. **兼容接口 `factorlab.demo.evaluate/v1`**：`module:function(context, state)`，返回 `{"state": {}, "intents": []}`。`context.event` 为 `probe` / `closed_bar`，带配置及只读行情/账户投影。`probe` 不得生成意图。既有 HOLD 验收包与旧包完全兼容。
2. **S01 通用决策协议 `FACTORLAB_DECISION_V1`**：`module:ClassName` 实现 `UnifiedStrategy`（`describe() -> StrategySpec`、`decide(event, state) -> Decision`），经 S01 干净隔离工作进程（`LoadedStrategy.from_strategy_package`、`python -I -B`、`sys.addaudithook`、白名单环境、无凭据传入）加载。
   - 清单必填映射：`strategy_id`、`strategy_version`、`state_version`、`code_commit`、`entrypoint`、`files`（逐文件 SHA256）、`dependencies`（精确 `name==version`）、`configuration`、`evidence`、`migration`（`wait_flat` / `reduce_only`），以及可选 `execution_mode`（`target` / `orders`）、`interval_seconds`（默认 60）与 `package`（目录包相对导入布尔标志）。
   - `target` 决策经 `DecisionJournal + SignalAdapter + DemoStrategyRuntime` 执行；`orders` 决策经 `DecisionJournal + OrderStrategyRuntime + ControllerExecution` 执行，完整保留 `NEW / PARTIALLY_FILLED / FILLED / CANCELED / UNKNOWN`、`DEFERRED` Outbox、撤单尝试与确认终态分离及 HTTP 200 未确认回执的 GET 恢复。
   - **安全边界声明**：可信 Python 隔离工作进程用于防止误访问凭据、外部文件、网络与子进程，不等价于针对恶意代码的 OS 安全沙箱。

```python
def evaluate(context, state):
    return {"state": state, "intents": []}
```

manifest schema v1 必须有 `environment=BINANCE_USDM_DEMO`、`symbol=NVDAUSDT`、`code_commit`、`entrypoint`、`files`、`dependencies`、`configuration`、`state_version`、`migration` 和 `evidence`。`files` 是显式 Python 清单，`build` 替换为逐文件 hash 并生成 `package_id`。`dependencies` 只支持已安装的精确 `name==version`。迁移为 `wait_flat`，或显式 `reduce_only` 加 `reduction: {"explicitly_allowed": true, "max_attempts": 2}`，仍受原预算/过滤器约束。

```sh
python -m factorlab.integrations.binance_futures_trading.azure_strategy_deploy build \
  --source /trusted/source --manifest /trusted/manifest.json --archive /trusted/package.zip
```

可信生产者写入 `/var/lib/factorlab-demo/champions`，先写完整包、最后原子发布 `*.candidate.json`：

```json
{
  "archive": "package.zip",
  "candidate": {
    "schema_version": 1,
    "decision": "PROMOTE",
    "qualified": true,
    "strictly_better": true,
    "selection_id": "<unique result>",
    "expected_active": "<comparison baseline ID or legacy-grid>",
    "package_id": "<built package ID>",
    "evidence": {"selection_sha256": "<same evidence as package>"}
  }
}
```

也可附 inbox 下的 source 相对目录和 manifest，由 timer 自动构建。布尔资格字段来自可信研究生产者，不能替代研究证据。S02 原生结果使用 selection 字段；没有晋升不需要 archive，有晋升则检查 Demo 执行口径、代码、参数及 OOS/tracker 绑定。当前 A 股结果不能自动部署到 NVDA Demo。S01 类协议仍待集成，不能简单重命名包来替代接口适配。

## 状态与恢复

```sh
python -m factorlab.integrations.binance_futures_trading.azure_strategy_deploy status \
  --state-root /var/lib/factorlab-demo/execute-state
python -m factorlab.integrations.binance_futures_trading.azure_strategy_deploy accept \
  --state-root /var/lib/factorlab-demo/execute-state --archive /trusted/package.zip \
  --candidate /trusted/candidate.json --mode restart
```

默认 inbox 模式 hot；restart 请求到 READY_RESTART 后，原 deploy timer 根据 boot PID 请求一次受控重启，新进程 GET 恢复后激活。策略异常自动回退；rollback 命令也可请求前一稳定版本。未知订单、未退出持仓或失败回退继续安全等待，不能删除状态库或重设授权绕过。

`tools/validate_demo_strategy_release.py` 为主机上的 HOLD 包验收工具，记录 restart/hot/异常回退并恢复原 Grid；软件验收不代表收益资格。详见 [REPORT](validation/p8-s03/REPORT.md)。
