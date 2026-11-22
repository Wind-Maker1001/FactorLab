# P8-C07 验证与恢复演练报告：Azure Demo 自动备份与灾难恢复

**NO LIVE MONEY.** 本次以 `origin/main`（`85076f6`）为基线，在独立 worktree（`feat/p8-c07-backup-dr`）中开发并完成了 Azure 云端自动一致性备份配置、一次真实可控维护窗口备份、独立隔离目录恢复演练、restore quarantine 验证、异地加密备份评估与灾难恢复手册编写。

---

## 1. 核心结论摘要

| 检查项 | 实际结果 |
| --- | --- |
| 真实备份是否成功 | **成功**（`factorlab-demo-backup.service` 退出码 `0`，`status=SUCCEEDED`，`verified=true`） |
| 是否需要中断 Execute 及中断时长 | **需要极短维护窗口**（跨 12 个 SQLite 数据库一致性约束）；实测中断 **`2.045` 秒**（`16:55:48.363Z` → `16:55:50.407Z`） |
| 恢复验证是否通过 | **全部通过**（独立隔离目录 9/9 项一致性核验通过，restore quarantine 与 C05 交接拦截验证通过） |
| 备份位置、大小与保留情况 | `/var/backups/factorlab-demo/backup-20261122T165547Z`；原始 **`16,770,025` 字节（15.99 MiB，12 个 SQLite DB）**；`zstd -3` 压缩体量 `825,978` 字节（0.79 MiB）；当前保留 **1 份**有效副本（受唯一可恢复副本保护 `protected_min_copy=true`，每日 1 次保留 14 天） |
| Azure Execute 当前状态 | **持续健康运行**（`active (running)`，PID `9745`，`NRestarts=0`，`execute_healthy=true`，`demo_execution_ready=true`，`unknown_count=0`，预算与控制状态零重置） |
| 异地备份是否真正启用 | **未启用**（`offsite_enabled=false`）；已完成 Azure Blob 免费额度/费用评估与客户端加密接入设计，未默认创建任何收费云资源 |
| 密钥安全核查 | **零泄露**（`InaccessiblePaths=/etc/factorlab-demo`，无 `LoadCredential`，真实密钥字节扫描 `/var/backups/factorlab-demo` 命中数 `0`） |

---

## 2. Azure 运行环境与磁盘空间盘点（2026-11-22 UTC）

- **云主机**：`factorlab-demo-01`（Azure `MalaysiaWest`，`Standard_B2ats_v2`，单 OS 盘 `64 GiB` `/dev/sda1`）
- **系统与运行时**：Ubuntu `24.04.5 LTS`，内核 `Linux 7.0.0-1020-azure`，`systemd 255 (255.4-1ubuntu8.18)`，Python `3.13.18`（`/opt/factorlab/venv/bin/python`）
- **基线提交**：`/opt/factorlab/repo` 与 `/opt/factorlab/venv` 安装基线为 `85076f6`（P8-C05），未修改任何 Grid 策略、交易授权或既有 `factorlab-demo-execute.service` 单元。
- **磁盘与容量**：
  - `/`（`ext4`，`/var/lib/factorlab-demo` 与 `/var/backups/factorlab-demo` 同挂载）：总计 `61G`，已用 `5.7G`（10%），可用 **`56G`**（`59,308,834,816` 字节），inode 已用 `2%`（可用 `8,146,389`）。
  - 在线状态目录 `/var/lib/factorlab-demo/execute-state`：共 **12 个 SQLite 数据库**，总计 `16,769,039` 字节（约 `16.0 MiB`，其中 `eb66189ca89a5d1f8253a4e35c66bd12/controller.sqlite3` 约 `15.4 MiB`）。
  - 14 天每日保留本地原始备份仅约 `~224 MiB`，占当前可用空间不足 `0.5%`。
  - **风险提示**：当前状态目录与本地备份目录位于同一块 64 GiB 系统盘上，无独立数据盘，因此本地备份无法单独防范整盘/整机损坏。

---

## 3. 在线备份可行性分析与维护窗口设计

