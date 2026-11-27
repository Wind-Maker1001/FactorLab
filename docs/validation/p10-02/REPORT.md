# P10-02：FactorLab 执行端轻量化及 ARM64 跨架构适配验证报告

> **状态**：已完成优化、配对基准、ARM64 原生验证与跨架构一致性校验；未修改或升级马来西亚生产模拟主机，未自动合并 PR。  
> **开发分支**：`perf/p10-02-lightweight-arm64`（独立 worktree，基于 `origin/main` `388a8ee`）  
> **证据归档目录**：`docs/validation/p10-02/evidence/`

---

## 一、任务背景与硬约束

### 1.1 生产与验证环境对比

| 环境 | 主机 / 规格 | CPU / 架构 | 内存 / Swap | OS / Python | 本任务操作边界 |
| --- | --- | --- | --- | --- | --- |
| 生产模拟目标约束 | `factorlab-demo-01`（Azure `MalaysiaWest`，`Standard_B2ats_v2`） | 2 vCPU `x86_64` | 887 MiB 可见 RAM（1 GiB VM），2.0 GiB Swap | Ubuntu 24.04 x64 / CPython 3.13.18 | **P10-01 独占部署与写入**；本任务仅做低影响只读观测 |
| ARM64 验证环境 | `factorlab-arm-01`（Azure Japan，`Standard_B2pts_v2`） | 2 vCPU `aarch64`（Neoverse-N1） | 944 MiB 可见 RAM（1 GiB VM），2.0 GiB Swap | Ubuntu 24.04.5 LTS / 原生 CPython 3.13.18 (`aarch64`) | 原生安装、全量基线、热点优化、配对基准与持续观测 |
| 本地对比环境 | Windows x64 开发机 | AMD64 | — | Windows / CPython 3.13.9 | 跨架构金融结果对比、本地单测与打包校验 |

### 1.2 生产模拟主机只读基线观测（优化前痛点证据）

对 `factorlab-demo-01`（`85.211.193.242`）执行只读观测发现显著的资源浪费与内存压力：
- 系统 Swap 已使用 **581.9 MiB / 2.0 GiB**（总物理内存 887 MiB，可用 555 MiB），常驻运行 `factorlab-alpaca-paper.service`、`factorlab-demo-execute.service`、`factorlab-demo-tradfi-observer.service` 及 Azure 监控代理。
- 单机虚拟环境磁盘极度臃肿：
  - `/opt/factorlab-alpaca/current/venv`：**1.4 GiB**（含 `nvidia-nccl-cu12==2.32.6`、`xgboost==2.1.4`、`lightgbm==4.6.0`、`scikit-learn==1.7.2`、`scipy==1.16.3`、`matplotlib==3.10.9`、`pillow`、`fonttools` 等全部研究与 CUDA 组件）。
  - `/opt/factorlab/tradfi-observer/current/venv`：**1.4 GiB**。
  - `/opt/factorlab/releases/<commit>/venv`：每个发布版 **1.3 GiB**（历史 24 个发布版因 `shutil.copytree` 全量复制累计占用 31+ GiB）。

---

## 二、依赖及导入链审计结论

### 2.1 原始依赖与锁文件问题

