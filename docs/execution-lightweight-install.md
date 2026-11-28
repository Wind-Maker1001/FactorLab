# FactorLab 执行端轻量化与跨架构（x86_64 / ARM64）安装说明

本说明面向生产及预发低资源 Linux 主机（Azure Standard B2ats v2 `x86_64` 与 Standard B2pts v2 `aarch64`，2 vCPU / 1 GiB RAM，Ubuntu 24.04 LTS），不适用于离线研究、回测、机器学习训练或大规模蒙特卡洛计算。

## 1. 依赖分层契约

`pyproject.toml` 与 `requirements/` 严格区分五类依赖，杜绝研究依赖及 CUDA/NCCL 二进制被引入执行主机：

| 层级 | 安装入口 / 锁文件 | 核心内容 | 排除项 | ARM64 实测 venv 体积 |
| --- | --- | --- | --- | --- |
| 公共交易核心 | `pip install .` | Python 3.13 标准库 + `tzdata` | 全部科学计算/ML/绘图/GPU | ~18 MiB |
| Binance USD-M / Equity 执行端 | `requirements/execution-binance.txt` / `.[execution-binance]` | `tzdata`, `websockets` | `numpy`, `pandas`, `pyarrow`, `scipy`, ML, CUDA/NCCL | **22 MiB** |
| TradFi 观察与日历调度 | `requirements/execution-tradfi.txt` / `.[execution-tradfi]` | `numpy`, `pandas`, `exchange-calendars`, `tzdata` | `pyarrow`, `scipy`, ML, 绘图, CUDA/NCCL | **122 MiB** |
| Alpaca Paper 执行端 | `requirements/alpaca-paper-linux-py313.lock.txt` / `.[execution-alpaca]` | `numpy`, `pandas`, `pyarrow`, `websockets`, `tzdata` | `scipy`, `scikit-learn`, `lightgbm`, `xgboost`, `matplotlib`, `nvidia-nccl-cu12` | **261 MiB** |
| 同机双市场 + 公共组件统一环境 | `requirements/execution-dual-linux-py313.lock.txt` / `.[execution-dual]` | Binance + Alpaca + TradFi 全部最小运行期依赖 | 全部研究/ML/绘图/GPU/NCCL | **262 MiB** |
| 离线研究环境（兼容保留） | `.[research]` 或 `requirements/py314-core.txt` | 完整 4 模型研究、回测、统计与绘图栈 | — | 仅研究机使用 |
| GPU 加速（仅研究机） | `.[monte-carlo-cuda]` | `cupy-cuda13x[ctk]` | 禁止安装于执行主机 | 仅 GPU 研究机 |

## 2. 原生 Python 3.13 准备（禁止 x86 仿真）

Ubuntu 24.04 默认系统 Python 为 3.12，FactorLab 要求 `>=3.13,<3.15`。在 x86_64 与 ARM64 (`aarch64`) 主机上均通过原生 `uv` 安装对应架构的独立 CPython 3.13 至全局只读路径 `/opt/factorlab/python`（确保 `factorlab-demo` / `factorlab-alpaca` 系统用户可读）：

```bash
# 确认主机架构（x86_64 或 aarch64），确认无 binfmt x86 仿真
uname -m

# 安装对应架构原生 uv 并安装原生 CPython 3.13 到 /opt/factorlab/python
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo mkdir -p /opt/factorlab/python
sudo UV_PYTHON_INSTALL_DIR=/opt/factorlab/python "$HOME/.local/bin/uv" python install 3.13

ARCH=$(uname -m)
PY_BIN=$(find /opt/factorlab/python -maxdepth 3 -path "*cpython-3.13*-linux-${ARCH}-gnu/bin/python3.13" | head -n 1)
PY_ROOT=$(dirname "$(dirname "$PY_BIN")")
sudo ln -sfn "$PY_ROOT" "/opt/factorlab/python/cpython-3.13-linux-${ARCH}-gnu"

# 验证架构为原生 ELF 且 HTTPS TLS 校验正常
file -L "/opt/factorlab/python/cpython-3.13-linux-${ARCH}-gnu/bin/python3"
"/opt/factorlab/python/cpython-3.13-linux-${ARCH}-gnu/bin/python3" -c \
  "import platform, ssl, urllib.request; print(platform.machine(), ssl.OPENSSL_VERSION); print(urllib.request.urlopen('https://pypi.org/simple/', timeout=10).status)"
```

## 3. 服务最小安装与部署

全部部署脚本已移除 `x86_64` 硬编码，默认 `--python auto` 自动解析 `/opt/factorlab/python/cpython-3.13-linux-$(uname -m)-gnu/bin/python3`：

- **Alpaca Paper 运行时**：
  ```bash
  python tools/deploy_alpaca_paper_runtime.py \
    --repo . --commit <full-40-sha> --host <host> --user azureuser --key <key.pem>
  ```
  安装器仅安装 `requirements/alpaca-paper-linux-py313.lock.txt`（`--only-binary=:all:`）+ `--no-deps` 源码并执行 `pip check`。
