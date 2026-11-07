# P2-09 大规模因子计算验收

`P209_ENGINEERING_ACCEPTED`（软件工程）

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 基线与边界

启动 fetch 的 origin/main：`45b35c0a51b7e7159d20ab09a168543fcfa3f2e6`，与任务指定基线一致。
分支 `feat/p2-09-scalable-factor-computation`；worktree `D:/Code/FactorLab-worktrees/p2-09`。
Commit、Tree、Draft PR 以交付消息和 PR head 为准，另存本地 `local-p209/delivery.json`，避免自引用哈希。
Git 作者 `Wind-Maker1001 <88176063+Wind-Maker1001@users.noreply.github.com>`。

系统查询实际 CPU **i7-14700KF / 20 核 28 线程**，Windows 11、64GB DDR4，
复用 `D:/Environments/factorlab-py313/Scripts/python.exe`（3.13.9）。无 GPU 计算依赖。
numpy 2.3.4 / pandas 2.3.3 / pyarrow 22.0.0；仅补装轻量进程监测依赖 psutil 7.2.2。
初次 PyPI TLS 安装失败，改用清华 HTTPS 镜像安装成功；未创建新环境。

## 实现与瓶颈

新增 Registry V2 `compute_chunked`、DatasetBuilder `build_chunked/read_partitioned`、
可验证分区读取器和外部资源监督基准。原稠密路径、250,000 行限制、DiscoveryConfig 预算不变。
完整证券历史 → 证券分区 → 完整日期横截面 → 原资格/标签/预处理算法 → 日期分区。
参数默认 32 证券、16 因子、20 日、2 工作线程；只保留有界组任务、表达式缓存与日期宽表。
合法 AST 定义按固定字段/窗口/滞后生成，不读取标签，不使用随机列冒充计算。

注册数、请求数、实际独立定义数、成功输出数分开统计；所有容量池均为独特定义哈希，
不声称它们等于独立经济信号；最终筛选数 null 表示容量运行未执行选择器。
300 定义小样本已真实执行并与原解释器逐批精确对照；L4 大规模容量未执行。

主要瓶颈为原全量稠密面板/双份宽表、AST 累计缓存预算，以及逐日逐因子 pandas 预处理。
实测原预处理追加 raw 列产生碎片警告；本轮保留计算语义，没有用改变候选或合成机制追逐成绩。
原选择器已有 summary_only 汇总，300 因子仍需 44,850 对逐日检查，未修改其统计语义。

## 实际容量与资源

| 级别 | 日历证券日 / 输入实际行 | 因子 | 端到端秒 | CPU秒 | 树RSS GiB | 磁盘峰值 GiB | 非缺失有效值 | 有效值/秒 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| L0 | 10,000 / 9,994 | 14 | 4.53 | 4.61 | 0.341 | 0.003 | 122,367 | 27,015 |
| L1 | 600,000 / 599,991 | 14 | 76.52 | 83.59 | 0.562 | 0.373 | 8,232,898 | 107,593 |
| L2 | 600,000 / 599,991 | 100 | 755.65 | 767.62 | 0.818 | 1.642 | 58,281,569 | 77,128 |
| L3 | 1,250,000 / 1,249,991 | 100 | 2020.51 | 2082.84 | 1.328 | 3.615 | 123,269,869 | 61,009 |

L0、L1、L2、L3 各正常完成一次；L4 为 `NOT_RUN`，未重复大型阶段刷分。
缺失交易日属于固定原有合成机制，因此输入实际行略少于日历证券日。
因子在完整日历网格上计算，研究表经原上市年龄、质量、状态与历史 membership 过滤，行数更少。
总因子单元格与有效值分别见每级 metrics；吞吐分母为全部端到端耗时。

端到端包含实际因子计算、证券写入、完整横截面处理、标签、研究表、持久化和完整性读回；
合成输入生成单独计时，L0 一致性及四模型验证另计，不包含在容量墙钟时间中。
CPU 为工作进程 CPU 时间；进程树 RSS 和磁盘由父进程每 100ms 采样，覆盖输入生成和导入，
L0 还包括四模型验证，属于比构建更宽的监测范围。Python tracemalloc 峰值独立报告。
I/O 字节为 Windows 进程累计读写传输计数，包含缓存；不是物理 SSD 写入字节。
采样可能遗漏极短瞬态，保留的私有提交峰值可用于核查没有依赖巨量换页。
L1 与相关测试部分重叠，性能为当次台式机观察值；L2/L3 不与模型回归并行。
L1–L3 设置 OMP/OPENBLAS/MKL 线程为 2，仅限制库并行度，不是 SLS CPU 性能模拟。

SLS 16GB 设备没有实机测试。60/150/240 分钟只是设计目标，**SLS_PERFORMANCE_NOT_VERIFIED**；
不依据台式机跑分或线程数推算后标记 PASS。

## 正确性、完整性与下游