1. **`pyproject.toml` 未区分执行期与研究期**：`[project].dependencies` 将 `numpy`、`pandas`、`scipy`、`scikit-learn`、`pyarrow`、`matplotlib`、`PyYAML`、`lightgbm`、`xgboost==2.1.4` 全部列为核心依赖，导致任意执行端安装均被迫引入全部研究栈。
2. **CUDA/NCCL 被无必要引入无 GPU 执行主机**：`requirements/alpaca-paper-linux-py313.lock.txt` 第 12 行明确锁定了 `nvidia-nccl-cu12==2.32.6`（由 `xgboost==2.1.4` 在 Linux 平台传递引入），无 GPU 执行主机平白承担数百 MiB 磁盘与依赖风险；同时包含 `xgboost`、`lightgbm`、`scikit-learn`、`scipy`、`matplotlib`、`exchange_calendars` 等非 Alpaca 运行期包。
3. **部署脚本硬编码 `x86_64`**：`tools/deploy_alpaca_paper_runtime.py` 与 `tools/deploy_demo_strategy.py` 硬编码 `/opt/factorlab/python/cpython-3.13-linux-x86_64-gnu/bin/python3`，无法直接在 ARM64 主机运行。
4. **隐藏的顶层重型导入链（静态 AST + 动态钩子追踪实证）**：
   - **暗链 1（导致 `scipy` 雪崩）**：任何导入 `factorlab.strategies.*`（包括 `protocol`、`loader`、`worker`、`grid_execution`、`decision_runtime`、`demo_grid_supervisor`、`alpaca.cloud_runtime`）都会触发 `strategies/__init__.py:3 -> dual_ma.py:15 -> backtest/__init__.py:3 -> backtest/fast.py:21 -> portfolio/__init__.py:5 -> portfolio/optimizer.py:11 -> import scipy`。在未安装 `scipy` 的 ARM64 环境中，哪怕仅 `import factorlab.strategies.loader` 都会直接抛出 `ModuleNotFoundError: No module named 'scipy'`。
   - **暗链 2（Binance 纯 Decimal 核心被迫加载 `pandas/numpy/pyarrow`）**：
     - `strategies/grid.py`、`strategies/grid_execution.py`、`backtest/fees.py` 仅因用 `pd.Timestamp` 解析 ISO 字符串而顶层 `import pandas`。
     - `trading/strategy.py` 顶层 `import pandas` 与 `dual_ma.crossover_indicators`。
     - `binance_futures_trading/protocol.py -> integrations/binance/__init__.py -> client.py -> data/__init__.py / data/evidence.py / binance/store.py` 与 `rules.py -> binance/market.py` 顶层拉起 `pandas + numpy + pyarrow`。
   - **暗链 3（策略隔离 Worker 每次决策冷启动风暴）**：`LoadedStrategy._call()` 每次决策都新建临时目录、全量写源码、以 `subprocess.run` 冷启动全新 Python 进程并在 `worker.py` 顶层 `import factorlab.strategies.adapters`（拉起 `scipy + pandas + numpy + pyarrow`，单次决策在 ARM64 耗时 **~810 ms**、瞬时峰值 **~143 MiB RSS**）；且每次 `decide()` 都通过 `runtime_identity()` 对全仓 `src/factorlab/**/*.py` 200+ 文件重新读盘计算 SHA-256。

### 2.2 最小充分运行期依赖分层（已落地并实测 `pip check` 通过）

详见 [执行端轻量化与跨架构安装说明](../../execution-lightweight-install.md)。

| 依赖分层 | `pyproject.toml` extra / 锁文件 | 包含包 | ARM64 实测 venv 体积 | 较旧生产 venv 缩减 |
| --- | --- | --- | --- | --- |
| 公共交易核心 | 默认 `dependencies` | 仅 `tzdata>=2026.4`（+ Python 3.13 标准库） | ~18 MiB | -98.6% |
| Binance USD-M / Equity 执行端 | `.[execution-binance]` / `requirements/execution-binance.txt` | `tzdata`, `websockets`（2 包，零 `numpy/pandas/pyarrow/scipy/ML/CUDA`） | **22 MiB** | **-98.3%**（对比 1.3 GiB） |
| TradFi 观察与日历调度 | `.[execution-tradfi]` / `requirements/execution-tradfi.txt` | `numpy`, `pandas`, `exchange-calendars` 等（9 包） | **122 MiB** | **-91.3%**（对比 1.4 GiB） |
| Alpaca Paper 执行端 | `.[execution-alpaca]` / `requirements/alpaca-paper-linux-py313.lock.txt` | `numpy`, `pandas`, `pyarrow`, `websockets`, `tzdata`, `python-dateutil`, `pytz`（7 包，零 `scipy/ML/CUDA/NCCL`） | **261 MiB** | **-81.4%**（对比 1.4 GiB） |
| 同机双市场 + 公共组件统一环境 | `.[execution-dual]` / `requirements/execution-dual-linux-py313.lock.txt` | Binance + Alpaca + TradFi 全部最小运行期包（11 包） | **262 MiB** | **-93.6%**（对比三环境合计 4.1 GiB） |
| 研究环境（完全兼容保留） | `.[research]` / `requirements/py314-core.txt` | 保留原全部 10 项核心研究依赖（含 4 模型/回测/绘图） | 仅研究机 | 现有 `py314-core.txt` + `--no-deps` CI 入口不变 |
| GPU 依赖（仅研究机） | `.[monte-carlo-cuda]` | `cupy-cuda13x[ctk]` | 禁止上执行主机 | 与执行环境完全隔离 |

