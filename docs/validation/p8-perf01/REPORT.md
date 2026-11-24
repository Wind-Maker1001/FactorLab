# FactorLab P8-PERF-01：Monte Carlo、多账户及完整交易生命周期性能优化验证报告

> **分支**：`perf/p8-perf01-research-throughput`（基线提交 `5e9f29a`）  
> **范围**：仅本地离线性能审计、Benchmark、工程优化与金融语义回归，不涉及 Azure 与云端变更。  
> **机器可读数据**：[`summary.json`](summary.json) 及 [`data/`](data/)（共 13 份基准与 Profiling 原始 JSON）。  
> **复现脚本**：[`tools/research/benchmark_p8_perf01.py`](../../../tools/research/benchmark_p8_perf01.py)

---

## 1. 测试硬件与软件环境

| 项目 | 实测探测结果 |
| :--- | :--- |
| **CPU** | Intel Core i7-14700KF（20 物理核 = 8 P-core + 12 E-core，28 逻辑线程） |
| **内存** | 63.82 GiB DDR4（测试期间可用 23.8–31.0 GiB） |
| **GPU** | NVIDIA GeForce RTX 2070（8192 MiB 显存，驱动 `617.42`，空闲显存 1.06–5.68 GiB） |
| **操作系统** | Windows 11 (`Windows-11-10.0.26340-SP0`) |
| **Python 环境** | `D:/Environments/factorlab-py313/Scripts/python.exe`（Python `3.13.9`，NumPy `2.3.4`，Pandas `2.3.3`） |
| **CUDA / CuPy 探测** | `factorlab-py313`、`factorlab-py314` 与 `anaconda3` 均未安装 `cupy` / CUDA Toolkit（`nvcc`）；`ComputeConfig(device="gpu")` 正确触发 fail-closed 报错 `runtime cupy unavailable: No module named 'cupy'` |
| **BLAS 线程控制** | 默认子进程通过 `factorlab.compute.parallel` 强制固定 `OMP_NUM_THREADS=OPENBLAS_NUM_THREADS=MKL_NUM_THREADS=NUMEXPR_NUM_THREADS=1` |

> **测试环境载荷说明**：首次 `after_bench_*` 运行期间宿主后台 CPU 占用较高（约 48%，影响 Windows `spawn` 进程启动和单核标量耗时）。为此额外执行了与基线 `5e9f29a` 交替执行的 **ABABAB 配对对比实验**（[`data/paired_ab_and_scaling.json`](data/paired_ab_and_scaling.json)，宿主起始 CPU 9.6%）及最优 worker 数下 1024 路径实测（[`data/after_scale_1024_w12.json`](data/after_scale_1024_w12.json)），所有性能结论均同时给出原始基准与同载荷配对结果。

---

## 2. 基线性能审计与真实热点证据

通过对基线提交 `5e9f29a` 执行 `cProfile`、`PaperStore` 事务/加载/保存插桩计数与 IPC `pickle` 字节统计（[`data/baseline_profile.json`](data/baseline_profile.json)），定位到五大真实热点：

### 2.1 R09 完整生命周期（`:memory:` SQLite，4 paths × 168h，基线 16.95s = 4.24s/path）

SQLite `COMMIT` 本身仅占 **0.008s（0.05%）**，**91.3%** 的耗时集中在 `TradingController._atomic` 内部的重复内存拷贝与全量序列化：

| 热点位置 | 调用次数 | 累计耗时 (s) | 占比 | 根因分析 |
| :--- | ---: | ---: | ---: | :--- |
| `storage.dumps` / `encode` | 48,314 / 2,474,587 | **8.589** | **50.7%** | `GridExecution._authority`（5.15s）每条命令都将全部约 148 个历史订单重新 JSON 编码并 SHA-256；`PaperStore.save`（3.06s）每次提交都重新序列化最大 **70,488 B** 状态快照 |
| `copy.deepcopy(self.broker.state)` | 2,103,645 递归调用 | **4.758** | **28.1%** | `TradingController._atomic` 在进入事务前深拷贝整份 `PaperState`，但随后立即 `self.broker.state = self.store.load()` 覆盖引用，导致深拷贝结果大部分白费 |
| `PaperStore.load` (`json.loads` + `decode`) | 1,130 / 966,517 | **2.124** | **12.5%** | 每条命令都从 SQLite 读出 70KB JSON 并递归 `decode` 重建全部 `OrderRecord`/`Decimal`/`datetime` 对象 |
| SQLite `commit()` | 1,130 | 0.008 | 0.05% | `:memory:` 模式下数据库提交本身几乎零开销 |

### 2.2 R10 资金费适应性网格（文件 WAL SQLite，6 accounts × 168h，基线 13.75s = 2.29s/acct）

