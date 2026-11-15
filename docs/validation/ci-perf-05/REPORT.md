# CI-PERF-05：全合并目标的实测重基线与双版本分片规划

NO LIVE MONEY。

## 证据范围

分析目标为 `bdd4e6f5eef14bd3f383c5af73ec695bcb34b901`。`prerequisites.json` 记录 CI-PERF-01（#172）、02（#173）、03（#175）、04（#174）、控制修复 #176、#177 的合并身份。最后合并时间为 **2026-11-15 16:47:29 UTC**。采集时该时间之后的 Actions run 数为 **0**；本研究读取既有证据，不触发 CI。

主证据是 **#173 的合并前验收 run，不是最终 post-merge 运行**。其完整节点、模块源码及受测 Python/配置输入匹配当前目标，可用于该目标的离线重基线；不能改称最终合并后的运行成绩。

| 证据 | Run / attempt | PR head SHA | 实际受测 subject SHA | 每版本节点 / 模块 | 对当前目标的适用状态 |
|---|---|---|---|---:|---|
| 主证据 #173 | [43735547182](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43735547182) / 1 | `cfdaafaee9cc1c2fad045696bc18133c59c28361` | `31346cca2d233ce65bf64728009884c53788b86c` | 7,819 / 315 | 两版本各 315 模块 MEASURED |
| 独立比较 #175 | [43735048156](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43735048156) / 1 | `0c5dfe89962fdf056f7f4eb66ab3a7fc8cd66f64` | `847e807d7a3257a8eef0d6d5ed2804d0df0b2c40` | 7,811 / 315 | 两版本各 315 模块 PARTIAL |

两次 run 的 trusted base / event SHA 均为 `27180431626e6395dfb7907bab5e81f74cbb20fe`。页面 head、执行 subject、分析目标分别保存，不能互换。完整身份及来源见 `sources.json`、`sources-43735547182.json`、`sources-43735048156.json`。封存 `risk.json` 的 current、locking、package、review 均为 true，其摘要、run/attempt、base/head/subject 与执行收据匹配；两次均为 14 个成功 job。

#175 比当前目标早，运行时差异涉及：

- `tests/evidence/test_pit_archive_verifiers.py`
- `tests/migration/test_independent_boundary.py`
- `tools/migration/verify_pit_archive.py`

适用性要求节点多重集合、模块源码摘要和运行时输入匹配；运行时不匹配使 #175 全部目标模块保守标为 PARTIAL。**PARTIAL 不否认源库存本身的观测，而是禁止其作为当前目标的完整实测权重。** `observed_inventory_plans` 保留其源库存模型。两次 run 不拼接、不平均，不用 #175 补主 run 的值。

优化前数据与当前候选的差别可直接看到：PIT 模块在 #175 的 3.13/3.14 JUnit 总和为 **165.926 / 213.783 秒**，#173 为 **69.520 / 73.151 秒**。这是不同源码与运行条件下的观测，不是受控加速比。P01 模块在 #173 为 **3.061 / 4.648 秒**。历史 run `42508208514` 的正式 V1 权重仅用于重放既有分配；更早 P03 归档不再次复制，也不作为当前测量。

## 计时口径与实际执行

模块成本是 JUnit testcase `time` 总和。原始环境 pytest **8.4.2**，默认 `junit_duration_report=total`，收据命令及配置无 duration override，因此包含 **setup + call + teardown**，不是仅 call。独立阶段秒数均为 **UNMEASURED**：CSV 三个阶段列留空，不用 0 或总值代填。

JUnit testcase 总和、suite 墙钟、receipt elapsed、GitHub test step 与 job 是不同层次。共享 fixture 成本归属于触发它的 testcase；重分片可能改变归属及重复次数。模块加性成本不是已经执行过的反事实墙钟测量，安装也不摊入模块权重。

主 run 的实际六片，单位秒；receipt 展示三位小数，JSON 保留原精度：

