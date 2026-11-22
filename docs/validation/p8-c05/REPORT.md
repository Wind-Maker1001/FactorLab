# P8-C05 Linux 官方 Demo Execute 状态交接

**最终状态 `CLOUD_NOT_DEPLOYED`，2026-11-22（Asia/Shanghai）。**

本次以 `origin/main` 的 `bfdd4a9b9e1fc7769b3957a82bbc92f0975d92da` 为基线，包含 F10 资金费账本、F11 BREAKOUT 退出以及 F12 增量恢复。当前环境没有发现可用于部署的远程 Linux 主机及管理登录入口。已识别的 Linux 是本机 Debian WSL2。Windows 正式 Demo 账户和 F13 的状态未进入切换窗口。

| 项目 | 实际记录 |
| --- | --- |
| 云主机、Linux、systemd、Python | 云端未部署。本地预备环境为 Debian 13.7、WSL2 内核 `6.6.87.2-microsoft-standard-WSL2`、systemd 257 和 Python 3.13.18。 |
| 部署提交与服务状态 | 云端无已安装提交。本地 WSL2 的 Execute unit 未安装，GET-only unit 为 inactive，已有 health timer 为 active。 |
| Windows 到 Linux 的实际状态一致性 | 未迁移正式状态。真实账户的身份绑定、Grid、行情游标、订单、成交、资金费及覆盖水位、UNKNOWN、Campaign 和累计预算没有跨主机核对结果。 |
| 实际 Binance Demo GET | 未发起切换对账。账户开放订单、成交、资金费、仓位与权益的云端观测待取得。 |
| 持续授权、预算及控制状态 | 尚未交接。预备流程以现有授权和 Grid 的账本为输入，并在目标激活前校验。 |
| 自动重启与运行记录 | 云端尚无 systemd 自动重启、订单或健康记录。24 小时、72 小时和七天的观察均未开始。 |

## 已准备的交接路径

Windows 将原账本中的所有 SQLite 数据库作为同一份冻结快照导出，并从 Credential Manager 读取非密钥账户绑定。发布交接包之前，源状态目录先持久封存，既有 STOP、HALTED 和 PAUSED 不被改写。Linux 将交接包限定到预先指定的主机与状态路径，核对快照后，使用原 systemd 凭据通过现有仅 GET 会话对账。目标路径只在对账完成后发布。普通 restore 保持原有 quarantine。

部署准备、切换顺序、`LoadCredential` 瞬态导入命令和观测证据位置见 [云端运行手册](../../binance-demo-cloud-runtime.md#transfer-an-existing-windows-execute-owner-to-linux)。当前部署依赖可管理的云主机、持久本地磁盘和可通过系统凭据方式供应的原 Demo 密钥。满足这些部署条件后，先准备接收端，再确认 Windows Execute 和重启入口均已停止，然后按串行流程移交唯一执行权。

## 软件验证所覆盖的故障

离线验证使用合成账户和被测试夹具拦截的 HTTP。验证覆盖发布中断后沿用原封存包、源端拒绝重新取得执行锁、目标路径及内容校验、GET 失败时目标路径保持缺失、以及对账后保持原累计预算与 PAUSED、STOPPED、HALTED。UNKNOWN 使用原订单 ID 恢复，夹具中的 POST 数保持不变。

Debian WSL2 上以 Python 3.13.18 运行 Execute、云存储、云运行时和凭据四组测试，80 项通过。Windows 上以 Python 3.12.7 定向实测了导出、封存及发布重试，1 项通过，4 项 Linux 接收测试按平台跳过。测试覆盖的是本地软件行为。

## F01 Windows Fast CI 测试收集修复

PR #228 初始提交 `2b8f8fc` 的 Windows Fast Python 3.13 收集了 32 项测试，28 项通过、4 项跳过。四项 Linux 接收场景由 `skipif` 标记，仍进入 Windows 收集清单，可信 targeted 回执因此记录 `required_execution_observed=false` 并判定 FAIL。[原 Fast CI 运行](https://github.com/Wind-Maker1001/FactorLab/actions/runs/45031466384/job/137235953145)

两个 Linux 接收测试现在仅在 `sys.platform == 'linux'` 时定义，原测试断言、交接实现及可信验证规则保持原样。

| 本地验证 | 修复后结果 |
| --- | --- |
| Windows，Python 3.13.9，原 Execute 测试文件 | 28 项通过，0 项跳过。 |
| Windows，原可信基线的 `integration_checks.py --suite targeted` | `targeted PASS`，28 项通过，0 项跳过、失败或错误，`required_execution_observed=true`。 |
| Debian 13.7 WSL2，Python 3.13.18，Execute 与 C05 集成回归 | 80 项通过。PAUSED、STOPPED、HALTED 和错误目标／篡改快照共四个 Linux 专属场景均收集并执行。 |