| 热点位置 | 实测指标 | 耗时 (s) | 占比 | 根因分析 |
| :--- | :--- | ---: | ---: | :--- |
| SQLite `COMMIT (synchronous=FULL)` NTFS fsync | **2,034 事务 / 18 次打开** | **8.138** | **59.2%** | `run_paired_window(..., cleanup_dir=True)` 为每个临时研究账户创建 6 个磁盘 SQLite 文件、执行约 171 次 `FULL` 刷盘提交、随后重开 3 次并立刻删除；多进程下 NTFS fsync 队列饱和导致 8 worker 并行效率仅 **22.8%** |
| 每根 K 线多余的 `project_paper_account()` | **1,026 次 `events` 全表扫描** | ~0.57 | 4.1% | `grid_paper.py` 每小时额外打开只读事务并扫描全量 `events` 表重算 `turnover_usdt`，使事务总数翻倍（2,034 → 1,026） |
| 跨进程 IPC `pickle` 冗余 | **58,780 B / 任务** | — | — | `grid_r10_study.py` 将 193 天完整 `all_funding` 列表（**33,503 B**）重复 pickle 发送给每个 7 天窗口任务（1000 任务浪费 56.1 MB IPC 带宽） |
| 资金费结算与因果查询 | `O(E×T)` 重复解析 | — | — | `position_settlements()` 与 `causal_funding_rate_bps_h()` 对每个时间点线性扫描全部事件并重复调用 `datetime.fromisoformat` |

### 2.3 纯 Monte Carlo 路径生成与风险统计（10,000 × 252，基线 0.464s）

- **NumPy CPU 路径生成极快**：1 资产 168h GBM 吞吐达 **106k–224k paths/sec**（1024 路径仅需 **7.3–9.7 ms**，占 1024 路径完整 R09 生命周期总耗时的 **0.03%**）。
- **统计尾部排序占 60.5%**：`summarize_paths` / `tail_statistics`（0.281s）在 499–999 次 bootstrap 重采样中对长度为 10,000 的完整数组执行 `np.sort`（`O(n log n)`），实际只需要尾部 1%–5% 分位元素。

### 2.4 S02 自动研究（6 候选基线 17.85s）

- `run_custom_strategy`（**12.24s，68.5%**）：每个交易日执行 Pandas 布尔掩码切片 `frame.loc[frame.date <= day, columns]`（15,055 次索引调用）。
- `research_source_sha256`（**2.21s，12.4%**）：校验源码树时对每个文件重复沿祖先目录执行 `assert_no_link_components`（9,416 次重复 `os.lstat`）。

### 2.5 S01 协议隔离策略回放（8 bars / 16 次 `decide()`，基线 39.87s = 2,359 ms/event）

- **92%+ 耗时在每事件冷启动 Python 子进程**：`LoadedStrategy._call()` 每次 `decide()` 都 `subprocess.run([python, -I, -B, worker.py])`，重新冷导入 `pandas`/`numpy`/`adapters`（单次 2.0–2.4s）。
- **4.6% 耗时在每事件全量扫描源码树**：每次 `decide()` 都调用 `runtime_identity()` 递归 `rglob('*.py')` 并 SHA-256 全部源码文件。
- **状态增长风险**：`GridAdapter` 将全部历史命令序列化进 `state['commands']`（仅 8 根 K 线即达 **12,053 B**），长窗口下存在 `O(N²)` 重放膨胀。

---

## 3. 已实施的代码优化与工程边界

所有改动均聚焦公共计算、存储、控制器与并行调度层，**不修改 R11 专属脚本、S02 Champion 晋升逻辑及 S04 云端部署控制器**，且默认保持 100% 既有持久化与安全隔离语义。

