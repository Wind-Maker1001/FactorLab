# P8-S02：自动研究、优化与 Champion 选择验证报告

日期：2026-11-23。独立分支：`feat/p8-s02-champion-selector`。
起点：`bfdd4a9b9e1fc7769b3957a82bbc92f0975d92da`（main）。

## 结论

工程流程已实现并完成合成多策略配对实验。**本次没有合格 Challenger，保持
原现金 Champion，版本号仍为 0。没有真实金融样本外优势结论。**

实验中的 TrainMean 虽然样本外净收益为正、目标点估计高于现金，但配对收益
及风险调整目标的置信区间跨越门槛，不能区分优势和统计噪声。Trend 的改善
还未达到目标门槛及块一致性要求。因此不将“回测收益最高”视作晋升依据，
也不预设存在最优盈利策略。

## 复用检查与实现范围

先检查了以下实现，未另建回测、数据下载、实验追踪或常驻调度系统：

| 已有组件 | 本次处理 |
| --- | --- |
| `experiments/tracker.py` | 直接复用不可变实验/运行、SHA256 工件、同口径 `compare` 和 `reproduction` |
| `research/workflow.py`、config、CLI | 复用其固定数据/成本、显式运行与证据输出模式；其任务枚举不适合多策略网格，不强改旧 schema |
| `validation/walk_forward.py`、splitter、partitioned source | 检查训练/验证选择/测试隔离和标签成熟度设计；当前策略契约没有前瞻标签，使用显式三段窗口及历史前缀，下一滚动周期需全新测试窗口 |
| `research/monte_carlo`、reliability | 复用分数边界 CVaR；既有模拟是固定权重重平衡且零交易成本，不能当作有成本策略的收益证据 |
| `evaluation/research.py`、`metrics/robustness.py` | 复用策略评估、HAC 和移动块 bootstrap 净收益差推断；额外配对重采样非线性目标差 |
| `portfolio/optimizer.py`、现有验证参数网格 | 前者优化单期资产权重，并非策略版本选择；新流程只增加有限确定性策略参数网格 |
| `strategies/custom.py`、`backtest/target.py`、fees | 直接复用本地策略加载、历史前缀、次日开盘执行、费用/滑点、T+1 与状态限制；最小增加 train-only fit 和共享 max_weight 检查 |
| `forward/integrity.workflow_lock` | 直接复用独占进程锁；state 和选择结果原子写入 |

当前适配范围是日频、单标的、只做多 A 股、零资金费无杠杆现货；非零资金费
及其他执行模型拒绝运行。策略包暂用文件 SHA256、类名、规范化参数适配，
S01 的包接口合并后仍需完成包 ID/代码版本映射集成。本次未依赖未合并的
S01/S03 接口，也未声称已经完成最终集成。

## 多策略配对实验

输入由 `examples/research/champion_demo.py` 固定种子 `20261123` 生成，
620 个合成交易日。策略代码 SHA256：
`4197b415ad449ad1bd35b7eeaf889c898d5a2bc59809c135203f028c005d6fd0`。

- 训练：2021-02-12 至 2021-11-04。
- 验证：2021-11-05 至 2022-05-19。
- 样本外：2022-05-20 至 2023-05-18，共 260 个交易日。
- 初始资金 100,000 元；共享整手 100 股、max_weight=1；佣金 3 bps、
  最低佣金 5 元、滑点 5 bps、卖出印花税 5 bps；资金费显式为零。
- Trend：window=[5,10,20]、weight=0.8；TrainMean：
  multiplier=[0.95,1,1.05]、weight=0.6；原 Champion 为 Cash。

每种策略在验证阶段只冻结一个参数版本，测试只运行 Trend(window=10)、
TrainMean(multiplier=1) 和原 Champion。六个参数版本全部计入多重比较族。
TrainMean 验证分数并列时由稳定候选 ID 确定版本，没有查看测试结果打破并列。

