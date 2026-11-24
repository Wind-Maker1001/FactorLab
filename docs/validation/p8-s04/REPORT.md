# FactorLab P8-S04：S01/S03 通用策略协议与 Azure 云端执行联合验收报告

- 日期：2026-11-24（UTC+8 2026-11-25 00:40）
- 独立分支：`feat/p8-s04-unified-cloud-strategy`
- 基线 `origin/main`：`5e9f29abcbe8d577b7dabae761f0ae0b6365c54c`（含 PR #234、#235、#236、#237、#238）
- Azure 实机部署提交：`94b604bccdaa318ffbff52aa7e0350bac52cf54d`
- 验收分类：`SOFTWARE_CONFORMANCE_ONLY`（`research_qualification = false`，不构成 Champion 收益资格认证，不使用真实资金）

---

## 1. 原实机提交的分项判定

以下结果对应历史部署 `94b604bccdaa318ffbff52aa7e0350bac52cf54d`。PR #246 独立审查在 `69f728c5606f77e59e8d42dd9106407248644702` 确定性发现 P1/P2，当前合并阻断；修复与复验状态见第 7 节。

| 验收项 | 判定 | 实际证据依据 |
| --- | --- | --- |
| `ENGINEERING_ACCEPTED` | **PASS** | S01 `FACTORLAB_DECISION_V1` 与 S03 版本化部署协议已通过最小接线合入单一账户所有者，离线 220+ 用例全绿，Azure 同账本无损发布并完成端到端切换、下单、撤单、重启与自动回退验证 |
| `S01_S03_PROTOCOL_INTEGRATED` | **PASS** | `StrategyPackage` 原生支持 `FACTORLAB_DECISION_V1` 与兼容 `factorlab.demo.evaluate/v1`；通过 `LoadedStrategy.from_strategy_package` 隔离工作进程加载 `target` 与 `orders` 两类决策策略，走原有 `DecisionJournal` / `DemoStrategyRuntime` / `OrderStrategyRuntime` / `ControllerExecution` |
| `OFFICIAL_DEMO_CUSTOM_STRATEGY_VERIFIED` | **PARTIAL_VERIFIED** | Azure 官方 Demo 实机已验证：`legacy-grid → S01 LimitCycle` 激活、真实官方 POST 获订单确认（`order_id=195433064`）、合法 `DELETE` 撤单与签名 GET `CANCELED` 终态、预算与账本连续性。**未完成项**：实机真实成交（`FILLED`）与实机卖出平仓未在本次执行（详述见第 6 节：部署前既有冻结 100 POST 预算已自然消耗至 99/100，仅剩 1 笔 POST，旧验收工具选择了仅挂单再撤单的路径；该路径仍可能成交并耗尽退出预算，本次 P2 修复禁止沿用该风险假设） |
| `HOT_SWITCH_VERIFIED` | **PASS** | Azure 实机同 PID 热切换验证：`PID 343417` 完成 `legacy-grid → S01 A2`；重启后 `PID 343968` 完成 `A2 → A3`（含 `DRAINING` 撤销活跃挂单）、`A3 故障 → A2 自动回退`、`A2 → legacy-grid 恢复` |
| `RECOVERY_VERIFIED` | **PASS** | Azure 实机带着 S01 活跃挂单 `195433064` 执行 `systemctl restart`（`PID 343417 → 343968`），重启后签名 GET 恢复原策略包、原挂单、策略状态与 `100/100` 预算且 0 重复 POST、全部 SQLite inode 不变；实机验证 `STRATEGY_EXCEPTION:StrategyWorkerError` 自动回退且回退后不重复开仓；离线完整覆盖崩溃/超时/非法输出回退、HTTP 200 未确认撤单重试、挂单/残仓/UNKNOWN 切换阻断 |
| `CONTINUOUS_24H` | **PENDING** | 新版本 `94b604bc` 于 `2026-11-24T16:35:34Z` 上线，面向新版本的 24h 持续窗口起点刚确立（历史混合版本 C06 滚动 24h 已采集 1,367 样本、覆盖率 94.93%、存活率 99.63%） |
| `CONTINUOUS_72H` | **PENDING** | 72h 窗口未完成（历史混合版本 C06 滚动 72h 为 2,725/4,320 样本，`window_complete=false`） |
| `CONTINUOUS_7D` | **PENDING** | 7d 窗口未完成（历史混合版本 C06 滚动 7d 为 2,725/10,080 样本，`window_complete=false`） |

