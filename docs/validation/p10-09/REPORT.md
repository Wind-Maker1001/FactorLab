# P10-09 FactorLab 双市场异地灾备验证

2026-11-27，Asia/Shanghai。基线为 `388a8ee0f28e4b90ee7dbb20403a29c7fe085898`，独立分支为 `feat/p10-09-disaster-recovery`。订单演练使用合成账户和拦截的官方 HTTP 请求。运行步骤、密钥部署和单主隔离方式见 [灾备运行手册](RUNBOOK.md)。

## 恢复状态和对应证据

| 必须恢复的状态 | Binance Demo | Alpaca Paper |
| --- | --- | --- |
| 账户绑定和代码身份 | `binding.json`、协议及运行时账户绑定。快照记录源主机和部署 Git SHA。 | `account.sqlite3` 的 Paper account ID 和 `runtime.sqlite3` 中的报告及启动记录。定时捕获从当前发布目录读取 Git SHA。 |
| 原始订单、成交、费用 | 账户 journal 的 `protocol.sqlite3`、`controller.sqlite3` 与 `runtime.sqlite3`。原派发意图、交易所订单 ID、累计成交和资金费覆盖水位均参与追赶。 | `account.sqlite3` 的 E01 原始回执及 `portfolio.sqlite3` 的 E03 执行计划、原客户端订单 ID、持仓和成交。费用依官方活动身份留存，只有有归属证据的经济事件才进入既有账本。 |
| 现金、持仓、归属、已结算资金费和公司行为 | 已对账的 wallet/position 基线、交易所成交费用、已实现损益与资金费事件。未经验证的资金流单独保留。 | 已对账现金报告、官方成交、活动原件、公司行为 journal 和 sleeve 归属。公司行为通过原 entitlement 与执行状态接线。 |
| 累计订单、名义金额、损失预算与退出义务 | 连续授权、累计计数、预算和退出预留，以及 Grid/策略执行的待退出状态。既有风险限制在接管后继续生效。 | 原 PortfolioStore 订单成本、成交、买入或退出预留、已配置的 capital policy 和暂停状态。接管读取同一风险账本。 |
| 策略实例、包版本、检查点、水位和待执行决策 | `grid-supervisor.sqlite3`、`strategy-execute.sqlite3`、`strategy-deployment.sqlite3`、`portfolio.sqlite3` 及市场/调度库，连同标准 `strategy-releases` 的受验证包文件。 | `deployments.sqlite3`、`runtime.sqlite3`、`portfolio.sqlite3`、市场/调度库和标准 `strategy-releases`。实例启动参数和根目录之外的资产由 launch contract 证明。 |
| 外部 TaskMailbox | 现有 C07 使用 SQLite Backup API 冻结邮箱数据库，存入 `external/task-mailbox/tasks.sqlite3`。接管前停止产生和消费邮箱消息的源端服务。 | 基线部署的 Alpaca Paper 执行入口使用 PortfolioStore 和实例部署库，外部 TaskMailbox 属于 Binance 工作流。 |
| 停止、暂停、熔断 | 源封存独立于原应用状态。目标继续读取原运行时和策略控制状态。 | 接管复用原资本策略和实例状态。正常 cloud owner lock 拒绝封存及 quarantine。 |
| 动态资本事件 | 捕获已存在的 SQLite 库。P10-07 的事件身份、增量水位和重放接线发布后需要用其合同验收。 | 同左。历史账户活动可以证明对外资金流，内部 sleeve 转移和调拨授权需要原账本。 |

C07 原 SQLite Backup API 仍负责跨库快照，异地传输只消费已发布快照。策略包文件按原包清单捕获，绑定文件拒绝凭证字段，独立 systemd 密钥目录不参与快照。传输校验覆盖完整文件清单、SHA-256 和 SQLite。恢复从验证过的工件生成单独状态目录，备份保留原样。

## 执行接管

