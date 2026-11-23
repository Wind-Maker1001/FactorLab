# P8-S02 自动研究与 Champion Selector

实现入口是 `factorlab.research.automatic.run`、`ChampionSelector` 和
`tools/research/select_champion.py`。结果用于研究及下游系统读取；现有
`FORMAL_ADMISSION_BLOCKED` 不因研究晋升而解除，不会调用交易或部署接口。

## 可复现实验与一次性运行

在仓库根目录使用已安装依赖的 Python 3.13/3.14：

```powershell
python -I examples/research/champion_demo.py --output D:/research/s02-demo
python -I tools/research/select_champion.py --config D:/research/config.json --output D:/research/study-002 --store D:/research/tracker --state D:/research/champion.json
```

第一条命令生成固定种子合成行情、两种自定义策略的六个参数候选及现金
Champion，输出完整配置示例。第二条消费固定校验和的历史 parquet；已有历史
数据可直接导出为现有 `run_custom_strategy` 接受的行情格式，不下载或伪造数据。

`replay-config.json` 指向运行目录中保存的原始行情和策略源码快照；使用它和
**独立的审计 state/store/output** 可复放同一实验。不得让审计 state 替换生产
研究 state。可通过 `ExperimentTracker.reproduction(run_id, verify_artifacts=True)`
校验输入、策略源码、参数配置、交易、净值和评估文件。源码若有额外本地依赖，
还须保存对应环境；当前适配契约的策略文件应自包含，或只依赖固定版本的
FactorLab/已记录依赖。SHA256 是一致性收据，不是安全签名。

## 窗口与搜索协议

配置必须明确指定互不重叠、依时间递增的 `train`、`validation`、`test` 闭区间，
边界必须是输入中已观察到的交易日，测试结束日必须早于当前 UTC 日。
每次运行是一组三段窗口；滚动研究通过下一次运行显式提供新的窗口实现。

每种策略声明 `strategy_id`、本地文件、类名、源码 SHA256、基础参数和有限
`grid`。网格在展开前检查总量上限；默认最多 32 个候选、最多 4 个最终候选。
构造函数默认参数被规范化并纳入候选 ID；重复的规范化候选拒绝运行。
所有参数先在验证集比较，每种策略只保留一个版本，再按验证分数裁剪
`max_finalists`。冻结清单写入 `frozen-search.json` 后才进入测试。
未入围版本不会获得测试分数，防止根据测试表现回头选择参数。

已有线上 Champion 由首次配置 `champion` 指定，后续以持久化 state 中的完整
策略身份为准，不从本次候选中推断。Champion 和 Challenger 在本次相同的
样本外窗口重新执行；失败或缺失的 Champion 证据阻止晋升。

最小策略契约复用 `strategies.custom.StrategyProtocol`：

- `required_fields` 为市场字段元组；`warmup_sessions >= 2`。
- 构造参数必须明确标注 int/float/bool/str，禁止可变参数。
- `generate_targets(bars, calendar)` 每次只获得截至当前收盘的历史前缀，
  输出当前交易日的 `date/symbol/target_weight`，NaN 表示持有原仓位。
- 可选 `fit(bars, calendar)` 只获得训练窗口的市场字段，没有验证/测试行或
  未来收益标签。每个阶段使用新实例、同一训练窗口；不在测试中再训练。

当前支持单标的、日频、只做多 A 股；信号在下一开盘执行，沿用已有 T+1、
交易状态、涨跌停和资金/整手限制。`initial_cash`、`max_weight`、`board_lot`、
手续费规则、滑点、执行模型、资金费必须全候选共用。超出 max_weight 的
目标拒绝执行；本适配器仅接受零资金费的无杠杆现货，其他市场或非零资金费
明确拒绝，不能静默忽略。S01 合并后，只需在 `_register` 与自定义策略加载层
映射包 ID、版本和参数；不应重写选择规则、证据或调度。

## 目标、风险与统计不确定性

默认目标为 `risk_adjusted_net_return`：

`年化日均净收益 - risk_aversion × 年化日收益标准差`。

