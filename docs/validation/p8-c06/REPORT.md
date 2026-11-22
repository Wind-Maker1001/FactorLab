# P8-C06 Azure Demo 运行监控与异常告警验证报告

**状态：已在 Azure VM 部署并持续运行；外部 Webhook 凭据未配置（手机推送未开通）；24h / 72h / 7d 长窗口正在持续累积采样。**
**验证日期：2026-11-22 UTC（2026-11-23 Asia/Shanghai）**
**独立分支 / 基线：`p8-c06-demo-monitoring`（基于 `origin/main` `85076f6`）**

---

## 1. Azure 当前实际运行状态（实机核验）

目标主机：`factorlab-demo-01`（Ubuntu 24.04.5 LTS，Linux `7.0.0-1020-azure`，1 GiB RAM 规格：总内存 `887 MiB` + Swap `2047 MiB`，根盘 `/dev/root` `61 GiB` 可用 `55.20 GiB`）。

| 检查项 | 部署前基线（16:39–16:40 UTC） | 部署后持续运行实测（17:02 UTC） |
| --- | --- | --- |
| Execute 服务 | `factorlab-demo-execute.service` `active (running)`，PID `3792`，`NRestarts=0`，内存 `110.6 MiB` | `active (running)`，PID `9745`，`NRestarts=0`，内存 `102.8 MiB`（`16:55:48 UTC` 由同机 `factorlab-demo-backup.service` 维护窗口有序停启 2.045s，C06 自身零重启 Execute） |
| 服务存活 / 就绪 / 调度 | `alive=true`，`ready=true`，`execute_healthy=true`，`dispatch_eligible=true` | `alive=true`，`ready=true`，`execute_healthy=true`，`dispatch_eligible=true` |
| 账户恢复与行情新鲜度 | `account_recovered=true`（age `0.650s`），`market_fresh=true`（age `1.303s`），`unknown_count=0` | `account_recovered=true`（age `0.639s`），`market_fresh=true`（age `0.652s`），`unknown_count=0` |
| 挂单 / 持仓 | 挂单 `1`（NVDAUSDT），持仓 `0` | 挂单 `1`（NVDAUSDT），持仓 `0` |
| 全局预算（只读核验） | `state=RUNNING`，已消耗 `7/100` POST（剩余 `93`），已提交名义额 `41.3308/700` USDT（剩余 `658.6692`） | 完全一致：已消耗 `7/100` POST（剩余 `93`），剩余名义额 `658.6692/700` USDT（零重置、零扩大） |
| 健康检查 Timer | `factorlab-demo-health.timer` `active (waiting)`，每 60s 触发 `demo_cloud_runtime health` | `factorlab-demo-health.timer` `active (waiting)`，每 60s 触发 `demo_cloud_runtime monitor`；新增 `factorlab-demo-daily-report.timer`（每日 `00:05 UTC`） |
| 整机内存与磁盘 | 可用内存 `534 / 887 MiB`，磁盘可用 `56 GiB`（10%） | 可用内存 `555 / 887 MiB`，磁盘可用 `55.20 GiB` |
| journald 磁盘配置 | 全部使用默认注释配置，无显式大小/保留上限；execute stdout 15 分钟产生 `5.95 MiB` | 已配置 `/etc/systemd/journald.conf.d/factorlab-demo.conf`，持久化 + 严格上限，当前占用 `16.0 MiB` |

---

## 2. 方案与自动监控配置

本方案严格复用现有 `cloud-health.json`、`demo_cloud_runtime`、systemd 健康 Timer 和 `journald`，**未引入新交易数据库或重量级监控平台**，适配 1 GiB 内存 VM（oneshot 短进程执行，常驻额外内存 `0 MiB`）。

### 2.1 日志持久化与大小上限

