# P10-05 FactorLab 双节点部署与在线验收

目标 `origin/main` 为 `864671649626993bce84629dc9f64d3cc4e3f015`。现场操作于 2026-11-27 开始，报告更新至 2026-11-28（Asia/Shanghai）。[机读证据](evidence.json) 使用 UTC 时间和脱敏的模拟账户回执。

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
