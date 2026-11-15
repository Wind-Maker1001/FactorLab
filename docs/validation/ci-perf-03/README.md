# CI-PERF-03：真实 CI 时间证据与模块分片研究

NO LIVE MONEY。

## 来源与计时口径

采用两次已成功完成、attempt 1 的 GitHub Actions 运行，分别保存，不混合成一次测量：

| Run | PR head SHA | 实际受测 subject SHA | Python 3.13 / 3.14 各自收据数量 |
|---|---|---|---:|
| [43717685960](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43717685960) | `2670ed3d67a63d912948ec431e0ce7ccaa57912b` | `1e1a6e12f27ce5e94092a4c3ff8be05274e3b5c8` | 7,679 |
| [43721499381](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43721499381) | `8ff6c4933e0136e91e4ff923efbdb2562af87aac` | `00456e49bf0f4a51baa333f6b52b2074ed895f55` | 7,712 |

运行页面的 head SHA 不是执行的事件合并 subject SHA。完整身份（包括 base/event/run/attempt）、原始文件摘要、artifact ID、GitHub job/step 时间、Runner 名称和平台保存在 `provenance-*.json`、`source-metadata-*.json` 与 `run-*.json`。逐模块来源在 `module-timings.csv`，包含版本、实际测试数量、artifact、shard、job ID。原始下载保持在仓库外；收据、selection、execution、JUnit、dependencies 均作为输入读取，不修改。

两个版本分别为 CPython 3.13.17、3.14.10，平台为 `Windows-2025Server-10.0.26100-SP0`，GitHub 标签为 `windows-latest`。两个版本的依赖摘要分别核对，不能要求它们相同。各版本三片依赖、输入与运行身份一致；实际 started/completed、JUnit 和 selected 均与旧分配独立重算结果匹配，三片并集等于完整非历史集合，并与独立聚合产物相符。

任务背景的 7,866 条不是上述两次原始收据的数量。对 `ac3505faa13f2cc9533fb13ba16551c4d521cd36` 独立 collection 得到 7,712 条、314 个模块；与最后一次成功运行节点及测试模块源码摘要一致，当前目标集合全部有实测。旧权重对其中 125 个模块没有测量，均按 205.881 秒储备；这会把大量很快的新模块当作高成本，解释了旧分配与实测负载的偏离。更早运行与当前集合不同的模块保留 PARTIAL_OR_CHANGED / UNMEASURED 标识。

**模块成本 = JUnit testcase `time` 的和。** 原始运行使用 pytest 8.4.2；其 JUnit 默认 `junit_duration_report=total` 累加 setup/call/teardown，单条时间四舍五入到毫秒。收据命令没有改变该设置，受测 `pyproject.toml` 与本次基线相同，未配置覆盖。数值不是仅 call 时间，也不是整个模块独立进程的墙钟成本：共享 fixture 的 setup/teardown 归属于触发它的 testcase，重新分片可能改变归属和重复次数。没有逐 phase 完整计时日志，不能精确重构反事实分片下的 fixture 总成本。

JUnit suite 时间另含 collection/session 开销；receipt elapsed 包括执行及输入快照等工作；GitHub test step 还含 wrapper 启动等成本；job 另含环境准备及上传清理。各层分开保存，不把安装时间摊成模块实测值。单次运行差异同时含 Python、依赖和 Runner 噪声，不解释为语言版本的因果性能差异。

## 可复现

需要 Python 3.11+、已认证的 `gh`，仅下载已有成功运行，不启动 CI。所有目录参数均为新目录；脚本从仓库运行，下载内容不作为代码加载：

```bash
python -I docs/validation/ci-perf-03/fetch.py --run 43717685960 --output ../ci-p03-raw-43717685960
python -I docs/validation/ci-perf-03/fetch.py --run 43721499381 --output ../ci-p03-raw-43721499381
python -I docs/validation/ci-perf-03/profile.py \
  --evidence ../ci-p03-raw-43717685960 ../ci-p03-raw-43721499381 \
  --old-weights docs/validation/ci-perf-03/previous_module_timings.json \
  --target-selection docs/validation/ci-perf-03/target-inventory.json \
  --output ../ci-p03-rebuilt
```

`fetch.py` 通过固定 artifact ID 下载，并对下载后的证据文件核对已记录摘要。API 元数据是可变的（例如 artifact 到期状态），原始采集快照另存于来源文件；日后重新采集不应覆盖历史快照。产物到期前应归档原始下载；不能用重跑代替这两次测量。