1. **journald 持久化与上限**（[journald-factorlab-demo.conf](../../../deploy/systemd/journald-factorlab-demo.conf) -> `/etc/systemd/journald.conf.d/factorlab-demo.conf`）：
   - `Storage=persistent`、`Compress=yes`
   - `SystemMaxUse=512M`、`SystemKeepFree=4G`、`SystemMaxFileSize=32M`、`SystemMaxFiles=32`
   - `RuntimeMaxUse=32M`（保护 1 GiB RAM 主机 tmpfs 内存）
   - `MaxRetentionSec=14day`、`MaxFileSec=1day`、`SyncIntervalSec=30s`
   - **实际增长速度与 512 MiB 配额关系（实机测算）**：实测 `16:25–17:10 UTC`（45 分钟）内，`factorlab-demo-execute.service` 产生 911 行、原始文本 `17.08 MB`（约 `521 MiB/天`），`factorlab-demo-health.service` 产生 `0.77 MB`（约 `23.6 MiB/天`），合计原始文本约 **`545 MiB/天`**；开启 `Compress=yes` 后按 JSON 常见压缩比（6:1–12:1）估算，磁盘占用约为 `45–90 MiB/天`。因此 `SystemMaxUse=512M` 下实际磁盘轮转周期预计约 **5–11 天**（遇异常日志突发或低压缩比时可能更短，仅约 **2–5 天**）。**`MaxRetentionSec=14day` 仅为时间上限，绝不代表必然保存 14 天**——磁盘配额（`512M` / `SystemKeepFree=4G`）优先生效并会提前滚动清理旧归档。
2. **独立监控目录与有界滚动存储**（`/var/lib/factorlab-demo/monitoring`，`factorlab-demo:factorlab-demo 0700`）：
   - 与交易状态目录 `/var/lib/factorlab-demo/execute-state` 完全隔离；systemd unit 使用 `ProtectSystem=strict` + `ReadWritePaths=/var/lib/factorlab-demo/monitoring`，对交易状态目录保持内核级只读。
   - `samples.jsonl`：保留 **8 天**且单文件上限已由 `16 MiB` 上调至 **`48 MiB`**（并精简了未使用的 `systemd.load_state`、`recovery.last_failure_at`/`last_recovered_at` 等字段）。实测每条样本约 `2.1–2.55 KiB`，完整 8 天（`11,520` 条）约占用 **`24–28 MiB`**，`48 MiB` 上限可提供约 `1.7x–2.0x` 余量，确保容量裁剪绝不会提前截断 7 天统计；也是 journald 提前轮转后保障 24h/72h/7d 统计完整的独立基石。
   - `alert-events.jsonl`：保留 14 天且单文件上限 `2 MiB`。
   - `daily-reports/`：保留最近 14 天 `.json` + `.md` 报告。

### 2.2 自动化组件清单

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| 核心监控与报告引擎 | `src/factorlab/integrations/binance_futures_trading/demo_cloud_monitor.py` | 采样脱敏、有界 JSONL、journald 回填（含 `--all` 兼容 >4KB 消息）、24h/72h/7d 统计、7 类异常检测、去重/升级/提醒/限流/恢复状态机、HTTPS Webhook 投递、每日报告、隔离自测 |
| CLI 扩展 | `src/factorlab/integrations/binance_futures_trading/demo_cloud_runtime.py` | 抽出 `evaluate_health()`（100% 保持原 `health` 兼容），新增 `monitor` / `report` / `backfill-monitor` / `test-alert` |
| 每分钟健康监控 | `deploy/systemd/factorlab-demo-health.service` + `factorlab-demo-health.timer` | 复用现有每分钟 Timer，输出健康 JSON 并落盘脱敏采样与评估告警，保留 `0/1` 退出码 |
| 每日运行报告 | `deploy/systemd/factorlab-demo-daily-report.service` + `.timer` | 每日 `00:05:00 UTC` 生成并落盘 JSON + Markdown 日报，区分**服务存活**、**交易就绪**和**真实订单执行** |
| 外部 Webhook 模板 | `deploy/systemd/alert-config.example.json` | 仅占位模板；真实配置放于 `/etc/factorlab-demo/alert-config.json`（`0600 root:root`）并通过 systemd `LoadCredential` 注入，不入库 |

---

## 3. 告警触发测试与实机验证证据

### 3.1 隔离模拟 Webhook 端到端告警测试（Azure 实机，以 `factorlab-demo` 用户执行）

命令：`sudo -u factorlab-demo /opt/factorlab/venv/bin/python -m factorlab.integrations.binance_futures_trading.demo_cloud_runtime test-alert`

