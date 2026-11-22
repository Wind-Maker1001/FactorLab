# Azure Binance Demo 自动备份与灾难恢复操作手册（NO LIVE MONEY）

本手册面向 Azure Linux 云主机上运行的 FactorLab Binance 官方 Demo Grid Execute（`/var/lib/factorlab-demo/execute-state`），复用现有 `demo_cloud_runtime backup/restore`（SQLite Backup API）与 P8-C05 封存交接机制，不引入第二套备份引擎，不迁移研究环境，不复制任何 Binance API Secret。

---

## 1. 一致性备份策略：为何采用极短维护窗口

Demo Execute 运行时会跨 4 个核心 SQLite 数据库（以及若干辅助数据库）写入关联账本状态：

- `runtime.sqlite3`：连续授权、Campaign 窗口、累计 POST 与提交名义金额消耗（`dispatches`）、持久 `STOP`/`HALTED` 控制状态。
- `<journal_id>/protocol.sqlite3`：订单事件、成交回执、`UNKNOWN` 订单保护、F10 资金费账本与覆盖窗口（`futures_income_coverage`）。
- `<journal_id>/controller.sqlite3`：Grid 策略事件、持仓与快照。
- `grid-supervisor.sqlite3`：Grid Execute 控制状态（`RUNNING`/`PAUSED`/`STOPPED`）与部署配置。

SQLite 原生 `backup()` API 仅保证**单个 SQLite 数据库文件内部**的页级一致性，无法在写入并发期间提供跨多个独立 SQLite 文件的原子时间点一致性。如果在 Execute 持续写入时去掉排他锁直接热备，可能产生跨库撕裂快照（例如 `runtime.sqlite3` 已记录 `dispatch` 消耗但 `protocol.sqlite3` 尚未落盘订单，或窗口轮换状态错位）。

因此，C07 保留并复用现有 `demo_cloud_storage.backup_state` 的强一致性契约（获取 `session.lock` + 全库 `BEGIN IMMEDIATE` + SQLite `backup()` API + `PRAGMA integrity_check` + `fsync` + `renameat2(RENAME_NOREPLACE)` 原子发布），并通过**极短可控维护窗口**将中断时间压缩到数秒：

1. **停服前前置检查**：以只读方式采集 `cloud-health.json` 基线并检查备份目录可用空间（至少大于状态总字节 + 256 MiB）。空间不足时直接失败告警，**不停止 Execute 服务**。
2. **最短维护窗口**：使用正常 `systemctl stop factorlab-demo-execute.service`（纯 `SIGTERM` 优雅退出，**绝不调用**会改写持久控制状态的 `factorlab-demo stop`）→ 以非特权用户 `factorlab-demo` 执行 `demo_cloud_runtime backup` → 在 `finally` 块中**立即** `systemctl start factorlab-demo-execute.service`。
3. **恢复后验证与清理（零额外停机）**：
   - 轮询 `demo_cloud_runtime health`，验证新 PID 正常启动、账户 GET 重对账通过、`unknown_count == 0`、控制状态未被改写、累计 POST 与名义金额单调不减。
   - 对刚生成的不可变备份目录执行只读深度校验（全部 SQLite `PRAGMA integrity_check`、`backup-manifest.json`、账户绑定、历史订单、资金费连续性、控制状态、累计预算、无密钥文件）。
   - **仅当新备份通过校验后**，才执行过期清理；单次备份失败绝不删除任何既有有效备份。

---

## 2. 定时备份配置与保留策略

### 2.1 systemd 单元

仓库中提供两个独立单元文件（不修改现有 `factorlab-demo-execute.service`、`factorlab-demo.service` 与 `factorlab-demo-health.*`）：

- `deploy/systemd/factorlab-demo-backup.service`（`Type=oneshot`）
- `deploy/systemd/factorlab-demo-backup.timer`（每日 `03:15:00 UTC`，避开 Binance 永续 `00:00 / 08:00 / 16:00 UTC` 资金费结算整点及美股交易高峰，`Persistent=true`）

