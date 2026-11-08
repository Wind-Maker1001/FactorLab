# P3-03 Partitioned Walk-forward 验收

`P303_ENGINEERING_ACCEPTED`（软件工程）

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 接口与语义

基线为通过完整 Git fetch 获得并精确核对的
`d165c65a17c1ff4b7d174a063a6607589cd38522`。

`PartitionedDataset.read_dates(dates)` 接受显式递增、唯一的信号日期，仅解码相交分区，
返回精确日期和全部证券，保持列/因子顺序、dtype、值和缺失掩码。
共用原文件、内容、主键、模式与顺序校验；每次读取另检查目录库存和冻结的 manifest 身份。
`read_metadata(columns)` 逐分区完整验证，只保留窄规划表。

`WalkForwardRunner.run_partitioned(dataset, calendar=research_calendar)` 是可选入口。
原 `run(DataFrame)` 默认行为、四模型工厂与算法不变；两条路径共用训练、选优、预测及诊断循环。
规划表仍交给原 Splitter，使用逐证券实际 entry/exit 计算成熟度、Purge/Embargo，
再按折主键取得 Train/Validation/Test。未将 Test 外的未来特征或标签值用于历史训练决策。
标签和因子由原构建阶段在完整预热日历上产生，窗口读取不重新计算或截断其历史依赖。
运行日历是明确的研究区间，不将构建预热期当作必须存在训练行的研究区间。

动态筛选仍将完整、成熟 Train 的全局候选集合交给原选择器，相关性、去冗余、Top-K、方向
及 P2-08 比较定义不变。四模型继续接收每折 DataFrame，**没有 partial_fit 或真正流式训练**。
DataHub、PortfolioOptimizer、金融准入与来源认证逻辑未修改。

窗口收据绑定原分区布局身份，未伪造全量宽表内容身份；因此选择收据的
`dataset_identity_hash` 和派生 `model_prediction_identity` 与原内容收据不同。
训练选择哈希、方向、模型输入和输出不因此改变。

## 正确性证据

最终 `reviewed-tests.log/xml`：26 项通过，包含 23 项新增测试和 3 项共享列校验回归。
`regression-tests.log/xml`：相关分区、Splitter、Runner、动态选择和阶段诊断 87 项通过。
执行历次日志保留，初始测试日历错误及修正说明见 [REPRODUCE.md](REPRODUCE.md)。

同一确定性、真实分块构建的小数据集，对照完整 DataFrame 和分区路径：

| 模型 | 固定因子 | 动态全局因子选择 |
|---|---|---|
| FactorScore | EXACT | EXACT |
| Ridge | EXACT | EXACT |
| LightGBM | EXACT | EXACT |
| XGBoost | EXACT | EXACT |

阶段输入逐表精确比较，包括 Train/Validation/Test 日期、证券、特征、标签和缺失值；
预测、候选验证指标、RankIC、系数/重要性、阶段诊断、因子质量/去冗余、选择与方向均精确一致。
未放宽任何 Golden 容差。另覆盖同日不同证券的跨边界标签及缺失标签窗口、未来值扰动、
相交分区读取范围、缺失/损坏/重叠/身份变更、规划后损坏、候选 pins、自定义审计列、
原异常类型兼容、禁止调用 `to_frame()`，以及前折宽表在下次读取前可被释放。

## 单次中等规模 A/B

80 证券、400 个构建交易日、40 个合法注册因子；研究表 26,800 行、17 个分区、5 折。
原因子构建单独执行；端到端计时覆盖打开、校验、读取、训练、选优、预测和诊断结果生成。
A/B 各一个独立 Python 3.13.9 进程，各运行一次，无本任务测试并发；
OMP/OPENBLAS/MKL 线程数为 2。未执行 P2-09 L2/L3 或多轮刷分。

| 测量 | A：完整 DataFrame | B：窗口适配 |
|---|---:|---:|
| 最大观测 RSS（字节） | 406,773,760 | 315,940,864 |
| 最大观测 RSS（MiB，约） | 387.9 | 301.3 |
| 端到端秒 | 72.94 | 53.92 |
| 解码分区次数 | 17 | 69 |
| 解码行数 | 26,800 | 109,600 |
| 宽表累计物化行数 | 26,800 | 73,760 |
| SHA-256 扫描字节 | 50,815,380 | 129,262,787 |
| 解码分区文件字节 | 25,407,690 | 103,855,097 |
| Windows 进程读字节 | 76,772,250 | 234,904,009 |

本次最大观测 RSS 降低 **22.33%**，耗时降低 **26.07%**；校验扫描与解码文件字节总和为 A 的
**3.06 倍**。重叠的扩展 Train 窗口产生重复 I/O，没有跨折缓存。
这是单次缓存环境中的观察，不将进程读取或文件大小计数解释成物理 SSD 流量。
总范围和各折均有独立 10ms RSS 采样；表中取所有采样的最大观测值。
A 的独立折采样比总范围采样高约 1.5 MiB，短瞬态可能被某个采样线程遗漏。

各折物化 Train 行数、实际非缺失标签拟合行数和峰值如下；Validation/Test 行数、分区与字节
增量见 `dense-metrics.json`、`window-metrics.json`。规划阶段还逐分区解码 26,800 行，
只保留窄表，不计入宽表累计物化行数。

| 折 | Train 行 | 实际拟合行 | A 折峰值 RSS 字节 | B 折峰值 RSS 字节 |
|---|---:|---:|---:|---:|
| 0 | 1,280 | 1,280 | 283,648,000 | 211,533,824 |
| 1 | 6,560 | 6,555 | 312,303,616 | 238,440,448 |
| 2 | 11,840 | 11,828 | 340,561,920 | 264,437,760 |
| 3 | 17,120 | 17,102 | 373,628,928 | 291,024,896 |
| 4 | 22,240 | 22,222 | 406,773,760 | 315,940,864 |

A/B 的逐折行数和 8,560 条预测一致，预测内容哈希均为
`25b0925a8bc106af97b5a99b3f91c9944ec82b5b413f60b8484edfae100a6e66`；
折汇总（含 RankIC 等指标）哈希均为
`00f42eccfe305b7de1ae493c2a07ce8d7d77b4496c6b02a28bde09578a9becee`。
自动对照摘要见 `comparison.json`。

## 保留的物化与交付

扩展 Train 的整个候选矩阵、筛选方向转换副本及模型自身矩阵仍需物化。
为保持完整内容哈希合同，相交分区包括窗口边缘的额外行仍需解码，但额外行不交给训练阶段。
规划元数据和累积的 Train/Validation 诊断预测输出仍随历史长度或折数增长；
本次没有改写诊断保留方式或源端输入 DataFrame 构建，也不据此外推无限规模。

环境与生产源代码哈希见 `code-identity.json`，数据布局及输入身份由
`data-receipt.json` 的冻结 manifest 哈希/身份绑定；原始可重建分区保留在被忽略的
`local-p303/dataset`，不提交大型数据。性能执行时的驱动哈希单列为
`measurement_driver_sha256`；最终驱动仅将规划行的缺省测量字段以标准 JSON null 序列化，
不改变采样或结果，未重跑性能。

复现命令见 [REPRODUCE.md](REPRODUCE.md)。最终 Commit、Tree 由包含本报告的 Git 提交确定，
Draft PR head 绑定该提交，避免报告自引用提交哈希。不自动合并。