---

## 三、ARM64 原生安装与功能一致性验证

### 3.1 原生运行环境与系统原语实测（日本 `factorlab-arm-01`）

1. **零 x86 仿真证明**：
   - `uname -m` = `aarch64`（2 vCPU `Neoverse-N1`），`/proc/sys/fs/binfmt_misc` 无任何 `qemu-x86_64` 注册项。
   - `uv 0.13.10` 与 `/opt/factorlab/python/cpython-3.13-linux-aarch64-gnu/bin/python3`（`Python 3.13.18`）经 `file -L` 实证均为 `ELF 64-bit LSB pie executable, ARM aarch64, interpreter /lib/ld-linux-aarch64.so.1`。
   - 全部 4 个分层最小 venv 均通过 `--only-binary=:all:` 安装原生 `manylinux_*_aarch64` wheel，`pip check` 均输出 `No broken requirements found.`，真实模块 import 全部通过。
2. **系统原语逐项验证**（证据见 [parity_arm64_optimized.json](evidence/parity_arm64_optimized.json)）：
   - **SQLite**：`3.54.1`，`PRAGMA journal_mode=WAL` 与 `PRAGMA integrity_check = ok` 均通过。
   - **systemd**：`systemd 255 (255.4-1ubuntu8.18)`，对 `deploy/systemd/*.service` 与 `*.timer` 全量执行 `systemd-analyze verify`，全部沙箱指令（`LoadCredential`, `ProtectSystem=strict`, `LockPersonality`, `RestrictAddressFamilies` 等）语法与兼容性通过。
   - **文件锁**（证据见 [lock_arm64.json](evidence/lock_arm64.json)）：`owner_lock` 与 `controller_lock` 非阻塞单写入者保护均已跨进程实测通过（持锁期间第二进程分别严格触发 `BlockingIOError` / 返回 `False` 拒绝，释放后可重新获取）；阻塞 `fcntl.flock(LOCK_EX)` 8 独立 `spawn` 进程 × 25 轮（共 200 次并发临界区增量）实测计数精确等于 `200/200`（含 8 进程启动总耗时 `2836.08 ms`）。
   - **WebSocket**：`websockets 16.1.1` 本地回环异步客户端/服务端收发校验通过。
   - **TLS**：`OpenSSL 3.5.9`，`capath=/etc/ssl/certs`，真实 HTTPS 握手（`https://pypi.org/simple/`）返回 `HTTP 200`。
   - **Decimal 与时区/DST**：固定精度乘法/量化/余额校验与 `ZoneInfo('America/New_York')` 春季夏令时跳变（`01:30-05:00` → `03:30-04:00`）及秋季回拨（`01:30-05:00`）全部准确无误。
3. **ARM64 最小无 `scipy` 环境下离线执行测试套件**：
   - 在 `venv-opt-dual`（仅 11 个执行包，不含 `scipy`/`sklearn`/`xgboost`/`lightgbm`/`matplotlib`）上运行 16 个核心离线执行、资金核算、订单恢复、热切换、原生 V69、自适应网格 V2、一致性备份与 P10-02 回归测试文件：**227 passed, 1 skipped（Windows Credential Manager 专属用例）, 0 failed**（总耗时 70.85 秒）。
   - 该轮实测直接发现了两项仅在 Linux 无 `scipy` 环境触发的真实缺陷并已根治：
     1. `DualMAStrategy` 经 `dual_ma.py -> backtest/execution.py -> portfolio/__init__.py -> optimizer.py` 隐式依赖 `scipy`（已通过 `portfolio/__init__.py` 延迟导出与 `dual_ma.py` 回测依赖下沉修复）。
     2. Linux 隔离策略沙箱下 `V69AlpacaStrategy` 首次本地化 `America/New_York` 访问 `/usr/share/zoneinfo` 被 `addaudithook` 拦截（已在 `worker.py` 沙箱前对数值/日历策略预热 `UTC`/`America/New_York` 时区表修复，不扩大文件白名单）。

