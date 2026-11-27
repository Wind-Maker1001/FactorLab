# P10-05 FactorLab 双节点部署与在线验收

目标 `origin/main` 为 `864671649626993bce84629dc9f64d3cc4e3f015`。现场操作于 2026-11-27 开始，报告更新至 2026-11-28（Asia/Shanghai）。[机读证据](evidence.json) 使用 UTC 时间和脱敏的模拟账户回执。

## R01 存量 V69 迁移

在原分支和 PR #277 上从 `53549906c8e08f807c53d57ad45774d243910c97` 继续。活动 V69 的包源码、配置、模型身份字段、策略声明和证据维持原值。变化项是 `LoadedStrategy.runtime_identity()` 所覆盖的执行器源码。旧程序首次绑定时保存了整个策略运行绑定，所以新版在进入实例恢复前会拒绝该摘要。

迁移工具在现有 Alpaca owner lock 下，让前后两个已安装的解释器分别加载同一原生活动包。它核对标准 V69 入口、完整包清单及绑定，以及包外实际导入的 V69 计算源码、Python 和数值依赖。SQLite 用 `BEGIN IMMEDIATE` 追加该活动包对新执行器的准入，原始绑定继续供旧版代码使用。未提交决策的冻结事件需要原运行时先完成。已经提交的冻结收据沿原 ID 恢复。处理过程中继续使用原策略 checkpoint、订单及经济账本。

隔离复演使用了云端实际活动包 `d5271af9…` 和其持久部署绑定，资金、持仓、订单账本由隔离用投影提供。旧版启动通过，未准入的新版复现原拒启，追加准入后新版启动通过。重复迁移没有追加第二条记录。旧二进制在同一投影账本上回滚成功，原绑定和 checkpoint 保留。定向回归 46 项通过，完成审查反馈后的迁移专项通过。专项覆盖了准入事务中断、模型身份变化拒绝和已冻结计划的同身份重放。

影响面检查确认 Alpaca 外部 V2 与 Binance 外部 V2 同样经过 `InstanceDecisionRuntime` 的持久绑定。两者仍需各自针对活动包的显式迁移，R01 的运维工具只针对原生 V69。Binance 现役网格使用 legacy-grid。Binance V1 的 DecisionJournal 另有自己的持久绑定，R01 不切换在役策略。

### 当前状态

| 项目 | R01 |
| --- | --- |
| `V69_RUNTIME_MIGRATION` | `ACCEPTED`，实际活动包和原账本已在新版恢复 |
| 部署 | `DEPLOYMENT_ACCEPTED`，马来西亚三个组件均运行在 `80d22fa23b2bedd557684dd4db9559e6180ffb9e` |
| 双市场 | `PARTIAL`，两市场生产模拟服务均健康。Alpaca 新版首次自然周调仓待观察 |
| 资源 | `RESOURCE_ACCEPTED`，覆盖目前同版负载和实测备份窗口 |
| 灾备 | `NOT_VERIFIED`，双向受限 SSH 验证已完成。异地盘加密回执仍受 Azure 权限阻塞 |
| `SOAK_24H`、`SOAK_72H`、`SOAK_7D` | `IN_PROGRESS`，同版 T0 为北京时间 2026-11-28 03:17:37 |

Alpaca 从开始停机到新版由 systemd 启动用时 42.182 秒，首个健康心跳在维护开始后 105.122 秒。账户权益和现金均为 100000.05 USD，交易流及 IEX 行情流连接成功。官方现金查询区间已持久化。原 V69 包仍是 active 和 stable，准入仅一条。随后滚动对齐 Binance 和 Observer，Binance 同账本恢复用时 9.263 秒，累计 POST 消耗 147 次、名义额 917.5849000 USDT，重启前后一致，`UNKNOWN=0`。沿用首轮 QQQUSDT/SPYUSDT 官方成交回执。前一轮混合版本窗口在此次 V69 维护开始时结束，累计约 3 小时 45 分钟。

### 新备份、异地链路和资源