实机结果：
- `passed`: `true`
- `total_webhook_deliveries`: `12`（6 类故障触发 `ALERT_FIRING` + 6 类恢复 `ALERT_RESOLVED`，投递到本地模拟 loopback 接收端）
- `fired_alert_keys` & `resolved_alert_keys`: `account_recovery_abnormal`、`budget_low`、`disk_space_low`、`order_unknown`、`sustained_health_failure`、`unexpected_restart`（第 7 类 `process_exited` 在自动化单元测试及 CLI 测试中验证触发与恢复）
- `dedupe_verified`: `true`（重复故障采样 `step_event_counts` 为 `0`）
- `recovery_verified`: `true`
- `sensitive_key_leak_detected`: `false`
- `unconfigured_behavior`: `{"configured": false, "delivered": false, "mobile_push_configured": false, "status": "NOT_CONFIGURED"}`

### 3.2 真实线上告警触发 → 去重 → 自动恢复闭环（Azure 实机）

部署后监控引擎实时捕获到 `16:55:48 UTC` 由同机 `factorlab-demo-backup.service` 维护窗口导致的 Execute 进程切换（PID `3792 -> 9745`，`starts 2 -> 3`）：

1. **`16:56:15 UTC` 触发 `ALERT_FIRING`**：`unexpected_restart`（`WARNING`），`delivery.status="NOT_CONFIGURED"`，`mobile_push_configured=false`。
2. **`16:57` – `17:00 UTC` 连续 4 个采样周期去重**：`alert-events.jsonl` 未产生任何重复记录，`occurrences` 累加至 5。
3. **`17:01:30 UTC` 自动恢复 `ALERT_RESOLVED`**：连续 5 次健康采样稳定后自动清除活动告警并记录恢复通知（`duration_seconds=314.97`），`active_alerts` 归零。

### 3.3 持续运行与每日运行报告实机快照（`17:02:47 UTC`）

- 已采集 `37` 条真实健康采样（回填 27 条历史 journald 快照 + 10 条 C06 实时自动采样，`samples.jsonl` 占用 `91 KiB`）。
- 三维度诚实拆分实测输出：
  - **服务存活（Service Liveness）**：`SERVICE_ALIVE`（24h Alive Ratio `100.00%`）
  - **交易就绪（Trading Readiness）**：`DISPATCH_ELIGIBLE`（24h Ready Ratio `97.30%`，24h Dispatch Eligible Ratio `75.68%`）
  - **真实订单执行（Real Order Execution）**：`NO_NEW_ORDER_SUBMISSION_IN_24H_WINDOW`（窗口内 POST 增量 `0`，诚实区分就绪与真实下单）
- **POST 预算统计（只读）**：剩余 `93/100` POST（`93.00%`），剩余名义额 `658.6692/700` USDT，窗口内消耗速率 `0.000 POST/h`，`policy=READ_ONLY_NEVER_RESET_OR_AUTO_EXPAND`。

### 3.4 零敏感信息泄露实机扫描

在 Azure VM 扫描 `/var/lib/factorlab-demo/monitoring/` 全部 7 个产物文件（`samples.jsonl`、`alert-events.jsonl`、`alert-state.json`、`daily-reports/*`），对照真实 `demo-secret.json` 与 `binding.json` 中的 `key`、`secret`、`account_id` 哨兵值：`sentinels_checked=3`，`leaks_found=[]`，`passed=true`。

---

## 4. 自动化测试记录

- 新增单元测试 `tests/integrations/test_demo_cloud_monitor.py`：**13/13 通过**（覆盖采样脱敏、有界 JSONL 裁剪、**完整 8 天 11,520 条真实体积采样容量测试与 7 天统计完整性验证**、24h/72h/7d 多窗口聚合与按 PID 分段计数、POST 消耗速率、7 类异常监测与去重/升级/提醒/限流/恢复、**C07 预期备份维护重启区分（仅记录 `MAINTENANCE_RESTART` 不触发误报）及维护失败/停机超时/未对账/服务未恢复强制告警**、HTTPS 校验与未配置诚实声明、本地模拟 Webhook 端到端测试、每日报告三层状态区分、CLI 状态根目录隔离、`journalctl --all` 回填）。
- CI 同款完整 Demo 云端回归套件（初始 9 个测试文件 152 passed）及 PR #230 修复后定向回归（`test_demo_cloud_monitor.py` + `test_demo_cloud_runtime.py` + `test_demo_execute_systemd.py`）均全部通过，无失败或跳过。
- Azure 实机 `systemd-analyze verify` 校验全部 5 个 unit（`execute`、`health.service/.timer`、`daily-report.service/.timer`）通过；`17:18 UTC` 实机验证单条精简样本 `2,296 B`（8 天 11,520 条约 `25.22 MiB` << `48 MiB` 上限），并自动将 `16:55:48 UTC` 的 C07 2.045s 维护重启分类为 `Planned Maintenance Restarts: 1`（`Unexpected / Maint Restarts: 1 / 1`）。

