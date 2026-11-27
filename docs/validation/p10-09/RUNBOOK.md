# FactorLab 双市场异地恢复

两台服务器之间各市场只有一个执行所有者。马来西亚是当前主节点，日本保留受验证的副本。两个市场各自生成、传输、对账和接管，使用各自的凭证、执行用户及状态目录。接管其中一个市场不授予另一个市场执行权。

## 主节点和异地副本

1. 按 P10-01/P10-02 的版本安装方式部署同一份已审查的 Git 提交。记录实际安装版本和有效启动配置，包括执行模式、服务单位、两端状态路径、实例或包目录、外部 TaskMailbox 路径以及私有配置文件的摘要。把这些信息保存为一份按市场管理、两端内容完全一致的 launch contract 文件。密钥通过 systemd 凭证单独配置。
2. Binance 沿用 C07 的备份 unit、timer 和 TaskMailbox drop-in。给备份 unit 增加 `Environment=FACTORLAB_DR_REVISION=<已部署的完整 Git SHA>`。P10-01 更新实际部署时应一并更新此值。Alpaca 安装 `factorlab-alpaca-backup.service` 和对应 timer，把 `/var/backups/factorlab-alpaca-paper` 建成 `factorlab-alpaca` 所有、权限 0700 的目录。Alpaca 从 `current` 指向的发布目录读取版本；最终切换前应在该版本中完成至少一次账户对账并保存官方现金请求区间。两端安排不同时间的巡检和验证，避免同时耗尽 VM 的余量。
3. 为日本接收节点的 `factorlab-backup` 用户创建独立 SSH 密钥，按市场保存在 root 所有的 `/etc/factorlab-dr/binance.ssh-key` 和 `alpaca.ssh-key` 中。主节点创建仅用于拉取的 `factorlab-backup-reader`，具有 `/bin/sh`、锁定的密码和 root 管理的专用 authorized keys 文件。两个市场的服务用户继续使用 `nologin`。固定主节点 SSH 主机公钥，并通过控制台或原可信连接核实。
4. 在日本准备 `/var/backups/factorlab-offsite/binance` 和 `/var/backups/factorlab-offsite/alpaca`，所有者均为 `factorlab-backup`，权限 0700。确认实际承载该目录的现有 Azure 托管磁盘使用静态加密，将磁盘资源 ID 和控制面返回的加密类型记入维护证据。SSH 加密传输，静态加密由该磁盘提供。无需新资源。
5. 安装 offsite service/timer 和失败告警 unit。在 `/etc/factorlab-dr/<市场>.env` 填写下面三项，配置 `/etc/factorlab-dr/ssh_config` 中相应 Host。首次拉取成功后验证两个市场的独立副本，再启用两个 timer。失败 unit 输出 `SYSLOG_IDENTIFIER=factorlab-dr`、优先级 error 的事件，由已有告警接收器消费。

```ini
FACTORLAB_OFFSITE_HOST=dr-binance
FACTORLAB_OFFSITE_SOURCE=.
FACTORLAB_OFFSITE_MAX_BYTES=268435456
```

```sshconfig
Host dr-binance
    HostName <primary-host>
    User factorlab-backup-reader
    IdentitiesOnly yes
    IdentityFile ${CREDENTIALS_DIRECTORY}/ssh-key
    UserKnownHostsFile /etc/factorlab-dr/known_hosts
```

另一个 Host 使用同一读取账户及 Alpaca 专用密钥。源端以 `Match User factorlab-backup-reader` 配置公钥登录及 root 管理的专用 `AuthorizedKeysFile`，关闭密码、键盘交互、TTY 和转发。两把公钥分别使用以下受限命令，后接对应公钥。

```text
restrict,command="/usr/bin/sudo -n -u factorlab-demo /usr/bin/rrsync -ro /var/backups/factorlab-demo" <Binance public key>
restrict,command="/usr/bin/sudo -n -u factorlab-alpaca /usr/bin/rrsync -ro /var/backups/factorlab-alpaca-paper" <Alpaca public key>
```

