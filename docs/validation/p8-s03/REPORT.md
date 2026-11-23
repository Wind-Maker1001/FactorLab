# P8-S03 — Azure 版本化策略部署与切换

## 验收结论

2026-11-23，现有 Azure `factorlab-demo-01` 完成自动发布和官方 Binance USD-M Demo 切换验证。未新建交易服务、重新绑定账户、重置授权、恢复旧 SQLite 或扩大预算。

| 项目 | 结论 | 验证边界 |
| --- | --- | --- |
| 自动部署 | **已验证** | 固定提交归档、版本目录、原 systemd Execute 受控重启及原账本 GET 恢复 |
| 策略重启式切换 | **已验证** | 验收包 1：PID 113459 → 113963，原 state-root |
| 策略热切换 | **已验证，限部署协议验收包** | 包 2、3 均保持 PID 113963；执行 Supervisor 未重启 |
| 策略异常自动恢复 | **已验证** | 包 3 在真实收盘事件抛错后恢复包 2，随后沿稳定历史恢复原 Grid |
| 官方 Demo 订单迁移 | **已验证撤单及 GET 对账** | 原开仓单按原身份撤销并确认；验收时零持仓 |
| 自定义策略下单、部分成交、减仓 | **离线网关组合验证** | 经过 DemoSession、原 reserve/receipt/累计预算；官方验收包只 HOLD |
| S02 原生结果消费 | **已接入检查，合格 Demo 候选部署待验证** | 无晋升保持当前版本；当前 S02 没有合格候选，且执行口径为 A 股 |
| S01 通用协议联合集成 | **待 S01 稳定接口** | 当前独立适配为 `factorlab.demo.evaluate/v1`，不是 S01 `FACTORLAB_DECISION_V1` 的完整执行适配 |
| 24h / 72h / 7d 长期运行 | **待验证** | C06 完整窗口尚未形成 |

本 PR 交付独立可运行的 S03 控制器、离线适配器和实际部署。**S01/S02/S03 联合的任意通用策略自动选优部署尚未完成验收**，没有将缺少合格候选解释为研究成功。

## 实际发布

首个验证版本 `42daa9aa5329f224550dbf7c518515024154d10f`：PID 107003 → 113459，约 9.805 秒恢复，SQLite inode 全部保留，前后均为 39 / 100 POST、243.2948000 / 700 USDT。见 [首次发布](runtime-deployment.json)。

最终运行代码 `362660ebf27ba0f90ce1794411ffedb8c1a3a3eb`：PID 113963 → 117136，约 12.130 秒恢复，原 SQLite inode 全部保留，前后均为 40 / 100 POST、249.9664000 / 700 USDT，最大损失仍为 1 USDT，UNKNOWN=0。见 [最终发布](runtime-deployment-final.json)。两次发布之间原 Grid 自然交易消耗 1 POST；不是预算重置。

沿用 `/opt/factorlab/releases/<SHA>/source` 和独立 venv，保留原 state-root、LoadCredential、session.lock、服务用户及重启配置。一次后续上传曾发生 SSH 中断，尚未进入安装阶段，运行服务不受影响；之后有限重试成功。

## 包和候选

StrategyPackage schema v1 包含 Git 代码身份、逐文件 SHA256、精确依赖、配置、接口和状态版本、研究证据、Demo 环境/标的及迁移政策。规范 manifest 的 SHA256 为包 ID；显式 zip 清单、路径限制、大小限制和加载校验保证代码内容身份。不会复制凭据或交易状态；缺失依赖拒绝激活，不执行在线安装钩子。

可信 Python worker 接收 JSON context/state，返回 state/intents；不接收执行网关或账户写方法。崩溃、超时和非法结果触发拒绝或回退。这是可信代码边界，不声称是针对恶意 Python 的 OS 沙箱。

15 秒 inbox timer 支持现成 zip 或 staged source/manifest 自动构建。原账户 Execute 所有者统一下单，部署 timer 不交易。候选绑定 selection ID、包/证据、资格、严格改善和 `expected_active`；基准已变化的旧结果不能覆盖当前版本，单个坏文件不阻断其他候选，单个唯一待切换索引防止并发部署。

S02 适配器消费 `FACTORLAB_CHAMPION_SELECTION_V1`，检查 OOS 资格、执行口径、代码/参数及 tracker 证据。原研究标签保留，不产生真实资金权限。当前 A 股结果不能成为 NVDA Demo 优势证据；无晋升无需构建包。完整 S01 接口仍须后续对接，不将独立函数适配器称作所有 S01 策略兼容。