`previous_module_timings.json` 冻结旧输入以便重放，正式权重更新后不能拿新权重验证旧分配。固定的 `target-inventory.json` 使复现不依赖未来 main 的测试增长。要研究另一个提交，先在其受控依赖环境中用现有 current selector 做 `--collect-only`，显式提供新的 selection，不执行或删减测试。

## 分配与模型解释

按模块不可拆的确定性 LPT 分配；排序为耗时降序、模块名，平局选编号最小的最空分片。不均衡率为 `max / mean - 1`。下界为 `max(total / k, largest_module)`；与下界差距是最优性区间，不把 LPT 称为已证明全局最优。

每个版本提供按自身实测重排的候选，以及同一 Python 3.13 分配在另一版本上的评估（`shared_python313_assignment_metrics`）。后者对应现有单权重文件接口；版本各自最平衡，不等于同一个正式分配在两个版本都平衡。

新模块及节点集合发生变化的模块在当前预测中均为 UNMEASURED：估计不低于 60 秒且不低于最大有效实测模块。CSV 的 PARTIAL_OR_CHANGED 仍保留历史测量值，但不冒充整个当前模块的测量。正式候选仅收录节点集合完全一致的模块。

P01/P02 情景分别对 `tests/integrations/test_demo_grid_supervisor.py`、`tests/evidence/test_pit_archive_verifiers.py` 做局部敏感性分析，剩余成本比例为 100%、75%、50%、25%、0%。这些是模块级假设，不是源函数优化结果，也不假定其他模块同样受益；没有引用未完成任务的本地成绩。四类情景保持目标测试集合相同，0% 是乐观端点而非可实现承诺。正式权重只使用原始未优化实测。

Runner 模型在每片测试本体外加该版本实测最大的 test-step 残差与非测试 job 开销；因此是保守的开销包络，不是新 CI 测量。总计算秒数与每 job 向上取整分钟分别报告，分钟不是美元价格，也不包含付费计划、平台倍率或免费额度。6→8 个 Runner 增加两个独立环境的准备/收尾成本；并发容量不够时还会排队。

端到端模型包含 run 创建至 risk 完成的前缀、分片派发、job 非测试开销、测试步骤、coverage 验证和 merge gate。其他并行任务分别采用原始排队结束时间和去掉额外排队的敏感性假设；两者不能混为已实现的 15 分钟结果。

## 正式权重暂不替换

**候选权重为 `candidate_module_timings.json`，正式 `tools/ci/current_module_timings.json` 保持不变。** 当前 `current-coverage` job 从 PR base 检出可信聚合器及其权重，而候选侧 current selector 使用候选树的权重。两侧都通过 `partition` 计算期望分配；只替换候选文件会导致 trusted-base 分配与候选执行不一致，覆盖验证拒绝。这不是分片数量问题，三片同样会发生。

TODO: 在另行审查的可信迁移中协调权重发布与执行/聚合输入绑定，再启用本候选；不得通过放松 coverage 检查使其通过。本研究不修改工作流或可信门禁。

候选保留 V1 schema 和 Python 3.13 标量口径，并附新来源、历史来源及未测量模块。优化敏感性值不进入候选 MEASURED 数据。

## 结果

详见 [RESULTS.md](RESULTS.md)。`run-*.json` 包含完整情景网格、模块分配、理论下界和共享权重分配评估。

报告表格可用 `python -I docs/validation/ci-perf-03/report.py` 从已生成 JSON/CSV 重建。当前集合采集可用独立的 Python 环境运行：

```bash
python -I docs/validation/ci-perf-03/collection.py --output ../ci-p03-collection
```

依赖采用 `.github/workflows/factorlab-pr.yml` 的 extended 安装集合；`py313-win64-full.lock.txt` 本身不含全部供应商和运行时依赖，单独安装不足以完整 collection。

定向验证：

```bash
python -m pytest -q docs/validation/ci-perf-03/test_profile.py tests/security/test_validation_v2.py
```

2026-11-15 定向验证：85 passed in 3.83s；两次真实运行均完成端到端证据重建。覆盖 JUnit 参数化/嵌套类、无效与重复证据、模块原子性、三/四片确定性、未知与源码变化模块、局部敏感性，以及单边更新权重触发可信分配拒绝。现有三分片验证测试同时通过。

