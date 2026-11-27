# P10-02：FactorLab 执行端轻量化及 ARM64 跨架构适配验证报告

> **状态**：已修复 PR #268 的 one-shot 隔离、并发、管道清理、纳秒精度和最小环境邮箱执行问题，并完成 Windows x64 最小环境业务验收。原生 Linux x64 / ARM64 的同场景与逐字节对照由 PR 检查发布。
> **开发分支**：`perf/p10-02-lightweight-arm64`（原独立 worktree，PR #268，修复基于 `108dc07`，包含 #264 / P10-11 和 #267 / P10-01）
> **证据归档目录**：`docs/validation/p10-02/evidence/`

---

## 一、任务背景与硬约束

### 1.1 生产与验证环境对比

| 环境 | 主机 / 规格 | CPU / 架构 | 内存 / Swap | OS / Python | 本任务操作边界 |
| --- | --- | --- | --- | --- | --- |
| 生产模拟目标约束 | `factorlab-demo-01`（Azure `MalaysiaWest`，`Standard_B2ats_v2`） | 2 vCPU `x86_64` | 887 MiB 可见 RAM（1 GiB VM），2.0 GiB Swap | Ubuntu 24.04 x64 / CPython 3.13.18 | **P10-01 独占部署与写入**；本任务仅做低影响只读观测 |
| ARM64 验证环境 | `factorlab-arm-01`（Azure Japan，`Standard_B2pts_v2`） | 2 vCPU `aarch64`（Neoverse-N1） | 944 MiB 可见 RAM（1 GiB VM），2.0 GiB Swap | Ubuntu 24.04.5 LTS / 原生 CPython 3.13.18 (`aarch64`) | 原生安装、全量基线、热点优化、审查修复后配对复测 |
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
   - **暗链 3（one-shot 策略 Worker 因重型顶层导入导致冷启动风暴）**：优化前 `worker.py` 每次决策均在顶层通过 `adapters` 拉起 `scipy + pandas + numpy + pyarrow`，单次决策在 ARM64 耗时 **~810 ms**、瞬时峰值 **~143 MiB RSS**、24 实例每分钟消耗 ~30s CPU（突破 B2pts 持续基线 24s/min）；且每次 `decide()` 都通过 `runtime_identity()` 对全仓 `src/factorlab/**/*.py` 200+ 文件重新读盘计算 SHA-256。

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
2. **系统原语逐项验证**（证据见 [parity_arm64_optimized.json](evidence/parity_arm64_optimized.json) 与 [lock_arm64.json](evidence/lock_arm64.json)）：
   - **SQLite**：`3.54.1`，`PRAGMA journal_mode=WAL` 与 `PRAGMA integrity_check = ok` 均通过。
   - **systemd**：`systemd 255 (255.4-1ubuntu8.18)`，对 `deploy/systemd/*.service` 与 `*.timer` 全量执行 `systemd-analyze verify`，全部沙箱指令（`LoadCredential`, `ProtectSystem=strict`, `LockPersonality`, `RestrictAddressFamilies` 等）语法与兼容性通过。
   - **文件锁**（证据见 [lock_arm64.json](evidence/lock_arm64.json)）：`owner_lock` 与 `controller_lock` 非阻塞单写入者保护均已跨进程实测通过（持锁期间第二进程分别严格触发 `BlockingIOError` / 返回 `False` 拒绝，释放后可重新获取）；阻塞 `fcntl.flock(LOCK_EX)` 8 独立 `spawn` 进程 × 25 轮（共 200 次并发临界区增量）实测计数精确等于 `200/200`（含 8 进程启动总耗时 `2836.08 ms`）。
   - **WebSocket**：`websockets 16.1.1` 本地回环异步客户端/服务端收发校验通过。
   - **TLS**：`OpenSSL 3.5.9`，`capath=/etc/ssl/certs`，真实 HTTPS 握手（`https://pypi.org/simple/`）返回 `HTTP 200`。
   - **Decimal 与时区/DST**：固定精度乘法/量化/余额校验与 `ZoneInfo('America/New_York')` 春季夏令时跳变（`01:30-05:00` → `03:30-04:00`）及秋季回拨（`01:30-05:00`）全部准确无误。
