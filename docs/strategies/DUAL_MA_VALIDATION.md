# 双均线软件兼容性验证

日期：2026-11-04，Asia/Shanghai。
分支：`codex/dual-ma-2018-20261104`。
起点：最新读取的 `origin/main` = `da5ed287d328d5a65b1da94393695b75b372652a`。

`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

|检查范围|实际结果|结论范围|
|---|---|---|
|新增策略、账本、报告、CLI（Python3.14.0）|49通过|双均线软件合同|
|同一新增测试（Python3.13.9）|49通过|声明的兼容范围|
|现有 core 回归|455通过，0失败/错误/跳过|原因子、Golden、四模型、walk-forward、统计及两级回测|
|现有 financial-gates 回归|129通过，0失败/错误/跳过|既有安全与真实数据拒绝边界|
|wheel构建、隔离安装、年度合成CLI|完成|独立模块可分发、可执行；非真实数据资格|
|真实CLI请求|退出码2，输出目录不存在|既有独立信任根拒绝仍有效|
|历史定向检查（源码导入环境修正后）|4通过、1失败、1收集错误|历史对象与控制文件仍阻塞|
|额外完整 Legacy 回归|NOT_COMPLETED；约20分钟后在20%左右停止，进度已有F/E|不能声称完整回归PASS，不能推算最终失败总数|
|中国石油2018真实回测|BLOCKED / NOT_RUN|原始2018数据与公司行为执行/金融资格未具备|

core 和 financial-gates 使用现有 `tools/ci/integration_checks.py` 的原始套件定义。
最终提交还以独立源码环境重新执行新增测试与相关套件；确切最终提交号、逐输入哈希、
解释器、命令、依赖版本和JUnit统计保存在本次外部证据包的最终收据中。
不能用本表的增量结果代替完整项目验收、远程CI或合并批准。

历史定向检查的失败为
`tests/market_structure/test_delivery.py::test_protected_tracked_content_is_unchanged`：
Git无法读取原历史对象 `39eecf869c9c2075ba3f6e3c76aeba3338c63432`。
收集错误为 `tests/market_admission/test_revision_contract.py`：缺少原
`.github/workflows/market-admission.yml`。两项均已存在于仓库原有
`docs/ci/CI_BASELINE_FAILURE_MATRIX.csv`。本任务没有补造Git对象、修改历史预期、
跳过旧断言、重生成Golden或降低准入门禁。

首轮历史定向检查另有一次子进程无法导入FactorLab的环境失败；当时借用的依赖环境
没有安装本工作区源码。原始日志保留，未把它算成历史拒绝。后续建立任务独立venv，
通过显式源码/只读依赖路径确认 `factorlab.__file__` 指向当前工作区；子进程CLI检查
恢复通过，仍保留上面两项真实历史阻塞。未修改共享Python环境。

初轮新增测试48通过、1失败来自新测试样例：所称“已多头”输入实际产生真实金叉。
已按手工均线重新构造样例，保留初轮失败日志，后续49项全部执行；未改旧测试或旧Golden。

实现复用原订单执行器与持仓账本，未编辑 Fast、原Execution、Top-K、费用表、
涨跌停规则、因子注册表、ModelFactory、安全/身份/准入代码、冻结控制或已有Golden。
新增模块及测试已通过 `git diff --check`。真实复权与公司行为限制见
[数据阻塞报告](DUAL_MA_2018_DATA_STATUS.md)，行为及复现命令见
[策略指南](DUAL_MA.md)。

原始供应商数据、绝对本机路径、日志与JUnit保留在本次本地外部证据包，不提交仓库。
远程CI状态须按PR实际HEAD另行读取；PR保持Draft，不自动合并。
