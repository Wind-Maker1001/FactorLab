# P10-12 双市场多策略在线验收

## 基线

2026-11-28 08:06 UTC 的现场只读核实：马来西亚 `factorlab-demo-01` 为 x86_64，Binance Execute、Alpaca Paper 和 TradFi Observer 均为 active/running，systemd `NRestarts=0`。三个组件实际运行版本为 `80d22fa23b2bedd557684dd4db9559e6180ffb9e`，不是当前仓库基线 `47e58d9044e27dc47fc140f87b48b73531db6d3f`。#261、#262、#263、#264、#266、#268、#277 均已合并。

Binance 有效状态根为 `/var/lib/factorlab-demo/execute-state-r01-return`，Alpaca 为 `/var/lib/factorlab-alpaca-paper/r01-return`。Observer 使用 Binance 当前根内的 TaskMailbox。日本 `factorlab-arm-01` 为 aarch64；三个执行/观察单元均 masked/inactive、PID=0，Alpaca 与 Observer 发布链接指向同一 `80d22fa…` 版本。

Binance 当前是 legacy-grid，不是多实例验收结果。08:06:30 UTC 健康快照记录零持仓、零挂单、UNKNOWN=0；官方账户投影钱包和可用余额均为 4999.40753423 USDT。全局累计提交 149 次、名义额 929.3581000 USDT，原最大损失预算为 250 USDT。当前 NVDAUSDT Campaign 名义额上限为 7 USDT，数量上限 0.04，最多一个挂单，余下 4 次 POST，2026-11-28 10:57:20 UTC 到期。这些事实不构成 P10-12 官方成交验收。

## 当前验收状态

本次已完成公告输入修复的在线验证及隔离资本构造预检，尚未产生官方订单、成交或生产资本变更。逐项结果和原始回执关联见 [evidence.json](evidence.json)。健康快照中的历史成交不标为本次多策略生命周期证据。

现有多实例、动态资本、资本命令恢复和 V179/V182 定向回归为 60 项通过（275.13 秒），证据等级为 `OFFLINE_ACCEPTED`，不代表本次官方成交。新增只读采集入口为 `tools/p10_12_online_acceptance.py`，实际主机快照见 [runtime-baseline.json](runtime-baseline.json)。采集包含活跃 WAL 的账本计数与进程 PSS/RSS/Swap，不输出账户标识或凭据。

Alpaca 官方时钟确认休市，下次开市为 2026-11-30 14:30 UTC。现有 V69 健康心跳为 true，但执行状态为 `BLOCKED_INPUTS`，原因为 `SATURATED_CORPORATE_ACTION_WINDOW`，不能视为策略就绪。官方公告接口对 2026-11-20 至 2026-11-30 实际返回 1451 个不同 ID；两个子窗口分别返回 834 和 618 条，跨窗身份需要去重。部分非验收资产公告没有 ex_date。当前已实现共享日期分窗读取，仍保留无法拆分的饱和单日阻塞；36 项受影响模块与包准备回归通过（36.08 秒）。新函数在主节点通过只读官方接口取得 1451 个不同公告 ID，没有账户写入。该阻塞随后于 11:00 UTC 的部署解除，11:04:43 候选心跳确认原 V69 已进入 WAITING_NEXT_WEEK，详见部署记录。

Alpaca 计划从原账户预算内划出 100 USD，V179/V182 各 50 USD，余下保留给 V69，测试结束回收。原 V69 包、历史状态和 Champion 身份保持不变；资本迁移尚未执行。

Binance 原历史只读清点取得 80 个 NVDAUSDT grid deployment 和 7 个 QQQUSDT/SPYUSDT portfolio task，后者分属 `tradfi-demo-acceptance` 与 `tradfi-qqq-spy-test` 两个历史 allocation ID；账户 journal 目录内尚无 portfolio.sqlite3。现有导入入口是 `demo_cli capital-history-import --source ...`，要求独立完整经济来源，不能把两个历史 ID 未经核实地合并成一个 20 USDT 来源，也不能重复给两者各分配 20 USDT。TODO：核实两段原始资金来源及承接关系，沿既有迁移路径保留全部订单身份后再接入 V2。清点见 [binance-history-inventory.json](binance-history-inventory.json)，未执行导入或账户写入。进一步只读追踪原批次和账户快照确认：前一个 allocation 有 6 笔订单（4 笔 FILLED、2 笔零成交 CANCELED），后一个有 8 笔且全部 FILLED；两个 allocation 的 QQQUSDT/SPYUSDT 净归属均为零。前段 6 笔订单全部保存在原账户快照，但不在 demo_order_owners 投影中，因此仅扫描已有 owner 表会漏掉真实历史。原订单回执显示前段最后退出成交为 2026-11-24 15:51:00.143 UTC，后段首次买入成交为 15:53:13.279 UTC。原 static-shared-account 预算事件为 grid=7、portfolio=20 USDT；没有据此认定资金承接已经证明，亦未补写历史 owner。2026-11-28 12:01:47 UTC 以只读 Demo userTrades 按原订单回读，12 笔实际成交数量全部与原快照相符；前段手续费 0.00539527、已实现损益 -0.07130000 USDT，后段手续费 0.02173584、已实现损益 -0.01340001 USDT。净交易过账分别为 -0.07669527 与 -0.03513585 USDT，未混入资金费或资本划拨；原始 trade ID 摘要见 [binance-official-portfolio-history.json](binance-official-portfolio-history.json)。这些是历史成交核实，不是本次新成交验收。