3. **ARM64 最小无 `scipy` 环境下离线执行与审查修复后测试套件**：
   - 修复前全量 16 个核心离线执行/核算/恢复测试文件在 `venv-opt-dual` 上运行：**227 passed, 1 skipped（Windows Credential Manager 专属用例）, 0 failed**（70.85 秒）。
   - PR #268 P1/P2 修复后，在 ARM64 `venv-opt-dual` 复跑核心执行与新增隔离/并发回归（`test_p10_02_lightweight_arm64.py`、`test_unified_cloud_strategy_s04.py`、`test_alpaca_native_v69.py`、`test_demo_adaptive_grid_v2.py`、`test_alpaca_deployment.py`）：**51 passed, 0 failed**（60.27 秒）。
   - 在本地 x64 复跑上述全部文件及原研究隔离契约测试 `tests/research/test_p8_perf01_optimizations.py::test_s01_one_shot_worker_isolates_module_globals_and_blocks_escape`：**52 passed, 0 failed**（114.43 秒）。
   - ARM64 最小环境实测并根治了两项 Linux 专属隐性缺陷：
     1. `DualMAStrategy` 经 `dual_ma.py -> backtest/execution.py -> portfolio/__init__.py -> optimizer.py` 隐式依赖 `scipy`（已通过 `portfolio/__init__.py` 延迟导出与 `dual_ma.py` 回测依赖下沉修复）。
     2. Linux 隔离策略沙箱下 `V69AlpacaStrategy` 首次本地化 `America/New_York` 访问 `/usr/share/zoneinfo` 被 `addaudithook` 拦截（已在 `worker.py` 沙箱前预热 `UTC`/`America/New_York` 及 `calendar_timezone` 修复，不扩大文件白名单）。

### 3.2 AMD x64 与 ARM64（及优化前后）四向金融结果一致性对照

使用固定行情、固定订单、固定资金费及固定 252 日 8 资产矩阵，在 **AMD x64 基线**、**AMD x64 修复后最终版**、**ARM64 基线**、**ARM64 修复后最终版** 四个组合下运行同一确定性校验脚本（证据见 `evidence/parity_*.json`）：

| 验证场景 | 核心检验字段 | 一致性标准 | 实测结果（4 向对比） |
| --- | --- | --- | --- |
| Binance USD-M 网格 + 资金费 + SQLite 重载 | 网格价位、提交订单（`cid/side/qty/limit/status`）、最终 `USDT` 现金 `4999.87654322`、持仓、资金费总额 `-0.12345678` | Decimal 字段逐位精确一致（容差 `0`） | **4 向 100% 精确一致** |
| V2 隔离策略包多轮决策与状态演进 | `package_id`、`code_sha256`、3 轮目标权重（`0.5`, `0.5`, `0`）与 `Decimal` 状态字段 | Decimal 字段与哈希逐位精确一致（容差 `0`） | **4 向 100% 精确一致** |
| Alpaca Native V69 8 资产 252 日组合决策 | 全部 8 资产浮点目标权重、4 笔调仓单（`JPM`, `GE`, `TQQQ`, `QQQ`）`adjusted_quantity`、周状态 | 预先定义浮点容差 `atol=1e-12, rtol=1e-12`（用于容忍不同 CPU 架构 BLAS/libm 末位舍入） | **实测最大绝对偏差 `0.0`**（4 向逐位一致） |

---

### 3.3 PR #268 P2 最小 Binance 环境邮箱验收

审查命中的依赖链是 Execute 的邮箱解析进入 `tradfi_schedule`，随即加载 pandas 和交易日历。现在 `PortfolioTask`、`TaskMailbox`、传统组合接收和派发、动态组合接受和过期恢复统一使用标准库 UTC 时间工具。它以 UTC datetime 和纳秒余数排序，继续写入原有 `+00:00` 格式及 0、6 或 9 位小数。Observer 继续用原来的 pandas 交易日历挑选自然周首个交易日。

沿 Execute 的策略恢复路径发现，V2 自适应网格也会间接进入 pandas / numpy。研究端数据处理现在按需导入这些包，恢复和出决策时用标准库计算样本波动率。实验记录器按需加载，避免从实验包入口引入研究数据构建器。

复现命令从仓库根目录执行。先建两个全新的 venv，把 `requirements/execution-binance.txt` 和 `requirements/execution-tradfi.txt` 分别装进 Execute 和 Observer，并设置 `PYTHONPATH=src`。用 Execute 的解释器运行 `python -m tools.ci.binance_mailbox_acceptance <observer-python>`。脚本通过真实 Observer 调度和派发函数写入共享 SQLite，Execute 通过正式 Demo HTTP 客户端对接 loopback 交易所，使用隔离的临时账本和合成凭据。

| 场景 | Windows x64 / CPython 3.13.9 实测 |
| --- | --- |
| Observer 自然周任务和 QQQ/SPY 验收任务 | 两批各写入一笔 QQQUSDT 和一笔 SPYUSDT，使用原订单 ID 恢复并在到期后清理。一条仍在排队的验收任务过期后直接拒绝。 |
| 动态 BTC 任务 | 一笔在途单重启后按原 ID 对账并撤销。另一笔按 `0.04 @ 150` 成交，邮箱写入 `COMPLETED`。 |
| 时间戳与 SQLite | 读取由旧 pandas 序列化生成的带 1 ns 有效期任务，重投命中原身份。关闭并重开执行账本和邮箱后，已完成任务及终态回执保持幂等。 |
| HTTP 与最小安装 | 6 次订单 POST、5 次原订单 DELETE。Execute venv 只有锁文件列出的运行期包，`pip check` 通过，业务运行期间无 pandas / numpy 导入尝试。 |
| 其他延迟导入 | 资金费网格和波动率网格均在纯执行环境中恢复并处理首条观察。 |