| 优化模块 | 修改文件 | 核心改动 | 正确性与安全边界保证 |
| :--- | :--- | :--- | :--- |
| **序列化与状态缓存** | [`src/factorlab/trading/storage.py`](../../../src/factorlab/trading/storage.py) | 1. `encode`/`decode` 精确类型快速分派 + `_DATACLASS_FIELD_CACHE` + 复用 `_ENCODER`<br>2. `PaperStore` 对不可变 `OrderRecord`/`OrderEvent`/`FundingEvent` 按对象引用缓存 JSON 片段（`_dumps_paper_state`）<br>3. `PaperStore.load()` 校验数据库 payload 与 `_cached_payload` 完全相等后返回快速隔离克隆 `_clone_paper_state()` | 序列化输出与原 `dumps()` **逐字节完全一致**；若外部 SQL 直接修改快照或内存被事务外篡改，`row[1] == self._cached_payload` 校验失败时自动回退完整反序列化，100% 保留 `test_fill_admission.py` 权威重载契约 |
| **事务冗余拷贝消除** | [`src/factorlab/trading/control.py`](../../../src/factorlab/trading/control.py) | 1. 移除 `_atomic()` 中冗余的 `copy.deepcopy(self.broker.state)`，改为保留旧引用 `before = self.broker.state`（因 `store.load()` 返回独立副本，异常回滚时 `before` 完好无损）<br>2. `assess()` 缓存 `RiskLimits` 基础字典 | 异常回滚与风险准入判定逻辑完全等价，无任何行为变化 |
| **Grid 权威哈希缓存** | [`src/factorlab/strategies/grid_execution.py`](../../../src/factorlab/strategies/grid_execution.py) | 1. `_authority()` 按 `(record is cached_rec and conf is cached_conf)` 缓存每笔订单序列化字节<br>2. 缓存 `_paper_deployment(controller)` 指纹 | 仍计算完整 SHA-256（含 `UNKNOWN` 状态归一化），比特级一致 |
| **Grid Paper 与 R10 内存研究模式** | [`src/factorlab/experiments/grid_paper.py`](../../../src/factorlab/experiments/grid_paper.py)<br>[`src/factorlab/experiments/grid_funding_adaptive.py`](../../../src/factorlab/experiments/grid_funding_adaptive.py)<br>[`src/factorlab/experiments/grid_funding.py`](../../../src/factorlab/experiments/grid_funding.py)<br>[`src/factorlab/experiments/grid_monte_carlo.py`](../../../src/factorlab/experiments/grid_monte_carlo.py) | 1. 消除每根 K 线冗余的 `project_paper_account()` 只读事务与全表扫描，改为增量累计 `turnover` 与 `fees_to_date`，`datetime.fromisoformat` 替换每 bar `pd.Timestamp`<br>2. 新增可选参数 `in_memory_store=False, return_artifacts=False`（默认保持磁盘 WAL）；R10 在 `cleanup_dir=True`（临时即焚研究窗口）时使用 `PaperStore(":memory:")` 并在内存中运行完整 `paper_fifo_attribute_events()` 零残差对账<br>3. `position_settlements()` 改为 `O(E + T log T)` 双指针扫描；`causal_funding_rate_bps_h()` 与 `_observe()` 使用 `bisect_right`<br>4. `close_bars()` 缓存小时级 UTC 时间戳元组 | 1. 磁盘模式（`cleanup_dir=False` / 默认 `PaperStore`）**完全保留 `synchronous=FULL` 与落盘审计文件**<br>2. R10 内存研究模式仍完整执行 `PaperStore(":memory:")` + `TradingController` + `PaperBroker` + `GridExecution` + FIFO 对账，测试已逐字段验证与 `cleanup_dir=False` 完全一致<br>3. **严谨区分复用语义**：R09 的资金费会改变权益并触发 `max_loss` 停机决策，故**严禁**跨资金费档位复用 R09 成交账本；R10 的 Model A/C 策略决策不依赖资金费，因此 `precompute_ac_multi_regime` 与 `run_paired_window_multi_overlay` 合法复用原账本 |
| **多进程池与 BLAS 控制** | [`src/factorlab/compute/parallel.py`](../../../src/factorlab/compute/parallel.py)<br>[`src/factorlab/compute/__init__.py`](../../../src/factorlab/compute/__init__.py)<br>[`src/factorlab/experiments/grid_r10_study.py`](../../../src/factorlab/experiments/grid_r10_study.py)<br>[`src/factorlab/experiments/p8_r09_study.py`](../../../src/factorlab/experiments/p8_r09_study.py) | 1. 新增公共模块 `factorlab.compute.parallel`：`create_process_pool`、`single_thread_blas_initializer`、`chunksize_for_tasks`、`recommended_workers`<br>2. `grid_r10_study.py` 新增 `_slice_window_funding()` 按窗口+回望期切片资金费事件（单任务 IPC 资金费负载削减约 95%），并在 `run_full_r10_study` 内复用单一进程池<br>3. `p8_r09_study.py` 将历史/随机/确定性任务合并至单一复用进程池 | 保持各账户完全隔离与固定种子确定性；彻底避免多进程下 BLAS 线程过量抢占 |
| **MC 尾部统计加速** | [`src/factorlab/research/monte_carlo/reliability.py`](../../../src/factorlab/research/monte_carlo/reliability.py)<br>[`src/factorlab/research/monte_carlo/__init__.py`](../../../src/factorlab/research/monte_carlo/__init__.py) | `tail_statistics()` 与 `_loss_tail()` 将全量 `np.sort` 替换为 `np.partition` + 仅对前 `k` 个尾部元素排序 | 数学等价，与全量排序最大绝对误差 `0.0` |
| **S02 策略与源码哈希加速** | [`src/factorlab/strategies/custom.py`](../../../src/factorlab/strategies/custom.py)<br>[`src/factorlab/forward/integrity.py`](../../../src/factorlab/forward/integrity.py) | 1. `run_custom_strategy` 预切片列并用 `np.searchsorted` + `iloc[:end_pos].copy()` 替代每日布尔索引<br>2. `research_source_sha256` 缓存已校验祖先目录，避免 9,400+ 次重复 `os.lstat` | 仍逐文件校验 symlink/reparse 点与读取前后 `os.lstat` 一致性，且不修改 S02 Champion 状态机 |
| **S01 常驻隔离 Worker（可选）** | [`src/factorlab/strategies/worker.py`](../../../src/factorlab/strategies/worker.py)<br>[`src/factorlab/strategies/loader.py`](../../../src/factorlab/strategies/loader.py) | 新增 `--serve` 持久化隔离 worker 与 `LoadedStrategy(..., persistent_worker=False, verify_runtime_each_call=True)` 及 `close()` / 上下文管理器 | 默认 `persistent_worker=False` 与线上完全兼容；开启后仍保持 `-I -B`、清空敏感环境变量、独立 `cwd`、4 MiB 帧上限、`sys.addaudithook(guard)`、每次 `decide()` 重新实例化策略及超时强制击杀 fail-closed |

