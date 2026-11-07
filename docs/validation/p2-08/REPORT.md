# P2-08 动态选择与 Train 定向消融验收

`P208_ENGINEERING_ACCEPTED`，仅软件工程。

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

启动 fetch 核对 main 为 `d3f5aae7f451e268a0f05dcded5b8726dce39da4`，Tree `dfc9a33b6565a00ea782bebb503ffec9a70c341b`；
分支 `feat/p2-08-factor-selection-ablation`，工作区 `D:/Code/FactorLab-worktrees/p2-08`。
Git 作者 `Wind-Maker1001 <88176063+Wind-Maker1001@users.noreply.github.com>`。
最终 Commit/Tree 以 PR head 为准，并另存本地 `local-validation/delivery.json`，避免自引用哈希。
已阅读 P2-07 验收报告、选择器、Runner、Splitter、DatasetBuilder 及相关测试。

## 实现与对照约束

新增 `validation/ablation.py`、`ablation_statistics.py`、`ablation_resources.py`，复用原注册表、
TrainFactorSelector、Splitter、模型工厂和 WalkForwardRunner；四模型及原核心算法文件字节未改。
每折只计算一次动态选择，严格要求 K，不足即报错，不补因子、不因结果好坏改变规则。
冻结原 Splitter 已审计的行索引，再交由原 Runner 执行训练、Validation 选参与 Test 预测。
A/B 同一预定集合和顺序，C/D 同一 Train Top-K 集合和顺序。
方向只取已成熟 Train 标签的逐日 RankIC 均值符号；A/C/E 的方向全部为 +1。
固定因子不应用动态筛选门槛；固定 B 若 Train 方向证据不足则显式失败。

复用 P2-07 的 32 因子池、24 只合成证券和 300 个会话，研究表 5630 行，3 折共享 Test。
K=10。A/B 在生成任何合成标签前，按 builtin ID 字典序取前 K 个：
`AMIHU20, MA20_MA60, MA5_MA20, MOM20, MOM60, PRICE_MA20, REV1, REV5, TURN20, TURN5`。
C/D 从相同 pinned 32 因子来源中逐折选 10。
E 保留原 14 builtin 因子及顺序、关闭定向；E 不参与任何主消融差异。
本次所有组共享参数候选：Ridge alpha=(0.1,1.0)，LightGBM/XGBoost 各比较 12/20 棵树，
其余参数见 manifest；Validation 决定胜出候选，Test 不参与选择。
E 是旧 14 因子规格的本次重跑；树模型候选较 P2-07 增加 20 棵树，故不能把 E 的树模型数值当作 P2-07 原样复刻。

FactorScore 对已应用方向的输入使用正等权 1/K（E 为 1/14），绝对权重总和为 1，
`normalize_by_absolute_weight=False`，不再乘第二次符号，不混入原配置负权重。
缺失值仍由原模型按既有规则处理。

## 描述性结果

全体有效 OOS 日期等权 RankIC（不是三折均值的简单平均）：

| model | A | B | C | D | E |
|---|---|---|---|---|---|
| factor_score | 0.000523 | 0.409957 | -0.089255 | 0.338767 | -0.007964 |
| lightgbm | 0.413994 | 0.417678 | 0.426118 | 0.422430 | 0.413497 |
| ridge | 0.425616 | 0.425616 | 0.421406 | 0.421406 | 0.408082 |
| xgboost | 0.399078 | 0.397442 | 0.397071 | 0.396110 | 0.394080 |

FactorScore 的主要改善来自定向，动态选择本身在本次样本中降低表现；固定定向 B 优于动态定向 D。
Ridge 对符号变换保持相同预测；LightGBM 的 C 优于 A，但 D 低于 C；XGBoost 各效应较小且没有统一改善。

同折差异的三折等权均值；C−A 为动态选择效应、B−A 为固定定向效应、D−C 为动态定向效应，
D−C−B+A 为交互。逐折完整六组配对、交互及 IC/ICIR 差异见 contrasts.csv。

| model | C-A | B-A | D-C | D-C-B+A |
|---|---|---|---|---|
| factor_score | -0.089389 | 0.410466 | 0.428633 | 0.018167 |
| lightgbm | 0.012199 | 0.003653 | -0.003696 | -0.007349 |
| ridge | -0.004252 | 0.000000 | 0.000000 | 0.000000 |
| xgboost | -0.001875 | -0.001645 | -0.000980 | 0.000665 |

以下逐折 Test 指标 ICIR 为逐日 IC 均值/样本标准差（ddof=1），不年化；
标准差 ≤1e−12 时 ICIR 为无定义，避免机器精度波动形成巨大比率。
RankICIR 亦按此规则保存于 metrics.csv。