现有 `demo_cloud_storage.backup_state` 在备份前必须同时获取 `session.lock` 排他锁并对状态根下所有 SQLite 数据库开启 `BEGIN IMMEDIATE` 写锁，再调用 SQLite Backup API（`reader.backup(writer)`）复制并做 `PRAGMA integrity_check` 与 `renameat2(RENAME_NOREPLACE)` 原子发布。

由于 Demo Execute 在运行期间跨 4 个核心数据库（`runtime.sqlite3`、`grid-supervisor.sqlite3`、`<journal_id>/protocol.sqlite3`、`<journal_id>/controller.sqlite3`）写入强关联账本（dispatch 预算消耗、订单事件、资金费覆盖窗口、策略控制状态），SQLite 单库 `backup()` API 无法提供跨多个独立数据库文件的原子一致性快照。若跳过排他锁进行运行中热备，存在跨库撕裂快照风险。

因此本次采用**极短可控维护窗口**：
1. 停服前先做只读 `health` 基线采集和可用磁盘空间预检（空间不足直接失败，**不停止 Execute**）。
2. 使用正常 `systemctl stop factorlab-demo-execute.service`（纯 `SIGTERM`，**绝不调用** `factorlab-demo stop`）停止写入。
3. 降权为 `factorlab-demo` 调用现有 `demo_cloud_runtime backup`（SQLite Backup API）生成快照。
4. 在 `finally` 保护块内**立即** `systemctl start factorlab-demo-execute.service` 恢复在线（即使备份失败也必恢复）。
5. 恢复在线后再执行重对账校验、备份深度校验与条件清理，将停机时长压缩至物理下限。

---

## 4. 定时备份部署与单次真实备份结果

### 4.1 已部署配置

- 模块：`/opt/factorlab/venv/lib/python3.13/site-packages/factorlab/integrations/binance_futures_trading/demo_cloud_backup.py`（`root:root 0644`，`factorlab-demo` 不可写）
- 备份目录：`/var/backups/factorlab-demo`（`factorlab-demo:factorlab-demo 0700`）
- systemd 单元与 drop-in：
  - `/etc/systemd/system/factorlab-demo-backup.service`（`Type=oneshot`，无 `LoadCredential`，`InaccessiblePaths=/etc/factorlab-demo`）
  - `/etc/systemd/system/factorlab-demo-backup.timer`（每日 `03:15:00 UTC`，避开 `00:00/08:00/16:00 UTC` 资金费结算窗口，`Persistent=true`）
  - `/etc/systemd/system/factorlab-demo-backup.service.d/10-state-root.conf`（指定 `FACTORLAB_DEMO_STATE_ROOT=/var/lib/factorlab-demo/execute-state`）
- 验证：`systemd-analyze verify` 通过，`factorlab-demo-backup.timer` 已启用并激活（下一次计划触发：`Mon 2026-11-23 03:15:00 UTC`）。未改动 `factorlab-demo-execute.service`、`factorlab-demo.service`、`factorlab-demo-health.*` 及其 drop-in，与并行开发的 C06 零文件冲突。

### 4.2 单次真实维护窗口备份实测（2026-11-22 16:55 UTC）

通过 `sudo systemctl start factorlab-demo-backup.service` 执行一次完整真实备份：

| 指标 | 备份前（`pre_health`） | 备份后重对账（`post_health`） |
| --- | --- | --- |
| 服务状态与 PID | `active (running)`，PID `3792`，`NRestarts=0` | `active (running)`，PID `9745`，`NRestarts=0` |
| 停机与恢复时间（UTC） | 停止 `16:55:48.362649Z` | 拉起 `16:55:50.407347Z`（**停机 `2.045s`**） |
| 模式与控制状态 | `EXECUTE` / `status=RUNNING` / `desired=RUNNING` | `EXECUTE` / `status=RUNNING` / `desired=RUNNING` |
| 健康与对账 | `execute_healthy=true`, `account_recovered=true`, `market_fresh=true` | `execute_healthy=true`, `account_recovered=true`, `market_fresh=true` |
| 活跃部署与订单 | `fl-d7345f8795464a65-g-00002`, `open_orders=1`, `positions=0`, `unknown_count=0` | `fl-d7345f8795464a65-g-00002`, `open_orders=1`, `positions=0`, `unknown_count=0` |
| 全局累计预算 | `consumed_posts=7`, `submitted_notional=41.3308000`, `remaining_posts=93`, `remaining_notional=658.6692000` | `consumed_posts=7`, `submitted_notional=41.3308000`, `remaining_posts=93`, `remaining_notional=658.6692000` |