---

## 4. 四类 Benchmark 与实测加速结果

所有基准均采用 3–5 次重复测量，记录冷启动、热运行中位数与波动区间，并逐项校验金融语义（订单数、成交数、轮转数、手续费、滑点、资金费、净收益、最大回撤、VaR/CVaR、亏损概率与 FIFO 零残差）。

### 4.1 BENCH-A：纯路径生成与风险统计（NumPy CPU vs GPU）

| 场景 | 模型 | 路径数 | Batch Size | 路径生成中位数 (ms) | 吞吐 (paths/s) | 尾部统计 499-boot (ms) |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| `1asset_168h` | `gbm` | 64 | 64 | 0.91 | 70,175 | 1.70 |
| `1asset_168h` | `gbm` | 256 | 256 | 1.64 | 156,098 | 2.40 |
| `1asset_168h` | `gbm` | 1,024 | 1,024 | 7.49 (full: 9.66) | 136,679 | 5.45 |
| `1asset_168h` | `gbm` | 1,024 | **64** | **4.58** (full) | **223,776** | — |
| `1asset_168h` | `gbm` | 4,096 | 4,096 | 35.19 (full: 51.63) | 116,403 | 25.68 |
| `1asset_168h` | `gbm` | 4,096 | **64** | **16.92** (full) | **242,052** | — |
| `1asset_168h` | `gbm` | 10,000 | 4,096 | 78.30 (full: 124.29) | 127,720 | 89.39 |
| `1asset_168h` | `gbm` | 10,000 | **64** | **54.94** (full) | **182,017** | — |
| `1asset_168h` | `mbb` | 10,000 | 4,096 | 61.73 (full: 102.41) | 161,985 | 90.09 |
| `1asset_168h` | `mbb` | 10,000 | **64** | **25.64** (full) | **390,077** | — |
| `5asset_252d` | `gbm` | 10,000 | 4,096 | 337.45 | 29,634 | 90.90 |
| `5asset_252d` | `gbm` | 10,000 | **64** | **239.10** | **41,823** | — |

- **`tail_statistics` ABABAB 配对对比（10,000 路径 × 499 bootstrap）**：全量 `np.sort` `127.67 ms` → `np.partition` **`97.65 ms`（1.31x，最大误差 `0.0`）**。
- **`batch_size` 缓存效应说明**：`batch_size=64` 恰好贴合 i7-14700KF L2 缓存（每 P-core 2 MB），可提速 1.5–3.9x；但由于修改 `batch_size` 会改变逐批次 RNG 抽取顺序进而影响既有固定种子复现，**默认 `batch_size=4096` 保持不变**，仅建议新研究显式调优。
- **RTX 2070 GPU 评估**：
  - 探测结果：硬件为 RTX 2070（8192 MiB，驱动 617.42，测试时可用显存 1.06–5.68 GiB），但标准环境未安装 `cupy`/CUDA Toolkit，无法零依赖执行。
  - **含/不含 PCIe 传输收益量化**：即使安装 CuPy，1024×168 float64 路径矩阵仅 **1.38 MB**（10,000×168 仅 **13.44 MB**）；在 PCIe 3.0 x16（~10–12 GB/s）上传输耗时约 0.15–1.5 ms，内核启动+同步开销约 1–3 ms，而 CPU 完整生成 1024 路径仅需 **7.3 ms**（占完整 R09 生命周期 25.84s 的 **0.028%**）。因此 **GPU 对当前 Grid 完整生命周期研究无显著净收益**，不强行引入 CUDA 依赖以防破坏跨平台 CI。仅当未来出现 `>= 50,000 路径 × >= 50 资产 × 252 步` 的纯矩阵蒙特卡洛（如多资产相关性组合纯收益分布）时，RTX 2070 才具备净加速价值。

### 4.2 BENCH-B：完整 Grid 生命周期模拟（R09 F11/F08 与 R10）