机器可读完整判定见 [acceptance-verdict.json](acceptance-verdict.json)。

---

## 2. 现有能力审计与最小接线定位

在 `origin/main`（`5e9f29a`）上逐文件审计前置 PR #234–#238 与 C06–C08 后确认：

| 模块 | 已有真实能力 | 缺失的最小接线 | 复用 / 兼容策略 |
| --- | --- | --- | --- |
| `strategies/protocol.py`、`loader.py`、`worker.py` | `StrategySpec`、`Decision`、干净子进程 `python -I -B` + `sys.addaudithook` 隔离加载（可信 Python 边界，非 OS 恶意代码沙箱） | 未支持从 S03 内容寻址目录读取 `StrategyPackage` 及校验 `dependencies` 精确版本 | 新增 `LoadedStrategy.from_strategy_package()` 与 `worker.py` 依赖校验，原 `LoadedStrategy(path, trusted=True)` 零改动 |
| `strategies/decision_runtime.py`、`strategy_runtime.py` | `DecisionJournal`、`SignalAdapter`、`OrderStrategyRuntime`、`ControllerExecution`（含 `DEFERRED` Outbox、撤单尝试与终态分离）、`DemoStrategyRuntime` | 多版本切换共用单一 `gateway.state.runtime` 槽会触发 `PersistenceConflict`；缺少复用已对账状态的 `sync_reconciled()` 与切换前未解决工作检查 `has_unresolved_work()` | 新增可选 `runtime_namespace`（默认 `None` 100% 保持原行为）、`sync_reconciled()`、`has_unresolved_work()`、`dispatch_cancellations()`、`clear_pending()` |
| `strategy_package.py`、`champion_release_adapter.py` | S03 内容寻址 zip、逐文件 SHA256、`package_id`、S02 `FACTORLAB_CHAMPION_SELECTION_V1` 适配 | 仅允许 `interface == 'factorlab.demo.evaluate/v1'`，缺少 S01 字段映射 | 新增 `DECISION_INTERFACE = 'FACTORLAB_DECISION_V1'` 与 `strategy_package_identity()`；旧 `evaluate/v1` 100% 保持兼容 |
| `strategy_deployment.py`、`demo_strategy_release.py`、`demo_runtime.py` | `DeploymentStore`、`DeploymentController`（含 PR #236 并发回滚原子保护）、`ReleaseConsumer`、`open_grid_session` | `ReleaseConsumer` 仅走 `evaluate/v1` 函数与简单 `BUY/SELL` 字典；`open_grid_session` 与 `write_guard` 未向 `DemoRuntime.reserve` 传 `cancel_recovered_at`（PR #237 HTTP 200 未确认撤单重试未接通）；未在 `reconcile()` 校验 S01 `pending_cancellations` | 在原 `ReleaseConsumer` 与 `DemoReleaseAdapter` 内按 `manifest['interface']` 分发，直接复用 `DemoStrategyRuntime` 与 `OrderStrategyRuntime`；`deployment_bindings` 表为每个 S01 包分配稳定、零碰撞的 `<=26` 字符 `deployment_id`；全面补齐 `cancel_recovered_at` |
| `demo_session.py`、`grid_execution.py`、`network.py` | 既有 `NVDAUSDT` 单标的会话、风控与只读对账 | Azure 共享账本（已被 `demo:static-shared-account:v1` 写入历史 `QQQUSDT`/`SPYUSDT` 标的）打开与定时历史对账时被单标的白名单拒绝 | 仅增加最小只读账本兼容（保留已存在共享标的清单、只读 `GET /fapi/v1/order` 放行三标的、共享账本下按单标的投影），**所有 POST/DELETE 写入权限仍严格锁死在 `NVDAUSDT`** |

---

## 3. S01/S03 协议字段映射