关键安全边界：

- **禁止访问 Secret**：`factorlab-demo-backup.service` 不配置 `LoadCredential`，并声明 `InaccessiblePaths=/etc/factorlab-demo`，从 mount namespace 层面阻断备份进程读取 Binance API Secret。
- **最小权限数据操作**：备份、校验、隔离演练与清理全部降权为 `factorlab-demo` 用户执行，所有文件权限严格 `0700`/`0600`。
- **备份目录隔离**：默认存放于 `/var/backups/factorlab-demo`（`factorlab-demo:factorlab-demo 0700`），位于状态目录之外；通过 `/etc/systemd/system/factorlab-demo-backup.service.d/10-state-root.conf` 指定实际状态目录 `/var/lib/factorlab-demo/execute-state`。

### 2.2 保留策略与不可变护栏

- 保留最近 **14 天** 内通过完整性校验的每日备份（`--retention-days 14`）。
- **唯一可恢复副本硬保护**：`--min-valid-copies` 下限为 `1`（参数 `< 1` 直接拒绝）。即使最后一份通过校验的副本已超过 14 天，也**禁止删除**。
- 新备份校验通过后才允许清理超过保留期的旧备份；备份或校验失败时跳过清理。
- 每次运行均将脱敏元数据持久化到：
  - `/var/backups/factorlab-demo/backup-status.json`（最近一次状态）
  - `/var/backups/factorlab-demo/backup-history.jsonl`（创建时间、数据库数量、校验结果、大小、停机秒数、错误分类）

### 2.3 安装与常用运维命令

```sh
sudo install -d -o factorlab-demo -g factorlab-demo -m 700 /var/backups/factorlab-demo
sudo install -o root -g root -m 644 deploy/systemd/factorlab-demo-backup.service /etc/systemd/system/factorlab-demo-backup.service
sudo install -o root -g root -m 644 deploy/systemd/factorlab-demo-backup.timer /etc/systemd/system/factorlab-demo-backup.timer
sudo install -d -m 755 /etc/systemd/system/factorlab-demo-backup.service.d
printf '%s\n' '[Service]' \
  'Environment=FACTORLAB_DEMO_STATE_ROOT=/var/lib/factorlab-demo/execute-state' \
  'Environment=FACTORLAB_DEMO_BACKUP_ROOT=/var/backups/factorlab-demo' \
  | sudo tee /etc/systemd/system/factorlab-demo-backup.service.d/10-state-root.conf > /dev/null
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/factorlab-demo-backup.service /etc/systemd/system/factorlab-demo-backup.timer
sudo systemctl enable --now factorlab-demo-backup.timer
```

查看定时任务、最近状态与历史记录：

```sh
systemctl list-timers factorlab-demo-backup.timer --no-pager
sudo -u factorlab-demo cat /var/backups/factorlab-demo/backup-status.json
sudo -u factorlab-demo tail -n 10 /var/backups/factorlab-demo/backup-history.jsonl
journalctl -u factorlab-demo-backup.service -n 50 --no-pager
```

在不中断在线 Execute 的前提下，对任意备份执行隔离恢复演练：

```sh
sudo -u factorlab-demo /opt/factorlab/venv/bin/python \
  -m factorlab.integrations.binance_futures_trading.demo_cloud_backup \
  drill-restore --backup /var/backups/factorlab-demo/backup-<YYYYMMDDTHHMMSSZ>
```

---

## 3. 灾难恢复操作手册

### 3.1 铁律（所有恢复场景必须遵守）