## 共享风险修复

显式账户总标的限额实现中：`CapitalPolicy.configure(symbol_limits=...)` 持久化显式来源，实例仍受自己的资本和风险限额约束；没有显式配置的标的保留原最小 owner 上限规则。新增、重配与重启均不得提高已持久化的账户标的上限。Alpaca 配置接入该映射，准备入口要求提供独立核实的完整资产限额，不从账户现金推导。32 项共享资本回归通过（28.01 秒），覆盖实例/聚合超额拒绝、在途及不确定订单预留、合法减仓、重启和已持久化上限不可提高；15 项包准备、原生 V69 和 Alpaca 资本兼容回归通过（50.60 秒）。配置集成的受影响联合运行另有 67 项通过，新测试最初因前次调仓未完成被正确拒绝，修正为真实买入完成流程后已在上述 32 项中通过；各运行有重叠，不累加。独立审查未发现具体正确性问题。2026-11-28 11:44:16 UTC 的复制账本预检连续两次构造三个实例成功：QQQ 账户聚合限额在重启前后均为 99900 USD，V179/V182 实例限额各为 50 USD，分配记录保持 3 条；官方账户和生产账本写入均为零。该结果为 OFFLINE_ACCEPTED，尚未生产部署；此前 50 USD 聚合限额失败记录保留为修复前证据。

## 验收矩阵

| 范围 | 状态 | 证据与下一触发条件 |
| --- | --- | --- |
| Alpaca 公告完整读取及原 V69 输入恢复 | ONLINE_ACCEPTED | 官方只读取回 1451 个 ID；已部署候选心跳为 WAITING_NEXT_WEEK，无输入错误 |
| 冻结 V179/V182 包构建、安装与装载 | OFFLINE_ACCEPTED | proposed-only 准备测试通过；未晋升 Champion |
| 历史零归属资本初始化修复 | OFFLINE_ACCEPTED | 45 项资本回归及复制账本三实例构造通过；未部署该修复 |
| Alpaca 三实例共享风险兼容 | OFFLINE_ACCEPTED | 显式聚合限额修复后，复制账本两次构造均为 QQQ 总上限 99900 USD、验收实例各 50 USD；生产配置未激活 |
| Alpaca 自然开市调仓 | PENDING_NATURAL_EVENT | 下一开市 2026-11-30 14:30 UTC；三实例执行仍先受上述工程阻塞约束 |
| Binance 原历史迁移接入 | FAILED | 两个历史 portfolio allocation ID 的经济承接尚未证明，现入口仅接受一个 20 USDT portfolio 袖套 |
| 双市场实例成交、退出与库存隔离 | IMPLEMENTED | 现有运行时具备相关路径；本次尚无官方生命周期证据 |
| 资本追加、撤回、转移、分阶段回收及 TWR/XIRR | IMPLEMENTED | 既有资本路径有离线回归；本次未形成在线资本事件证据 |
| 风险增险拒绝、合法减仓及重启恢复 | IMPLEMENTED | 既有风控路径有离线回归；本次未形成在线闭环证据 |
| 2 vCPU / 1 GiB 多实例实际负载 | IMPLEMENTED | 已提供只读采集入口及原工作负载快照，尚无多实例 Worker 采样 |

Binance 迁移入口的静态约束已核实：`register_account_history_source` 固定要求 7/20 USDT 两袖套且 manifest allocation 集合与袖套完全相同；`export_portfolio_sleeve` 只按单一原 allocation ID 导出任务。因此资金承接证明本身还不足以直接把两个 ID 填入当前入口；须在保留全部订单身份的前提下处理历史来源表示，不能静默遗漏其中一段。

## 可重复入口与准备结果

