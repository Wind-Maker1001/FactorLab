# P3-11 多因子候选库、Train-only 去冗余与消融

**P311_ENGINEERING_ACCEPTED** — 合成数据工程合同验证；真实市场预测能力 **NOT_VERIFIED**。

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

基线：`72571efeac498515a0765b6fa344ced296307daf`。

## 研究定义与身份

`catalog.json` 固定了 `p311-v1` 的 64 个表达式、公式、输入字段、价格基准、lookback、收盘可得声明、家族及 definition hash。16 个家族各使用 5/10/20/60 窗口，分为动量/反转、趋势、波动、量价和流动性代理五类。这里的反转方向不预先宣称有效；筛选组方向由 Train RankIC 冻结。窗口变体属于同一家族，跨家族也可能相关。所有价格表达式只使用 HFQ 输入；成交量/额/换手率指标不是财务因子，也不证明实际市场流动性。

复用 Registry V2 和受限 AST 编译器，不新增解析器或计算引擎。深度 5、节点 15、滚动操作 2、lookback 130 的上限不变；本目录最大 prior-session lookback 为 60。发现生成器单批最多 50 的预算不变，目录通过注册接口加载。缺少必需字段时目录标为 NOT_AVAILABLE，计算拒绝缺字段，不生成财务数据或补零。SESSION_CLOSE 是软件可得性声明，不是 PIT 证据。

| 数量口径 | 实测值 |
|---|---:|
| 候选表达式定义 | 64 |
| 合法注册且有有效执行观测的候选 | 64 |
| definition hash 去重后 | 64 |
| 家族数 | 16 |
| Train 质量筛选并相关性去冗余后的候选池：折 0/1/2 | 36 / 36 / 40 |
| 筛选组实际入模：折 0/1/2 | 10 / 10 / 10 |
| 固定候选组 / 原有基准实际入模 | 64 / 14 |
| 已证实的真实市场预测能力 | NOT_VERIFIED |

36/36/40 是按固定阈值保留的候选池，不是统计独立性证明；也不是对 64 个定义仅按相关性聚类得到的总数。每折分别有 19/19/15 个候选标为 highly_redundant，其余淘汰原因见 quality.csv。

## 预声明实验与边界

使用既有固定种子 `20261014` 合成生成器、24 个证券、300 个合成工作日，前 65 日在成员期前用于预热。最终数据 5,630 行；合成输入保留已有缺失观测，不强求每一天每个因子均有效。Registry 快照包含原有 14 个定义和 64 个候选。DatasetBuilder 的现有分块构建使用默认 ChunkedConfig；没有新缓存归档。

3 月初始 Train、1 月 Validation、2 月 Test、2 月步进，purge/embargo 各 5 日，得到 3 折与 132 个共同 Test 日期。现有 WalkForwardRunner 负责时间拆分与模型拟合。TrainFactorSelector 每折只读取已 purge 的 Train，并在任何 Test 评估前冻结所有选择与方向；Validation 维持原有合法边界。本实验每模型只有一组预声明参数，不进行 Test 调参。

候选筛选固定 K=10，最低覆盖率 0.8、缺失率上限 0.2、非法无穷比例上限 0、至少 100 观测、12 横截面样本、20 日期、绝对 RankIC 至少 0.01、方向一致率至少 0.5、配对覆盖率 0.8、冗余阈值 0.95。常数/低变化诊断是独立 Train 诊断，不修改选择器：按日有效样本数至少 2，唯一值比率不超过 0.01 为低变化。未获得证据标为 NOT_DETERMINED。每折 pins、Train hash、方向、规则、质量及冗余保存在结果中。

四模型比较原有 14 因子、固定 64 候选和 Train-only 选中集合。FactorScore 使用均匀权重；只有选中集合使用 Train 冻结的符号。类别删除消融是在固定 64 候选上删除整类，保留其余因子，仅运行 FactorScore，共 5 个预声明删除组。它是组删除的描述性对照，不是单因子独立贡献或等 K 因果归因。未改变原有等 K FactorAblationRunner 合同。

每组每模型每折必须有相同日期、证券、标签以及有限预测配对；不允许事后取交集。所有 17 个组/模型组合均通过该校验。ICIR 使用每日等权 RankIC 均值/样本标准差，不年化；不足 20 日、不完整或零方差统计标为 NOT_DETERMINED。

## 实测观察

下表只描述此固定合成样本，不能解释为真实市场 Alpha：