- **Binance Demo Execute 运行时**：
  ```bash
  python tools/deploy_demo_strategy.py \
    --repo . --commit <full-40-sha> --host <host> --user azureuser --key <key.pem>
  ```
  如需重建轻量 Binance venv（摆脱旧 1.3 GiB 继承环境），可在目标机执行 `install_demo_strategy_runtime.py --rebuild-lightweight-venv`。
- **TradFi 观察与日历调度**：
  ```bash
  sudo sh tools/install_tradfi_observer.sh <archive.tar> <full-40-sha> auto
  ```
  安装器仅安装 `requirements/execution-tradfi.txt`（`--only-binary=:all:`）+ `--no-deps` 源码并执行 `pip check`。

## 同包运行时升级

两个执行端安装器在切换 release 前停止旧宿主，以服务身份取得原有 owner/session 锁，并自动执行同包兼容性证明。手动入口（同样必须先停止对应宿主）为：

```bash
sudo -u factorlab-alpaca /new/release/venv/bin/python -I -m factorlab.strategies.runtime_migration \
  --market alpaca --state-root /var/lib/factorlab-alpaca-paper \
  --previous-python /old/release/venv/bin/python
sudo -u factorlab-demo /new/release/venv/bin/python -I -m factorlab.strategies.runtime_migration \
  --market binance --state-root /var/lib/factorlab-demo/execute-state \
  --previous-python /old/release/venv/bin/python
```

已冻结但未提交决策的输入必须先由旧 release 排空；拒绝时仅恢复原本运行中的旧服务，原本停用的服务保持停用，完成这些输入后重新部署。已提交收据不重新计算，未确认订单、资本、checkpoint 与旧运行时绑定保持原位；代码回滚直接恢复旧 release，不恢复或覆盖数据库。

证明固定包、配置、模型、策略静态传递导入、worker/loader、经济执行入口，以及解释器、标准库和已安装依赖文件内容。宿主生命周期/备份/保留逻辑的例外仅绑定生产版本 `80d22fa23b2bedd557684dd4db9559e6180ffb9e`、基线 `47e58d9044e27dc47fc140f87b48b73531db6d3f` 与本次已审计版本的完整模块 AST 摘要（支持反向回滚），不是函数名或任意后续版本豁免；迁移模块自身也固定摘要。策略导入这些模块时仍冻结其完整内容，不使用例外。动态代码加载或不能解析的运行时导入拒绝。标准库/数值依赖变化、跨架构切换、模型/参数改变均不能通过同包 admission；使用既有新包部署流程，不删除 runtime identity 检查。`--releases` 可指定非默认策略包目录。

## 4. 运行期并发与资源边界参数

为严格保持逐请求 OS 进程隔离（防止模块全局变量、类级可变状态、包内子模块及解释器全局上下文跨请求泄漏），所有策略调用均使用**轻量化 one-shot 隔离子进程**，并通过宿主进程与整机双层有界并发控制限制峰值内存与 CPU：

- **`FACTORLAB_STRATEGY_WORKER_POOL_MAX`**（默认 **`1`**，下限 `1`）：单个宿主进程（如 `factorlab-demo-execute` 或 `factorlab-alpaca-paper`）内同时存活的策略 Worker 子进程上限。
  - 默认 `1` 时，同机运行的 Binance Execute 与 Alpaca Runtime 各自至多拉起 1 个子进程，整台 2 vCPU / 1 GiB VM 峰值合计天然不超过 **2 个并发策略子进程**，且无需放宽两个 systemd 服务账户（`factorlab-demo` / `factorlab-alpaca`）之间的 `ProtectSystem=strict` 与 `PrivateTmp=true` 隔离边界。
  - 空载时策略 Worker 常驻内存为 **0 MiB**；容量仅在旧子进程确认退出（`proc.poll() is not None`）后释放。
- **`FACTORLAB_STRATEGY_WORKER_VM_MAX`** 与 **`FACTORLAB_STRATEGY_WORKER_LOCK_DIR`**（可选整机跨进程上限）：
  - 当需要在多宿主进程间动态共享槽位（例如允许单个市场在另一市场空闲时突发至 `POOL_MAX=2`，同时严格保证整台 VM 总并发 Worker 不超过 `VM_MAX=2`）时，可为同机服务配置共享的 `FACTORLAB_STRATEGY_WORKER_LOCK_DIR` 与 `FACTORLAB_STRATEGY_WORKER_VM_MAX=2`（基于内核 `fcntl.flock` / `msvcrt.locking` 槽位锁，进程异常崩溃由内核自动回收，无需独立常驻调度服务）。
- **策略包可选依赖与 P10-11 兼容**：纯标准库/Decimal 策略包（及声明 `runtime_profile: "lightweight"` 或 `model` 工件的 P10-11 策略包）跳过重型库预导入，单次轻量决策仅 ~14 MiB 瞬时 PSS；确需数值库的策略包显式锁定 `numpy==...` / `pandas==...`，Worker 仅在检测到数值依赖时于沙箱前按需预导入并预热 `America/New_York` / `UTC` 时区表。