| Python | shard | testcase 数 | testcase 总和 | receipt elapsed | test step | install | job |
|---|---:|---:|---:|---:|---:|---:|---:|
| 3.13 | 0 | 3,130 | 712.400 | 742.184 | 744 | 82 | 858 |
| 3.13 | 1 | 2,304 | 423.748 | 438.186 | 439 | 67 | 527 |
| 3.13 | 2 | 2,385 | 517.803 | 533.835 | 534 | 77 | 636 |
| 3.14 | 0 | 3,130 | 780.208 | 813.100 | 814 | 96 | 939 |
| 3.14 | 1 | 2,304 | 774.983 | 797.535 | 799 | 94 | 926 |
| 3.14 | 2 | 2,385 | 770.018 | 791.667 | 793 | 87 | 913 |

平台 `Windows-2025Server-10.0.26100-SP0`，标签 `windows-latest`，CPython 分别为 **3.13.17、3.14.10**。Runner 名称、job ID、步骤起止、依赖摘要及逐模块 artifact/shard 映射在 analysis、sources、CSV 中；`dependencies-*.txt` 冻结实际包清单。跨版本差异同时包含解释器、依赖与 Runner 差异，不作为解释器版本的因果结论。

Runner **pre-job 内部准备时间 UNMEASURED**。可见时间轴包括主 run created → risk completed **60 秒**，risk completed → extended job started **2 秒**。这不是已精确拆分的启动或排队时间。逐 step 元数据保留 checkout、Python setup、安装、执行、上传及 post 步骤；`non_test_job_seconds` 为 job 减 test step，已包括安装，不得再次叠加。

### App 与 gate-job 两种 E2E 时钟

900 秒验收优先参考受保护 App status；离线模型明确使用 gate-job 完成时钟。

| Run | run created | 受保护 App success | App E2E | gate job completed | gate-job E2E |
|---|---|---|---:|---|---:|
| #173 / 43735547182 | 16:27:12 UTC | 16:44:16 UTC | **1,024 秒** | 16:44:19 UTC | **1,027 秒** |
| #175 / 43735048156 | 16:21:46 UTC | 16:42:16 UTC | **1,230 秒** | 16:42:19 UTC | **1,233 秒** |

日期均为 2026-11-15。`commit-statuses-*.json` 保存原始 App 记录；分析核对 context、publisher、success 与指向该 run 的 URL，sources 的 `protected_status_e2e_seconds` 保存相应值。后续预测 E2E 为 **run created → FactorLab merge gate job completed**，不是 App status 的精确预测。

## 理论下界与独立权重分配

直接调用 `tools/ci/sharding.py:partition`，不另写近似算法：模块不可拆，按 `(-seconds, module)` 排序，选择 `(current_load, shard_index)` 最小者。理论下界为 `max(total / k, largest_module)`；接近下界只说明加性成本模型内平衡，不证明重新运行后的墙钟最优。

| Python | shard 数 | 实测成本总和 | 理论下界 | 各 shard 模型负载 | 最大负载减下界 |
|---|---:|---:|---:|---|---:|
| 3.13 | 3 | 1,653.951 | 551.317000 | 551.317 / 551.317 / 551.317 | 0.000000 |
| 3.13 | 4 | 1,653.951 | 413.487750 | 413.488 / 413.488 / 413.487 / 413.488 | 0.000250 |
| 3.14 | 3 | 2,325.209 | 775.069667 | 775.069 / 775.070 / 775.070 | 0.000333 |
| 3.14 | 4 | 2,325.209 | 581.302250 | 581.303 / 581.302 / 581.302 / 581.302 | 0.000750 |

每模块分配见 `analysis-43735547182.json` 的 `versions[version].plans["3"|"4"].assignments`；其 metrics 与 prediction 分别存放模型本体和墙钟层级。

### 共用权重的跨版本偏差

同一份分配分别用两版本各自实测成本评估；不是用共用权重自证两版本负载。