| 工作负载 | 模式 | 优化前中位数 [min, max] | 优化后中位数 [min, max] | 吞吐变化 (accts/s) | 加速比 | 金融语义校验 |
| :--- | :--- | ---: | ---: | ---: | ---: | :---: |
| **R09 小规模** (16 accts, 1w, ABABAB 配对) | `:memory:` | 14.64s [11.34, 20.95] | **1.71s** [1.71, 3.95] | 1.09 → **9.33** | **8.54x** | 完全一致 (`pnl=-8.61609875`, `fills=188`, `rots=226`) |
| **R09 中规模** (64 accts, 1w) | `:memory:` | 66.92s [66.02, 67.20] | **11.91s** [10.48, 12.22] | 0.96 → **5.37** | **5.62x** | 完全一致 (`orders=1113`, `fills=649`, `rots=783`) |
| **R09 大规模** (256 accts, 8w) | `:memory:` | 37.56s [37.38, 37.80] | **10.80s** [10.60, 10.96] | 6.82 → **23.70** | **3.48x** | 完全一致 (`pnl=-126.01178361`, `orders=4531`, `fills=2770`, `var95=0.30861151`) |
| **R09 大规模** (256 accts, **最优 12w**) | `:memory:` | 37.56s (8w 基线) | **7.73s**（热计算 **4.72s**） | 6.82 → **33.14**（热 **54.28**） | **4.86x** | 完全一致，零 FIFO 残差 |
| **R10 小规模** (12 accts, 1w, `cleanup_dir=True`) | 临时即焚→内存权威 | 27.69s [24.24, 31.86] | **1.28s** [1.24, 1.28] | 0.43 → **9.40** | **21.69x** | 完全一致 (`final_pnl=-2.77519166`, `fills=10`) |
| **R10 大规模** (72 accts, 8w, `cleanup_dir=True`) | 临时即焚→内存权威 | 80.02s [74.54, 83.29] | **13.82s** [13.57, 17.84] | 0.90 → **5.21** | **5.79x** | 完全一致 (`final_pnl=-1.77056723`, `fills=36`, `orders=97`) |
| **R10 落盘对照组** (12 accts, 1w, `cleanup_dir=False`) | 磁盘 WAL `FULL` | 27.69s | 27.37s [24.95, 28.35] | 0.43 → 0.44 | 1.01x | **证伪与证据**：只读事务削减不减少 WAL 写刷盘，证实 NTFS `synchronous=FULL` fsync 是落盘模式绝对瓶颈 |

> **Profile 内部耗时佐证**（[`data/after_profile.json`](data/after_profile.json)）：
> - R09 4 路径：`PaperStore.load` 从 `2.124s` 降至 **`0.310s`（-85.4%）**，`PaperStore.save` 从 `3.059s` 降至 **`1.417s`（-53.7%）**，`deepcopy` 从 `4.758s` 降至 **`0.000s`（-100%）**，总墙钟从 `16.95s` 降至 **`4.86s`（3.49x，含 cProfile 探测开销）**。
> - R10 6 账户：SQLite 打开次数从 `18` 降至 **`6`**，事务数从 `2,034` 降至 **`1,026`（-49.6%）**，`order_events` 扫描从 `1,026` 降至 **`12`（-98.8%）**，`commit_seconds` 从 `8.138s` 降至 **`0.011s`（-99.9%）**。

### 4.3 BENCH-C：多账户并行扩展效率与 BLAS 线程控制

#### A. 优化后 256 账户 R09 扩展曲线（分离 Windows Spawn 与稳态热池计算）

| Workers | Spawn 耗时 (s) | 稳态计算耗时 (s) | 总耗时 (s) | 热池吞吐 (accts/s) | 稳态加速比 | 稳态并行效率 | 端到端加速比 | 端到端效率 | 语义一致 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| 1 | 0.00 | 53.73 | 53.73 | 4.76 | 1.00x | 100.0% | 1.00x | 100.0% | ✅ |
| 4 | 2.64 | 12.09 | 14.73 | 21.17 | 4.44x | 111.1%* | 3.65x | 91.2% | ✅ |
| 8 | 2.64 | 6.33 | 8.97 | 40.42 | 8.48x | 106.0%* | 5.99x | 74.9% | ✅ |
| **12** | **3.01** | **4.72** | **7.73** | **54.28** | **11.39x** | **94.9%** | **6.95x** | **58.0%** | ✅ |
| 16 | 3.77 | 5.19 | 8.96 | 49.34 | 10.36x | 64.7% | 6.00x | 37.5% | ✅ |
| 20 | 5.14 | 4.90 | 10.03 | 52.27 | 10.97x | 54.9% | 5.36x | 26.8% | ✅ |
| 24 | 5.22 | 5.70 | 10.92 | 44.90 | 9.42x | 39.3% | 4.92x | 20.5% | ✅ |

*\*注：4/8 worker 热池出现轻微超线性加速（106%–111%），因单进程连续运行 256 账户存在 Python GC 与 L3 缓存压力，拆分至 4–8 个独立 P-core 后每进程工作集常驻各核 L2/L3。*

#### B. 关键扩展规律与 BLAS 过量实测