正式发布需要三项证据同时成立。源执行用户在最后一个一致性切面持久封存，最终快照绑定目标机器、路径、代码及启动配置摘要；目标确认管理员出具的持久隔离证据与该快照相符；两个市场各自完成只读追赶、差异归属和风险账本核验。只有通过这一序列，程序才把对账后的暂存目录原子发布到执行路径。正常运行从原控制状态启动，已耗尽的预算和未完成退出依旧由原风险机制约束。

源端意外退出后，周期备份可以重建官方可见的订单、成交、资金费和公司行为，但它无法证明备份后发生的内部调拨、预算预留、暂停及熔断。当源盘可恢复时，操作员可以先隔离旧执行者，再取回最新内部状态，生成最终快照并走同一接管流程。当只有周期备份时，目标保留检查目录及 quarantine。

最小增量异地日志应覆盖原始派发意图及已消费预算、控制事件、资本授权及账户内划转、退出义务、策略决策与邮箱确认。序号必须与所属 SQLite 事务共同提交，使快照水位和异地持久确认之间没有缺口。若希望在源盘永久丢失后也能按计划接管，产生外部副作用的路径需要在提交前获得异地持久确认。仅异步复制能缩短内部状态丢失窗口，无法证明窗口为空。当前跨多个库的控制、邮箱与执行链路没有共同的可证明序列，复制原始 WAL 也无法补上这个合同。

## 离线演练

| 场景 | 观测结果 |
| --- | --- |
| 计划内停机迁移 | 旧状态封存后拒绝锁，目的地账本差异阻止发布。对账成功的夹具保留 HALTED、累计 19 次消耗、原退出义务和持久决策。 |
| 异常退出与原节点重新上线 | 周期备份即使官方事实一致也只能隔离恢复。重新取源状态的原执行锁继续被拒绝。管理员不可控的证明路径被拒绝。 |
| 新增成交、在途部分成交、POST 响应丢失 | Binance 和 Alpaca 的真实适配器在捕获的 HTTP 夹具下沿用原订单 ID，仅使用 GET；后续回执推进实际累计成交，重复追赶保持成交总数及成本。 |
| 资金费及公司行为 | Binance 测试覆盖资金费，以及同一事件重复追赶保持经济结果。Alpaca 使用现有公司行为归属，定向回归覆盖迟到收据和分拆产生的子标的。 |
| 策略决策持久化后尚未提交 | 发布夹具保留原决策和原 ID，GET 追赶不执行该决策。 |
| 资本转移期间故障 | Binance 的外部转移无法被余额基线吸收，重试继续阻止接管。内部划转夹具在官方总额无变化的情况下保留隔离。 |
| SQLite 损坏 | 即使篡改后的 SQLite 仍可打开，内容摘要不符会阻止恢复和发布。 |
| 异地传输中断 | rsync 测试保留旧副本，按同一暂存路径续传。新副本通过校验后才替换保留集合。 |
| STOPPED/HALTED | Binance 追赶测试覆盖两个状态。原授权消耗和退出保留。 |
| 跨架构 | CI 将 x64 生成的两市场快照作为同一件 tar 工件送到原生 Linux ARM64，由 ARM64 验证同一组文件并检查 SQLite 中的控制、现金、预算和原订单 ID。 |

Windows 的 C07 和 Binance 定向回归共 45 项通过。Linux x64 的七组定向测试共 65 项通过。原生 ARM64 的结果以本 PR 的 `FactorLab disaster recovery offline` 工作流为准。可信 PR 验证仍走仓库 `FactorLab PR validation` 的基线定义。期间不需要 Azure 凭证或真实账户。