| shard 数 | 分配来源 | 在 3.13 上的最大负载 | 在 3.14 上的最大负载 |
|---|---|---:|---:|
| 3 | 现有 V1 | 712.400 | 780.208 |
| 3 | 共用 3.13 权重 | 551.317 | 789.150 |
| 3 | 共用 3.14 权重 | 580.177 | 775.070 |
| 4 | 现有 V1 | 449.587 | 624.353 |
| 4 | 共用 3.13 权重 | 413.488 | 633.702 |
| 4 | 共用 3.14 权重 | 465.839 | 581.303 |

四片共用 3.13 权重在 3.14 上比其下界高 **52.399750 秒 / 9.014%**；反向共用在 3.13 上高 **52.351250 秒 / 12.661%**。独立版本权重四片的 E2E 包络不能直接用于任意共用权重方案。完整 shared 分配及评估在 analysis 的 `shared` 中。

## 墙钟情景区间与 900 秒预算

模型添加同 run、同版本 test-step 残差与非测试 job 开销的观测范围，再结合可见前缀、dispatch、coverage/gate 依赖路径：

- 3.13：test-step 残差 **15.252–31.600 秒**；非测试 job **88–114 秒**。
- 3.14：分别 **22.982–33.792 秒**、**120–127 秒**。
- 主 run coverage wait **2 秒**、执行 **14 秒**，gate dispatch **4 秒**、执行 **6 秒**。
- 其他并行分支固定在观测完成点 **622 秒**；先将 coverage 加到 extended 路径，再与其他分支取 `max`，最后加 gate 等待/执行。并行 job 不求和。

| Python | shard 数 | 测试本体模型最大负载 | test-step 区间 | job 区间 | E2E 区间 |
|---|---:|---:|---:|---:|---:|
| 3.13 | 3 | 551.317 | 566.569–582.917 | 654.569–696.917 | 742.569–784.917 |
| 3.13 | 4 | 413.488 | 428.740–445.088 | 516.740–559.088 | 632.000–647.088 |
| 3.14 | 3 | 775.070 | 798.052–808.862 | 918.052–935.862 | 1,006.052–1,023.862 |
| 3.14 | 4 | 581.303 | 604.285–615.095 | 724.285–742.095 | 812.285–830.095 |

这些是 **同 run overhead envelope，不是置信区间**。测试本体为固定观测成本的加性离线点预测，下界与实际 LPT 最大值另列；不把该点包装成重复采样区间。墙钟包络组合了观测开销的上下端，不能解释为 P95、未来保证或真实四片成绩。

两版本并行，完整 E2E 取较慢路径：独立权重三片 **1,006.052–1,023.862 秒**，仍超 900 秒；四片 **812.285–830.095 秒**，上沿余量 **69.905 秒**。四片是模型内可用预算候选，不是实际通过。它将 extended 从六个 job 增至八个，第四 Runner 的排队、fixture 重复和并发影响尚无实测。较本次 App 终点多含的约 3 秒不用于承诺未来 App 完成时间。

实际主 run App **1,024 秒**、gate-job **1,027 秒**均超预算。最终全合并后运行及真实四片验收仍需独立证据；本研究不以采样为由触发完整 CI。对于 #175 当前目标的 PARTIAL 输入，脚本保留原算法缺测估算，但明确标为 `INCOMPLETE_INPUT_ESTIMATE` / `UNDETERMINED_MISSING_MEASUREMENTS`，不据此作预算通过判断。

## 候选接口与归档

`candidate_module_timings.json` 为 **FACTORLAB_MEASURED_MODULE_TIMINGS_V2**，顶层包含 `analysis_target`、`evidence_manifest`、`versions`。每版本表保留 source_run、三个 source_artifact 名称、subject_sha、python、test_count、module_seconds 及详细身份/口径。主候选两表各 315 模块，未测量模块 0。