逐模型、逐组、逐折 RankIC、IC、ICIR 与有效日期数完整见 [metrics.csv](metrics.csv)。
差异逐日核对证券键、标签与有限成对观测身份，要求整折全部计划日期可比；
日期/证券/标签不一致或任一组日度相关无定义时，差异留空并写出状态和有效/总日期数。
不以共同子集悄悄剔除某组表现。ICIR 分母无定义另标 `UNDEFINED_ICIR`。
本次 RankIC 配对状态：`{"OK": 84}`。
这些是单一种子、三折合成描述性结果，扩展训练折相互依赖；不提供显著性推断，
不得据此宣称真实市场有效、可交易或金融准入。

## 稳定性与成本

折间指标标准差、有效折数见 volatility.csv；入选频率包含所有 32 候选（含零频率），见 frequency.csv。
Jaccard 对所有折对计算，见 jaccard.csv；A/B/E 恒为 1，C/D 共享以下动态选择：

- 折 0：REV5, PRICE_MA20, MOM20, MA20_MA60, MOM60, REV1, TURN5, VOL20, VOLUME_RATIO, AMIHU20；方向 `{'REV5': -1, 'PRICE_MA20': 1, 'MOM20': 1, 'MA20_MA60': -1, 'MOM60': -1, 'REV1': -1, 'TURN5': -1, 'VOL20': 1, 'VOLUME_RATIO': -1, 'AMIHU20': 1}`。
- 折 1：REV5, PRICE_MA20, MA20_MA60, MOM20, MOM60, REV1, TURN5, TURN20, VOLUME_RATIO, synthetic_015；方向 `{'REV5': -1, 'PRICE_MA20': 1, 'MA20_MA60': -1, 'MOM20': 1, 'MOM60': -1, 'REV1': -1, 'TURN5': -1, 'TURN20': -1, 'VOLUME_RATIO': -1, 'synthetic_015': 1}`。
- 折 2：REV5, PRICE_MA20, MA20_MA60, MOM20, MOM60, REV1, TURN20, TURN5, VOL20, AMIHU20；方向 `{'REV5': -1, 'PRICE_MA20': 1, 'MA20_MA60': -1, 'MOM20': 1, 'MOM60': -1, 'REV1': -1, 'TURN20': -1, 'TURN5': -1, 'VOL20': 1, 'AMIHU20': 1}`。

动态 Jaccard：

| left_fold | right_fold | jaccard |
|---|---|---|
| 0 | 1 | 0.666667 |
| 0 | 2 | 0.818182 |
| 1 | 2 | 0.666667 |

RankIC 折间标准差：

| model | A | B | C | D | E |
|---|---|---|---|---|---|
| factor_score | 0.040350 | 0.040316 | 0.014390 | 0.027643 | 0.034615 |
| lightgbm | 0.019893 | 0.020422 | 0.027274 | 0.025122 | 0.021793 |
| ridge | 0.035176 | 0.035176 | 0.033453 | 0.033453 | 0.039870 |
| xgboost | 0.025173 | 0.025621 | 0.034233 | 0.033626 | 0.032497 |

共享因子选择耗时 51.54 秒，Python 跟踪峰值
19.73 MiB，只计一次；未向各模型重复摊入。
整次演示含数据构建、sidecar 核验、60 个模型保存和精确重载证明共 444.04 秒。
逐组累计耗时和折峰值如下（非隔离生产基准，部分测试同期运行）：

| group | model | runner_seconds | fit_validation_seconds | candidate_fits | python_peak_MiB | sampled_peak_RSS_MiB |
|---|---|---|---|---|---|---|
| A | factor_score | 14.789067 | 0.902161 | 3 | 20.883253 | 235.093750 |
| A | lightgbm | 16.556606 | 3.601258 | 6 | 25.516420 | 257.316406 |
| A | ridge | 16.105001 | 2.095634 | 6 | 20.888394 | 238.730469 |
| A | xgboost | 15.120973 | 2.619039 | 6 | 20.909904 | 378.679688 |
| B | factor_score | 13.957372 | 0.815155 | 3 | 20.881650 | 378.878906 |
| B | lightgbm | 18.184349 | 1.866863 | 6 | 20.950239 | 383.097656 |
| B | ridge | 16.210723 | 2.181193 | 6 | 20.889503 | 380.824219 |
| B | xgboost | 16.027689 | 2.298185 | 6 | 20.911543 | 384.445312 |
| C | factor_score | 15.493519 | 0.884769 | 3 | 20.881978 | 384.804688 |
| C | lightgbm | 16.883308 | 2.163966 | 6 | 20.893509 | 387.113281 |
| C | ridge | 16.420698 | 1.734596 | 6 | 20.886488 | 386.207031 |
| C | xgboost | 16.448188 | 2.438779 | 6 | 20.963108 | 389.503906 |
| D | factor_score | 15.944698 | 0.904160 | 3 | 20.880512 | 390.230469 |
| D | lightgbm | 17.005677 | 2.436229 | 6 | 20.892973 | 390.753906 |
| D | ridge | 15.608050 | 1.710384 | 6 | 20.887803 | 389.421875 |
| D | xgboost | 16.778710 | 2.658623 | 6 | 20.880197 | 390.832031 |
| E | factor_score | 16.706006 | 0.811777 | 3 | 23.850372 | 393.750000 |
| E | lightgbm | 18.868490 | 2.282421 | 6 | 23.859688 | 396.421875 |
| E | ridge | 16.777851 | 1.834737 | 6 | 23.854634 | 395.695312 |
| E | xgboost | 16.387233 | 2.007594 | 6 | 23.857860 | 397.792969 |