```bash
python -m pytest -q tests/integrations/test_disaster_recovery.py \
  tests/integrations/test_offsite_backup.py tests/integrations/test_binance_disaster_recovery.py \
  tests/integrations/test_alpaca_disaster_recovery.py tests/integrations/test_demo_cloud_storage.py \
  tests/integrations/test_alpaca_cloud_runtime.py tests/integrations/test_alpaca_cloud_execution.py

export PYTHONPATH=src OPENBLAS_NUM_THREADS=1
export PROBE_ROOT="$(mktemp -d)"
taskset -c 0,1 python tools/ci/disaster_recovery_portability.py export \
  --root "$PROBE_ROOT" --revision "$(git rev-parse HEAD)" --payload-mib 12
taskset -c 0,1 python tools/ci/disaster_recovery_portability.py restore \
  --root "$PROBE_ROOT" --payload-mib 12
```

## 性能和恢复时间

本机 Debian WSL2 x86_64、Python 3.13、SQLite 3.53.1，限制 CPU 亲和性为两核。每个市场使用 12 MiB 的输入行，SQLite 页开销后的备份约为 16 MiB。

| 阶段 | Binance | Alpaca |
| --- | ---: | ---: |
| 备份字节 | 16,826,851 | 16,826,850 |
| 创建并发布快照 | 1.013 s | 0.390 s |
| 验证并恢复至隔离目录 | 0.428 s | 0.030 s |

两个市场顺序捕获的探针共使用 0.269 CPU 秒、155.1 MiB 进程峰值 RSS。顺序验证及恢复使用 0.188 CPU 秒、147.0 MiB 峰值 RSS。首次调用包含 Python 模块加载和缓存冷启动。该实验给出快照阶段的成本，全天两市场并行运行的 VM 容量需要在目标部署验证。

本地以 rsync 3.5.0 配合 `rrsync -ro` 拉取两个真实 SQLite 工件，使用本地管道代替 SSH 连接。16,826,851 字节的首次副本耗时 0.179 s，接收 63,776 字节，发送 141 字节。只更新一个 SQLite 记录后，第二次拉取耗时 0.181 s，接收 902 字节，发送 24,778 字节。计数包含 rsync 的发现、试传和传输协议。两份已核验副本合计 33,653,702 字节，子进程合计使用 0.229 CPU 秒。夹具重复负载的压缩率较高，不能外推实际生产账本的压缩率。

独立计时的官方 HTTP 夹具追赶，Binance 单次发出 20 至 21 个 GET，耗时 6.2 至 8.7 ms，POST 和 DELETE 均为零。Alpaca 单次发出 10 至 11 个 GET，耗时 0.9 至 1.7 ms。该请求数量对应上述单订单及短资金费窗口，耗时只计本机回执与账本重放。

在同一 Linux 环境运行真实 systemd 维护演练。Alpaca 调度器停止和重启隔离测试 unit，捕获 16,826,850 字节。停机窗口为 1.973 s，包含后续校验的全流程为 3.593 s，结束时测试 unit 为 active。新 systemd 定时与失败告警单位通过本机 `systemd-analyze verify`，用现有测试 Python 路径代替部署前尚不存在的路径。

RPO 按故障发生时距日本最新**已校验**快照水位的差记录。每日捕获加每小时拉取是配置周期，备份或传输失败会拉长实际窗口。RTO 从故障发现到唯一执行者恢复服务，包含外部隔离、必要的末次状态提取、传输、对账与服务启动。本次没有跨区维护窗口的时间样本，阶段测量可用于排查开销，云端 RPO、RTO、实际停机、官方 API 限流和双市场持续 RSS 待 P10-01/P10-02 协调场景实测。实际切换沿用 [运行手册](RUNBOOK.md)。

Binance 未成交的撤销或过期订单有较短的官方历史保留窗口。历史超界、发现无归属 ID 或与原现金基线有差异时继续隔离。Alpaca 的订单查询按提交时间过滤，活动按创建时间过滤，追赶同时复核旧挂单及期间成交，晚到的未归属费用或资金活动阻止接管。[Binance USD-M 交易 API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade) [Binance USD-M 账户 API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account) [Alpaca Paper 订单](https://docs.alpaca.markets/us/reference/getallorders-1) [Alpaca Paper 账户活动](https://docs.alpaca.markets/us/reference/getaccountactivities-2)