| 概念 | `FACTORLAB_DECISION_V1` 包清单字段 | S01 运行时 / 账本映射 | `evaluate/v1` 兼容行为 |
| --- | --- | --- | --- |
| `strategy_id` | `manifest.strategy_id`（必填非空） | 强校验等于 `StrategySpec.strategy_id` | 默认回退 `manifest.entrypoint` |
| `strategy_version` | `manifest.strategy_version`（必填非空） | 强校验等于 `StrategySpec.version` | 回退 `str(manifest.state_version)` |
| `package_id` | `manifest.package_id`（规范 JSON SHA256） | 绑定于 `LoadedStrategy.binding['package_id']` 与 `DeploymentStore.deployment_bindings` | 原逻辑不变 |
| 源码身份 | `code_commit`（40 hex）+ `files`（逐文件 SHA256） | 与 `LoadedStrategy.code_sha256`、`runtime_sha256` 双重绑定 | 原逻辑不变 |
| 配置身份 | `manifest.configuration` | 作为 `ClassName(**parameters)` 构造入参并记录 `configuration_sha256` | 作为 `context['configuration']` |
| 策略协议版本 | `interface: "FACTORLAB_DECISION_V1"` | `LoadedStrategy.binding['protocol']` | `interface: "factorlab.demo.evaluate/v1"` |
| 状态模式版本 | `state_version`（正整数或非空字符串） | 强校验 `str(state_version) == StrategySpec.state_version` | 正整数 |
| 执行模式 | `execution_mode`（`target` / `orders`，可选） | 若声明则强校验等于 `StrategySpec.decision_kind` | `evaluate_v1` |
| 标的与周期 | `symbol: "NVDAUSDT"`、`interval_seconds`（默认 60） | 校验命中 `StrategySpec.symbols`、`StrategySpec.intervals` 与当前 feed 周期 | `NVDAUSDT` + 1m |
| 研究证据身份 | `manifest.evidence`（必填非空对象） | `LoadedStrategy.binding['evidence_sha256']` 与 `candidate.evidence` 强绑定 | 原逻辑不变 |

安全边界明确说明：**可信 Python 隔离工作进程**（`python -I -B`、最小白名单环境变量、临时目录、`close_fds=True`、`sys.addaudithook` 限制外部文件/网络/子进程/ctypes/注册表）用于防止受控策略误接触凭据、账本或外网，**不等价于隔离恶意代码的 OS 沙箱**。

---

## 4. 离线验收证据（A–I）

全部测试使用白名单环境、假凭据与离线 HTTP 桩，未读取或输出任何真实 API 令牌。

| 回归集合 | 命令 / 产物 | 结果 | 覆盖范围 |
| --- | --- | --- | --- |
| P8-S04 联合验收套件 | `tests/integrations/test_unified_cloud_strategy_s04.py`（[offline-s04.xml](offline-s04.xml)） | **13 passed, 0 failed** | A：legacy `evaluate/v1` 兼容、清单校验、依赖版本校验、哨兵凭据隔离与 C06 `sanitize_sample`；B：S01 `DualMA` 版本包预热+新鲜 Bar 下单；C：S01 `CloseMomentum` 相对导入目录包完整 BUY/SELL 目标仓位闭环；D：S01 `GridAdapter` 与 `LimitCycle` 原生订单 BUY/部分成交/SELL/撤单；E：同 PID 多版本热切换链与预算连续；F：工作进程 `crash` / `timeout` / `invalid_output` 自动回退；G：SQLite 关闭重启恢复原订单身份/状态/预算；H：残仓 `wait_flat`、未完成撤单阻断切换；I：PR #236 P2 暂停 Bar 新鲜度、PR #237 HTTP 200 未确认 DELETE GET 对账后原 ID 重试与 `dispatch_retry_history` 归档、共享账户账本兼容与瞬时报价抖动保护 |
| S03 + S01 核心回归 | `test_strategy_deployment_s03.py` + `test_unified_strategy.py` | **61 passed, 0 failed** | PR #236 P1 并发回滚、P2 恢复新鲜度、S02 适配、S01 研究/Demo 协议 |
| PR #237 审查关键回归 | `test_protocol_http200_cancel_recovery.py`、`test_protocol_cancel_outbox.py`、`test_protocol_order_outbox.py`、`test_protocol_grid_withdrawal.py`、`test_protocol_demo_working_order.py` | **54 passed, 0 failed** | 持久 `DEFERRED` Outbox、撤单尝试/确认终态分离、HTTP 200 未确认回执恢复、Grid 本地撤回 |
| C06 / 连续窗口 / Runtime 回归 | `test_demo_cloud_monitor.py`、`test_demo_continuous_campaign.py`、`test_binance_demo_runtime.py` | **47 passed, 0 failed** | 监控脱敏、日报汇总、连续授权窗口、累计预算守卫 |
| Grid Supervisor 回归 | `test_demo_grid_supervisor.py` | **45 passed, 0 failed** | Supervisor 启停、突破退出、轮换、健康快照 |
| 网络守卫回归 | `test_binance_demo_network.py` | **63 passed, 0 failed** | `GET /fapi/v1/order` 对账白名单与写路径强约束 |
| 静态检查 | `python -m compileall -q src tests tools examples` + `git diff --check` | **通过** | 语法与空白规范检查 |