runner_seconds 含所有候选拟合、Validation 评分、三阶段诊断与模型保存；
fit_validation_seconds 为候选训练及 Validation 选参合计，不等同于纯底层 fit 耗时。
不含输入变换、独立重载证明与共享选择。candidate_fits 给出实际候选训练次数。
Python 峰值是每折启动 tracemalloc 后新增分配的峰值，组汇总取折最大值；
RSS 每 20ms 采样，含原生库、共享数据及进程存量，是真实峰值的下界，不能用于因果性能比较。
OS 进程生命周期峰值 RSS 为 398.80 MiB。

## 验证与审计

新增测试 14 PASS，相关回归 81 PASS，最终共 95 PASS / 0 FAIL，退出码均为 0。
合成演示退出码 0，60 个选中模型保存重载后的 Test 预测与实际 Runner 输出逐值完全一致。
覆盖 A/B 与 C/D 同集合同顺序；禁用方向全 +1；Train 扰动影响方向；每折 Validation/Test 标签扰动
不影响该折选择/方向；标签成熟边界；四模型每个候选实际 fit 的输入；负向因子反例；
A/E 与原固定 Runner 的逐日 Test 指标和观测身份精确一致；摘要独立复算；
零方差 ICIR、缺组/缺日期/缺证券及无定义相关的显式状态；频率/Jaccard 独立集合运算；数据身份失配拒绝。
相关 81 项覆盖原选择器、Runner、阶段审计、预测指标、工厂调用与金融准入边界。

开发失败如实保存在 validation.json：首次测试收集导入路径错误；6 个 list/tuple API 失败；
2 个 CSV 往返时机器级方差引起的 ICIR 失败。均已修复，未修改测试集、历史 Oracle 或金融门禁。
本次未跑已知历史文件依赖失败矩阵、双 Python 全矩阵、Legacy 回归、B4/B5 冻结或独立复审。

plans.json 保存每折集合、方向、Train 哈希、三段窗口、固定规格、动态选择哈希和配置绑定；
audit.csv 保存每阶段输入/训练输入/模型文件/预测哈希，以及与计划结合的 model_prediction_identity；
validation.csv 保存所有候选的 Validation 分数和选择；manifest.json 保存 pinned refs、完整候选参数及数据身份。
每个模型原本另有 .ablation.json sidecar；可重建模型与 sidecar 在验收后清理，关键身份仍保留于摘要。
quality.csv/redundancy.csv 只存逐折质量与相关性摘要，不存大规模逐因子逐日明细。
daily.csv 仅保存日度 IC、RankIC、观测计数及身份（2640 行），无需完整预测即可复算主统计。
recomputation.json 绑定必要摘要哈希和复算证明；validation.json 绑定测试退出码、数量与日志校验和。

## 复现

使用已有 Python 3.13.9，未创建环境。在仓库根目录 PowerShell：

```powershell
$python = 'D:/Environments/factorlab-py313/Scripts/python.exe'
New-Item -ItemType Directory -Force local-validation | Out-Null
& $python -B -m pytest tests/research/test_factor_ablation.py -q -p no:cacheprovider --basetemp=local-validation/repro-p208
& $python -B -m pytest tests/research/test_walk_forward_factor_selection.py tests/research/test_walk_forward_runner_phase2.py tests/research/test_walk_forward_stage_diagnostics.py tests/research/test_prediction_metrics_phase2.py tests/research/test_walk_forward_factory_equivalence.py tests/research/test_local_admission_boundaries.py -q -p no:cacheprovider --basetemp=local-validation/repro-regression
$env:PYTHONPATH = 'src'
& $python -B -m examples.research.factor_ablation_demo --output local-validation/repro-demo
& $python -B src/factorlab/validation/ablation_statistics.py --summary docs/validation/p2-08 --output local-validation/recomputed
```

全部临时文件位于 D:/Code/FactorLab-worktrees。交付保留报告、摘要、原始测试日志/XML、退出码与关键哈希；
不保存完整合成面板、重复预测、大型归档或可重建模型。仅创建 Draft PR，不自动合并。