### 3.2 AMD x64 与 ARM64（及优化前后）四向金融结果一致性对照

使用固定行情、固定订单、固定资金费及固定 252 日 8 资产矩阵，在 **AMD x64 基线**、**AMD x64 优化后**、**ARM64 基线**、**ARM64 优化后** 四个组合下运行同一确定性校验脚本（证据见 `evidence/parity_*.json`）：

| 验证场景 | 核心检验字段 | 一致性标准 | 实测结果（4 向对比） |
| --- | --- | --- | --- |
| Binance USD-M 网格 + 资金费 + SQLite 重载 | 网格价位、提交订单（`cid/side/qty/limit/status`）、最终 `USDT` 现金 `4999.87654322`、持仓、资金费总额 `-0.12345678` | Decimal 字段逐位精确一致（容差 `0`） | **4 向 100% 精确一致** |
| V2 隔离策略包多轮决策与状态演进 | `package_id`、`code_sha256`、3 轮目标权重（`0.5`, `0.5`, `0`）与 `Decimal` 状态字段 | Decimal 字段与哈希逐位精确一致（容差 `0`） | **4 向 100% 精确一致** |
| Alpaca Native V69 8 资产 252 日组合决策 | 全部 8 资产浮点目标权重、4 笔调仓单（`JPM`, `GE`, `TQQQ`, `QQQ`）`adjusted_quantity`、周状态 | 预先定义浮点容差 `atol=1e-12, rtol=1e-12`（用于容忍不同 CPU 架构 BLAS/libm 末位舍入） | **实测最大绝对偏差 `0.0`**（4 向逐位一致） |

---

## 四、已实施的热点轻量化优化汇总

所有优化均基于基线实测热点实施，严格保持现有进程隔离、审计沙箱、单账户所有权、SQLite 账本持久性与金融精度，未引入独立调度平台或重量级框架：

1. **切断非必要重型导入链**（`strategies/__init__.py`, `backtest/__init__.py`, `portfolio/__init__.py`, `data/__init__.py`, `data/hub/__init__.py`, `integrations/binance/__init__.py`, `integrations/alpaca/market_data/__init__.py`, `backtest/fees.py`, `strategies/grid.py`, `strategies/grid_execution.py`, `strategies/dual_ma.py`, `trading/strategy.py`, `data/evidence.py`, `integrations/binance/client.py`, `integrations/binance/market.py`）：
   - 包入口全面改为 PEP 562 `__getattr__` 延迟导出，对外公共导入 100% 兼容。
   - `grid.py`、`grid_execution.py`、`backtest/fees.py` 改用纯标准库 ISO 时间解析替代仅为解析日期引入的顶层 `pandas`。
2. **常驻隔离策略 Worker + 有界 LRU 池 + 增量 `runtime_identity()` 校验**（`strategies/worker.py`, `strategies/loader.py`, `integrations/alpaca/strategy_instances.py`, `integrations/alpaca/native_v69.py`）：
   - 保持每个策略独立隔离子进程（`-I -B`、空环境变量、`close_fds=True`、`sys.addaudithook`、4 MiB 响应上限与单次超时强杀），且每次请求在进程内重新实例化策略对象，防止隐藏实例状态跨 tick 泄漏。
   - 通过 `FACTORLAB_STRATEGY_WORKER_POOL_MAX`（默认 `12`）实施有界 LRU 并发背压与空闲回收，防止多实例常驻突破 1 GiB 内存上限。
   - `worker.py` 基于 AST/manifest 仅在策略确需数值库时预导入 `numpy/pandas` 并预热时区表，纯 Python/Decimal 策略零重型库加载。
   - `runtime_identity()` 增加目录/文件 `(st_mtime_ns, st_size)` 快速验证缓存，任意源码变动立即触发全量 SHA-256 重算拒绝执行。
