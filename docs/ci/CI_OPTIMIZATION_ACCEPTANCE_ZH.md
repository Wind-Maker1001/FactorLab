# CI 精简验收

main 已启用绑定独立 GitHub App 的受信任合并门禁。Full CI 三分片验收当前 `BLOCKED`：[42515899004](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42515899004) 的六片因三个新增模块缺少历史耗时而在 collection 失败；尚无完整软件验证性能实测。已加入显式 `UNMEASURED` 估算分配，修复定向验证 44 项通过（2.85 秒），待一次必要 Full CI 重验。

该次历史隔离实际执行 186 条，50 FAIL / 136 ERROR，原始退出码 1，与原历史清单一致；单独保留为历史失败，不计为本轮软件新回归，也不转换为通过。历史失败仍阻断完整发布，不构成正式金融准入或发布许可。

## 执行路径

- 原 PR：Windows 3.13/3.14 各重复执行增量与全部软件测试，另行执行 Legacy、V2 比较、打包、双平台锁压力与历史锁诊断，共 8 个 Job。
- 普通 PR：main 上的 `pull_request_target` 工作流判定风险；Windows Python 3.13 执行原 `integration_checks.py` 的 incremental、configuration、locking 与安装/依赖检查。三个增量套件目标不变，不删除任何测试。
- 扩展 PR：基于 merge-base 文件差异与 Python 静态 import 消费者闭包判断影响；仅显式覆盖的模块可走快速路径。未知、新增、删除、重命名、文件类型/权限变化保守追加完整软件测试与 Windows/Linux 锁压力。CI、测试、测试配置、依赖及基线变更追加双版本完整软件验证、打包和 Environment 人工批准。标签与标题不参与选择。
- Full：手动、每周一 UTC 03:17、`v*` 标签触发；Python 3.13/3.14 各三个独立 Windows Runner，每片串行。保留 V2、双平台锁验收、历史锁诊断与包验收；历史作业仅执行原 186 条隔离记录，不重复非历史基线。旧 `factorlab.yml` 保留手动兼容入口。
- 分片：冻结的 `validation_layers.py` 先生成完整非历史清单，新插件再按模块分配。耗时来自 [42508208514](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42508208514) 的成功 `extended-3.13` JUnit（5584 条），来源及模块实测秒数保存在 `tools/ci/current_module_timings.json`。分片内保留原收集顺序；输出实时进度及 `--durations=30`。没有历史 JUnit 耗时的模块使用 `max(60 秒, 历史最慢模块实测秒数)` 作为确定性保守分配权重，在 `selection.json` 的 `module_plan` 中标记 `UNMEASURED`；该权重不是实测时间。已有耗时标记 `MEASURED` 并优先使用，不跳过模块；不足三个模块而产生空分片时明确失败。
- 覆盖：聚合器独立重算分片，核对实际开始、完成及 JUnit 三份记录，要求每版本三片无重复、互不重叠且并集严格等于原完整非历史清单；同时核对提交、依赖、运行时及冻结输入。分片失败、缺失、取消或覆盖异常均阻断 `Full release gate`。
- 发布：`Full release gate` 要求所有完整检查成功。历史原始非零退出码继续导致失败；V2 对照 186 条历史记录（50 FAIL、136 ERROR）、已验证的 current 实际执行结果及原始冻结输入，不再额外执行一遍完整基线。历史失败未解决时完整发布仍阻断，不修改历史矩阵、证据定义或正式准入。

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
| PR 总耗时 | 2695 秒 | NOT_VERIFIED |
| 核心测试耗时（core 收据） | 42.953 秒 | NOT_VERIFIED |
| Runner 启动（Set up job 步骤） | 待汇总 | NOT_VERIFIED |
| 3.13 依赖安装 | 70 秒 | NOT_VERIFIED |
| Job 数 | 8 | 普通路径声明 3 个运行 Job；实测 NOT_VERIFIED |
| 测试执行数量 | 增量/配置/基础锁 1030；另重复全量 | NOT_VERIFIED |
| 加速比例 | — | NOT_VERIFIED |

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