1. **P-core / E-core 拐点清晰**：i7-14700KF 拥有 8 个 P-core + 12 个 E-core。在 `w=8..12` 区间内稳态并行效率保持 **94.9%–106%**，**`w=12` 达到端到端与稳态双峰值（`7.73s`，`54.28 accts/s`）**；超过 12 后进入 E-core 混合与 SMT 争抢区间，且 Windows `spawn` 启动时间从 `3.01s` 攀升至 `5.22s`，导致 `w=16/20/24` 的总耗时反而劣化。
2. **文件 WAL 模式瓶颈**：基线 BENCH-C2（24 账户落盘 WAL）从 `1w (44.55s)` 到 `12w (19.15s)` 仅 `2.33x`（效率 `19.4%`），`24w (17.52s)` 效率跌至 `10.6%`；改为内存权威模式后单核即降至 `6.22s`（7.16x）。
3. **小批量切忌频繁建池**：优化后 64 账户单核仅需 `11.91s`（约 5.4 accts/s），若每批重建进程池，Windows `spawn`（2.6–5.2s，高负载下甚至达 10–18s）将吞噬多核收益。因此在 `r09`/`r10` 研究入口实施了**单池复用**与任务阈值判断。
4. **BLAS 单线程锁定实测**：16 worker 并行执行多资产 Cholesky + 矩阵乘法时，不限 BLAS 线程中位数 `0.095s`，锁定单线程（`OMP/OPENBLAS/MKL/NUMEXPR_NUM_THREADS=1`）降至 **`0.078s`（1.21x 净提升）**，彻底消除 `16 × 28 = 448` 线程过度订阅。

### 4.4 BENCH-D：自动策略研究与 S01 协议回放

| 工作负载 | 优化前中位数 | 优化后中位数 | 加速比 | 说明与语义验证 |
| :--- | ---: | ---: | ---: | :--- |
| **D1: R10 Walk-Forward (12w) + 8 档资金费扫描** (8w) | **377.46s** [359.4, 390.2] | **29.32s** [27.9, 31.0] | **12.87x** | 四阶段全面提速：`precompute_ac` 112.7s→17.9s，`wf_mainnet` 67.8s→3.5s，`wf_demo` 67.4s→3.4s，`8-regime sweep` 111.6s→5.2s；OOS 收益 (`A=0.1648%, B=0.0%, C=0.641%`) 与 32 行扫描表 100% 精确一致 |
| **D2: S02 自动研究 12 候选** (ABABAB 配对) | **12.69s** [8.10, 13.49] | **7.42s** [7.13, 12.90] | **1.71x** | 选出候选 `c-e6b0d748191fb67033994937`、`promoted=False`、`reason` 完全一致 |
| **D3: S01 隔离 Grid 回放 8 bars / 16 events** (常驻 worker + 每调用校验) | **39.87s** [36.15, 40.25] | **6.90s** [4.95, 10.68] | **5.78x**（同载荷对比 **11.95x**） | 单事件耗时 `2,359 ms` → **`430 ms`**；仍保留 `-I -B`、审计算法钩子、4 MiB 帧上限与每次源码校验 |
| **D3: S01 隔离 Grid 回放 8 bars / 16 events** (常驻 worker + 启动单次校验) | **39.87s** [36.15, 40.25] | **2.59s** [2.08, 4.55] | **15.40x**（同载荷对比 **31.87x**） | 扣除一次性冷加载 `2.03s` 后，16 次 `decide()` 回放仅 **`0.557s`（34.8 ms/event）**；`orders_created=1`、`sqlite_tx=85`、`final_state_bytes=12053` 逐字节一致 |

### 4.5 1024 / 4096 / 10,000 路径大规模吞吐实测与推算

使用真实 R09 F11/F08 完整交易生命周期（每路径 168 小时、完整限价撮合、资金费、轮转、风险预算与零残差 FIFO 对账）：

| 规模 | 实测 / 推算 | 配置 | 路径生成 | 完整生命周期执行 | 尾部统计 | 总耗时 | 吞吐 |
| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| **1,024 路径** | **实测（最优 12w，低背景负载）** | `workers=12` | 7.3 ms (0.03%) | **25.83s (99.95%)** | 6.7 ms (0.03%) | **25.84s** | **39.63 paths/s** |
| **1,024 路径** | **实测（16w，48% 背景高负载）** | `workers=16` | 37.0 ms | 65.26s | 15.3 ms | 65.32s | 15.68 paths/s |
| **4,096 路径** | 线性推算（基于 12w 实测） | `workers=12` | ~29 ms | ~103.3s | ~27 ms | **~103.4s (~1.72 min)** | ~39.6 paths/s |
| **10,000 路径** | 线性推算（基于 12w 实测） | `workers=12` | ~71 ms | ~252.2s | ~65 ms | **~252.3s (~4.21 min)** | ~39.6 paths/s |

> 1,024 路径语义账本全量统计：共产生 **34,110 笔订单**、**21,248 笔成交**、**24,102 次网格轮转**，`sum_net_pnl_usdt = -62.85139499`，`var_95 = 0.0121419`，`cvar_95 = 0.01855361`，全部 1,024 账户 FIFO 残差为 `0`。对比优化前（8w 约 `3.41 paths/s`，1024 路径约需 `300s`、10,000 路径约需 `49 min`），大规模研究吞吐提升约 **11.6x**。

---

## 5. S01 / S04 共享状态协议增量演进建议（未强改共享核心）

