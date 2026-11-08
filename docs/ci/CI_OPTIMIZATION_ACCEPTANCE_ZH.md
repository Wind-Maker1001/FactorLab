# CI 精简验收

当前状态：`BLOCKED`。Fast 远程运行数据待生成；受信任合并门禁需要独立 GitHub App 和一次 main 引导合入。不得将本报告当作正式金融准入或发布许可。

## 执行路径

- 原 PR：Windows 3.13/3.14 各重复执行增量与全部软件测试，另行执行 Legacy、V2 比较、打包、双平台锁压力与历史锁诊断，共 8 个 Job。
- 普通 PR：main 上的 `pull_request_target` 工作流判定风险；Windows Python 3.13 执行原 `integration_checks.py` 的 incremental、configuration、locking 与安装/依赖检查。三个增量套件目标不变，不删除任何测试。
- 扩展 PR：基于 merge-base 文件差异与 Python 静态 import 消费者闭包判断影响；仅显式覆盖的模块可走快速路径。未知、新增、删除、重命名、文件类型/权限变化保守追加完整软件测试与 Windows/Linux 锁压力。CI、测试、测试配置、依赖及基线变更追加双版本完整软件验证、打包和 Environment 人工批准。标签与标题不参与选择。
- Full：手动、每周一 UTC 03:17、`v*` 标签触发；保留双版本软件验证、Legacy、V2、原双平台锁验收、历史锁诊断与包验收；不重复先跑已被 current 覆盖的增量测试。旧 `factorlab.yml` 保留手动兼容入口。
- 发布：`Full release gate` 要求所有完整检查成功。Legacy 原始非零退出码继续导致失败；V2 对照 186 条历史记录（50 FAIL、136 ERROR）与全部 current 结果，区分历史状态漂移、新回归和覆盖遗漏。当前历史失败未解决时完整发布仍阻断；本任务不修改历史矩阵、证据定义或正式准入。

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

## 合并门禁激活

仓库审查时 main 无分支保护、无 ruleset。已创建两个仅允许 main 的 Environment：

- `ci-control-review`：由仓库所有者手动批准控制面变更；允许所有者批准自己触发的运行，审批发生在工作流之外。
- `ci-merge-publisher`：只供可信最终 Job 使用；不在其中执行或 checkout PR 代码。

**BLOCKED：**需要仓库所有者创建并安装独立 GitHub App，仅授予本仓库 Commit statuses 写权限。在 `ci-merge-publisher` 中配置变量 `CI_GATE_APP_ID` 和 secret `CI_GATE_PRIVATE_KEY`。不要将私钥放在仓库、普通仓库 secret 或 PR 执行环境中。

引导顺序：

1. 审查并手动合入本 Draft PR，使可信工作流进入 main；本任务不自动合并。
2. 配置 App 后，对现有 PR 同步或重新打开，生成针对精确 PR head 的 `FactorLab trusted merge gate` 状态。`pull_request_target` 自带检查绑定 base，因此最终 Job 必须另外向 head 发布 App 身份状态。
3. main 分支保护启用 strict Required Check：context 为 `FactorLab trusted merge gate`，`app_id` 必须为上述独立 App 的数字 ID，启用管理员约束、禁止强推和删除。不要选择同名 GitHub Actions 检查，也不要使用不绑定 App 的状态名。
4. 启用前确认状态来自该 App、绑定当前 head。旧 main 无强制检查可迁移；此次不会提前删除已配置的 Required Check。

引导分支 push 仅用于本次实测；它无权访问 main-only 的发布/审批 Environment，不构成合并门禁验收。普通 PR Workflow 无写权限，checkout 不持久化凭证；只有最终 Job 使用 App 安装令牌，限本仓库 statuses 权限。Environment 设置和分支保护属于独立管理边界。

## 验证与复现

已执行：选择器覆盖未知/敏感/控制面变更、跨模块 import 消费者、失败/跳过/取消状态传播；既有 V2 丢失安全测试、历史漂移、新失败和伪造库存拒绝测试。Workflow 经 actionlint 1.7.12 验证。必要测试范围仍直接来自原 `integration_checks.py`。

```bash
python -m pytest tests/security/test_pr_risk.py tests/security/test_validation_v2.py -q -o addopts=
actionlint -shellcheck= .github/workflows/factorlab.yml .github/workflows/factorlab-pr.yml .github/workflows/factorlab-full.yml
python tools/ci/integration_checks.py --suite incremental --output local-ci/pr
python tools/ci/integration_checks.py --suite configuration --output local-ci/pr
python tools/ci/integration_checks.py --suite locking --output local-ci/pr
```

完整验证通过 GitHub Actions 的 FactorLab full validation 手动执行；历史失败仍以真实 FAIL 保留。远程测试次数、日志、Commit、PR 与 CI 链接待实测后更新。未运行的完整回归及正式部署门禁不得标为通过。

`NO LIVE MONEY`、`PIT_UNVERIFIED`、`HFQ_UNVERIFIED`、`FORMAL_ADMISSION_BLOCKED` 均保持不变。pip 下载缓存由受信任 Full 写入，Fast 仅恢复、不保存，避免 `pull_request_target` 中的 PR 代码污染 main 缓存；其余 Full Job 使用 setup-python 的 pip 缓存。不缓存研究结果、执行状态或凭证；制品保留 14 天并限于收据、选择结果和诊断日志。