L0 50×200×14 与原单次算法：主键、列顺序、因子、缺失掩码、横截面处理、标签、资格和 V2
身份均精确一致。无需放宽 rtol/atol。21 项分块测试覆盖批次变更、输入乱序、跨块窗口、
缺失交易日、停牌/未知状态、上市、终止行情、历史成分变更、研究期后的未来标签价格、
未来扰动不改变历史因子，以及损坏/遗漏/多余分区、主键重复、模式漂移、身份不匹配、
资源中止、输出拒绝覆盖和失败不发布。

首次相关回归 170 项中 169 通过，1 项暴露目录监测与清理竞态；修复后分块 20 项通过，
补充研究期/未来标签期边界后 20 项通过；补充测试打乱 benchmark 触发原市场状态排序契约，
保留生产契约、修正测试夹具后仅补跑该项通过，累计 21 项均有最终通过结果。详见 tests.json 和 XML/日志；
没有重新运行不受影响的 Legacy、四模型工厂全量矩阵或 P2-08 完整实验。
后续补充的显式执行类型白名单、证券分区到日期分区行数核对和静态日历辅助方法只改变
校验/元数据；L0–L2 测量收据保留当时真实内容，没有改写旧收据或重跑大型数据。

各级在原子提交前全分区读回核验 SHA-256、内容、模式、主键、库存，完成后再通过 builder
校验日历、快照与输入 binding 并再次流式验证。manifest 保存完整 FactorRefs、定义、输入摘要、
每个分区文件/内容/主键哈希与行数。布局身份与物化内容身份分开；哈希是校验和，不是数据可信认证。

L0 分区读回后进入原 FactorScore、Ridge、LightGBM、XGBoost 和 WalkForwardRunner，
每模型完成 1 折、1,150 条预测；训练和验证最大标签退出日均严格早于对应下一阶段首日。
模型预测摘要哈希见 L0-metrics.json。未改模型、工厂、Runner、选择或消融算法。

## 限制与清理

输入长表仍需常驻 DataFrame；四模型和原选择器也仍需全量研究 DataFrame。
`to_frame()` 是显式物化接口，不能将分块因子容量等同为 125 万×300 因子训练回测已通过。
资源守卫为协作式，外部监督可杀死超限进程树；单个库调用不能精确中断，100ms 不是 OS 硬配额。
强制关机可能留下未发布 pending 目录，正式读取不会将其视为成功结果。
全部大规模原始合成输入只在内存生成；保留小型指标、清单、哈希、测试和失败摘要。
清理本轮可重建日期数据文件的命令在执行前被自动安全审查拒绝（blocked by policy），
删除未执行，也未换方式重试或绕过。生成的数据仍在 local-p209/L0–L3/dataset 中，
未纳入 Git；详见 cleanup.json。不删除任何已有冻结证据。
基线 checkout 的 demo.py 有 CRLF 与 eol=lf 的既有属性冲突，仅行尾差异，未纳入提交。

## 验收状态

| 状态 | 结果 |
|---|---|
| P209_FUNCTIONAL_ACCEPTANCE | PASS |
| P209_DESKTOP_CAPACITY | PASS（L2、L3） |
| P209_MEMORY_BUDGET | PASS（L2 ≤5 GiB、L3 ≤7 GiB；磁盘也满足预算） |
| P209_SLS_PERFORMANCE_NOT_VERIFIED | NOT_RUN |
| P209_FINANCIAL_ADMISSION_BLOCKED | PASS（维持 BLOCKED） |
| P209_ENGINEERING_ACCEPTED | PASS |

## 复现

接口细节见 [分块架构](../../architecture/scalable_factor_computation.md)。在指定 worktree PowerShell：

```powershell
$py = 'D:/Environments/factorlab-py313/Scripts/python.exe'
$env:PYTHONPATH = 'src;.'
$env:OMP_NUM_THREADS = '2'
$env:OPENBLAS_NUM_THREADS = '2'
$env:MKL_NUM_THREADS = '2'
& $py -m pytest tests/research/test_partitioned_dataset.py -q
# 相关范围原命令：
& $py -m pytest tests/research/test_partitioned_dataset.py tests/research/test_factor_registry_v2.py tests/research/test_dataset_registry_v2.py tests/research/test_walk_forward_factor_selection.py tests/research/test_factor_ablation.py -q
# 输出必须使用尚不存在的新目录，按顺序运行；先生成时间另计。
& $py -m examples.research.scalable_factor_computation --level L0 --output local-reproduce/L0 --timeout 1200
& $py -m examples.research.scalable_factor_computation --level L1 --output local-reproduce/L1 --timeout 3600
& $py -m examples.research.scalable_factor_computation --level L2 --output local-reproduce/L2 --timeout 3600
& $py -m examples.research.scalable_factor_computation --level L3 --output local-reproduce/L3 --memory-gib 7 --disk-gib 12 --timeout 7200
```