3. **热点查询、缓存与流存储收敛**（`strategies/decision_runtime.py`, `integrations/alpaca/market_data/cache.py`, `integrations/alpaca/cloud_runtime.py`）：
   - `DecisionJournal._count()` / `_latest()` 由全表扫描改为 `events.id` 主键前缀范围索引查询。
   - `MarketCache` 增加有界 LRU 内存缓存（默认 32 条），每次 `load()` 仍完整校验磁盘 `bars.parquet` SHA-256（零安全妥协），命中后跳过重复 Parquet 反序列化。
   - `MarketWorker` 的行情流缓存库 `market-stream.sqlite3`（非账户账本）启用 WAL 与 7 天滚动窗口清理。
4. **依赖分层与跨架构安装器适配**（`pyproject.toml`, `requirements/*`, `tools/deploy_*.py`, `tools/install_*.sh`, `tools/install_demo_strategy_runtime.py`）。

---

## 五、优化前后配对实测基准（日本 ARM64 `Standard_B2pts_v2`，2 vCPU / 944 MiB RAM）

> 以下所有优化前/后数据均由同一套基准脚本在 `factorlab-arm-01` 同机采集，原始 JSON 见 [bench_arm64_baseline.json](evidence/bench_arm64_baseline.json) 与 [bench_arm64_optimized.json](evidence/bench_arm64_optimized.json)。

### 5.1 冷启动及依赖导入峰值

| 执行入口模块 | 优化前 Wall (ms) | 优化后 Wall (ms) | 优化前 RSS / PSS (MiB) | 优化后 RSS / PSS (MiB) | 重型导入变化 |
| --- | ---: | ---: | ---: | ---: | --- |
| `factorlab.strategies.worker` | 574.3 | **28.6** (-95.0%) | 142.8 / 135.7 | **19.8 / 13.6** (-86.1% / -90.0%) | `numpy,pandas,scipy,pyarrow` → `[]` |
| `factorlab.strategies.loader` | 589.5 | **60.5** (-89.7%) | 143.6 / 136.4 | **24.2 / 17.7** (-83.1% / -87.0%) | `numpy,pandas,scipy,pyarrow` → `[]` |
| `factorlab.strategies.grid_execution` | 590.3 | **37.3** (-93.7%) | 143.5 / 136.3 | **22.0 / 15.5** (-84.7% / -88.6%) | `numpy,pandas,scipy,pyarrow` → `[]` |
| `factorlab.strategies.decision_runtime` | 633.8 | **136.2** (-78.5%) | 145.4 / 138.2 | **29.5 / 23.0** (-79.7% / -83.4%) | `numpy,pandas,scipy,pyarrow` → `[]` |
| `demo_grid_supervisor`（Binance Execute） | 641.7 | **228.8** (-64.3%) | 147.4 / 140.2 | **32.8 / 26.3** (-77.7% / -81.2%) | `numpy,pandas,scipy,pyarrow` → `[]` |
| `demo_instance_owner` | 397.2 | **68.4** (-82.8%) | 111.5 / 104.4 | **25.7 / 19.2** (-77.0% / -81.6%) | `numpy,pandas,pyarrow` → `[]` |
| `alpaca.cloud_runtime` | 641.6 | **479.9** (-25.2%) | 150.8 / 143.6 | **117.5 / 110.3** (-22.1% / -23.2%) | 清除 `scipy`（仅保留所需 `numpy,pandas,pyarrow`） |
| `alpaca.strategy_instances` | 659.2 | **483.2** (-26.7%) | 151.4 / 144.2 | **116.6 / 109.5** (-23.0% / -24.1%) | 清除 `scipy` |

### 5.2 历史行情预热峰值（8 资产 × 252 日 + 5 日 1Min Parquet Cache）

| 指标 | 优化前 | 优化后 | 变化 |
| --- | ---: | ---: | ---: |
| 峰值内存 HWM (MiB) | 168.76 | **132.23** | **-21.6%** |
| 多策略 3× 重复读取耗时 (ms) | 188.30 | **16.17** | **-91.4%（11.6× 加速）** |
| 缓存磁盘占用 (KiB) | 840.5 | 840.5 | 保持一致（Parquet 完整校验不变） |

### 5.3 单市场与双市场 + 公共组件同机运行