收益来自现有净值账本，已扣手续费与滑点；年化日均收益不是 CAGR。
也可配置 `net_return` 或 `sharpe`，零波动 Sharpe 为不可计算。
默认要求正净收益、最大回撤不超过 20%、日损失 95% CVaR 不超过 3%、
容量不少于 10 万元，并且参与率不超过 1%。回撤包括初始净值 1，CVaR
沿用 Monte Carlo 的分数边界尾部均值算法。日损失 CVaR 与终端损失 CVaR
不是同一个指标。

容量是保守研究筛选代理：同时使用意向入场权重和真实成交股数，除以信号
收盘已知的历史滚动平均成交量；不使用下一成交日的实现成交量授予容量。
容量金额为 `initial_cash × participation_limit / max_participation`。
没有交易/意向或历史量不足时容量未知，不授予无限容量。本模型不模拟市场
冲击、订单簿深度或衍生品容量；容量违规候选被淘汰，不把不可执行回测收益
用作合格结果。

晋升需同时满足：

- 风险和容量约束、默认至少 120 个完整样本外交易日、至少 5 个期望尾部观察。
- 目标改善至少 0.02，且配对移动块重采样的目标改善区间下界超过该门槛。
- 配对净收益差的 HAC 和移动块 bootstrap 区间下界均超过默认 1% 年化改善。
- 默认至少 60% 的完整不重叠测试块净收益改善为正；距上次晋升至少 30 个日历日。
- bootstrap 每侧调整后尾部至少有 5 个期望重采样，否则证据精度不足。

区间置信度按**全部搜索参数版本数量**作 Bonferroni 调整，包括验证失败的版本，
不是只统计看起来最好的候选。块长度、重采样次数、种子及所有门槛在测试前
固定；恒定差值、样本不足、区间跨越门槛均不能晋升。符合全部条件的候选按
样本外目标排序；并列以稳定候选 ID 排序。没有合格候选时保留完整 Champion
身份和版本号，不强行选赢家，也不会把 Champion 当前不合格解释为退出指令。

这些是依赖弱相关、分布稳定假设的历史样本外证据，不是未来盈利保证；
调整控制的是本次搜索族，不提供无限次滚动研究的终身错误率保证。
验证集参数选择也不证明搜索空间以外存在或不存在更优策略。
合成数据上的晋升只证明软件判定路径，不是金融优势证据。

## 机器接口、状态与失败恢复

`selection.json` 是版本化结果：`schema/study_id/version/selected/promoted/reason`、
代码版本、规范化参数、窗口、共同执行口径、全部候选、tracker run ID/校验和、
风险指标与配对统计证据。`candidates.json` 提供单独清单，`REPORT.md` 为可读表格。
`champion.json` 原子更新，指向结果文件及 SHA256；版本只在晋升时递增。
`financial_admission` 必须由消费系统检查，不能将研究晋升当成实盘准入。

state 必须持久保存且只属于一个部署研究口径。复用已有独占文件锁，阻止并发；
测试前先保存 `TEST_RESERVED` 和已消费测试末日，再执行测试。中断后 Champion
保持原样，该测试窗口仍被消费，下一研究必须提供全新的、不重叠的测试窗口。
成功结果先写完整 JSON，再更新 state 指针；失败不会留下半个 Champion。
不得删除 state 来重复尝试同一个 holdout。锁崩溃遗留时须确认没有活跃进程后
人工恢复，复用现有锁工具的行为。

## 定时入口

Windows 使用系统 Task Scheduler，以下注册动作由操作者执行一次：

```powershell
powershell -NoProfile -File tools/research/schedule_champion.ps1 -Python D:/env/Scripts/python.exe -Config D:/research/config.json -OutputRoot D:/research/runs -Store D:/research/tracker -State D:/research/champion.json -At 06:00
```

相同脚本加 `-RunNow` 执行一次。任务使用当前用户、忽略重叠实例、不覆盖
同名任务、不存储凭据，运行时直接调用 Python。注册脚本需符合本机的脚本
签名/执行策略；不更改系统执行策略。Linux/macOS 可用现有 cron 调用 Python
入口并指定 `--output-root`，以 study_id 创建每次运行的独立输出目录。
每个周期之前由现有数据/研究流程更新已固定校验和的数据、唯一 study_id 和
新窗口配置；配置没有更新时任务会拒绝重复研究，并返回退出码 2。
这里没有引入常驻服务或新的调度系统。本交付没有在机器上注册实际系统任务。