---

## 5. Azure 官方 Demo 实机部署与验收

### 5.1 部署前实机实时状态核查

- 主机：`factorlab-demo-01`（沿用 `/var/lib/factorlab-demo/execute-state`、systemd `factorlab-demo-execute.service`、`LoadCredential`、`session.lock`）
- 首次只读核查（UTC 12:30）：运行 `362660eb`，PID `215390`，累计消耗 `83 / 100 POST`、`530.5328 / 700 USDT`，`UNKNOWN=0`。
- 发布前二次只读核查（UTC 16:12）：由于期间有其他未合并共享账户分支（`201d76ad`）在主机运行及 `legacy-grid` 自然交易，累计预算已自然推进至 **`99 / 100 POST`**（`629.9972 / 700 USDT`，仅剩 **1 笔 POST**、`70.0028 USDT` 名义额度），账户净值 `4999.32325687 USDT`（开户基准 `4999.97114760`，累计亏损 `0.6479 < 1 USDT` 上限），持仓 `0`，挂单 1 笔 `fl-d7345f8795464a65-g-00036-00000001`，`UNKNOWN=0`。

### 5.2 同账本版本发布

通过标准发布入口 `tools/deploy_demo_strategy.py` 安装固定 Git 归档 `94b604bccdaa318ffbff52aa7e0350bac52cf54d` 至 `/opt/factorlab/releases/94b604bccdaa318ffbff52aa7e0350bac52cf54d`（详见 [runtime-deployment.json](runtime-deployment.json)）：

- 进程切换：PID `342858 → 343417`，GET 恢复耗时 `19.44s`
- SQLite inode 完整性：`ledger_inodes_preserved = true`
- 预算不变：`99 / 100 POST`、`629.9972000 / 700 USDT`，`UNKNOWN = 0`，`status = RUNNING`

### 5.3 官方 Demo 实机联合验收过程（`SOFTWARE_CONFORMANCE_ONLY`）

执行 `/opt/factorlab/releases/94b604bccdaa318ffbff52aa7e0350bac52cf54d/source/tools/validate_p8_s04_demo.py`（完整机器可读记录见 [official-demo-acceptance.json](official-demo-acceptance.json)）：

| 阶段 | 切换/操作 | PID 变化 | 官方 Binance Demo 实机结果 |
| --- | --- | --- | --- |
| 1. 同 PID 热切换 + 真实 POST | `legacy-grid → S01 A2`（`a46d8cb1...`，`limit_cycle:LimitCycle` v1.0.2，`buy_offset=-2.00`） | `343417 → 343417`（同 PID） | `switch 9 (seq 25-27)` `SUCCEEDED`；真实官方 POST 获交易所确认：`client_order_id=fl-d7345f8795464a65-sa46d8-entry`、`order_id=195433064`、`NVDAUSDT BUY LIMIT 0.04 @ 149.42 GTC`、`status=NEW`；累计预算 `99 → 100 POST`（`629.9972 → 635.9740 USDT`） |
| 2. 受控重启恢复 | `systemctl restart factorlab-demo-execute.service` | `343417 → 343968` | 重启后自动完成签名 GET 对账，`active` 保持 `a46d8cb1...`，原活跃挂单 `195433064` 完整保留在账本与 `open_orders` 中，观测期 0 重复 POST（`posts=100`），全部 SQLite inode 不变 |
| 3. 同 PID 热切换撤单 + 故障自动回退 | `S01 A2 → S01 A3`（`66eed5f9...`，`mode=fault_on_quote`） | `343968 → 343968`（同 PID） | `switch 10 (seq 28-31)` 进入 `DRAINING (WAIT_TERMINAL_ORDERS)` 合法撤销原挂单 `195433064`，签名 GET 确认终态 `CANCELED`（`2026-11-24T16:37:10.599Z`）后激活 A3；首条实盘报价进入 A3 隔离 worker 抛错，触发 `switch 11 (seq 32-33)` `ROLLBACK_REQUESTED (STRATEGY_EXCEPTION:StrategyWorkerError)` 自动回退至 A2，且 A2 凭持久化 `DecisionJournal` 未重复开仓 |
| 4. 恢复稳定运行 | `S01 A2 → legacy-grid` | `343968 → 343968`（同 PID） | `switch 12 (seq 34-35)` `ROLLED_BACK` 恢复 `legacy-grid`；服务 `RUNNING`、`account_recovered=true`、`UNKNOWN=0`、`open_orders=[]`、`positions=[]`、`nav_usdt=4999.32325687` |