Alpaca 新版备份 `backup-20261127T191227Z` 为 844,318,466 字节，带官方现金锚点。备份总耗时 182.871 秒，Alpaca 服务中断 131.716 秒。Binance 新版 C07 备份 `backup-20261127T191530Z` 为 30,523,283 字节，14 个库通过完整性和恢复演练，执行中断 2.374 秒。两个备份串行运行，实测 Alpaca 备份 cgroup 内存峰值 356.3 MiB、Swap 峰值 72.1 MiB，Binance 分别为 284.6 MiB、6.5 MiB。Alpaca 备份中的主机可用内存采样为 463,444 KiB。

日本已安装同一新版 release，执行和邮箱生产单元仍保持屏蔽。两端已准备受限 rrsync 账户和独立的市场密钥，正反向只读列表均验证通过。两端准备了同一内容的启动合同。日本接收路径在 `/dev/sda1` 的 ext4 Azure 托管 OS 磁盘上，接收前可用 59,064,213,504 字节。Alpaca 844 MB 副本低于 2 GiB 接收上限，按当前体积保留两份加一份暂存约需 2.53 GB。Binance 副本低于 256 MiB 上限。北京时间 03:18:25 的控制面复查仍对两块磁盘返回 HTTP 403。待现有身份获得磁盘范围的 `Microsoft.Compute/disks/read` 后，从首次验证性拉取继续。跨区 RPO、RTO、账本差异和回切仍待实测。

同版 T0 采样中，主机可用内存 438,136 KiB。Binance、Alpaca、Observer 的 PSS 分别为 56,233、56,966、69,936 KiB，进程 Swap 分别为 84、30,996、0 KiB。备份中到 T0 的主机累计换入增加 24,669 页，换出增加 18,867 页，`oom_kill` 增量为零，三个单元自动重启数均为零。Alpaca 行情库从混合版本末次采样的 707,502,080 字节增长到 842,973,184 字节。新版七天窗口在市场流启动时应用，此次最早行距 T0 约三天，SQLite freelist 为零。跨七天的删除与空间复用、多策略 Worker 并发峰值和自然周调仓仍在观察范围内。CPU 积分查询需要单独的指标访问权限。

在马来西亚隔离恢复了两个新周期副本。Binance 用 32 次官方 GET 完成追赶，官方阶段 6.234 秒。Alpaca 用 11 次官方 GET，含恢复复制全程 119.631 秒，现金锚点可用。两市场均返回 `reconciled=true`、`budget.verified=true`，现金残差为零，周期副本继续隔离。该段观察结束于北京时间 03:20:48，同版窗口已记 191.758 秒，生产进程 PID 保持稳定。期间主机换入 8,717 页、换出 27,386 页，OOM 增量为零，Alpaca 行情文件物理大小保持 842,973,184 字节。24h、72h、7d 的时间门槛分别是北京时间 11 月 29 日、12 月 1 日、12 月 5 日的 03:17:37。

### 同账本回滚

R01 的旧 Alpaca release 路径在主节点 `/root/p10-05-r01/prior-alpaca-release`。停止 Alpaca 服务与备份 timer，将 `/opt/factorlab-alpaca/current` 原子切回该路径，再启动 Alpaca。现有 SQLite 和原 V69 绑定可直接使用。若同时恢复旧版备份入口，将 `/root/p10-05-r01/prior-alpaca-backup-overlay.conf` 装回原 Alpaca 备份 drop-in 目录，执行 daemon-reload，再恢复备份 timer。R01 滚动前的 Binance 和 Observer unit 与旧链接目标保存在同一目录，可按下文的代码回滚流程使用。

## P10-05 首轮现场记录

| 验收项 | 状态 | 实际范围 |
| --- | --- | --- |
| 部署 | `DEPLOYMENT_BLOCKED` | 主节点 Binance Execute、TradFi Observer 及其运维入口已切换。目标版 Alpaca 拒绝现存 V69 运行时绑定，已用当前账本回到健康的上一版。 |
| 双市场 | `PARTIAL` | Binance Demo 完成四笔官方模拟成交和同账本重启。Alpaca Paper 官方账户、行情和交易流均恢复，V69 等待下一自然调仓。 |
| 资源 | `RESOURCE_ACCEPTED` | 当前混合版本的双市场与 Observer 实测负载。完整目标版本、峰值 Worker 与长期容量待观察。 |
| 灾备 | `NOT_VERIFIED` | 日本已安装原生目标环境并获得两市场官方只读回执。写入单元保持持久屏蔽，未执行所有权转移。 |
| `SOAK_24H`、`SOAK_72H`、`SOAK_7D` | `IN_PROGRESS_MIXED_REVISION` | 现存健康单元持续运行，三组件同版窗口为 `NOT_STARTED`。 |