| 测试候选 | 净收益 | 默认目标 | 最大回撤 | 日损失 CVaR95 | 容量代理/元 | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Cash / 原 Champion | 0% | 0 | 0% | 0% | 未知（无订单） | 保留 |
| Trend / window=10 | 7.8183% | 0.001275 | 7.0510% | 1.0473% | 2,880,302 | 拒绝晋升 |
| TrainMean / multiplier=1 | 14.2671% | 0.054430 | 5.6321% | 0.9353% | 3,764,836 | 拒绝晋升 |

Cash 的未知容量并不构成自动退出理由；无 Challenger 合格时保留原身份。
容量数值只是信号收盘已知历史成交量的研究代理，不是实盘可成交承诺。

Bonferroni 调整后的双侧区间置信度为 99.1667%，块长 10，重采样 2000 次：

| 配对差 / 对 Cash | HAC 日均净收益差区间 | bootstrap 日均净收益差区间 | 风险调整目标差 bootstrap 区间 |
| --- | --- | --- | --- |
| Trend | [-0.0006152, 0.0012163] | [-0.0005420, 0.0012638] | [-0.234679, 0.214347] |
| TrainMean | [-0.0003050, 0.0013552] | [-0.0004084, 0.0013319] | [-0.186059, 0.251252] |

区间均未证明超过预先规定的最小改善。该实验属于 **SYNTHETIC / SOFTWARE
ONLY**；所有结果继续标记 `FORMAL_ADMISSION_BLOCKED`。

机器可读的全部候选、参数、风险、运行证据 ID、拒绝原因与完整配对推断见
[experiment-summary.json](experiment-summary.json)。该文件是便于审阅的便携摘要：
只将运行时绝对策略路径替换为仓库内源码路径，其余指标来自实际实验。
权威 tracker、原始 parquet、归档源码、净值、交易、配置与复放配置保留在
`local-p8-s02-final-evidence/`（忽略的本地工件目录）。源码/数据/环境身份均有记录。
最终实验从干净实现提交 `b66feb73002d729e7988d6db90d4e1db4e92b403` 运行，
`git_dirty=false`；与实现过程中的首轮实验相比，完整配对证据及保留决策完全一致。

## 验证与复现

```powershell
python -m pytest tests/research/test_champion_selector.py tests/research/test_custom_strategy.py tests/research/test_experiment_tracker.py tests/research/test_research_workflow.py tests/research/test_research_evaluation.py -q --basetemp=local-s02-check
python -I examples/research/champion_demo.py --output D:/research/s02-reproduce
python -I tools/research/select_champion.py --config D:/research/s02-reproduce/run/replay-config.json --output D:/research/s02-audit/run --store D:/research/s02-audit/tracker --state D:/research/s02-audit/champion.json
```

覆盖：合格晋升、统计不确定/退化差值不晋升、全部候选不合格、回撤/CVaR/容量
拒绝、测试前冻结、改变测试数据不改变验证指标或候选、train-only fit、次日
执行、源码/数据校验、同口径 tracker 比较、工件复放、冷却期、已消费窗口拒绝、
中断预留测试窗口且保留 Champion。PowerShell 定时脚本通过语法解析；没有
注册实际系统任务。本机执行策略阻止直接运行未签名 ps1，因此未实际运行
注册脚本；定时任务动作本身直接调用 Python，不修改或绕过系统执行策略。
`git diff --check` 通过。

最终完整回归：**79 项通过，100.52 秒**，没有未解决失败。
复放检查使用稳定候选 ID、代码版本与完整决策证据，允许归档源码位置变化。
定时 Python 入口已实际执行成功；重复消费测试窗口被拒绝。额外策略政策
边界检查 3 项通过，PowerShell 语法与 Python 编译检查通过。

## 交付、边界与发布状态

实现、配置说明及调度用法见 [使用文档](../../research/p8-s02-champion-selector.md)。
`selection.json` 和原子 state 指针可供部署系统读取，但研究晋升不执行部署，
更不授权实盘交易。统计结论依赖弱相关和稳定性假设，仅调整本次搜索族；不
提供无限研究周期的终身错误率或未来盈利保证。

发布使用当前用户的本机 Git / gh keyring 认证，已确认仓库写权限。此前受限
会话无法使用该认证，连接器另有 HTTP 403 权限限制；本机认证可以正常发布。
独立 PR 以 main 为目标，不启用自动合并。