| 场景 | 优化前 RSS / PSS (MiB) | 优化后 RSS / PSS (MiB) | 系统最低可用内存变化 | 进程 Swap |
| --- | ---: | ---: | ---: | ---: |
| 单市场：Binance Execute | 148.9 / 141.6 | **30.9 / 24.3** (-79.2% / -82.9%) | — | 0.0 MiB |
| 单市场：Alpaca Runtime | 156.7 / 149.4 | **121.5 / 114.2** (-22.5% / -23.5%) | — | 0.0 MiB |
| **双市场 + TradFi Observer 3 进程同机** | **425.6 / 285.8** | **273.1 / 193.5** (-35.8% / -32.3%) | **436.9 → 513.9 MiB (+77.1 MiB)** | **0.0 MiB** |

优化后同机 3 进程明细：Binance Execute `31.0 MiB RSS / 20.2 MiB PSS`、Alpaca Runtime `121.6 MiB RSS / 87.1 MiB PSS`、TradFi Observer `120.6 MiB RSS / 86.2 MiB PSS`。

### 5.4 多策略隔离工作进程扩容阶梯（3 / 6 / 12 / 24 实例，1 分钟周期）

> 注：测试为真实 `LoadedStrategy.from_strategy_package` 独立隔离子进程 + `InstanceDecisionRuntime` + `DeploymentStore` SQLite 持久提交；优化后使用默认池上限 `FACTORLAB_STRATEGY_WORKER_POOL_MAX=12`。

| 实例规模 | 阶段 | 初始化 (ms) | 单决策 p50 / p95 / p99 (ms) | 单 Tick 汇总 p50 (ms) | 3 Tick 子进程 CPU (ms) | 估算每分钟 CPU 占比（2 vCPU 基线 24s/m） | 峰值合计 PSS (MiB) | 系统最低可用 RAM (MiB) | 常驻子进程数 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **3** | 优化前 | 2,550.4 | 840.1 / 866.1 / 866.1 | 2,531.2 | 11,733.2 | 16.3% | 141.7 | 525.7 | 0（每次冷启） |
| **3** | **优化后** | **387.0** | **12.6 / 17.0 / 17.0** | **38.1** | **332.6** | **0.46%** | **70.8** | **579.7** | **3** |
| **6** | 优化前 | 4,846.2 | 804.9 / 815.6 / 815.6 | 4,825.1 | 22,589.1 | 31.4% | 142.4 | 512.2 | 0 |
| **6** | **优化后** | **758.6** | **11.6 / 14.5 / 14.5** | **73.7** | **659.2** | **0.92%** | **116.4** | **544.1** | **6** |
| **12** | 优化前 | 9,798.2 | 806.9 / 839.2 / 842.9 | 9,697.1 | 45,474.5 | 63.2%（高风险） | 144.4 | 504.2 | 0 |
| **12** | **优化后** | **1,513.1** | **11.1 / 14.0 / 15.3** | **139.6** | **1,327.5** | **1.84%** | **206.6** | **455.5** | **12** |
| **24** | 优化前 | 19,429.5 | 808.5 / 823.9 / 843.2 | 19,426.0 | 90,536.8 | **125.7%（超基线必耗尽积分）** | 147.5 | 498.1 | 0 |
| **24** | **优化后** | **3,076.4** | **125.2 / 129.3 / 135.1** | **3,028.2** | **10,507.5** | **14.6%（池轮换）** | **209.1**（封顶） | **452.6** | **12（LRU 封顶）** |

*说明*：Azure `Standard_B2pts_v2` / `Standard_B2ats_v2` 为 2 vCPU 可突发实例，持续基础性能基线为单核 20%（合计 40% 单核 = **24 秒 CPU 时间 / 分钟**）。优化前在 12–24 策略下仅依赖短时 CPU 积分才未当场超时，持续运行会耗尽积分导致严重降频；优化后 12 实例每分钟仅需 **0.44 秒 CPU**（基线占比 < 2%），具备真实可持续运行能力。24 实例在 `POOL_MAX=12` 下因每轮发生 LRU 进程驱逐重建，单决策 p50 升至 125.2 ms、每分钟 CPU 占基线 14.6%，虽不超内存但会增加子进程启停开销，因此**不建议在 1 GiB 同机双市场主机上以 24 实例长期常驻**。

### 5.5 断线补齐、交易高峰及 SQLite 写入/备份/日志轮换