## 发布与备份

马来西亚 VM 为 2 vCPU 的 `Standard_B2ats_v2`。部署前，Binance、Alpaca 和 Observer 三个进程均指向 `12335ebd56fda7a2d0e709176806d96e3e7b7b67`。Binance 启动参数、策略部署、健康和每日报告入口已指向 `/opt/factorlab/releases/864671649626993bce84629dc9f64d3cc4e3f015/venv/bin/python`。Observer 已由其独立 `current` 链接启动目标版环境，保留现有邮箱与状态目录。Alpaca 用其安装器建好并试启目标 release，随后将 `current` 回指 `12335eb…`。三个执行进程均通过各自 systemd 单元运行，使用原服务身份、`ProtectSystem=strict`、`ProtectHome=true`、`PrivateTmp=true` 和原有 root 私有 credential 文件。

日本 VM 为 2 vCPU 的 `Standard_B2pts_v2`。三个应用均已安装 `8646716…`，三个执行或邮箱生产 unit 都是 `masked`，PID 为 0。原 ARM64 CPython 3.13.18 路径解析到登录用户私有目录，现已将该原生解释器放在 root 所有的 `/opt/factorlab/python` 下。服务身份在 `ProtectHome=true` 中已成功调用解释器和官方 API。两节点均使用原生 CPython 3.13.18。

| 环境 | 最小依赖 |
| --- | --- |
| Binance | `tzdata==2026.6`、`websockets==16.1.1`。实际 release 的入口导入探针未加载 pandas、NumPy、PyArrow 或研究包。 |
| Alpaca | `numpy==2.3.5`、`pandas==2.3.3`、`pyarrow==22.0.0`、`python-dateutil==4.0.0`、`pytz==2026.6`、`tzdata==2026.6`、`websockets==16.1.1`。 |
| Observer | `exchange_calendars==4.13.3`、`korean_lunar_calendar==0.4.0`、`numpy==2.3.5`、`pandas==2.3.3`、`pyluach==2.3.0`、`python-dateutil==4.0.0`、`pytz==2026.6`、`toolz==1.2.0`、`tzdata==2026.6`。 |

Binance 升级前 `backup-20261127T150003Z` 为 30,024,749 字节，14 个 SQLite 库通过完整性和隔离恢复检查，覆盖账户绑定、控制及预算、订单、资金费和邮箱。维护中断 6.589 秒。Alpaca 升级前 `backup-20261127T150614Z` 为 574,585,452 字节，停机捕获 128.954 秒，之后对 6 个 SQLite 库做隔离恢复，用时 83.702 秒。

修复了现场复现的两个快照问题。SQLite Backup API 会把 WAL 模式带入副本，随后完整性读取生成 sidecar，导致异地清单校验失败。现在在复制连接关闭前把副本切换为 DELETE 模式。C07 的恢复演练原先还会在已发布副本创建 `session.lock`。带恢复清单的副本现通过既有不可变恢复入口。两项修改只走备份组件的运维覆盖 `/opt/factorlab/p10-05-staging/src`，三个交易和观察进程从各自 release 取解释器。最终 C07 快照 `backup-20261127T152324Z` 为 30,101,395 字节，C07 恢复演练与 P10-09 的完整清单校验均通过。Binance 维护中断 4.534 秒，Alpaca 在该维护窗口内保持健康。两项修复应随本 PR 进入下一次正式发布。

## 在线执行与资本风险

