# CI 精简验收

main 已启用绑定独立 GitHub App 的受信任合并门禁。三分片工程验收状态为 `SHARD_ENGINEERING_PASS`；Full CI 发布状态为 `FULL_RELEASE_BLOCKED`。

[Full CI #42516533467](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42516533467)（提交 [`31702b4`](https://github.com/Wind-Maker1001/FactorLab/commit/31702b4)）中 Python 3.13/3.14 六片全部 PASS，实际覆盖汇总和 Independent V2 比较 PASS，端到端墙钟时间 **16 分 50 秒**，达到 15–18 分钟目标。历史隔离仍实际执行 186 条，50 FAIL / 136 ERROR，退出码 1；该历史失败单独报告，不计作本轮新回归、不转换为通过，因而 Full release gate 继续 FAIL。

必要收据：[`verified-current-coverage`](https://api.github.com/repos/Wind-Maker1001/FactorLab/actions/artifacts/13631599845)、[`independent-v2-coverage`](https://api.github.com/repos/Wind-Maker1001/FactorLab/actions/artifacts/13631878746)、[`full-legacy-windows-3.14`](https://api.github.com/repos/Wind-Maker1001/FactorLab/actions/artifacts/13631791937)。此前 collection 阻塞 [#42515899004](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42515899004) 已由显式 `UNMEASURED` 保守权重修复；定向回归 44 项通过（2.85 秒）。

`SHARD_ENGINEERING_PASS` 仅表示六片实际覆盖、分片互斥/全集并集和 V2 比较通过；不表示金融发布或历史迁移通过。`FULL_RELEASE_BLOCKED` 是预期的真实历史失败传播。该状态不构成正式金融准入或发布许可。

## 执行路径

- 原 PR：Windows 3.13/3.14 各重复执行增量与全部软件测试，另行执行 Legacy、V2 比较、打包、双平台锁压力与历史锁诊断，共 8 个 Job。
- 普通 PR：main 上的 `pull_request_target` 工作流判定风险；Windows Python 3.13 执行原 `integration_checks.py` 的 incremental、configuration、locking 与安装/依赖检查。三个增量套件目标不变，不删除任何测试。
- 扩展 PR：基于 merge-base 文件差异与 Python 静态 import 消费者闭包判断影响；仅显式覆盖的模块可走快速路径。未知、新增、删除、重命名、文件类型/权限变化保守追加完整软件测试与 Windows/Linux 锁压力。CI、测试、测试配置、依赖及基线变更追加双版本完整软件验证、打包和 Environment 人工批准。标签与标题不参与选择。
- Full：手动、每周一 UTC 03:17、`v*` 标签触发；Python 3.13/3.14 各三个独立 Windows Runner，每片串行。保留 V2、双平台锁验收、历史锁诊断与包验收；历史作业仅执行原 186 条隔离记录，不重复非历史基线。旧 `factorlab.yml` 保留手动兼容入口。
- 分片：冻结的 `validation_layers.py` 先生成完整非历史清单，新插件再按模块分配。耗时来自 [42508208514](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42508208514) 的成功 `extended-3.13` JUnit（5584 条），来源及模块实测秒数保存在 `tools/ci/current_module_timings.json`。分片内保留原收集顺序；输出实时进度及 `--durations=30`。没有历史 JUnit 耗时的模块使用 `max(60 秒, 历史最慢模块实测秒数)` 作为确定性保守分配权重，在 `selection.json` 的 `module_plan` 中标记 `UNMEASURED`；该权重不是实测时间。已有耗时标记 `MEASURED` 并优先使用，不跳过模块；不足三个模块而产生空分片时明确失败。
- 覆盖：聚合器独立重算分片，核对实际开始、完成及 JUnit 三份记录，要求每版本三片无重复、互不重叠且并集严格等于原完整非历史清单；同时核对提交、依赖、运行时及冻结输入。分片失败、缺失、取消或覆盖异常均阻断 `Full release gate`。
- 发布：`Full release gate` 要求所有完整检查成功。历史原始非零退出码继续导致失败；V2 对照 186 条历史记录（50 FAIL、136 ERROR）、已验证的 current 实际执行结果及原始冻结输入，不再额外执行一遍完整基线。历史失败未解决时完整发布仍阻断，不修改历史矩阵、证据定义或正式准入。

## PR Extended 三分片验收

本次定向验证：本地 Python 3.12.7 执行 `tests/security`，188 项通过（142.24 秒），包含六片聚合、负例、可信风险分类、门禁和独立安全边界；`actionlint` 1.7.12 检查 PR / Full 工作流通过。

PR 接入的真实六分片运行：`NOT_RUN`。合成安全收据不作为真实测试覆盖或性能证据；本 PR 的新方案耗时、Runner 总时间与新旧覆盖一致性均为 `NOT_MEASURED`。上线前的可信验证继续使用 main 的工作流，不能用候选工作流替代控制面批准。

旧未分片 PR 实测基线为 [42508208514](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42508208514)：3.13/3.14 Extended Software Job 分别为 2296 / 2200 秒，总 Runner Job 时间 4496 秒，最长 2296 秒；其中实际 current 测试步骤为 2196 / 2103 秒。Job 时间包含安装和制品上传，不含排队。

已有 Full 三分片 [42516533467](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42516533467) 的 GitHub Job 实测：3.13 shard 0/1/2 为 642 / 837 / 651 秒，3.14 为 832 / 607 / 952 秒；六片合计 4521 秒，最长 952 秒，覆盖汇总 14 秒。这是不同提交上的 Full 历史参考，不是本 PR 的性能测量或加速比。

PR 身份绑定包含事件 PR HEAD、固定 merge subject、可信 base、事件 SHA、run ID 和 run attempt。只重跑失败 Job 会留下旧 attempt 的成功片，汇总将拒绝混用；需要完整重跑以产生同一次 attempt 的六片证据。

## 实际基线

以下为最近三个 PR 最新已完成运行；总耗时为 workflow `createdAt` 到 `updatedAt`，包含排队及依赖等待，不是单个 Job 时间。

| PR | 总耗时 | Job 数 | 3.13 增量三套 | 3.13 完整软件 | 3.13 依赖安装 | 结果 |
|---|---:|---:|---:|---:|---:|---|
| [35](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42503011080) | 44:55 | 8 | 194 秒 | 2028 秒 | 70 秒 | FAIL（Legacy 保留） |
| [34](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42502619327) | 42:08 | 8 | 102 秒 | 1196 秒 | 54 秒 | FAIL（Legacy 保留） |
| [33](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42502131279) | 42:22 | 8 | 151 秒 | 1487 秒 | 56 秒 | FAIL（Legacy 保留） |

PR 35 的 3.13 收据：core 455、cross-market 277、financial-gates 134、configuration 65、locking 99，均通过；共 1030 次测试执行（含套件间重复节点）。完整软件 5574 次测试执行。优化后仍通过同一个入口运行这些必要套件；新增安全选择器测试会使 financial-gates 数量增加。

| 指标 | 优化前（PR 35） | 优化后 |
|---|---:|---|
| Full workflow 总耗时 | 2695 秒（旧 PR 35） | **1010 秒（16:50）** |
| 3.13 完整软件耗时 | 2028 秒（旧单 Runner） | 三片分别 642、837、651 秒；并行墙钟取最长 837 秒 |
| 3.14 完整软件耗时 | 未列入旧表 | 三片分别 712、607、952 秒；并行墙钟取最长 952 秒 |
| Runner 启动（Set up job 步骤） | 待汇总 | 已包含在作业耗时；不从端到端结论中剥离 |
| Job 数 | 8 | 14（含六片、覆盖聚合与既有门禁） |
| 测试执行数量 | 完整软件 5574 次 | 六片实际覆盖并集严格等于每版本完整非历史清单；V2 PASS |
| 性能目标 | — | **达到 15–18 分钟目标** |
| 发布结果 | FAIL（Legacy 保留） | `SHARD_ENGINEERING_PASS`；`FULL_RELEASE_BLOCKED`（历史失败） |

高风险 PR 的扩展验证不承诺 3～6 分钟；普通 Fast Job 的超时为 15 分钟，不能以超时转换成功。

## 合并门禁

main Ruleset `26809886` 已启用：必须通过 `FactorLab trusted merge gate`，绑定 integration ID `5670895`；禁止强推和删除，要求经 PR 合并。`ci-control-review` 保留控制面人工批准，`ci-merge-publisher` 仅供可信最终 Job 发布精确 PR head 的状态，不执行 PR 代码。PR 工作流无写权限且 checkout 不保存凭证。Full release gate 不代替该可信 App 合并状态。

## 验证与复现

三分片定向验证：60 项通过（2.42 秒）；包括模块不拆分、完整分区、真实 pytest 执行记录、缺片、重复执行、计划正确但未执行、JUnit 漂移、失败/取消、收集错误、提交/输入/依赖/运行时漂移、金融关键节点及发布门禁传播。普通 PR 必要测试范围仍直接来自原 `integration_checks.py`；不修改共享 fixture、不启用 xdist、不修改研究算法。

```bash
python -m pytest tests/security/test_pr_risk.py tests/security/test_validation_v2.py -q -o addopts=
actionlint -shellcheck= .github/workflows/factorlab.yml .github/workflows/factorlab-pr.yml .github/workflows/factorlab-full.yml
python tools/ci/integration_checks.py --suite incremental --output local-ci/pr
python tools/ci/integration_checks.py --suite configuration --output local-ci/pr
python tools/ci/integration_checks.py --suite locking --output local-ci/pr
```

完整验证通过 GitHub Actions 的 FactorLab full validation 手动执行；历史失败仍以真实 FAIL 保留。远程测试次数、日志、Commit、PR 与 CI 链接待实测后更新。未运行的完整回归及正式部署门禁不得标为通过。

`NO LIVE MONEY`、`PIT_UNVERIFIED`、`HFQ_UNVERIFIED`、`FORMAL_ADMISSION_BLOCKED` 均保持不变。pip 下载缓存由受信任 Full 写入，Fast 仅恢复、不保存，避免 `pull_request_target` 中的 PR 代码污染 main 缓存；其余 Full Job 使用 setup-python 的 pip 缓存。不缓存研究结果、执行状态或凭证；制品保留 14 天并限于收据、选择结果和诊断日志。