`tools/prepare_p10_12_alpaca.py` 复用原生包构建器和现有冻结 Ridge 工件，生成 V179/V182 包及共享 owner 配置，不访问账户、不训练、不注册 Champion。参数要求显式提供原账户预算、费用预留、远端 release、^SP500TR 快照路径及由原账户风险政策独立核实的完整 `--aggregate-symbol-limits` JSON 映射。正式配置含账户标识，保存在仓库外，不纳入公开证据。已有包身份见 evidence.json；包安装和实际装载已通过本地测试。配置生成不会自动激活或分配资金。因共享总标的限额冲突，生成器只输出 `proposed-instances.json`，不再输出可直接传给运行时的 `instances.json`；未部署的激活入口已撤回，不生成激活请求。既有仓库外准备目录中的旧 instances/requests 文件不得应用到生产。生成配置仅适用于本次已核实的零分配、零 V69 经济现金过账状态，不是通用历史资本迁移入口。2026-11-28 10:49:36 UTC 再次只读确认：V69 plans=0、corporate postings=0、全部 allocations=0，AAPL/QQQ 归属数量及初始数量均为零；现有 cash_flow 的两个来源均为空，因此无需对不存在的 V69 经济过账执行 adoption。已有分配或非零历史过账的账户须保留原注册并使用现有资本迁移路径。随后用生产 portfolio/deployments 的只读备份、独立 account DB 和禁止写账户的 ObservationHTTP 预检三实例构造，发现 `CapitalPolicy.configure` 对零 claims/已终结历史订单读取缺失 allocation 导致 TypeError；生产资本未变。当前修复将 configure 与既有模式校验统一到有效归属集合，保留非零持仓、未终结或不确定订单和 capital_symbols 的约束；新增真实买入/清仓历史及重启回归与受影响资本模块合计 45 项通过（53.44 秒）。激活工具曾通过包准备/排队/重放定向测试，但因上述风险配置冲突撤回，不能作为可部署入口。修复后在生产账本的独立 SQLite 备份中再次构造成功：原生 V69 与 V179/V182 均装载、分配记录共 3 条，官方账户和生产账本写入均为零；同时实测 QQQ 共享总标的上限为 50 USD，确认风险兼容阻塞。仅输出 proposed 配置的包准备回归另通过 1 项（6.06 秒）。P10-13 已申请后续部署/备份测量，当前 Alpaca 生产写窗口已让出，三实例生产接入暂停至其完成。另发现必须在接入前解决的风险语义冲突：既有 SHARED_SAME_DIRECTION 将共享标的总额度单调收紧到各 owner 原始单标的上限的最小值；50 USD 验收袖套可能把 V69 同标的总敞口上限压至 50 USD。该配置不能在保留 V69 正常预算要求下直接上线（TODO：验证兼容的账户总限额与实例限额路径，不提高正式预算来制造通过）。分窗读取现保留父记录作为一致性锚点，子响应遗漏或改变已见公告时明确阻塞；36 项受影响回归通过（36.08 秒）。

官方 Paper 回读现金/权益均为 100000.05 USD，账户 ACTIVE、trading_blocked=false。只读 ^SP500TR Provider 已取到 252 个完整交易日，2025-11-26 至 2026-11-27。信号截止使用官方交易日历的提前收市时间 2026-11-27 18:00 UTC，而不是人为设定的当日末尾。该结果证明基准数据接入，不是自然调仓证据。

08:10 UTC 的资源快照中，Binance、Alpaca、Observer 的 PSS 分别为 41564、35929、20643 KiB，RSS 分别为 50312、45024、29568 KiB，进程 Swap 分别为 16472、50200、38216 KiB。这是原运行负载基线，尚非新增多实例 Worker 峰值验收。

## 部署与回滚

Alpaca 独占维护窗口已与并行任务协调，部署命令已启动：停止 Alpaca、只读备份 deployments.sqlite3、沿现有迁移工具为原 V69 包准入候选 runtime，再切换 current 并启动。首次迁移以 root 调用，被 `state root must belong to the service identity` 检查拒绝；脚本已重新启动旧服务，未切换 current。第二次以 `factorlab-alpaca` 调用，在打开迁移脚本时因候选源码继承隔离目录权限而被拒绝，旧服务再次恢复且 current 未切换。仅修正候选发布源码的目录遍历/文件读取权限后，第三次调用成功。2026-11-28 11:00:15 UTC，current 已切到 `2e02729157b1e6a9c277506533d1af1fc1a6a1a0`；原 V69 包的 runtime admission 从 `16f97f…` 到 `48b8ce8d…` 已创建，服务 PID=1450535、active/running、NRestarts=0。首次回读仍是 11:00:04 旧记录；随后完整采集取得候选 11:04:43 心跳：healthy=true、原 V69 `WAITING_NEXT_WEEK`、reason=null、执行 `IDLE`，原公告饱和阻塞已解除。该结果是输入修复的在线运行证据，不是自然调仓或成交验收。详见 [runtime-post-deployment.json](runtime-post-deployment.json)。状态根、资金与凭据权限未变。回滚记录目录为 `/root/p10-12-2e02729157b1e6a9c277506533d1af1fc1a6a1a0`，保留切换前 release 和 deployment DB；Binance、Observer 和日本节点不在本次写入范围。候选 `2e02729157b1e6a9c277506533d1af1fc1a6a1a0` 已在主节点隔离临时目录使用现役解释器加载原 V69 包：manifest、七个数值/日历模块、依赖、Python 版本及去除 runtime digest 后的绑定全部相等，详见 [v69-compatibility.json](v69-compatibility.json)。该探针没有写入 runtime admission。公告执行依据的六场景现役/候选配对结果全部一致，覆盖无行动、策略资产拆股、无关资产行动及执行日依据有无两种状态。候选独立安装已完成，pip check 通过；已安装候选解释器的第二次探针同样通过全部比较，runtime digest 与隔离源码探针一致。随后已按上述维护记录完成 current 切换和 runtime admission；正式回滚尚未执行。