| 指标 | 优化前 | 优化后 | 变化 |
| --- | ---: | ---: | ---: |
| 断线后 120 根 1m Bar 补齐 + 对账恢复 (ms) | 3,080.5 | **3,004.4** | -2.5%（受 SQLite `synchronous=FULL` 持久提交支配，未妥协） |
| 交易高峰 12 标的 × 10 波（120 单）p50 / p95 / p99 (ms) | 14.8 / 19.3 / 21.8 | **14.3 / 19.1 / 23.0** | 保持一致 |
| `DecisionJournal` 300 次决策 p50 / p95 (ms) | 13.2 / 17.7 | **12.8 / 17.3** | 范围索引查询稳定 |
| SQLite 一致性备份 + 校验耗时 (ms) | 62.2 / 1.9 | **39.7 / 1.9** | **备份耗时 -36.2%** |
| 2000 行健康日志写入 + 512 KiB 轮换 (ms) | 47.9 | **45.1** | -5.8% |
| 进程峰值内存 HWM (MiB) | 150.4 | **33.9** | **-77.5%** |

---

## 六、持续运行资源观测（Soak Observation）与容量建议

### 6.1 实测持续观测（明确区分基准、压力与持续观测）

- **观测类型**：加速连续运行观测（非 24h/7d 生产长期实测）。
- **观测时长**：**183.0 秒**（3 分钟连续运行，证据见 [soak_arm64.json](evidence/soak_arm64.json)）。
- **负载组合**：在 `factorlab-arm-01`（944 MiB 总 RAM）同机并发运行 **15 个真实 OS 进程**：
  1. Binance Execute（2 秒循环）
  2. TradFi Observer（10 秒循环）
  3. Alpaca Runtime + **12 个独立 V2 策略隔离子进程**，以 **10 秒 / 轮**（6× 加速于生产 1 分钟周期）连续完成 **18 轮 / 216 次** 完整隔离决策与 SQLite 持久化。
- **真实观测结果**：
  - 全部 15 进程合计 PSS：起始 `377.77 MiB` → 结束 `378.38 MiB`（**净增长 +0.61 MiB，无内存泄漏趋势**），全程进程 Swap = **0.0 MiB**，OOM = **0**。
  - 系统最低可用内存 `MemAvailable`：**343.68 MiB**（保留 36.4% 物理内存余量），任务退出后迅速恢复至 `645.45 MiB`。
  - 216 次真实隔离策略决策延迟：**p50 = 13.45 ms、p95 = 19.58 ms、p99 = 26.44 ms**；12 策略单轮汇总最大耗时 **200.38 ms**（仅为 1 分钟周期的 0.33%）。

### 6.2 推荐的长期同机运行容量（2 vCPU / 1 GiB RAM，Ubuntu 24.04 x64 或 ARM64）

在扣除操作系统内核 + `systemd` + Azure 监控代理（约 250–320 MiB）与双市场 + TradFi Observer 基础进程（合计约 **194 MiB PSS**）后，主机剩余约 **430–500 MiB** 可用于策略隔离工作进程、文件系统页缓存与瞬时备份峰值：

| 策略工作负载类型 | 推荐长期稳定运行实例数 | 建议 `FACTORLAB_STRATEGY_WORKER_POOL_MAX` | 预估总 PSS | 预估安全余量 |
| --- | --- | --- | --- | --- |
| 纯 Python / Decimal 策略（如网格、动量、规则类 V2 策略，单 worker ~15–20 MiB PSS） | **6 – 12 个实例**（双市场同机） | `8` – `12` | ~290 – 380 MiB | 物理可用 RAM ≥ 250–340 MiB，CPU < 3% 基线 |
| 含 `numpy/pandas` 数值策略（如 `V69AlpacaStrategy`、`DualMAAdapter`、`AdaptiveGridV2`，单 worker ~70–95 MiB PSS） | **2 – 3 个常驻数值实例** + 最多 **4 – 6 个轻量实例** | `6` – `8` | ~360 – 450 MiB | 需避免 >4 个重型数值 worker 同时常驻，防触发 Swap |
| 超过 12 个策略实例（13 – 24 实例） | **不推荐在 1 GiB 单机长期满载** | 仅应急轮换 `12` | 封顶 ~380 MiB | 每分钟发生 LRU 进程驱逐重建，CPU 消耗增至 ~15% 基线且 p50 增至 ~125 ms |

