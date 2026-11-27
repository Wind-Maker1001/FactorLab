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

## 4. 运行期资源调优参数

- **`FACTORLAB_STRATEGY_WORKER_POOL_MAX`**（默认 `12`）：限制单个运行期进程内同时常驻的 V2/V1 策略隔离子进程上限。超过上限时按 LRU 优雅回收最久未用的空闲隔离子进程并在下次决策时自动按需重建；设为 `0` 可退回每次决策一次性拉起子进程的兼容模式。
  - 在 2 vCPU / 1 GiB RAM 同机双市场主机上，建议保持 `8`–`12`。
- **策略包可选依赖**：纯标准库/Decimal 策略包保持 `dependencies: []`；确需数值库的策略包显式锁定 `numpy==...` / `pandas==...`，隔离 worker 仅在检测到数值依赖时按需在沙箱前预导入并预热 `America/New_York` / `UTC` 时区表，不向无数值需求的策略进程引入任何重型库。