### 5.4 Azure 当前实际运行状态、C06/C07 持续运行与未完成项

- **当前线上状态**：`factorlab-demo-execute.service` `active/running`，PID `343968`，运行提交 `94b604bccdaa318ffbff52aa7e0350bac52cf54d`，`active_deployment = legacy-grid`，`account_recovered = true`，`UNKNOWN = 0`，`open_orders = 0`，`positions = 0`，`nav_usdt = 4999.32325687`，`global_budget = 100 / 100 POST`（`635.9740 / 700 USDT`，`remaining_posts = 0`）。
- **C07 备份状态**（[backup-status.json](backup-status.json)）：最近计划备份 `backup-20261124T031503Z` `SUCCEEDED`、`verified=true`（13 个 SQLite 文件完整校验，4 份有效历史副本，停机维护窗口 3.31s，凭据文件已排除）。
- **C06 持续运行窗口**：
  - 历史混合版本滚动统计（截至 `2026-11-24T16:37Z`）：24h 样本 1,367（覆盖率 94.93%，存活率 99.63%）、72h 样本 2,725（覆盖率 63.08%，`window_complete=false`）、7d 样本 2,725（覆盖率 27.03%，`window_complete=false`）。
  - **P8-S04 新版本独立窗口起点**：`2026-11-24T16:35:34Z`（PID `343417 / 343968`），因此 `CONTINUOUS_24H`、`CONTINUOUS_72H`、`CONTINUOUS_7D` 均诚实标记为 `PENDING`。
- **明确未完成的线上验收项**：
  1. **官方 Demo 实机真实成交（`FILLED`）与实机平仓卖单**：历史执行时仅剩 1 笔 POST，旧工具仍提交了可能成交的挂单，因此存在无预算退出的风险。该历史订单实际最终 `CANCELED`、`filled_quantity=0`，保留原 POST、DELETE、重启与回退证据，但不能将未成交解释为预算规划安全。历史真实成交判定保持 **PARTIAL_VERIFIED**。当前实际累计 `100/100 POST` 不重置、不扩额；新修复尚未部署至官方 Demo，也未取得新的实机成交或平仓证据。
  2. **24h / 72h / 7d 完整持续运行观测窗口**：保持 `factorlab-demo-execute.service`、C06 health/daily-report timers、C07 backup timer 与 S03 strategy-deploy timer 持续启用自然累积。

## 7. PR #246 P1/P2 修复复验（2026-11-25）