定向回归在 Windows x64 上两批分别通过 70 和 44 例。前者覆盖 Observer 调度、组合消费者、动态过期、V2 自适应网格实盘形态的离线测试，以及 one-shot、双层并发和管道清理。后者覆盖新增邮箱纳秒边界、实例宿主、QQQ/SPY 持久化、P10-11 外部策略和统一策略部署。

[PR #268 检查](https://github.com/Wind-Maker1001/FactorLab/pull/268/checks) 的 `Lightweight Binance mailbox` 在原生 Linux x64 和 ARM64 上各自创建两套新环境，运行同一个脚本。随后 `Lightweight Binance mailbox parity` 对照两个证据文件中的回执、原订单 ID、数量、价格和状态。

## 四、已实施的轻量化优化与 PR #268 Codex Review 修复说明

所有优化均严格保持现有 OS 进程隔离、审计沙箱、单账户所有权、SQLite 账本持久性与金融精度，未引入独立常驻调度平台或重量级框架：

1. **切断非必要重型导入链**（`strategies/__init__.py`, `backtest/__init__.py`, `portfolio/__init__.py`, `data/__init__.py`, `data/hub/__init__.py`, `integrations/binance/__init__.py`, `integrations/alpaca/market_data/__init__.py`, `backtest/fees.py`, `strategies/grid.py`, `strategies/grid_execution.py`, `strategies/dual_ma.py`, `trading/strategy.py`, `data/evidence.py`, `integrations/binance/client.py`, `integrations/binance/market.py`）：
   - 包入口全面改为 PEP 562 `__getattr__` 延迟导出，对外公共导入 100% 兼容。
   - `grid.py`、`grid_execution.py`、`backtest/fees.py` 改用纯标准库 ISO 时间解析替代仅为解析日期引入的顶层 `pandas`。
2. **PR #268 Codex Review P1 修复 — 恢复严格逐请求 one-shot 隔离 Worker**（`strategies/worker.py`, `strategies/loader.py`）：
   - **初版问题**：初版引入常驻 `--serve` Worker 进程虽将轻量决策降至 ~11 ms，但复用 Python 解释器与模块会导致模块全局变量、类级可变状态、相对导入子模块及解释器全局状态（如 `decimal.getcontext()`）跨请求泄漏，破坏原 `test_s01_one_shot_worker_isolates_module_globals_and_blocks_escape` 逐请求隔离合同。
   - **最终修复**：彻底移除常驻 `--serve` 模式，恢复**轻量化后的逐请求 one-shot 隔离 Worker**（每次请求独立 OS 子进程 `-I -B`、空环境变量、`close_fds=True`、`sys.addaudithook`、4 MiB 请求/响应上限，执行完毕立即退出并释放全部内存）。
   - **P10-11 兼容性**：`worker.py` 与 `loader.py` 原生识别 `runtime_profile='lightweight'`、`model` 工件声明与 `calendar_timezone`，轻量包跳过重型预导入并仅预热 stdlib `ZoneInfo`，不重复实现 P10-11 策略协议。
   - **保留的独立加速**：`runtime_identity()` 保留 `(st_mtime_ns, st_size)` 变更校验缓存；纯 Python/Decimal 策略零 `numpy/pandas/scipy/pyarrow` 加载，单次冷启动从 574 ms 降至 **25.6 ms**（-95.5%）。
3. **PR #268 Codex Review P2 修复 — 原子容量预留、双层并发上限与管道清理无死锁**（`strategies/loader.py`）：
   - **初版问题**：原 `_register_pool_worker()` 与 `call()` 间存在无锁竞争窗口，被驱逐或关闭的 Worker 可能在未持有容量时继续启动子进程，导致实际存活子进程突破上限；且未区分单宿主上限与同机双市场 VM 总上限。
   - **二轮审查问题（comment `4717211384`）**：`_terminate_and_reap()` 曾在 `terminate()` 子进程前调用 `proc.stdin.close()`，当请求大于系统管道容量且子进程未读取 stdin 时，`_write_stdin` 线程阻塞在 `write()` 并持有缓冲区锁，导致 `close()` 等待同一把锁而阻塞超时清理。
   - **最终修复**：实现 `_WorkerCapacityPool` 原子容量状态机与无死锁清理顺序：
     - 容量在创建临时目录与 `subprocess.Popen` **之前**原子预留；进入 `Popen` 前在 `slot.lock` 下再次校验 `_closed / cancelled / shutting_down`，确保已关闭策略绝不拉起孤儿进程。
     - `_terminate_and_reap()` 与 `_communicate_bounded()` 严格遵循**先 `terminate()` → 必要时升级 `kill()` → 确认 `proc.poll() is not None` 退出 → join 全部读写线程（`stdin`/`stdout`/`stderr`）→ 幂等关闭管道 → 释放 VM 文件锁与宿主容量槽位**的顺序，杜绝缓冲锁死锁并保证重复清理、正常退出、主动 `close()`、超时与 `atexit` 全路径幂等。
     - 显式区分**单宿主进程上限** `FACTORLAB_STRATEGY_WORKER_POOL_MAX`（默认 **`1`**，Binance + Alpaca 同机天然 ≤ 2 并发且保持各自 systemd 隔离）与**跨进程 VM 总上限** `FACTORLAB_STRATEGY_WORKER_VM_MAX` + `FACTORLAB_STRATEGY_WORKER_LOCK_DIR`（基于内核 `fcntl.flock` / `msvcrt.locking` 槽位锁，进程崩溃由 OS 自动回收）。
     - **三平台复验与正常决策延迟抽查**（仅修改异常清理路径，保留既有容量阶梯与四向金融一致性证据，执行必要正常决策抽查）：
       - **Windows x64**（`AMD64`, Python 3.13.9）：`test_p10_02_lightweight_arm64.py` + `test_s01_one_shot_worker_isolates_module_globals_and_blocks_escape` **26 passed**（31.99s）；正常决策 10 次抽查 p50 **`244.28 ms`**（`218.18–262.96 ms`）。
       - **Linux x64**（WSL2 原生 ext4, Python 3.13.18）：全部非 pandas 执行与隔离/并发/管道超时/SIGKILL/close 用例 **22 passed**（9.69s）；正常决策 10 次抽查 p50 **`65.30 ms`**（`65.08–115.84 ms`）；完整套件同步由 GitHub Actions `ubuntu-latest` CI 验证。
       - **ARM64 Linux**（`factorlab-arm-01`, `venv-opt-dual`, Python 3.13.18）：`test_p10_02_lightweight_arm64.py` + `test_unified_cloud_strategy_s04.py` **38 passed**（40.66s）；正常决策 10 次抽查 p50 **`216.62 ms`**（`216.48–216.69 ms`）。
4. **热点查询、缓存与流存储收敛**（`strategies/decision_runtime.py`, `integrations/alpaca/market_data/cache.py`, `integrations/alpaca/cloud_runtime.py`）：
   - `DecisionJournal._count()` / `_latest()` 改为 `events.id` 主键前缀范围索引查询。
   - `MarketCache` 增加有界 LRU 内存缓存（默认 32 条），每次 `load()` 仍完整校验磁盘 `bars.parquet` SHA-256（零安全妥协）。
   - `MarketWorker` 的行情流缓存库 `market-stream.sqlite3`（非账户账本）启用 WAL 与 7 天滚动窗口清理。
5. **依赖分层与跨架构安装器适配**（`pyproject.toml`, `requirements/*`, `tools/deploy_*.py`, `tools/install_*.sh`, `tools/install_demo_strategy_runtime.py`）。

---

## 五、优化前后与审查修复前后配对实测基准（日本 ARM64 `Standard_B2pts_v2`，2 vCPU / 944 MiB RAM）

> **证据分层说明**：
> - **基线（优化前）**：[bench_arm64_baseline.json](evidence/bench_arm64_baseline.json)
> - **初版常驻池（PR #268 审查前，存在 P1 跨请求模块状态泄漏风险，已撤回，仅保留作对照）**：[bench_arm64_optimized.json](evidence/bench_arm64_optimized.json)、[soak_arm64.json](evidence/soak_arm64.json)
> - **最终修复版（轻量化 one-shot 严格隔离 + 宿主/VM 双层有界并发池，当前 HEAD 实测）**：[bench_arm64_post_review.json](evidence/bench_arm64_post_review.json)

### 5.1 冷启动及依赖导入峰值（基线 vs 最终修复版）

| 执行入口模块 | 基线 Wall (ms) | 最终版 Wall (ms) | 基线 RSS / PSS (MiB) | 最终版 RSS / PSS (MiB) | 重型导入变化 |
| --- | ---: | ---: | ---: | ---: | --- |
| `factorlab.strategies.worker` | 574.3 | **25.6** (-95.5%) | 142.8 / 135.7 | **19.5 / 13.4** (-86.3% / -90.1%) | `numpy,pandas,scipy,pyarrow` → `[]` |
| `factorlab.strategies.loader` | 589.5 | **40.7** (-93.1%) | 143.6 / 136.4 | **23.0 / 16.8** (-84.0% / -87.7%) | `numpy,pandas,scipy,pyarrow` → `[]` |
| `factorlab.strategies.decision_runtime` | 633.8 | **75.5** (-88.1%) | 145.4 / 138.2 | **26.1 / 19.9** (-82.0% / -85.6%) | `numpy,pandas,scipy,pyarrow` → `[]` |
| `demo_grid_supervisor`（Binance Execute） | 641.7 | **111.3** (-82.7%) | 147.4 / 140.2 | **29.6 / 23.3** (-79.9% / -83.4%) | `numpy,pandas,scipy,pyarrow` → `[]` |
| `alpaca.cloud_runtime` | 641.6 | **405.9** (-36.7%) | 150.8 / 143.6 | **115.5 / 108.7** (-23.4% / -24.3%) | 清除 `scipy`（仅保留所需 `numpy,pandas,pyarrow`） |

### 5.2 历史行情预热峰值与单/双市场常驻内存（与 Worker 改动独立，均保持有效）

- **历史行情预热（8 资产 × 252 日 + 5 日 1Min Parquet Cache）**：峰值 HWM `168.76 → 132.23 MiB`（-21.6%）；3× 重复读取 `188.30 → 16.17 ms`（-91.4%，11.6× 加速，仍校验完整 SHA-256）。
- **断线补齐 / 交易高峰 / SQLite 备份与日志轮换**：进程峰值 HWM `150.4 → 33.9 MiB`（-77.5%）；SQLite 一致性备份 `62.2 → 39.7 ms`（-36.2%）；120 根 Bar 补齐恢复 `3,004.4 ms`（完整保留 `synchronous=FULL` 账本语义）。
- **双市场 + TradFi Observer 3 宿主常驻（无常驻 Worker 占用）**：空载合计 PSS 由基线 `285.8 MiB` 降至 **`181.6 – 193.5 MiB`**（-32% ~ -36%），进程 Swap **`0.0 MiB`**。

### 5.3 多策略隔离工作进程扩容阶梯（3 / 6 / 12 / 24 实例，1 分钟周期 — 三阶段对照）

> 注：最终修复版采用**轻量化 one-shot 隔离 Worker + 有界并发池**（`POOL_MAX=1` 与 `POOL_MAX=2` 均完整实测，数据一致；表中列 `POOL_MAX=1` 最终实测，详见 [bench_arm64_post_review.json](evidence/bench_arm64_post_review.json)）。与已撤回的初版常驻池相比，最终版**牺牲了约 168 ms 单决策延迟以换取 100% OS 级逐请求状态隔离**，同时将 12–24 实例峰值内存再砍掉 **~163 MiB PSS**（空载 Worker 内存归零）！

| 实例规模 | 版本阶段 | 初始化 (ms) | 单决策 p50 / p95 / p99 (ms) | 单 Tick 汇总 p50 (ms) | 每分钟总 CPU (s) | B2pts 持续基线占比（24s/m） | 峰值合计 PSS (MiB) | 系统最低可用 RAM (MiB) | 峰值实时 Worker 数 / Swap / OOM |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| **3** | 基线（优化前） | 2,550.4 | 840.1 / 866.1 / 866.1 | 2,531.2 | 3.91 | 16.3% | 141.7 | 525.7 | 1 / 0 MiB / 0 |
| **3** | 初版常驻池（已撤回） | 387.0 | 12.6 / 17.0 / 17.0 | 38.1 | 0.11 | 0.46% | 70.8 | 579.7 | 3 常驻 / 0 MiB / 0 |
| **3** | **最终修复版（one-shot 有界池）** | **567.3** | **182.1 / 187.9 / 187.9** | **547.6** | **0.521** | **2.17%** | **41.6** | **599.5** | **1 / 0 MiB / 0** |
| **6** | 基线（优化前） | 4,846.2 | 804.9 / 815.6 / 815.6 | 4,825.1 | 7.53 | 31.4% | 142.4 | 512.2 | 1 / 0 MiB / 0 |
| **6** | 初版常驻池（已撤回） | 758.6 | 11.6 / 14.5 / 14.5 | 73.7 | 0.22 | 0.92% | 116.4 | 544.1 | 6 常驻 / 0 MiB / 0 |
| **6** | **最终修复版（one-shot 有界池）** | **1,153.3** | **179.6 / 194.5 / 194.5** | **1,080.5** | **1.086** | **4.53%** | **42.3** | **608.6** | **1 / 0 MiB / 0** |
| **12** | 基线（优化前） | 9,798.2 | 806.9 / 839.2 / 842.9 | 9,697.1 | 15.16 | 63.2%（高风险） | 144.4 | 504.2 | 1 / 0 MiB / 0 |
| **12** | 初版常驻池（已撤回） | 1,513.1 | 11.1 / 14.0 / 15.3 | 139.6 | 0.44 | 1.84% | 206.6 | 455.5 | 12 常驻 / 0 MiB / 0 |
| **12** | **最终修复版（one-shot 有界池）** | **2,254.7** | **179.5 / 189.3 / 190.6** | **2,173.7** | **2.108** | **8.78%** | **43.8** | **605.3** | **1 / 0 MiB / 0** |
| **24** | 基线（优化前） | 19,429.5 | 808.5 / 823.9 / 843.2 | 19,426.0 | 30.18 | **125.7%（耗尽积分）** | 147.5 | 498.1 | 1 / 0 MiB / 0 |
| **24** | 初版常驻池（已撤回） | 3,076.4 | 125.2 / 129.3 / 135.1 | 3,028.2 | 3.50 | 14.6% | 209.1 | 452.6 | 12 常驻 / 0 MiB / 0 |
| **24** | **最终修复版（one-shot 有界池）** | **4,469.8** | **180.6 / 187.8 / 204.8** | **4,382.1** | **4.266** | **17.77%** | **46.9** | **605.4** | **1 / 0 MiB / 0** |

*说明*：Azure `Standard_B2pts_v2` / `Standard_B2ats_v2`（2 vCPU）的持续 CPU 基础基线为每核 20% = **24 秒 CPU 时间 / 分钟**。最终修复版在严格逐请求 OS 进程隔离下，将 12 实例每分钟 CPU 从基线的 `15.16s`（63.2%）压降至 **`2.11s`（8.78%）**，24 实例从 `30.18s`（125.7%，必耗尽积分）压降至 **`4.27s`（17.77%）**，且峰值内存始终维持在 **~42–47 MiB PSS**（单宿主），较初版常驻池大幅释放 ~160 MiB 宝贵物理内存。

### 5.4 双市场同机 + TradFi Observer + 轻量与数值策略混合负载实测（最终修复版）

在 `factorlab-arm-01` 上同机并发启动 **3 个真实宿主 OS 进程**（Binance Execute + Alpaca Runtime + TradFi Observer），共运行 **12 个隔离策略**（Binance 6 个纯 Decimal 轻量策略 + Alpaca 4 个纯 Decimal 轻量策略 + Alpaca 2 个 `numpy/pandas` 数值策略：`DualMAAdapter` + `NumericMomentumStrategy` 含 `America/New_York` 时区转换），配置每宿主 `FACTORLAB_STRATEGY_WORKER_POOL_MAX=2` 且共享 `FACTORLAB_STRATEGY_WORKER_LOCK_DIR` + **`FACTORLAB_STRATEGY_WORKER_VM_MAX=2`**，连续运行 4 轮并发周期（证据见 [bench_arm64_post_review.json](evidence/bench_arm64_post_review.json)）：

| 观测指标 | 最终修复版 ARM64 实测值 | 结论 |
| --- | --- | --- |
| 最大实时总 OS 进程数（宿主 + Worker） | **`5`**（3 宿主 + 最多 2 实时 Worker） | 严格受控 |
| 全机最大同时存活 Worker 子进程数 | **`2`**（双宿主各 2 线程并发争抢下严格 ≤ `VM_MAX=2`） | **VM 总上限生效验证通过** |
| 3 宿主空载常驻合计 PSS | **`181.61 MiB`** | 空载零 Worker 内存驻留 |
| 并发高峰合计 PSS / RSS | **`286.63 MiB` / `471.75 MiB`** | 较初版 12 常驻池（378.4 MiB）再降 ~92 MiB |
| 系统最低可用物理内存 `MemAvailable` | **`426.13 MiB`**（总 RAM `944.15 MiB`，保留 **45.1%** 余量） | 充裕 |
| 进程 Swap / OOM 次数 | **`0.0 MiB` / `0`** | 零 Swap、零 OOM |
| 轻量策略单决策延迟 p50 / p95 / max | **`180.17 ms` / `1,332.67 ms` / `2,751.01 ms`** | 尾延迟为等待数值 Worker 让出 VM 槽位 |
| 数值策略（`numpy/pandas`）单决策延迟 p50 / p95 / max | **`727.67 ms` / `1,136.52 ms` / `1,136.52 ms`** | 每次独立沙箱执行完毕即释放全部数值库内存 |
| 单轮周期完成耗时（Binance / Alpaca）p50 / max | Binance **`1.35s / 2.76s`**；Alpaca **`1.18s / 2.19s`** | 远低于 60s（1 分钟）策略周期 |
| 全机 12 混合策略每分钟总 CPU | **`2.63 s / min`**（占 B2pts 24s/min 持续基线 **`10.96%`**） | 满足长期无积分耗尽运行要求 |

---

## 六、推荐的长期同机运行容量（2 vCPU / 1 GiB RAM，Ubuntu 24.04 x64 或 ARM64）

在扣除操作系统内核 + `systemd` + Azure 监控代理（约 250–320 MiB）与双市场 + TradFi Observer 3 宿主基础常驻内存（约 **182–194 MiB PSS**）后，主机保留约 **430–500 MiB** 物理内存用于瞬时 one-shot 策略 Worker、页缓存与每日 SQLite 备份。得益于轻量化 one-shot 架构（空载 Worker 占 `0 MiB`）与双层并发封顶（全机同时至多 `2` 个 Worker），容量建议如下：

| 策略工作负载组合（1 分钟最短周期） | 推荐长期稳定运行规模（双市场同机合计） | 并发配置建议 | 预估峰值总 PSS | 预估 1m CPU 基线占比 |
| --- | --- | --- | --- | --- |
| 纯 Python / Decimal / P10-11 `lightweight` 策略 | **6 – 12 个实例**（充裕）；最多可支撑 **16 – 18 个实例** | 默认每宿主 `POOL_MAX=1`（全机天然 ≤ 2）；或共享锁目录 `POOL_MAX=2, VM_MAX=2` | ~225 – 245 MiB | ~4.5% – 13%（< 24s/min 基线） |
| 混合负载（轻量 + `numpy/pandas` 数值策略，如 `DualMAAdapter` / `V69` / `AdaptiveGridV2`） | **2 – 4 个数值实例**（含日频/周频 V69）+ **6 – 8 个轻量实例**（合计 8 – 12 实例） | 同上（**严禁全机并发 Worker > 2**，防止 3+ 个数值 Worker 同时启动触发 Swap） | ~285 – 360 MiB | ~11% – 22% |
| 超过 20 个高频 1 分钟实例（尤其含多数值策略） | **不推荐在 1 GiB / 2 vCPU B2 突发实例长期满载** | — | 内存可控但 CPU 逼近或超过 20%/vCPU 基线 | 长期可能耗尽 Azure CPU 积分导致降频 |

---

## 七、已知依赖兼容性、尚未验证边界与风险说明

1. **已验证范围 vs 尚未验证的长时边界**：
   - 本次完成了 ARM64 修复后 3/6/12/24 实例配对基准、双市场 12 策略轻/数值混合负载实测、四向金融结果一致性验证以及初版 183 秒连续观测；**尚未在真实联网行情下开展 24 小时 / 7 天长期实测**（含真实交易所断线重连、WebSocket 长连接抖动、跨日历/夏令时真实切换、Azure Monitor 代理多日内存波动及每日定时备份叠加）。
   - **Azure CPU 积分**：本次通过精确测量进程与子进程 CPU 时间（`RUSAGE_SELF + RUSAGE_CHILDREN`）并与 B2pts/B2ats v2 标称 20%/vCPU 基线（24s/min）比对评估可持续性，未通过 Azure Monitor API 读取云端实时积分余额表。
2. **已知依赖兼容性与 P10-11 协同说明**：
   - `AdaptiveGridV2` 内部经 `grid_restore.py` 调用研究模块 `factorlab.experiments.grid_funding_adaptive`（顶层依赖 `numpy` / `pandas`），因此在部署 `AdaptiveGridV2` 的主机上需使用包含 `strategy-numeric` 或 `execution-dual` 的环境（纯 22 MiB `execution-binance` 仅适用于不依赖 `numpy/pandas` 的原生 `GridExecution` 与纯 Decimal / P10-11 `lightweight` 策略）。
   - 与 **P10-11（PR #264 `59dbcad`）语义集成与联合验证**：
     - 在合并 `origin/main` 时，按接口语义完整集成 3 个冲突文件（`strategies/__init__.py`、`strategies/worker.py`、`strategies/loader.py`）并核验自动合并的 `strategies/decision_runtime.py` 与 `integrations/alpaca/strategy_instances.py`：同时保留 P10-11 的外部策略协议、`strategy_event()` 参考行情裁剪、冻结线性模型工件（`LinearModel` / `model_effective_at` / `require_event_time` / `model_identity`）、`StrategyPackageError` 与 `read_package()` 无 TOCTOU 读取，以及 P10-02 的最小依赖、延迟导入、one-shot 逐请求隔离、宿主/整机双层并发上限和 `9e6cfc8` 先终止再关闭管道的清理顺序。
     - **联合回归**：本地 x64 运行 `test_external_strategy.py`、`test_p10_02_lightweight_arm64.py`、`test_alpaca_native_v69.py`、`test_demo_adaptive_grid_v2.py`、`test_demo_instance_host.py`、`test_unified_cloud_strategy_s04.py`、`test_s04_package_failures.py` 及 `test_s01_one_shot_worker_isolates_module_globals_and_blocks_escape` 共 **77 passed, 0 failed**（143.82s）；ARM64 最小无 `scipy` 环境（`venv-opt-dual`）运行上述执行套件 + `test_alpaca_deployment.py` 共 **78 passed, 0 failed**（75.88s）。
     - **P10-11 外部策略三类示例跨架构验证**（`examples/external_strategy/run.py a/b/c`）：外部轻量策略（`a`，`SPY: 0.45, IWM: 0.45`）、参考 ETF 策略（`b`，`SPY: 0.9`）、冻结线性模型（`c`，`linear-1.json`，`NVDAUSDT: 0.5`）在 x64 与 ARM64 产生的 `package_id`、`model.sha256`、`targets` 与 `state` **逐位完全一致**；ARM64 单次决策耗时分别为 **`116.73 ms` / `116.44 ms` / `116.57 ms`**，子进程峰值内存仅 **`24.3 MiB`**（`24,876–24,884 KiB`）。
     - **合并后 ARM64 双市场混合负载抽查**（3 宿主 + 12 策略 = 10 轻量 + 2 数值，共享 `VM_MAX=2`，3 轮并发）：最大总进程数 `5`（实时 Worker ≤ `2`），空载 PSS **`175.98 MiB`**，峰值 PSS **`302.95 MiB`**，最低 `MemAvailable` **`447.33 MiB`**，进程 Swap **`0.0 MiB`**，OOM **`0`**，轻量决策 p50 **`175.52 ms`**，数值决策 p50 **`722.09 ms`**，每分钟总 CPU **`2.65s`**（B2pts 持续基线占比 **`11.02%`**）。
   - 研究期/CI 专用锁测试 `tools/ci/lock_acceptance.py` 及 `tests/research/test_p8_perf01_optimizations.py` 顶层导入了研究依赖（`PyYAML`、`scipy`、`scikit-learn`），属于研究测试套件，不属于执行端最小运行期依赖；执行端隔离/并发回归与文件锁已在 ARM64 最小环境与本地 x64 完整验证。
   - Python 版本仍严格保持 `>=3.13,<3.15`；Ubuntu 24.04 系统自带 Python 3.12 不可直接运行，须按安装说明部署原生 `cpython-3.13-linux-<arch>-gnu` 至 `/opt/factorlab/python`。
3. **生产主机与审批边界确认**：
   - 本任务未对 `factorlab-demo-01`（马来西亚生产模拟机）执行任何写入、部署、重启或 VM 升配操作；亦未在 ARM64 主机配置任何生产账户凭证或启动第二写入者；未新增模拟盘审批门禁。

---

## 八、验证与交付清单

- [x] 最小执行依赖与 `pyproject.toml` 分层（保留研究环境 `.[research]` 与 `requirements/py314-core.txt` 完全兼容）
- [x] 跨架构（x86_64 / ARM64）安装与部署脚本适配及安装文档：[docs/execution-lightweight-install.md](../../execution-lightweight-install.md)
- [x] PR #268 Codex Review P1 修复（恢复轻量化逐请求 one-shot 隔离 Worker，`test_s01_one_shot_worker_isolates_module_globals_and_blocks_escape` 原封不动通过）
- [x] PR #268 Codex Review P2 修复（原子容量预留、先终止/升级 `kill` 确认退出后再 join IO 线程并幂等关闭管道、单宿主 `POOL_MAX` 与跨进程 `VM_MAX` 双层上限；覆盖 384 KiB 超大输入阻塞 + 忽略 `SIGTERM` + 主动 `close()` 真实子进程回归，Windows x64 / Linux x64 / ARM64 三平台全绿）
- [x] ARM64 原生安装与功能运行证据（4 个分层 venv `pip check` 全绿，[lock_arm64.json](evidence/lock_arm64.json) 跨进程文件锁验证全绿，ARM64 修复后执行回归 51 passed / 本地 x64 含 S01 原隔离测试 52 passed）
- [x] 优化前基线、初版常驻池（已撤回对照）与最终修复版 3/6/12/24 实例及双市场轻/数值混合负载实测证据：[bench_arm64_baseline.json](evidence/bench_arm64_baseline.json)、[bench_arm64_optimized.json](evidence/bench_arm64_optimized.json)、[bench_arm64_post_review.json](evidence/bench_arm64_post_review.json)
- [x] 四向（AMD x64 / ARM64 × 优化前基线 / 最终修复版）金融结果一致性证据：[parity_x64_baseline.json](evidence/parity_x64_baseline.json)、[parity_x64_optimized.json](evidence/parity_x64_optimized.json)、[parity_arm64_baseline.json](evidence/parity_arm64_baseline.json)、[parity_arm64_optimized.json](evidence/parity_arm64_optimized.json)
- [x] 回归单测：[tests/integrations/test_p10_02_lightweight_arm64.py](../../../tests/integrations/test_p10_02_lightweight_arm64.py)（覆盖导入链、锁文件、P1 全维度状态隔离、P10-11 `lightweight` 兼容、P2 真实存活子进程观测、`POOL_MAX=1/2`、登记后执行前关闭竞争、超时/异常/退出清理、跨进程 `VM_MAX` 上限与缓存防篡改）