---

## 七、已知依赖兼容性、尚未验证边界与风险说明

1. **已验证范围 vs 尚未验证的长时边界**：
   - 本次在 ARM64 完成了 **183 秒 6× 加速连续运行观测**（等效 ~18 分钟生产 bar 周期），验证了短时无内存泄漏、零 Swap 与低 CPU 占比；**尚未完成 24 小时 / 7 天真实联网行情（含真实交易所断线、WebSocket 抖动、跨日历/夏令时切换、连续多日 Azure Monitor 代理内存波动及每日定时备份叠加）的长期生产观察**。
   - **Azure CPU 积分**：本次通过精确测量进程 CPU 时间（`RUSAGE_SELF + RUSAGE_CHILDREN`）并与 B2pts/B2ats v2 标称 20%/vCPU 基线对比评估可持续性，未通过 Azure Monitor API 读取云端实时积分余额表。
2. **已知依赖兼容性约束**：
   - `AdaptiveGridV2` 内部经 `grid_restore.py` 调用研究模块 `factorlab.experiments.grid_funding_adaptive`（顶层依赖 `numpy` / `pandas`），因此在部署 `AdaptiveGridV2` 的主机上需使用包含 `strategy-numeric` 或 `execution-dual` 的环境（纯 22 MiB `execution-binance` 仅适用于不依赖 `numpy/pandas` 的原生 `GridExecution` 与纯 Decimal V2 策略）。
   - 研究期/CI 专用锁测试 `tools/ci/lock_acceptance.py` 导入了 `factorlab.forward`（进而导入 `PyYAML`、`scipy`、`scikit-learn`），属于研究测试套件依赖，不属于执行端最小运行期依赖；执行端 `owner_lock`、`controller_lock` 与 `fcntl.flock` 8 进程 200 次并发互斥已在 ARM64 最小环境单独验证通过（[lock_arm64.json](evidence/lock_arm64.json)）。
   - Python 版本仍严格保持 `>=3.13,<3.15`；Ubuntu 24.04 系统自带 Python 3.12 不可直接运行，须按安装说明部署原生 `cpython-3.13-linux-<arch>-gnu`（且安装至 `/opt/factorlab/python` 保证服务系统用户只读可见，勿指向权限为 `750` 的个人 `$HOME`）。
3. **生产主机操作边界确认**：
   - 本任务未对 `factorlab-demo-01`（马来西亚生产模拟机）执行任何写入、部署、重启或 VM 升配操作；亦未在 ARM64 主机配置任何生产账户凭证或启动第二写入者。

---

## 八、验证与交付清单

- [x] 最小执行依赖与 `pyproject.toml` 分层（保留研究环境 `.[research]` 与 `requirements/py314-core.txt` 完全兼容）
- [x] 跨架构（x86_64 / ARM64）安装与部署脚本适配及安装文档：[docs/execution-lightweight-install.md](../../execution-lightweight-install.md)
- [x] ARM64 原生安装与功能运行证据（4 个分层 venv `pip check` 全绿，227 项离线集成测试全绿，[lock_arm64.json](evidence/lock_arm64.json) 跨进程文件锁验证全绿）
- [x] 3/6/12/24 实例资源曲线与优化前后配对基准：[bench_arm64_baseline.json](evidence/bench_arm64_baseline.json)、[bench_arm64_optimized.json](evidence/bench_arm64_optimized.json)
- [x] 四向（x64/ARM64 × 优化前后）金融结果一致性证据：[parity_x64_baseline.json](evidence/parity_x64_baseline.json)、[parity_x64_optimized.json](evidence/parity_x64_optimized.json)、[parity_arm64_baseline.json](evidence/parity_arm64_baseline.json)、[parity_arm64_optimized.json](evidence/parity_arm64_optimized.json)
- [x] 183 秒同机双市场 + 12 策略持续观测证据：[soak_arm64.json](evidence/soak_arm64.json)
- [x] 本地 wheel/sdist 打包构建与 `inspect_distributions.py`（504 模块全检）通过
- [x] 回归单测：`tests/integrations/test_p10_02_lightweight_arm64.py`（16 项专属回归用例）