| 模型 | 14 因子 RankIC / ICIR | 固定 64 RankIC / ICIR | 选中 10 RankIC / ICIR |
|---|---:|---:|---:|
| FactorScore | -0.007964 / -0.044062 | 0.147449 / 0.786641 | 0.314018 / 1.749987 |
| Ridge | 0.409307 / 2.185085 | 0.378807 / 1.916093 | 0.412221 / 2.141540 |
| LightGBM | 0.414940 / 2.019228 | 0.382284 / 2.011091 | 0.389773 / 1.986976 |
| XGBoost | 0.395081 / 1.892681 | 0.383861 / 1.911268 | 0.384197 / 1.953653 |

固定 64 候选没有统一改善结果，三个拟合模型相对 14 因子基准的 RankIC 均下降；筛选后 Ridge 接近基准。报告工具以 RankIC 下降超过 0.05 标记描述性退化，不把这个标记用于重新选择。总体三主组未触发该阈值，各折差异保留在 contrasts.csv。样本不能支持统计显著性、泛化优越性或真实市场预测结论，均为 NOT_DETERMINED / NOT_VERIFIED。

FactorScore 类别删除后的 RankIC：删除流动性代理 0.236438；动量/反转 0.066832；趋势 0.066463；波动 0.152069；量价 0.204066。对照固定 64 的 0.147449，删除动量/反转或趋势出现超过 0.05 的描述性下降。该结果不用于改写本轮规则。

选中集合的两两跨折 Jaccard 为 0.428571、0.428571、0.666667，显示明显折间变化。frequency.csv 保留每个候选的入选折数，不将窗口变体的频率解释为独立经济信号。

每模型三折累计 runner 时间：14 因子约 8.27–9.30 秒；固定 64 约 16.03–18.14 秒；选中 10 约 7.51–8.16 秒。每运行测量 tracemalloc 峰值和进程 RSS 采样；RSS 范围约 287–439 MiB，包含保留的共享状态及原生库，不能作为单模型独占内存。资源记录不包含共享数据构建、Train 选择和结果重算时间。未做大型性能认证。

观察到既有预处理逐列写入导致 pandas fragmentation 警告；原位置已标记 TODO，未改变预处理数值合同。此处属于已记录的现有性能折衷，不影响数值验证。

## 验证与复现

使用 `D:/Environments/factorlab-py313/Scripts/python.exe`，仓库根目录设置 `PYTHONPATH=src`。

```bash
python -B -m pytest -q tests/research/test_factor_candidates.py \
  tests/research/test_factor_registry_v2.py \
  tests/research/test_walk_forward_factor_selection.py \
  tests/research/test_factor_ablation.py
python -B -m pytest -q tests/research/test_factor_candidates.py \
  tests/research/test_partitioned_dataset.py
python -B -m examples.research.factor_candidates_demo --output results/p311-new
python -B -m examples.research.factor_candidates_demo \
  --output docs/validation/p3-11/results --recompute-only
```

首条回归首次运行 91 passed / 1 failed：新增测试错误地要求预置缺失的合成输入末日全部有效，已修正为每个候选预热后有合法执行观测，并保留缺失传播断言。第二条复测 **30 passed**；最终候选专项 **9 passed**，包括乱序输入选择确定性。原有 registry、selection 和 ablation 用例在首轮均通过。新增字段不可得计算拒绝用例单独验证：**1 passed**。结果重算 CLI 通过，并核对筛选组四模型的 **36 条阶段审计记录**。

覆盖合法注册与固定哈希、非法 AST 拒绝、未来价格/成交量变化不影响历史特征、缺失传播、字段不可得、Train-only 方向与冗余选择、Test 标签改变不影响该折已冻结选择、共同样本拒绝以及常数/低变化识别。后续折可合法使用此前 Test 日期进入 Train，泄漏测试不将此误判为违规。小规模候选重算、同种子合成输入以及每日结果无训练重算均可复现。四模型特征消费按实测 fit-audit 对照冻结 plans 检查；每日统计重算成功。

首次研究执行曾因现有树模型参数白名单拒绝 `n_jobs` 停止；已移除该参数并按原工厂设置完成一次修正实验，没有重复参数搜索。

提交保留目录和小型 CSV/JSON：每日共同样本/标签摘要、统计、消融、Train 选择与方向、质量、冗余、变化诊断、资源和拟合审计。不提交原始合成行情、数据分块、模型文件或早期失败输出。结果仅是研究软件预览，不接交易执行接口，不修改 ResearchPipeline、PortfolioOptimizer 或 Backtester。
