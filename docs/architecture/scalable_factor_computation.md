# P2-09 分块因子与数据集接口

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

原 `snapshot.compute()`、`DatasetBuilder.build()` 的 250,000 行限制和 DiscoveryConfig
默认 40 / 最大 50 候选预算保持不变。新增可选入口：

```python
from factorlab.dataset.partitioned import ChunkedConfig, PartitionedDataset

config = ChunkedConfig(
    symbols_per_batch=32, factors_per_batch=16, dates_per_batch=20,
    workers=2, memory_limit_bytes=5 * 1024**3, disk_limit_bytes=8 * 1024**3,
    timeout_seconds=3600,
)
# 已有冻结快照、明确 FactorRefs、原 DatasetBuilder V2 配置与标准输入。
raw = snapshot.compute_chunked(
    bars, refs, calendar=calendar, output_dir="new-raw-run",
    context=data_context, config=config,
)
bundle = builder.build_chunked(
    bars, benchmark, membership, status, listings, calendar,
    output_dir="new-dataset-run", config=config,
)
verified = builder.read_partitioned(
    bundle.path, calendar=calendar,
    expected_identity_hash=bundle.identity_hash,  # 应保存在目录外的可信收据中
    expected_input_hashes=bundle.manifest["input_hashes"],
)
for complete_dates in verified.iter_partitions():
    consume(complete_dates)

# 小规模接入原四模型和 WalkForwardRunner；此调用会显式物化全部宽表。
research_frame = verified.to_frame()
```

`PartitionedDataset(path)` 可读取原始因子分区；DatasetBuilder 的读取入口还核对
snapshot、FactorRefs 顺序、context、data_version、calendar_id、完整日历摘要及构建选项。
`verify()` 流式验证所有分区，不物化全量；读取每个分区时再次校验文件哈希。

## 执行与语义

证券长表输入可以乱序；benchmark 继续要求日期递增，与原市场状态函数的契约一致。

证券组保留完整显式日历。滚动、滞后、历史资格过滤、next-open 标签均调用现有算法。
不存在时间块拼接滚动状态，也不压缩缺失交易日。完整历史包括调用方提供的预热期、
研究期与未来标签期；研究日期仍由 membership 的首尾界定。
与原标签函数一致，标签不会额外依据 status 改写；终止交易后的缺价格保留 NaN。
训练标签成熟度、purge 和 embargo 仍由原 WalkForwardRunner/Splitter 保证。

每组只准备一次因子输入面板；按独立定义哈希去重，别名递归解析到已固定依赖。
原始 FactorRefs、定义版本、定义哈希与快照不改变。当前闭合注册语言中的 builtin、
expression 均为 `STOCK_LOCAL_FULL_HISTORY` 语义，alias 继承目标执行类型。
AST 不允许 rank、截面算子或自定义 callable；注册/执行显式拒绝不支持的类型。
以后加入横截面因子时必须另设执行计划，不能默认为局部证券计算。

独立定义每批最多 16；AST 引擎继续使用原运算量、缓存与时间上限。
按静态运算估计和缓存余量在批内释放引擎，必要时建立更小执行子批，
不扩大探索引擎权限；单个表达式超预算仍明确失败并记录批号及 FactorRef。
最多同时提交 workers 个证券组，默认 2、允许 1–4；没有全量宽表任务队列。

中间证券分区按日期排序并写 Parquet row groups。日期归并使用谓词下推，
收齐所有证券组后才处理整个日期横截面。资格过滤先依据完整每日 membership 和
每只证券的完整历史状态，横截面预处理随后调用原函数。
最终分区保留原始和处理后因子，以及标签与市场特征；不另存重复全量特征表。

## 持久化与资源保护

输出目录必须不存在。新运行在同一父目录的唯一 `.pending-<uuid>` 中写入，
成功后完成全分区读回校验，再同盘原子 rename 发布。
失败仅清理本次 UUID 临时目录，并在输出旁写唯一 failure JSON；不覆盖既有研究证据。
意外断电/强制终止可能留下 pending 目录，读取正式输出时不会将它当成成功数据。

manifest 保存完整 binding、输入内容哈希、明确 Arrow 模式、每个文件的 SHA-256、
行数、日期范围、主键与内容哈希、计数和执行参数。拒绝缺失、多余、重复分区、
日期范围重叠、重复主键、模式漂移、内容或身份不符。
manifest 本身封存 identity_hash；这是校验和，不能证明未受信任目录的真实性。
分区身份含布局；不同分块参数可以改变分区文件哈希，但物化内容及原 V2 数据身份必须一致。

运行前检查可用内存、磁盘、目标目录；每 100ms 监测进程树 RSS、目录占用和超时，
批边界停止新任务。守卫是协作式限制，不能中断正在执行的单个原生库调用；
容量脚本额外使用父进程监督、超过上限时终止进程树并清理未发布临时目录。
100ms 采样不是操作系统硬配额，可能遗漏极短瞬态；实际结果留有预算余量。
读写字节为 Windows 进程 I/O 计数，包含缓存读取，不能解释为物理 SSD 写入量。

## 独立瓶颈

此接口的输入仍为标准长表 DataFrame。输入行情、membership、status 的常驻内存
没有实现源端 Parquet 流式化；当前验收规模可行，不能外推为无限证券/日期。
最终宽表只按日期块常驻，但每个证券组暂存该组全部候选输出。

原四模型和选择器仍消费全量 DataFrame；`to_frame()` 是明确的内存适配器，
不代表 125 万行 × 300 因子的训练/回测已获得低内存认证。
选择器已有 `summary_only=True` 去冗余路径，保留 O(F²) 汇总证据，
逐日执行相关矩阵与成对缺失/恒等检查。300 因子有 44,850 对，
不保存 2,500 × 44,850 条逐日明细，但 CPU 成本仍随日期及因子对增长。
本轮不更改筛选统计语义、候选排序、方向或 P2-08 消融算法。
需要更大候选池时应显式配置前置候选研究方案，不能把分组筛选冒充全局去冗余等价实现。

合成容量池为固定、标签无关的 14 builtin + 不同字段/窗口/滞后的合法 AST 比率，
包含 RollingMean、RollingStd、Delta、Lag、SafeDiv。
定义数不等于独立经济信号数；manifest 分开记录注册、请求、去重执行、成功数量。
`selected_factors=null` 表示本次容量计算没有执行选择器，不能当作零或全部通过。
