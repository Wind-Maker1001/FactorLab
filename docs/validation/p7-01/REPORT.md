# P7-01：统一离线研究工作流

2026-11-10 · 验证进行中

`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

## 范围与架构选择

基线：`origin/main = 2fffbee1e41ba342bf0adb7835467b7474cebc17`。

统一入口覆盖自定义日线策略和 P6-R01 的多证券独立账户计算路径。直接调用 `run_custom_strategy()` 与 `account_batch()`；后者继续调用六策略 `compare()`。原研究函数、原 CLI、历史冻结样本相等检查及金融准入均保持不变。保存与生命周期登记集中使用现有 ExperimentTracker，未引入另一套数据库或修改其状态枚举。

新增适配仅负责严格配置、本地绑定、输入验证、执行调度、产物序列化、Tracker 登记和 Markdown。ResearchEvaluation 仍由原引擎计算。嵌套拒单原因使用规范 JSON 字符串保存到 Parquet，避免空字典结构无法写入，不改变引擎结果。

便携配置绑定数据 manifest、策略源码、研究窗口、费用和固定执行/研究边界；本机绝对路径留在单独 runtime JSON。输入验证沿用 D01 → 上游 manifest → bars → config/quality 的既有合同。自定义策略需要原引擎要求的真实 adj_close 和恒定调整比例；不将 D01 raw_close 伪造成调整价格。

## 定向验证

Python 3.13.9（pandas 2.3.3、NumPy 2.3.4、PyArrow 22.0.0）：工作流及原 Tracker、自定义策略、稳健性、输入合同、真实比较、ResearchEvaluation 定向回归 **90 passed / 48.77秒**。其中覆盖失败重放、报告失败、路径搬迁及严格边界。最终 CLI 全操作验证正在执行。

验证重点包括：未知字段/任务拒绝；manifest、配置、策略及产物身份变化拒绝；缺私有输入为 DATA_UNAVAILABLE；执行错误保留原因和阶段；负收益不被误判为运行失败；不兼容结果拒绝比较；旧 ID/输出不覆盖；本地重放不自动联网。

## 实际操作

完整合成操作示例：

```sh
python -I examples/research_workflow.py --output '<NEW_PRIVATE_ABSOLUTE_DIRECTORY>'
```

示例生成两个证券的合成观察数据，调用已有 D01 准备工具，并通过统一配置执行自定义策略和六策略独立账户路径；随后比较、验证并重放。具体结果待本轮执行完成后登记。

[CLI 与配置说明](../../RESEARCH_WORKFLOW.md)。

## 金融研究边界

本轮是工程工作流验证，不为真实策略收益补证，不重新执行 P6-R01 的324条真实历史评价。小型配置不是完整历史冻结批次，不获得 `P6_R01_REAL_ROBUSTNESS_COMPLETED` 资格。

报告保留净成本价格收益、描述性统计、模拟执行及正式准入之间的区别。P6-E01 的 `TOTAL_RETURN_UNAVAILABLE / null` 保持原义；有界分红权益不加入净值，不改称全年总收益。