- `candidate-43735547182.json`、`candidate-43735048156.json` 分别绑定独立源 run；后者的 PARTIAL 不升级为 MEASURED，不将观察旧值填入候选 module_seconds。
- `candidate-python-3.13.json`、`candidate-python-3.14.json` 为分别导出的 V1 候选。
- 候选状态 `CANDIDATE_NOT_ACTIVE_PREMERGE_OBSERVATION`。不修改 `tools/ci/current_module_timings.json`、工作流，不启用四片。

可信聚合器与候选执行侧均独立计算预期分配；单边替换权重会不一致。启用双版本计划须同时绑定可信执行/聚合输入，不能放松覆盖检查。

原始外部目录为 `D:/Code/ci-perf-05-evidence/run43735547182`、`run43735048156`，保留 API 快照、`archives/<artifact>/download.zip` 和独立解压目录。Git 只保留规范化小型证据，不复制 ZIP、完整日志或 SQLite。sources 的 artifact ID、URL、API digest 和输入文件摘要关联原始证据；原始字节不被分析改写。

完整 node 清单在 `inventory-43735547182.json`、`inventory-43735048156.json`；固定目标为 `target-inventory.json`。跨版本相同库存只存一份，analysis 通过 inventory_reference/node_count 引用。`module-timings.csv` 保存逐 run/版本/模块观测、状态、空缺阶段时间与 artifact/job 来源。`receipts-*.json` 为规范化选择/执行/聚合相关收据摘要：重复的完整输入摘要提取至 `validation-inputs-*.json`，started/completed 数组改存计数；原始数组和 selection 保留在源 artifact，由原解析器执行无重复、无遗漏与集合一致性核验。不同输入摘要不合并。

## 复现与定向验证

从与本分析目标一致的受控仓库运行。下载需要已认证 `gh`；输出目录必须不存在。不要在下载目录启动解释器或执行下载内容；读下载数据的 Python 使用 `-I`。

```bash
python -I docs/validation/ci-perf-05/fetch.py --run 43735048156 --output D:/Code/ci-perf-05-reproduce/run43735048156
python -I docs/validation/ci-perf-05/fetch.py --run 43735547182 --output D:/Code/ci-perf-05-reproduce/run43735547182

python -I docs/validation/ci-perf-05/analyze.py \
  --normalized docs/validation/ci-perf-05 \
  --output D:/Code/ci-perf-05-reproduce/normalized

python -I docs/validation/ci-perf-05/analyze.py \
  --evidence D:/Code/ci-perf-05-reproduce/run43735048156 D:/Code/ci-perf-05-reproduce/run43735547182 \
  --target docs/validation/ci-perf-05/target-inventory.json \
  --output D:/Code/ci-perf-05-reproduce/raw-analysis
```

`--evidence` 最后一个 run 为主，不合并观测；该模式验证原始 JUnit、选择/执行、聚合、依赖摘要及封存风险身份，并从受控树读取目标运行时摘要。`--normalized` 使用冻结观测列、库存、旧权重重算分配和预测，不直接接受已保存的预测值；远端 artifact 过期后仍可重放离线计算，重新核验原始收据则使用外部保留的原始归档。

固定目标的 collection 使用现有 P03 入口，只收集、不执行测试；本次环境为 Python 3.13.9，收集结果与主 run 两版本实际完整库存逐节点一致：

```bash
python -I docs/validation/ci-perf-03/collection.py --output D:/Code/ci-perf-05-reproduce/collection
python -I docs/validation/ci-perf-05/test_analyze.py
python -m pytest -q docs/validation/ci-perf-03/test_profile.py docs/validation/ci-perf-05/test_analyze.py
```

collection 使用装有完整 extended 依赖的环境；新目标的集合另存，不以新 collection 将旧 run 变为新实测。pytest 定向验证 **21 passed in 0.59s**，涵盖复用 JUnit 映射/拒绝无效证据及新增分析模型；其中新增 unittest **10 项通过**。规范化离线重放的候选、库存和分析结果与原始证据导出的结果结构一致。原始证据模式也完成两 run、两版本的无重复、无遗漏、全 PASS 与聚合身份核验；这些不是新的 CI 或四片墙钟测量。