- 基线：`69f728c5606f77e59e8d42dd9106407248644702`；已获取 `origin/main` 至 `de67237` 并合并。`demo_session.py`、`network.py` 存在实际冲突，解决时保留主分支的按会话标的恢复、共享账户预算与合法 USDT 标的只读对账能力，其他主分支合并成果不回退。
- P1 旧代码复现：独立测试 `test_resume_after_skipped_bar` 在 Bar 4/5 返回 `False`，历史固定在 Bar 1，原因均为 `BAR_SEQUENCE_GAP`，末尾健康行为断言失败。
- P2 旧代码复现：独立测试 `test_resting_validation_entry_can_exhaust_exit_budget` 通过，证明旧路径从 `99/100` 提交最后一笔 BUY；模拟完全成交后留下 `0.04` 持仓，回退停在 `WAIT_OLD_POSITIONS`。
- P1 修复：行情全局游标仍独立持久化；每次生产 Feed 交付前按当前版本真实持久历史水位读取缺失闭合 Bar，复用原 REST 分页与连续性校验，每个历史 Bar 通过原 controller 原子事务持久化。分页未完成或读取失败时保持暂停，后续轮询及 SQLite 重启从真实历史水位继续。
- target/orders 历史语义：只恢复有界历史窗口，不调用旧决策、不生成历史订单、不推进未执行的交易意图状态；下一新鲜事件仍携带恢复后的窗口和原策略状态。原有订单确认、资金、持仓、费用、FIFO 与撤单义务不被重建或清空。orders 已持久化撤单在行情回补之前推进，不依赖新鲜 Bar。
- 正式生产 Feed 回归：`tests/integrations/test_s04_history_recovery.py`；正在执行暂停恢复、两种模式、分页重启、真实缺失/重复/乱序/API 失败、重新激活和故障回退覆盖。
- P2 预算模式：剩余 0/1 POST 为 `ZERO_POST_SWITCH_ONLY`；剩余 2/3 或更多 POST 仅在完整规则、现有风险/权益、账户状态与 entry+exit 名义金额均可容纳时选择 `ROUNDTRIP_2_POST`。买卖闭环之后的重启、切换和回退统一零 POST，不再使用 `SINGLE_POST_RESTING_CANCEL`，也不执行第三笔潜在成交挂单。阶段重新读取真实预算；准入竞争后未成交且对账平坦时自动降级，有成交或 UNKNOWN 则保留恢复状态。
- P2 原子准入：在原预算 SQLite `BEGIN IMMEDIATE` 事务中保留退出 POST/名义额度，所有其他开仓均受该预留约束；真实订单确认和累计消耗不重置。预留跨重启保留，仅终态、已对账、零成交 entry 可释放；有成交的 entry 只由匹配的 reduce-only exit 消耗预留。
- P2 成交证据：离线桩分别模拟 `0.04` 完全成交及 `0.02` 部分成交后撤单，退出必须对应原版本 `-entry/-exit` ID、真实交易所订单 ID、BUY 与 reduce-only SELL、实际成交量及手续费，且最终库存为零才 `real_fill_verified=true`；两笔未成交订单不能通过。未成交退出与低于最小退出金额的残仓保持恢复状态，不报告已平仓或已回退。
- 已完成回归：P2 集成预算/真实成交证据与预留重启 `28 passed`；部署与 PR #236/#237 关键异常集合 `125 passed`，期间源码变动引起的单项身份失败在固定源码后单独重跑通过。`compileall` 通过；`git diff --check` 发现主分支新文件 `publication.py` 尾部空行，去除后工作区与暂存区检查均通过。
- Codex Review：原 PR 已有额度耗尽反馈，本轮不重复请求。独立复审请求随修复推送发布至原 PR，结论待复审。
- P1 正式回归：13 项历史恢复用例均通过（其中 UNKNOWN 用例按新鲜边界修正后单独重跑通过）；覆盖 target/orders 暂停恢复、A→B→A、故障回退、SQLite 分页重启、缺失/重复/乱序/API 失败、部分成交及撤单、UNKNOWN 原订单恢复。历史回补阶段零新增 POST，DELETE 仅来自既有撤单义务；新鲜事件恢复正常决策。附加残仓切换阻断回归通过。
- 集成回归：原 S04 13 项、Feed、Runtime、连续预算、通用策略及最新主分支多品种/共享账户集合 `126 passed`；另一个凭据子进程用例补齐显式源码 `PYTHONPATH` 后单独重跑通过，合计 127 项。最终 `compileall` 与工作区/暂存区 `git diff --check` 均通过。
- 修复提交：本节所在修复提交（HEAD 可由 `git log -1` 精确定位）；可信 CI 与独立复审在推送后运行，尚未出具新 HEAD 结论。
- TODO：补录可信 CI 及独立复审结果。新代码尚未进行官方 Demo 实机验证；本轮仅离线 HTTP 桩，无 Azure 操作或交易网络请求。原始历史证据文件保持不变。