Binance 原 NVDAUSDT 1 分钟网格继续使用既有持久状态。挂单 `196451740` 在升级和备份重启后以原客户端 ID 回到 `NEW`，`UNKNOWN=0`。随后策略自然撤单，官方 GET 确认该单为 `CANCELED`、成交量为 0，并在官方订单历史中观察到后续两笔自然挂单及撤单。TLS 签名 REST 回读和官方 Demo WebSocket 的 NVDAUSDT 1 分钟 kline 均有实测。验收使用既有 20 USDT TradFi 分配和原 TaskMailbox。先提交目标，再等 Execute 自行下单及确认。OPEN 的首次对账因账户读取集合超过五秒而暂缓，随后完成。CLOSE 使用现有授权下的减仓单，两个验收标的最终持仓均为 0。

| 标的 | 官方 Demo 买单 | 官方 Demo 减仓单 | 数量 |
| --- | ---: | ---: | ---: |
| QQQUSDT | 66762421 | 66762885 | 0.01 |
| SPYUSDT | 66637963 | 66638303 | 0.01 |

以上订单均通过签名 `GET /fapi/v1/order` 和 `GET /fapi/v1/userTrades` 核实为 `FILLED`，合计费用 0.01092248 USDT。钱包由 4999.41385672 变为 4999.40753423 USDT。另从官方 income 接口回读 NVDAUSDT 历史资金费，本次 QQQ/SPY 验收窗口的资金费为零。全局累计 POST 由 141 增至 145，名义额由 878.4848000 增至 905.7905000 USDT，额度和 250 USDT 最大损失限额保持原值。两笔验收均为 `COMPLETED`，含历史任务共 4 个终态与 13 条回执。自然周 Observer 仍等待目标配置，验收任务有独立标识。

Alpaca 官方 Paper GET 显示现金和权益均为 100000.05 USD、零持仓、零挂单，维护开始后返回零新成交。当前 V69 在原包上运行，交易流和市场流均已连接，等待 11 月 30 日 22:30（北京时间）开盘后的自然周窗口。目标版退出码为 78。在只读官方连接与隔离 SQLite 副本中复现了 `instance runtime identity changed`。包 ID、策略代码、配置、策略声明和证据相同，变化项是 `runtime_sha256`。启动在调用已有包晋升控制器之前就要求绑定原活动包。本次保留原 V69 Champion、原账本及其绑定。下一次切换需要有明确的运行时身份升级合同，涵盖冻结决策、活动包、旧绑定和可回滚性。

线上当前覆盖了 Binance 带挂单恢复、账户级预算连续、官方挂单保证金、减仓、邮箱归属和官方费用。Alpaca 运行的 V69 是原全账户模式，当前组合库没有已配置的动态资本账户。正式账户的动态实例、内部划转、未准备资本恢复、组合风险高水位、同标的多策略、三类网格、部分成交和 V179/V182 带仓更新仍缺本次在线证据。危险边界由定向离线回归覆盖，包括动态资金、pending_capital、TQQQ 经济敞口和两市场灾备 HTTP 夹具。V179/V182 工件装载和相关轻量路径从本机运行测试，共 34 项通过。测试未改变金融准入或 Champion。

## 异地恢复

日本通过持久屏蔽的执行单元和独立 `LoadCredential` 授权访问两家官方模拟 API。Binance 和 Alpaca 的签名只读请求均成功。两把独立 SSH 密钥分别通过主节点的强制 `rrsync -ro` 访问对应备份目录，实测 `--list-only` 成功。备份接收 unit 和失败 journal 告警已安装，接收 timer 已错开到每小时 :45 和 :55。

`/var/backups` 位于日本节点的 Azure 托管 OS 磁盘，现有托管身份读取该磁盘的控制面加密类型返回 HTTP 403。P10-09 需要该回执以验证异地落盘条件。主节点目前仍是两市场唯一账户写入者。跨区传输与日本侧的隔离追赶待加密回执。计划内接管及反向切换还需先解决 Alpaca 运行时绑定升级。