主机已消费 S02 实际 `synthetic-paired-v1` 的不晋升输出，返回 `RETAIN_CHAMPION`；见 [原生结果消费记录](native-selector-consumption.json)，未改变当前策略。

## 切换和恢复

原 state-root 的 `strategy-deployment.sqlite3` 保存 active/stable/previous、checkpoint、决策、订单所属版本、旧/新版本、boot PID、水位及追加事件。包保存在 `strategy-releases/<ID>`，由 C07 原目录备份覆盖。

REQUESTED → 停止旧策略新开仓并撤回未提交意图 → DRAINING → 原身份撤单及 GET 终态 → 确认空仓 → 可选 READY_RESTART → 准备 worker → 原子激活，等待新鲜收盘事件。水位记录 controller event rowid 和行情水位；已记账身份只恢复，不重发 POST。

`wait_flat` 等待已有退出订单完成；若旧仓没有能完成的退出单，可持续等待，绝不擅自平仓或转属新策略。自主减仓须包中预配置 `reduce_only`、`explicitly_allowed=true` 和有界 max_attempts。仍受原 LIMIT、持仓、过滤器和累计预算约束；不满足时安全等待。

部分成交先恢复实际成交/仓位，撤剩余开仓量；未终结减仓单沿用原身份。UNKNOWN、未对账、不可归属订单或未退出持仓都阻止激活。回退选择旧稳定代码，但以当前真实账本准备；不恢复旧数据库，也不回退 dispatch、receipt 或预算。失败回退保留持久 ROLLBACK，跨重启阻止开仓，恢复后的历史链不会把失败版本当作前一稳定版本。

## 官方 Demo 证据

[完整记录](official-demo-conformance.json) 的包明确标记 `SOFTWARE_CONFORMANCE_ONLY` 和 `research_qualification=false`。资格字段仅是部署协议测试输入，不是 S02 收益资格认证。

| 顺序 | 模式 | 包 ID 前缀 | PID | 结果 |
| --- | --- | --- | --- | --- |
| Grid → 包 1 | restart | 541c31fc66cb | 113459 → 113963 | SUCCEEDED |
| 包 1 → 2 | hot | 90b06a6ef958 | 113963 → 113963 | SUCCEEDED |
| 包 2 → 3 | hot | 2b0a338948e7 | 113963 → 113963 | SUCCEEDED |
| 包 3 收盘故障 → 2 | 自动回退 | 90b06a6ef958 | 113963 | ROLLED_BACK |
| 稳定历史 → Grid | 恢复 | legacy-grid | 113963 | ROLLED_BACK |

验收切换段预算维持 39 POST、243.2948000 USDT；原单 `fl-d7345f8795464a65-g-00019-00000001` 按原 client ID 撤销。验收后服务 RUNNING、UNKNOWN=0、零持仓。当前快照见 [health](post-validation-health.json)。

## C06 / C07 和测试

C06 health、日报改用版本解释器，保留原目录、异常分类、采样和日报机制。样本增加版本/切换事件，日报按 seq 去重记录实际结果。15:27:02 UTC 日报记录 18 个切换事件，active=legacy-grid，24h window_complete=false。Execute、health、daily-report、backup、strategy-deploy timers 已启用。C07 原备份计时和目录保留。

| 检查 | 结果 |
| --- | --- |
| 最终 core + C06 + 轮换场景 | [offline-final.xml](offline-final.xml)：41 passed |
| 显式减仓和原预算、GET 空仓后激活 | [reduction-regression.xml](reduction-regression.xml)：1 passed |
| Grid Supervisor 回归 | 45 passed |
| Execute / continuous / backup / core 扩展检查 | [offline-regression.xml](offline-regression.xml)：133 passed，1 项受 5 秒墙钟场景截止影响 |
| 上述轮换复验 | [rotation-regression.xml](rotation-regression.xml)：1 passed；保留四轮断言、放宽夹具上限至 20 秒，最终 41 项也覆盖通过 |

覆盖 worker 崩溃/超时、重启恢复、网络、部分成交、UNKNOWN、原身份撤单、显式减仓、策略异常、回退失败、旧基准结果及坏 inbox。官方在线未注入网络故障、部分成交或 Execute SIGKILL，相关结论限离线。

复验和操作见 [部署接口](../../binance-demo-strategy-deployment.md)。独立 PR 不自动合并；S01 完整适配、兼容合格 S02 候选联合部署和完整长期窗口仍是待验收项。