生成快照与审计文件：
- `/var/backups/factorlab-demo/backup-20261122T165547Z`（`16,770,025` 字节，12 个 SQLite DB，`sidecar_bytes=0`，`integrity_all_ok=true`）
- `/var/backups/factorlab-demo/backup-status.json`（`0600`，`factorlab-demo:factorlab-demo`）
- `/var/backups/factorlab-demo/backup-history.jsonl`（`0600`，已记录首条审计记录）
- 保留评估：`retention_days=14`, `min_valid_copies=1`, `kept_valid_count=1`（`protected_min_copy=true`）, `pruned_count=0`。

---

## 5. 独立隔离恢复演练与 Restore Quarantine 验证（零停机）

在 `factorlab-demo-execute.service` 持续在线（PID `9745` 未中断）的情况下，对 `/var/backups/factorlab-demo/backup-20261122T165547Z` 执行了两次独立隔离验证：

1. **自动化隔离恢复演练（`demo_cloud_backup drill-restore`）**：
   - 恢复至独立隔离临时目录 `/var/backups/factorlab-demo/.restore-drill-774lgayn/restored-state`（演练完毕自动清理，未触碰 `/var/lib/factorlab-demo/execute-state`）。
   - **全部 9 项业务与完整性核验通过**：
     - `databases_match=true` & `integrity_all_ok=true`：全部 12 个 SQLite 数据库只读 `PRAGMA integrity_check == ok`。
     - `binding_match=true`：`environment=DEMO`, `host=https://demo-fapi.binance.com`, `journal_id=eb66189ca89a5d1f8253a4e35c66bd12`, `journal_id_matches_account=true`, `meta_account_matches_binding=true`。
     - `orders_match=true`：`orders_count=10`（`FILLED: 4`, `CANCELED: 3`, `REJECTED: 2`, `SUBMITTED: 1`），`events_count=14`, `receipts_count=14`, `unknown_orders_count=0`。
     - `funding_match=true`：`funding_events_count=8`, `funding_tran_ids_unique=true`, `funding_time_monotonic=true`, `funding_coverage.complete=true`（`1794650735948`–`1795366546269`，完整覆盖 8 条资金费 `1795338000000`–`1795363200000`），`funding_continuity_ok=true`。
     - `control_match=true`：`durable_control_state=RUNNING`, `grid_control_desired=RUNNING`, `grid_mode=execute`。
     - `budget_match=true`：`campaigns_count=15`, `dispatches_count=13`, `consumed_posts=7`, `consumed_submitted_notional=41.3308000`, `remaining_posts=93`, `remaining_notional=658.6692000`, `opening_equity=4999.97114760`, `max_loss=1`。
     - `controller_match=true`：`events_count=21140`, `snapshot_count=1`。
     - `secret_files_excluded=true`。
   - **Restore Quarantine 与 C05 防绕过核验通过**：
     - `restore-quarantine.json` 存在（`reason=restored-state-requires-GET-only-recovery`）；
     - `operator_lock(restored)` 被拒绝（`restored Demo state is restricted to cloud GET-only recovery`）；
     - `operator_lock(restored, allow_quarantined=True)` 允许 GET-only 观测；
     - `demo_cloud_handoff._payload` 同时拒绝备份目录与恢复目录，无法绕过 C05 交接流程。
2. **真实 Execute CLI 隔离拒绝验证（零凭据、零网络）**：
   - 将真实备份通过 `demo_cloud_runtime restore` 恢复到独立隔离目录后，尝试执行 `factorlab-demo grid-supervisor start --execute`：
   - 进程立即拒绝退出：**退出码 `78`**，输出 `{"event": "grid_start_refused", "reason": "RESTORE_QUARANTINE"}`，未产生任何网络请求，随后清理该临时目录。