1. **禁止覆盖在线状态**：任何恢复都必须进入全新的私有空目录（`0700`），保留损坏或存活的原状态目录作为取证现场。
2. **禁止删除 `restore-quarantine.json`**：`demo_cloud_runtime restore` 写入的 `restore-quarantine.json` 用于强制限制旧快照只能进行只读检查或 GET-only 观测。旧快照无法证明备份时间点之后没有新订单、预算消耗或 `STOP` 变更。
3. **禁止绕过 C05 交接直接启动 Execute**：普通 `backup`/`restore` 目录均被 `operator_lock` 和 `demo_cloud_handoff` 明确阻止直接获取 Execute 执行权。
4. **禁止重新启用已封存的 Windows 原账本**：Windows 原状态目录已通过 P8-C05 封存，仅作历史取证留存。
5. **禁止重置账本、STOP 状态、持仓或累计预算**：绝不通过删除数据库、清空 `runtime.sqlite3` 或重新 `setup` 来“修复”启动失败。
6. **禁止将 Binance API Secret 放入备份目录、Git 仓库或日志**。

---

### 3.2 场景 A：原主机存活但状态目录损坏（SQLite 校验失败 / 退出码 78）

1. **冻结写入并保存现场**：
   ```sh
   sudo systemctl stop factorlab-demo-execute.service factorlab-demo.service factorlab-demo-backup.timer
   sudo systemctl disable factorlab-demo-execute.service
   # 保留原状态目录不动，可改名归档取证（绝不删除）
   sudo mv /var/lib/factorlab-demo/execute-state /var/lib/factorlab-demo/execute-state.damaged-$(date -u +%Y%m%dT%H%M%SZ)
   ```
2. **校验候选最新有效备份**：
   ```sh
   BACKUP=/var/backups/factorlab-demo/backup-<YYYYMMDDTHHMMSSZ>
   sudo -u factorlab-demo /opt/factorlab/venv/bin/python \
     -m factorlab.integrations.binance_futures_trading.demo_cloud_backup \
     verify --backup "$BACKUP"
   ```
3. **恢复到新的隔离目录（保持 quarantine）**：
   ```sh
   RESTORED=/var/lib/factorlab-demo/recovered-$(date -u +%Y%m%dT%H%M%SZ)
   sudo -u factorlab-demo /opt/factorlab/venv/bin/python \
     -m factorlab.integrations.binance_futures_trading.demo_cloud_runtime \
     restore --backup "$BACKUP" --destination "$RESTORED"
   ```
4. **验证隔离目录与 GET-only 对账**：
   - 使用 `factorlab-demo.service`（GET-only 单元，绝非 Execute 单元）并通过 drop-in 指向 `$RESTORED` 运行受控 GET-only 观测，核对交易所侧当前开放订单、最新成交、资金费流水、仓位与权益。
   - 核对恢复快照与交易所实际状态之间是否存在**备份时间点之后的缺口**（例如备份后新增的订单、预算消耗或风控触发）。
5. **人工受审恢复决策**：
   - 在完成高水位差额核对、确认无未解 `UNKNOWN` 订单且明确当前授权/风控余量前，保持 Execute 停止。
   - 仅当经独立复核确认状态一致、且通过受审的恢复/交接流程完成账本补齐与单主确认后，方可重新启用 Execute（绝不允许直接删除 `restore-quarantine.json` 强行启动）。

---

### 3.3 场景 B：Azure 云主机损坏、OS 盘故障或整机重建

> **注意**：当前 Azure 主机 `factorlab-demo-01` 仅挂载一块 64 GiB 系统盘（`/dev/sda1`），`/var/lib/factorlab-demo/execute-state` 与 `/var/backups/factorlab-demo` 位于同一 OS 盘。因此，发生整块系统盘损毁时，恢复的前提是已从异地加密副本（或人工安全归档到本地离线介质的加密副本）取回最近一次有效备份。

1. **确认旧主机彻底下线（单主原则）**：
   - 在 Azure Portal / CLI 确认原 VM 已停止/释放（Deallocated / Deleted），或确认其已断网且无法再向 Binance Demo 发送请求。
   - 确认 Windows 封存原账本保持封存且无定时任务启动。