在主节点用 `visudo` 仅授权这两个确切命令。保留 `SSH_ORIGINAL_COMMAND` 供 rrsync 解析受限请求，工作进程以各自的快照所有者身份只读运行。快照可保持原有的 0700/0600 权限。

```sudoers
Defaults:factorlab-backup-reader env_keep += "SSH_ORIGINAL_COMMAND"
factorlab-backup-reader ALL=(factorlab-demo) NOPASSWD: /usr/bin/rrsync -ro /var/backups/factorlab-demo
factorlab-backup-reader ALL=(factorlab-alpaca) NOPASSWD: /usr/bin/rrsync -ro /var/backups/factorlab-alpaca-paper
```

先用一次手动 systemd oneshot 验证主机公钥、只读授权及失败告警，再启用定时运行。拉取保留两份发布副本和至多一个当前暂存目录。rsync 先估算字节数，再使用上一个副本和未完成文件做差量。SHA-256、包内容和 SQLite 校验后才发布。操作员需为保留副本和单次传输新增空间同时预留空间。初始上限 256 MiB 是部署配置示例，应依据监测到的状态增长和磁盘空间调整。

发生中断时重新执行同一 `pull`，相同名称使用相同的暂存路径。目标机的旧有效副本在新副本通过验证前保持可用。源端捕获和接收端拉取的时间可分别从 C07 状态文件、Alpaca unit 日志及 receiver JSON 查看。捕获过期触发告警，需要检查源端备份 unit。

## 计划内切换

1. 记录故障演练或维护窗口的开始时间。目标执行 unit 保持 masked，配置的最终状态路径尚不存在，其父目录归市场执行用户且权限为 0700。Alpaca 可用 `/var/lib/factorlab-alpaca-paper/execute-state`，对 Alpaca 执行、健康检查和备份三个单位设定相同的 `FACTORLAB_ALPACA_STATE_ROOT`。Binance 的执行、备份及监控单位使用同一 `FACTORLAB_DEMO_STATE_ROOT`。协调账户的所有其他写入入口。先停并 mask 源端自动备份、策略部署和外部邮箱生产者，再用 `systemctl mask --now` 持久禁用源端执行 unit。核实执行 unit 和所有辅助写入者无运行进程、无待执行 job，重启入口被持久禁用。停止进程时使用 systemd，保留应用内部 STOPPED/HALTED/PAUSED。
2. 以源端市场执行用户创建最终快照。Binance 传入外部 TaskMailbox 路径。若 mailbox 已在状态根内，SQLite 会随当前根一起捕获。其他进程写入的外部 SQLite、实例配置、以及状态根之外的策略包必须在维护前迁入受控恢复范围或单独作为已验证的发布资产准备。最终快照默认收录状态根下的标准 `strategy-releases`。

```bash
python -m factorlab.integrations.disaster_recovery snapshot \
  --market "$MARKET" --state-root "$SOURCE_STATE" --destination "$FINAL_BACKUP" \
  --revision "$DEPLOYED_SHA" --configuration "$LAUNCH_CONTRACT" \
  --target-machine "$TARGET_MACHINE_ID" --target-state "$TARGET_STATE"
```

源端会先落盘封存再发布快照。生成中断且目标快照尚未发布时，用原参数重试。封存后的源目录继续拒绝执行和 Binance 控制写入。源端留在封存状态，原地返回运行需要从新所有者按同一流程反向交接。

3. 使用 offsite pull 从目标端拉取最终快照的明确名称。以目标具备最小必要权限的市场执行用户运行 `describe --backup "$VERIFIED_COPY"`，取得该副本的 `snapshot_sha256`。供恢复使用的副本及其私有父目录必须归该市场执行用户所有。接收服务账号和市场执行用户不同时，由管理员在私有目录复制并调整副本所有者，再由恢复命令重新验证。目标的外部 TaskMailbox 生产者及消费者应指向恢复后根目录下的 `external/task-mailbox/tasks.sqlite3`，或原内置路径。
4. 管理员把已查证的隔离事实写入目标 `/etc/factorlab-dr/` 下的 root 所有 JSON 文件，使执行用户可读且不可改写。以下示例中的时间、摘要、目标和引用都取本次实测值。确认发生在封存后，并在执行接管前 30 分钟内记录。证明按市场、快照、目标机器和状态路径绑定。文件 `reference` 应指向已留存的 systemd 核查、Azure 控制面证据或券商撤销记录。