在马来西亚对最后一份 Binance 周期备份做了官方隔离追赶。44 次 GET、零写请求，追赶阶段 10.223 秒，现金和持仓残差为零。官方历史有两笔在快照之后派发并撤销的 NVDAUSDT 订单。它们的内部归属和累计预算更新不在快照内，程序返回 `reconciled=false`、`budget.verified=false`。Alpaca 用升级前副本做同类隔离追赶，隔离恢复和 9 次官方 GET 共耗时 77.344 秒。预算核验通过，现金因旧版报表缺少官方请求时间区间而无法建立基线，返回 `OFFICIAL_CASH_ANCHOR_MISSING`。两份周期恢复均保留隔离。跨区增量字节、转移中断、RPO、RTO 和接管前后账本差异尚无实测。 [P10-09 运行手册](../p10-09/RUNBOOK.md)

## 资源与持续观察

混合版本运行窗口从北京时间 11 月 27 日 23:23:57 起算，到 11 月 28 日 02:43:59 已运行 3 小时 20 分钟。主机 `MemAvailable` 为 498,808 KiB、根盘可用约 30.0 GiB。Binance、Alpaca 和 Observer 的 PSS 分别为 40,899、31,054、19,956 KiB，进程 Swap 分别为 23,600、88,520、38,436 KiB，累计 CPU 时间分别为 443、237、10 秒。三单元自动重启计数均为 0，观测区间内无 OOM。Binance 以 1 分钟 K 线决策。两个执行宿主各用一个 Worker 的默认上限，Observer 不启动策略 Worker。已有采样未命中存活的策略 Worker，决策延迟和峰值资源需在实际多实例和自然周调仓时测量。

最后一次备份重启后，Binance 进程记录 41,514 次 GET、2 次 POST 和 3 次 DELETE。八个错误分类为时钟偏差、账户读取过期和订单未找到，抽样未见 429。Alpaca 行情库从 14:57 UTC 的 564,289,536 字节增长到 18:43 UTC 的 707,502,080 字节，Observer SQLite 同期增加约 2.6 MB。该段包含开市流量和维护。目标版 Alpaca 的行情保留机制尚未进入当前运行链路。Azure CPU 积分和控制面使用率查询返回 HTTP 403。主节点沿用分钟健康定时器、每日报告、Binance C07 与 Alpaca 一致性备份定时器。混合版本 24 小时、72 小时和 7 天窗口分别在北京时间 11 月 28 日、11 月 30 日和 12 月 4 日 23:23:57 到点，三者均在观察中。跨周末与 11 月 30 日自然调仓的结果待现有服务积累。

## 使用当前账本回滚代码

主节点升级前的有效 unit 和 drop-in 已保存至 `/root/p10-05/prior-factorlab-units.tar`，原 Alpaca 和 Observer 链接目标分别保存在同目录的 `prior-alpaca-release` 和 `prior-observer-release`。确认需恢复的 release 支持当前账本 schema 后，使用以下入口。

- **Alpaca，已实际执行。** 停止 `factorlab-alpaca-paper.service`，用 root 将新链接指向 `cat /root/p10-05/prior-alpaca-release`，用 `mv -Tf` 原子替换 `/opt/factorlab-alpaca/current`，然后只启动原服务。用 health 核对账户恢复及 V69 实例。
- **Binance。** 停止策略部署与备份 timer，等待已启动的维护 unit 结束。停止 Observer 和 Execute，取归档中的 `factorlab-demo-execute.service.d/20-release.conf` 还原原发布入口。同步恢复归档中的策略部署、两个健康入口、备份及每日报告覆盖。将新加的 `factorlab-demo-backup.service.d/50-p10-05-revision.conf` 移出 systemd 目录，恢复原备份 unit 后运行 `systemctl daemon-reload`。启动 Execute、等待账户对账，再启动 Observer 与原 timers。
- **Observer。** 保留同一观察库和 TaskMailbox。恢复归档的 `factorlab-demo-tradfi-observer.service.d/40-formal-release.conf` 和 `factorlab-demo-tradfi-health.service.d/40-formal-release.conf`，将其 `current` 链接原子切回原指向。执行 `systemctl daemon-reload` 后重启 Observer 和健康 timer。

代码回滚使用现存状态根。旧备份仅用于独立目录的隔离恢复。跨区回切使用最终快照和所有权发布流程。