---

## 5. 尚未配置或仍在积累的事项（诚实声明）

1. **外部推送凭据尚未配置**：当前 Azure 主机未配置 `/etc/factorlab-demo/alert-config.json` 与 `20-alert-webhook.conf` drop-in，**手机推送/外部告警投递尚未开通**；告警目前记录于 `/var/lib/factorlab-demo/monitoring/alert-events.jsonl` 与 `journald`。待确定通知平台后，按 [云端运行手册](../../binance-demo-cloud-runtime.md#optional-private-https-webhook-configuration) 注入私有 HTTPS Webhook 即可启用外部推送。
2. **24h / 72h / 7d 长窗口持续积累中**：当前样本跨度约 37 分钟（`window_complete=false`），Timer 已在后台持续每分钟采集并将自动覆盖 24h、72h 和 7d 窗口。

## 6. PR #230 Codex Review 复验（2026-11-23 Asia/Shanghai）

- **P1，systemd 路径。** 两个 unit 默认读取 `/var/lib/factorlab-demo/execute-state`，只写 `/var/lib/factorlab-demo/monitoring`。Azure 保留两个服务现有的 `10-state-root.conf`，实测有效配置一致。北京时间 01:41 通过 `systemctl start` 运行健康检查和日报，两个服务均为 `Result=success`、`ExecMainStatus=0`。健康 stdout 显示 `alive=true`、`ready=true`、`execute_healthy=true`、`dispatch_eligible=true`、`unknown_count=0`、活动告警为 0。日报 stdout 与落盘 JSON 均生成，覆盖 74 条样本，24h 窗口仍在积累。
- **P2，告警恢复。** UNKNOWN、预算、账户恢复、磁盘和 C07 等告警按各自所需的有效证据确认恢复。已验证健康文件丢失、JSON 无法解析、字段缺失或为 `null` 时保留活动告警，完整健康样本到来后仍可发出 `ALERT_RESOLVED`。
- **P2，回填身份。** 已落盘样本按监控 stdout 中的观测时间关联，历史 health-only 记录使用 journal 时间戳。同一 heartbeat 下的不同观测均可入库。回归模拟 1 条健康及 4 条进程失联或过期观测，存活率为 20%，重复回填新增 0 条。Azure 对实际日志连续回填两次，均为 `imported=0`、`total_samples=74`。
- **历史重启。** 报告的 `Unexpected / Maint Restarts: 1 / 1` 对应两次独立的同 unit 停启。2026-11-22 16:25:49 UTC 是 PID `3323 → 3792`、`starts 1 → 2`；C07 成功备份 `backup-20261122T165547Z` 对应 16:55:50 UTC 的 PID `3792 → 9745`、`starts 2 → 3`，对账通过，停机 2.045 秒。生成的报告保持两类各计一次，原始 `unexpected_restart` 的触发和恢复事件仍在审计文件。
- **Execute 与排程。** 部署前后 PID 均为 `9745`，进程启动时间、`NRestarts=0`、Execute Owner 和 `desired=RUNNING` 一致。累计 POST 为 `7/100`，名义额已用 `41.3308000/700` USDT，剩余 `658.6692000` USDT，预算状态 `RUNNING`。健康和日报 timer 均在等待正常触发。发现备份 timer 已禁用后，核对了持久化触发记录并恢复原有每日排程，启用时没有补跑。最近一份备份为 `SUCCEEDED`、`verified=true`；下一次计划触发是北京时间 11:15，日报为 08:05。
- **回归。** 首次运行 CI 同款 9 文件套件得到 154 通过、2 失败，分别暴露了账户恢复计数在脱敏时被补成 0，以及 Windows 子进程缺少源码导入路径。修正脱敏并设置子进程可继承的 `PYTHONPATH` 后，监控全模块与 Windows 凭据独立进程用例合计 15 项全部通过。最终提交的自动化结果见 [PR #230 Checks](https://github.com/Wind-Maker1001/FactorLab/pull/230/checks)。