2. **重建精简运行环境（不搬迁研究环境）**：
   - 准备新的 Ubuntu 24.04 LTS（或兼容 Debian/Ubuntu systemd）主机，可选挂载独立持久化数据盘至 `/var/lib/factorlab-demo` 与 `/var/backups/factorlab-demo`（写入 `/etc/fstab`）。
   - 创建系统用户与私有目录：
     ```sh
     sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin factorlab-demo
     sudo install -d -o factorlab-demo -g factorlab-demo -m 700 /var/lib/factorlab-demo /var/backups/factorlab-demo
     sudo install -d -o root -g root -m 700 /etc/factorlab-demo
     ```
   - 安装受审版本的 Python 3.13/3.14 运行时与 FactorLab 包到只读 `/opt/factorlab/venv`（`root:root`，`go-w`），仅安装运行依赖，不安装研究数据集与无关工具链。
   - 安装 `deploy/systemd/` 中的单元文件（保持 `factorlab-demo-execute.service` 处于 `disabled` 与 `stopped`）。
3. **独立供应 Binance Demo API Secret**：
   - 通过安全人工途径单独供应 `/etc/factorlab-demo/demo-secret.json`（`root:root 0600`），保持原 Demo API Key 身份与原 `binding.json` 一致（**不更换 Key，不重跑 `setup`**）。
4. **取回并解密最近有效备份**：
   - 将异地加密备份传输至新主机私有临时目录，使用离线保管的解密密钥解密解包为 `/var/backups/factorlab-demo/backup-<YYYYMMDDTHHMMSSZ>`，确保全部文件归属 `factorlab-demo:factorlab-demo` 且目录 `0700`、文件 `0600`。
   - 执行深度校验：
     ```sh
     sudo -u factorlab-demo /opt/factorlab/venv/bin/python \
       -m factorlab.integrations.binance_futures_trading.demo_cloud_backup \
       verify --backup /var/backups/factorlab-demo/backup-<YYYYMMDDTHHMMSSZ>
     ```
5. **隔离恢复与 GET-only 对账**：
   - 按 3.2 第 3–5 步使用 `demo_cloud_runtime restore` 恢复到新的私有目录（保留 `restore-quarantine.json`），先通过 GET-only 模式核对交易所实盘状态、订单、资金费、持仓与权益。
   - 完成高水位核对与人工审批前，绝不启用 `factorlab-demo-execute.service`。
   - 恢复正常运维后，启用 `factorlab-demo-health.timer` 与 `factorlab-demo-backup.timer`。

---

## 4. 异地加密备份评估（Azure Blob Storage）

### 4.1 为什么需要异地备份

当前 Azure VM（`factorlab-demo-01`，`MalaysiaWest`，`Standard_B2ats_v2`）仅使用一块 64 GiB 系统盘 `/dev/sda1`。本机 `/var/backups/factorlab-demo` 可以防范进程异常、误操作或单个 SQLite 损坏，但**无法防范系统盘硬件损坏、误删 VM 或主机不可恢复故障**。

### 4.2 Azure Blob Storage 免费额度与实际费用核对

根据 Microsoft Azure 官方计费与免费权益规则（Azure Free Account 与 Azure for Students）：