遵守任务第 4D 节与第 7 节要求，为避免与并行进行的 **S04** 冲突，本次仅在 `loader.py`/`worker.py` 交付了向后兼容的 opt-in `persistent_worker` 优化，**未修改 `GridAdapter` 与 `decision_runtime.py` 的共享状态 Schema**。但审计显示 `GridAdapter` 在仅 8 根 K 线后状态快照即达 **12,053 B**，在 168–720 根 K 线的长回放中将面临 `O(N²)` 命令累计重放与 4 MiB 协议帧触顶风险。建议在 S04 合并后按以下最小兼容接口推进：

1. **协议版本协商**：在 `StrategyState` 增加可选字段 `checkpoint_version: int = 1` 与 `grid_snapshot: dict | None`（仅存当前活动网格槽位、边界价、累计轮转计数与最后处理的 `bar_ts`）。
2. **向后兼容回退**：当 `grid_snapshot` 存在且与 `state_sha256` 匹配时，`GridAdapter` 直接以 `O(1)` 恢复引擎而不再重放历史 `commands` 列表；若字段缺失则无缝回退原有全量重放路径。
3. **不削弱隔离边界**：状态校验仍由宿主 `TradingController` 权威账本主导，worker 进程继续维持 `-I -B`、`sys.addaudithook` 与单向 JSON 帧通道。

---

## 6. 当前机器（i7-14700KF / 64GB / RTX 2070）推荐运行配置

| 使用场景 | 推荐 `workers` | 进程池与存储策略 | BLAS / 环境变量 | 预期吞吐 |
| :--- | :---: | :--- | :--- | :--- |
| **小规模研究** (`< 32` 账户) | `1` | 进程内顺序执行，规避 2.6–3.0s Windows spawn | `OMP/OPENBLAS/MKL/NUMEXPR_NUM_THREADS=1` | ~5–9 accts/s |
| **中规模研究** (`32–128` 账户) | **`8`** | 复用单一 `create_process_pool(8)`，贴合 8 P-core | 单线程 BLAS 锁定 | ~28–40 accts/s |
| **大规模 MC / R09 / R10 研究** (`>= 128` 账户或 1024+ 路径) | **`12`** | 复用单一 `create_process_pool(12)` + `chunksize_for_tasks`；临时研究启用内存 `PaperStore` + 全量内存 FIFO 校验 | 单线程 BLAS 锁定 | **39–54 accts/s**（1024 路径 ~26s） |
| **必须保留落盘 WAL 审计文件** (`cleanup_dir=False`) | **`4–8`** | 磁盘 WAL `synchronous=FULL`；高于 8 worker 受 NTFS fsync 排队限制无增益 | 单线程 BLAS 锁定 | ~0.7–1.2 accts/s |
| **S01 离线策略回放研究** | `1`（或外层按候选并行 ≤ 8） | 显式启用 `persistent_worker=True`（静态源码目录可设 `verify_runtime_each_call=False`） | 保持 worker `-I -B` 隔离 | **35–430 ms/event**（提速 5.8–15.4x） |
| **GPU 加速开关** | — | **保持 `device="cpu"`** | — | CPU 路径生成已 >100k paths/s |

---

## 7. 核心八问最终回答

1. **R09/R10 慢的主要原因到底是什么？**
   - **R09（`:memory:` 模式）** 并非慢在 SQLite 或数学计算，而是 **91.3% 耗时消耗在 `TradingController._atomic` 内部的重复内存拷贝与全量序列化**：`storage.dumps`/`encode` 占 50.7%（主要来自 `GridExecution._authority` 每命令重哈希全部历史订单与 `PaperStore.save` 反复重编码 70KB 快照），`copy.deepcopy` 占 28.1%，`PaperStore.load` 全量 JSON 反序列化占 12.5%。
   - **R10（磁盘 WAL 模式）** 则是 **59.2% 耗时消耗在临时即焚文件的 `synchronous=FULL` NTFS fsync**（6 个账户共 2,034 次事务、18 次重开数据库、1,026 次 `events` 全表扫描），叠加每任务重复 pickle 33.5 KB 全量资金费表与进程池未复用。

2. **哪些优化已经实际实现？**
   - 已落地 7 大类工程优化（17 个文件修改/新增）：① `PaperStore` 精确类型编解码 + 不可变条目增量 JSON 片段缓存 + 负载一致校验快速克隆；② `TradingController._atomic` 移除冗余 `deepcopy` + `RiskLimits` 缓存；③ `GridExecution._authority` 按订单引用缓存序列化字节；④ `grid_paper` 消除每 bar 冗余 `project_paper_account()` 全表扫描、R10 `cleanup_dir=True` 启用内存 `PaperStore` + 完整内存 FIFO 对账、`position_settlements` `O(E+T log T)` 双指针与 `bisect_right` 资金费检索；⑤ 新增 `factorlab.compute.parallel` 实现 BLAS 单线程锁定、动态 `chunksize` 与 R09/R10 单进程池复用 + R10 窗口级资金费 IPC 切片；⑥ `tail_statistics` 用 `np.partition` 替代全量排序；⑦ `run_custom_strategy` `searchsorted` 切片 + `research_source_sha256` 祖先目录去重 + S01 可选常驻隔离 worker (`--serve`)。