3. **零密钥泄露字节扫描**：
   - 使用真实 `/etc/factorlab-demo/demo-secret.json` 中的 `key` 与 `secret` 在内存中对 `/var/backups/factorlab-demo` 全部文件进行字节扫描：`SECRET_LEAK_HITS=0`。
4. **演练后线上健康复核**：
   - `factorlab-demo-execute.service` 维持同一 PID `9745`、`NRestarts=0`、`LIVE_HEALTH_RC=0`、`ready=true`、`execute_healthy=true`、`demo_execution_ready=true`、`unknown_count=0`，累计预算仍为 `posts=7` / `41.3308000`。

---

## 6. 离线自动化测试回归结果

| 环境 | 测试范围 | 结果 |
| --- | --- | --- |
| Windows (`Python 3.12.7`) | `tests/integrations/test_demo_cloud_backup.py` | **9 passed, 0 skipped, 0 failed** |
| Windows (`Python 3.12.7`) | `test_demo_cloud_storage.py` + `test_demo_cloud_runtime.py` + `test_demo_cloud_credentials.py` + `test_demo_execute_systemd.py` | **64 passed, 0 skipped, 0 failed** |
| Debian 13.7 WSL2 (`Python 3.13.18`) | `test_demo_cloud_backup.py` + `test_demo_cloud_storage.py`（含 Linux `0700/0600`、owner、symlink、hardlink 检查） | **26 passed, 0 skipped, 0 failed** |

测试覆盖：多库一致性备份与业务校验、隔离恢复演练、restore quarantine 与 C05 交接拒绝、14 天保留与超期唯一可恢复副本保护、单次备份失败跳过清理并保证 `finally` 恢复 Execute、磁盘不足前置拒绝不中断交易、`STOPPED`/`PAUSED` 控制状态保留、资金费不连续与密钥文件拦截，以及 systemd unit 静态安全契约。

---

## 7. 异地加密备份评估结论与未完成项

- **费用与免费额度结论**（详见 [灾难恢复操作手册](../../binance-demo-disaster-recovery.md#4-异地加密备份评估azure-blob-storage)）：
  - Azure Free Account / Azure for Students 均包含 **前 12 个月 5 GB Hot LRS Block Blob + 20,000 读/月 + 10,000 写/月**（**非 Always Free**；满 12 个月或超额按 Pay-As-You-Go / 学生 \$100 信用额度计费；GRS/Archive/Private Endpoint 不在免费额度内）。
  - 同区域（`MalaysiaWest`）VM ↔ Blob 标准端点传输费用为 **\$0**。
  - 实测当前备份经 `tar | zstd -3` 压缩仅 **0.79 MiB**（`gzip -6` 仅 **0.88 MiB**），14–30 天保留总容量仅 **~11–24 MiB**（不到 5 GB 免费额度的 0.5%）；即使转 Pay-As-You-Go，月费亦低于 **\$0.01/月**。
- **未完成项与客观阻塞原因**：
  1. **异地 Azure Blob 自动上传未启用（`offsite_enabled=false`）**：遵循“不默认创建收费云资源”原则，且当前 VM 未安装 Azure CLI（`az`）、未分配 Azure Storage Account / Blob Container 写入授权（Managed Identity / SAS），亦未供应离线保管的 GPG 加密公钥。已提供完整客户端加密（`tar + zstd + gpg`）与 Managed Identity 接入方案，待订阅额度确认与授权配置后即可启用。
  2. **独立数据盘未拆分**：当前 Azure VM 仅挂载一块 64 GiB 系统盘（`/dev/sda1`），`/var/lib/factorlab-demo` 与 `/var/backups/factorlab-demo` 同盘存放；在启用异地加密备份前，建议按需通过 SSH 定期离线拉取加密快照作为异地副本。
  3. **多日定时自动触发观察**：`factorlab-demo-backup.timer` 已启用并计划于 `2026-11-23 03:15:00 UTC` 首次按时钟自动触发；本次已通过 `systemctl start factorlab-demo-backup.service` 验证端到端执行路径，连续 14 天滚动清理的实际线上观察待后续自然周期积累（保留与唯一副本保护逻辑已通过离线测试严格验证）。