| 项目 | Azure Free Account / Azure for Students 免费权益 | 超额或满 12 个月后 Pay-As-You-Go 参考费用 |
| --- | --- | --- |
| **免费性质** | **仅限前 12 个月免费**（**非 Always Free**）；Azure for Students 另含 12 个月 \$100 信用额度 | 满 12 个月后自动按标准 Pay-As-You-Go 计费（学生版从 \$100 额度扣减） |
| **免费容量** | **5 GB** Block Blob（**严格限定 Hot 层 + LRS 本地冗余**） | Hot LRS 约 \$0.018–\$0.021 / GB·月（随区域微调） |
| **免费操作次数** | 每月 **20,000 次读操作** + **10,000 次写操作** | 按每 10,000 次事务计费（Hot 层极低，Cool/Cold/Archive 更高） |
| **网络传输（同区域）** | 同区域（`MalaysiaWest`）VM ↔ Blob 标准端点入站/出站均为 **\$0** | 跨可用区约 \$0.01/GB；跨区域复制或下载到公网（超 100 GB/月免费出口后）约 \$0.087/GB 起 |
| **易产生额外费用的选项** | **不计入免费额度**：GRS/ZRS 冗余、Cool/Cold/Archive 层、Archive 检索解冻费、Private Endpoint 小时费与数据处理费 | 启用即计费，应默认避免 |

**结合 FactorLab 当前体量实测估算**：
- 当前完整一致性备份（12 个 SQLite 数据库 + `binding.json` + `backup-manifest.json`）原始体积为 **16,770,025 字节（15.99 MiB）**（其中 `controller.sqlite3` 约 15.4 MiB）。
- 实测流式压缩体积：`tar | zstd -3` 仅 **825,978 字节（0.79 MiB，压缩比约 20.3×）**；`tar | gzip -6` 仅 **918,294 字节（0.88 MiB，压缩比约 18.3×）**。
- 保留 14–30 份每日异地加密归档总容量仅 **~11–24 MiB**（不到 5 GB 免费额度的 0.5%），每月写入事务 < 100 次（不到 10,000 次免费额度的 1%）。
- 即使超过 12 个月免费期转入 Pay-As-You-Go，在同区域（`MalaysiaWest`）Hot LRS 标准端点下，月费用预计也低于 **\$0.01/月**。

### 4.3 原则与接入设计（本次不默认创建收费资源）

- **本次不默认创建 Azure Storage Account / Blob Container**，也不要求额外付费资源；当前 VM 未安装 Azure CLI（`az`）且未配置 Storage 写权限，因此 `offsite_enabled` 默认保持 `false`。
- 未来若在 Azure Portal 确认订阅免费额度/余额后启用异地加密备份，推荐方案如下：
  1. 在与 VM 相同的 `MalaysiaWest` 区域创建 Standard GPv2 Storage Account，显式选择 **LRS + Hot tier**（禁止 GRS/Archive/Private Endpoint），创建私有容器 `factorlab-demo-backups`（关闭公网匿名访问）。
  2. 为 VM 启用 System-Assigned Managed Identity，仅授予目标容器最小权限角色 `Storage Blob Data Contributor`（避免在 VM 存放永久 Storage Account Key）。
  3. **客户端先加密再上传（零 Secret 原则）**：
     - 仅打包已经过 `verify_backup` 校验的 `/var/backups/factorlab-demo/backup-<YYYYMMDDTHHMMSSZ>` 目录（天然排除 `/etc/factorlab-demo/demo-secret.json`）。
     - 使用主机已安装的 `tar` + `gzip`/`zstd` + `gpg`（公钥非对称加密，**私钥离线保存在运维本地，绝不存放于 Azure VM**）生成 `.tar.zst.gpg` 与 `.sha256` 校验文件：
       ```sh
       tar -C /var/backups/factorlab-demo -cf - backup-<TS> \
         | zstd -q -3 \
         | gpg --batch --yes --encrypt --recipient <OFFSITE_BACKUP_GPG_FPR> \
         > /tmp/backup-<TS>.tar.zst.gpg
       ```
     - 上传加密归档至同区域 Blob 容器，并配置 Blob Lifecycle Management 规则在 30 天后清理旧对象（同时保留至少一份最新归档）。
  4. **零成本临时替代方案**：在未开通 Azure Blob 前，运维人员可按需通过已验证的 SSH 通道将最新的加密快照定期拉取到本地离线存储介质作为异地副本。