3. **完整生命周期回测实际加速多少？**
   - **R09 单核配对（16 账户）**：`14.64s → 1.71s`（**8.54x**）。
   - **R09 多核大规模（256 账户）**：8w `37.56s → 10.80s`（**3.48x**）；最优 12w **`7.73s`（4.86x vs 8w 基线，热池计算 4.72s / 54.28 accts/s）**。
   - **R10 完整配对窗口（`cleanup_dir=True`）**：12 账户单核 `27.69s → 1.28s`（**21.69x**）；72 账户 8w `80.02s → 13.82s`（**5.79x**）。
   - **R10 Walk-Forward + 8 档资金费扫描（端到端）**：`377.46s → 29.32s`（**12.87x**）。
   - **S02 自动研究 12 候选（配对）**：`12.69s → 7.42s`（**1.71x**）；**S01 8-bar 隔离回放**：`39.87s → 6.90s / 2.59s`（**5.78x / 15.40x**）。

4. **当前设备最佳 worker 数是多少？**
   - 在 i7-14700KF（8 P-core + 12 E-core）上，**`workers = 12`** 是大规模内存研究负载的实测最优值（256 账户总耗时 7.73s 最低，热计算效率 94.9%，1024 路径达 39.63 paths/s）；中规模（32–128 账户）推荐 **`8`**；小规模（`< 32` 账户）推荐 **`1`** 以避免 2.6–3.0s Windows spawn 开销；磁盘 WAL `synchronous=FULL` 场景上限为 **`4–8`**。

5. **GPU 是否真的有净收益？**
   - **当前没有净收益。** 一方面标准环境未安装 CuPy/CUDA Toolkit 且 RTX 2070 空闲显存受桌面负载影响仅 1.06–5.68 GiB；更本质的是，NumPy CPU 生成 1024×168 路径仅需 **7.3 ms**，仅占完整 Grid 生命周期（25.84s）的 **0.028%**，其余 99.95% 为 Decimal 状态机、订单簿与账本逻辑（不适合 GPU）。在现有工作负载下强行迁移 GPU 会引入 PCIe 传输与跨平台依赖负担而无法改善端到端耗时。

6. **1024 / 4096 / 10,000 路径的实际吞吐如何？**
   - **纯路径生成（NumPy CPU）**：1 资产 168h GBM 达 **116k–242k paths/s**（10,000 路径 55–78 ms）；MBB 达 **162k–466k paths/s**；5 资产 252d GBM 达 **29.6k–41.8k paths/s**。
   - **完整 R09 F11/F08 交易生命周期（含 168h 撮合/风控/资金费/FIFO 对账）**：1024 路径在最优 12w 下实测 **25.84s（39.63 paths/s，34,110 订单 / 21,248 成交）**；按线性吞吐推算，4096 路径约 **1.72 分钟（103.4s）**，10,000 路径约 **4.21 分钟（252.3s）**（高后台负载 16w 下实测 1024 路径为 65.32s / 15.68 paths/s）。

7. **是否存在新的内存、I/O 或序列化瓶颈？**
   - **内存**：64 GB 内存极度充裕（单进程 RSS ~180 MB，12 worker 峰值总计约 2.2 GB）；`PaperStore` 增量 JSON 片段缓存随窗口结束自动释放，无跨账户泄漏。
   - **Windows Spawn 启动**：单路径耗时已大幅压低后，**进程池冷启动（2.6–5.2s）** 成为短任务的新主导项，必须通过单池复用与任务规模阈值规避。
   - **剩余单核热点**：在消除 `deepcopy` 与全量 `load` 后，R09 单路径残余耗时主要来自 `PaperStore.save` 的 JSON 字符串拼接（1.42s/4 paths）及 Python `Decimal` 逐 bar 撮合逻辑。

8. **哪些问题尚未解决？**
   - **磁盘 WAL `synchronous=FULL` 高频小事务**：当 `cleanup_dir=False` 需要落盘保留审计 DB 时，每 bar 写入完整 4–70 KB 快照触发 NTFS fsync 仍占主要耗时（属于持久化安全语义的物理下界；若未来需加速持久化模式，需在审计规范评审通过后采用批量检查点或追加式事件日志）。
   - **S01 `GridAdapter` 长窗口 `O(N²)` 状态增长**：因与 S04 共享状态协议，本次仅给出第 5 节增量检查点接口设计，待 S04 合并后落地。
   - **多候选自动研究（S02）内部并行与结果缓存**：`automatic.py` 仍顺序评估候选，待 S02-F01 稳定后可直接接入本次新增的 `factorlab.compute.parallel.create_process_pool` 与内容寻址缓存。
   - **超大规模纯矩阵 MC 的 GPU 加速**：当前未预装 CuPy，后续若新增 `>= 50k 路径 × >= 50 资产` 纯向量研究，可在独立可选 extra (`[cuda]`) 下启用并实测 RTX 2070 PCIe 净收益。