```json
{
  "market": "binance",
  "source_machine": "<源机器的 /etc/machine-id>",
  "target": {
    "machine_id": "<目标机器的 /etc/machine-id>",
    "state_root": "/var/lib/factorlab-demo/execute-state"
  },
  "snapshot_sha256": "<describe 返回的 SHA-256>",
  "mechanism": "source-sealed-and-units-masked",
  "observed_at": "<有 UTC offset 的核查时间>",
  "reference": "<维护记录 ID>"
}
```

5. 使用目标已准备的 systemd credential 运行接管。Binance 设置 `FACTORLAB_DEMO_SECRET_FILE=%d/demo-secret` 并按原 unit 传入 `LoadCredential=demo-secret:...`，Alpaca 使用 `FACTORLAB_ALPACA_SECRET_FILE=%d/paper-secret` 及对应 credential。可以用 `systemd-run --wait --pipe --collect -p User=<执行用户> -p UMask=0077` 加上这些属性运行。

```bash
python -m factorlab.integrations.disaster_recovery take-over \
  --backup "$VERIFIED_COPY" --destination "$TARGET_STATE" \
  --revision "$DEPLOYED_SHA" --configuration "$LAUNCH_CONTRACT" \
  --fence-file /etc/factorlab-dr/ownership-proof.json
```

6. 只有退出码为 0 且回执有 `published_for_execution=true`，才解除目标 unit 的 mask。先读回执中的余额差异、开放订单、预算和控制状态，记录首次正常恢复的时间。已保存的 STOPPED/HALTED/PAUSED 仍由原应用执行，启动接管不会发出恢复策略的控制命令。确认源端的隔离持续有效后，再恢复目标侧邮箱生产和周期备份。账户写入使用运行时原有自动化授权。

## 源端意外退出

1. 将目标服务继续保持 masked，记录检测时间。在目标的独立调查目录运行以下命令，从快照水位开始做 GET 追赶。C07 旧快照需要显式提供 `--legacy-since <可信 UTC 水位>`。使用与计划内切换相同的凭证注入。

```bash
python -m factorlab.integrations.disaster_recovery recover \
  --backup "$VERIFIED_COPY" --destination "$INSPECTION_STATE"
```

2. 无法访问 SSH 只说明观测失联。由 Azure 操作员根据确切 VM 资源 ID 完成 `az vm deallocate --ids "$SOURCE_VM_ID"`，通过 `az vm get-instance-view --ids "$SOURCE_VM_ID"` 取得 `PowerState/deallocated`。用现有的 Azure 权限或变更控制保证退役 VM 无权自动重启，并保留证据。受控取回最新内部账本时可在写入面持续隔离的条件下获取最新磁盘状态，以相同方式做最终快照。Alpaca 也可用已查实的原有纸面账户密钥撤销加同账户的新密钥；Binance 当前绑定基于原 API key，优先使用基础设施隔离。[Azure VM power states](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/tutorial-manage-vm#vm-power-states)
3. 账本追赶可以重建官方可见的订单、增量成交、资金费或公司行为。任何没有可信归属、资金或预算证据的差异会阻止发布。只有周期快照、且未获得最后内部状态时，继续使用隔离调查目录。例如，即使当前现金和仓位与快照一致，停机前也可能发生过暂停命令、撤单预算预留或划转。接管要求最终快照，或将来交付具有完整水位证明的内部增量日志。

原节点重新上线时，先检查它仍被基础设施围栏隔离，且本地执行 unit 保持 masked；进入业务网络前先作为只读灾备接收端重新初始化。保留旧根目录供审计。恢复中途失败应保留调查副本和异常证据。对账通过之前维持原有 quarantine。
