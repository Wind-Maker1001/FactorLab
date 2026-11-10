# P7-01：统一离线研究工作流

2026-11-10 · **P7_01_ENGINEERING_ACCEPTED**

`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

## 范围与架构选择

基线：`origin/main = 2fffbee1e41ba342bf0adb7835467b7474cebc17`。

统一入口覆盖自定义日线策略和 P6-R01 的多证券独立账户计算路径。直接调用 `run_custom_strategy()` 与 `account_batch()`；后者继续调用六策略 `compare()`。原研究函数、原 CLI、历史冻结样本相等检查及金融准入均保持不变。保存与生命周期登记集中使用现有 ExperimentTracker，未引入另一套数据库或修改其状态枚举。

新增适配仅负责严格配置、本地绑定、输入验证、执行调度、产物序列化、Tracker 登记和 Markdown。ResearchEvaluation 仍由原引擎计算。嵌套拒单原因使用规范 JSON 字符串保存到 Parquet，避免空字典结构无法写入，不改变引擎结果。

便携配置绑定数据 manifest、策略源码、研究窗口、费用和固定执行/研究边界；本机绝对路径留在单独 runtime JSON。输入验证沿用 D01 → 上游 manifest → bars → config/quality 的既有合同。自定义策略需要原引擎要求的真实 adj_close 和恒定调整比例；不将 D01 raw_close 伪造成调整价格。

## 定向验证

Python 3.13.9（pandas 2.3.3、NumPy 2.3.4、PyArrow 22.0.0）：工作流及原 Tracker、自定义策略、稳健性、输入合同、真实比较、ResearchEvaluation 定向回归 **90 passed / 48.77秒**。其中覆盖失败重放、报告失败、路径搬迁及严格边界。最终工作流复测 **17 passed / 43.33秒**，实际通过 CLI 执行两个入口及 inspect/compare/verify/replay。另增高费用负收益用例 **1 passed / 2.53秒**，确认负收益为 `SUCCEEDED / COMPLETED`，不混同执行失败。以上共91个不同测试用例，17项为重叠复测。

验证重点包括：未知字段/任务拒绝；manifest、配置、策略及产物身份变化拒绝；缺私有输入为 DATA_UNAVAILABLE；执行错误保留原因和阶段；负收益不被误判为运行失败；不兼容结果拒绝比较；旧 ID/输出不覆盖；本地重放不自动联网。

## 实际操作

完整合成操作示例：

```sh
python -I examples/research_workflow.py --output '<NEW_PRIVATE_ABSOLUTE_DIRECTORY>'
```

在实现提交 `1464f6a46caadb6068f5c1beebcb320e0e53fbe2` 上实际执行完成，Python 3.13.9。运行 Tracker 同时绑定实际工作区源码 SHA256、commit/tree、工作区差异身份和环境版本，不单凭提交号宣称代码身份。

示例生成601857.SH、601398.SH两证券的合成观察数据（2017-10-02起预热，2018-01-01—2018-04-30研究），调用已有 D01 准备工具。自定义入口分别执行 `custom-a`、`custom-b`；多证券入口执行两个独立账户、各六策略。所有输入均为生成数据，资格始终 `SYNTHETIC`。

|操作|实际结果|
|---|---|
|自定义日线策略两次运行|SUCCEEDED / COMPLETED|
|两证券六策略独立账户|SUCCEEDED / COMPLETED，共12条策略账户结果|
|custom-a 与 custom-b 比较|comparable=true|
|输入、配置、代码、环境及产物验证|IDENTITY_VERIFIED|
|自定义入口重放|REPLAY_VERIFIED|
|多证券入口重放|REPLAY_VERIFIED|

重放逐表比较信号、NAV、成交、拒单和汇总，以及原 ResearchEvaluation 的类型化结果；容差为 `rtol=1e-10 / atol=1e-12`。生命周期时间、报告中的新目录/命令不要求字节相同。原 run 和 replay 的配置身份、样本身份各自一致。

自定义配置身份：`bee54ed670e38989fa9f23142786d4afab2c71b96db6fd5e38ff19ddbcfd7b5e`；实际样本身份：`47d6259245a995c740227eb2be458a5a9f0226a94ae5c7cedb3dbc44da4ffe6e`。

多证券配置身份：`204e5726a9ee07634a75c10211c8cf12faf4419a9189b028cb58ff5ec85c5ec5`；实际样本身份：`79b2fa34d610f837c05951424805d9c3ea7a020603a646623015a9b55cef6ea4`。

产物报告 SHA256（完整产物索引由各自不可变 Tracker 收据绑定）：

|运行|REPORT.md SHA256|
|---|---|
|custom-a|`b31071c5f1ac050d162f5264320a032e9d7652a7ac85056a4cb53b6fca5b4059`|
|multi|`d96e1aa829df9ce5f90bf33f9b89db4427f6302cb58f30d96b24d9fe8409f023`|
|custom-replay|`f02167c7e057a3f6db3d15e449006fc2b5ac68421177f2a9420fe9ddf4f5889b`|
|multi-replay|`3369c74ee33fecde58fa994523607846fb051b1bbe4b205a82896f1b5df60509`|

生成行情、带本机路径的 runtime、完整 Tracker 与产物保存在本地私有输出。新运行使用示例命令生成新的 manifest 和独占输出；重放旧运行则使用其原 manifest 字节、保存的配置/本地绑定和记录的代码身份。准备时间等字段变化会改变 manifest 的字节身份，不能用重新准备的数据替代旧证据。

[CLI 与配置说明](../../RESEARCH_WORKFLOW.md)。

## 金融研究边界

本轮是工程工作流验证，不为真实策略收益补证，不重新执行 P6-R01 的324条真实历史评价。小型配置不是完整历史冻结批次，不获得 `P6_R01_REAL_ROBUSTNESS_COMPLETED` 资格。

报告保留净成本价格收益、描述性统计、模拟执行及正式准入之间的区别。P6-E01 的 `TOTAL_RETURN_UNAVAILABLE / null` 保持原义；有界分红权益不加入净值，不改称全年总收益。
