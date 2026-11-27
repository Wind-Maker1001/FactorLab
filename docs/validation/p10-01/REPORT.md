# P10-01：FactorLab 双市场执行端正式部署与持续模拟交易验收报告

> **NO LIVE MONEY**：本报告全部操作仅针对持续授权的官方模拟账户（Binance USD-M Demo `https://demo-fapi.binance.com` 与 Alpaca Paper `https://paper-api.alpaca.markets`），未触及任何真实资金账户，未调增风险预算，未暴露任何 API Key/Secret。

---

## 1. 部署版本与实际运行代码身份

| 项目 | 详情 |
| --- | --- |
| 基线提交（`origin/main`，已含 PR #255/#256/#257/#260/#261） | `388a8ee0f28e4b90ee7dbb20403a29c7fe085898` |
| 独立 Worktree 与发布分支 | `D:\Code\FactorLab-worktrees\p10-01-dual-market-release` / `deployment/p10-01-dual-market-release` |
| 实际部署可追溯提交 SHA | `12335ebd56fda7a2d0e709176806d96e3e7b7b67`（PR 后续审查加固提交未重复触发 Azure 重启，以保持 `T0` 长周期观测窗口连续性） |
| 目标主机 | Azure VM `factorlab-demo-01`（马来西亚区，AMD EPYC 7763 2 vCPU，887 MiB RAM + 2.0 GiB Swap，61 GiB Root Disk） |
| 升级前运行版本 | Binance Demo / Alpaca Paper：`ea9cbbdd7519e5400df097e5661b0d6c47b6c4da`；TradFi Observer：`a74fec9404e4c4ff0f699595d7ad5a6b03d1ec45-continuous1` |
| 升级后源码与运行一致性验证时间 | `2026-11-26T15:24:19Z`（完整证据见 `source-consistency.json`） |

### 1.1 逐文件 SHA-256 与运行进程加载验证

已通过独立脚本比对 Git 提交 `12335ebd56fda7a2d0e709176806d96e3e7b7b67` 下 `src/` 全部 515 个受跟踪文件与远端部署目录及实际安装包（忽略 `__pycache__`/`.pyc`/`.egg-info`）：

| 验证目标 | 对比文件数 | 不匹配数 | 多余文件数 | 结果 |
| --- | ---: | ---: | ---: | --- |
| `/opt/factorlab/releases/12335eb.../source/src` | 515 | 0 | 0 | **MATCH** |
| `/opt/factorlab-alpaca/releases/12335eb.../source/src` | 515 | 0 | 0 | **MATCH** |
| `/opt/factorlab-alpaca/releases/12335eb.../venv/lib/python3.13/site-packages/factorlab`（不含 6 个源码专用 `.gitattributes`） | 509 | 0 | 0 | **MATCH** |

实际进程与 Python 模块加载路径验证（`2026-11-26T15:36Z` 最终状态）：

- `factorlab-demo-execute.service`：PID `767167`，`/opt/factorlab/releases/12335ebd56fda7a2d0e709176806d96e3e7b7b67/venv/bin/python -m factorlab.integrations.binance_futures_trading.demo_grid_supervisor start --execute`
- `factorlab-demo-tradfi-observer.service`：PID `767202`，`/opt/factorlab/releases/12335ebd56fda7a2d0e709176806d96e3e7b7b67/venv/bin/python -m factorlab.integrations.binance_futures_trading.tradfi_cloud ...`
- `factorlab-alpaca-paper.service`：PID `767635`，`/opt/factorlab-alpaca/releases/12335ebd56fda7a2d0e709176806d96e3e7b7b67/venv/bin/python -m factorlab.integrations.alpaca.cloud_runtime run --revision 12335ebd56fda7a2d0e709176806d96e3e7b7b67`（`/opt/factorlab-alpaca/current` 符号链接指向同版本）
- 6 个核心模块（`adapter.py`、`protocol.py`、`demo_instance_host.py`、`cloud_runtime.py`、`native_v69.py`、`capital.py`）经 `inspect.getfile(...)` 实测全部位于 `12335ebd56fda7a2d0e709176806d96e3e7b7b67` 发布目录下。

---

## 2. 部署前诊断与本次最小缺陷修复

### 2.1 部署前发现的两项阻塞性线上问题

1. **根分区磁盘告急（97.89% 已用，可用仅 1.27 GiB）**：
   - 根因：`/opt/factorlab/releases` 积压 24 个历史 release（共 34 GiB）、`/opt/factorlab-alpaca/releases` 积压 9 个历史 release（共 13 GiB）、`/tmp/factorlab-alpaca-*` 残留临时部署包 2.5 GiB（原 `tools/deploy_alpaca_paper_runtime.py` 部署后未清理 `/tmp` 暂存目录）、`/root/.cache/pip` 占 2.0 GiB。
   - 处理：仅清理无任何 systemd unit、进程或 `current`/`previous-release` 引用的历史旧 release 及 `/tmp` 与 pip 缓存（完整保留上一版本 `ea9cbbdd` 与 `bbda2122` 回滚基线），释放约 36 GiB 空间，**未升级 VM，未放宽风控**。

2. **Binance Demo 历史 `UNKNOWN` 订单死锁缺陷（导致服务停滞约 19 小时及凌晨 C07 备份失败）**：
   - 现场：2026-11-25 15:29:28 UTC，旧版 `ea9cbbdd` 在 `BREAKOUT` 尝试发出减仓单 `fl-d7345f8795464a65-g-0003a-00000003`（SELL 0.04 NVDAUSDT @ 143.46, `reduce_only=true`）时进入永久 `PAUSED / ORDER_UNKNOWN`。
   - 根因定位：
     1. `FuturesAdapter.submit_order` 先向 `protocol.sqlite3` 写入 `UNKNOWN` pre-dispatch placeholder（`observed_at=1970-01-01`, `exchange_order_id=null`）。
     2. 随后调用 `FuturesClient.request('POST', '/fapi/v1/order', params)`；原 `protocol.py` 仅对 `GET` 自动刷新 45 秒 `DEMO_TIME_TTL_SECONDS` 时钟校准，在 `POST` 签名前跨越 45 秒 TTL 边界时抛出 `TimeCalibrationError`（`DataQualityError` 子类）。
     3. 该异常发生于本地签名阶段（未进入 `DemoNetworkTransport`、`write_guard`、`dispatches` 或 HTTP socket），但原 `adapter.py` 仅在 `except DemoWriteRefused:` 清理 placeholder，导致 `TimeCalibrationError` 落入 `except (..., DataQualityError, ...)` 被误标为 `UNKNOWN / OutcomeUnknown`。
   - 五重独立客观证据确认 `fl-d7345f8795464a65-g-0003a-00000003` 从未发出 HTTP POST：
     1. `runtime.sqlite3` 的 `dispatches` 与 `dispatch_retry_history` 均无该 `client_id`；
     2. 进程日志 `15:29:24 -> 15:29:28` 期间 `requests["POST"]` 维持 `6` 未递增；
     3. `protocol.sqlite3` 中 `exchange_order_id=null`、`observed_at=1970-01-01T00:00:00+00:00`、`futures_events`/`futures_receipts` 均无记录；
     4. `controller.sqlite3` 的 `confirmations` 无该订单；
     5. 官方 REST `GET /fapi/v1/order?symbol=NVDAUSDT&origClientOrderId=fl-d7345f8795464a65-g-0003a-00000003` 返回 HTTP 400 `code=-2013 ("Order does not exist.")`。

### 2.2 本次最小代码修复（提交 `12335ebd56fda7a2d0e709176806d96e3e7b7b67`）

- `src/factorlab/integrations/binance_futures_trading/protocol.py`：在已授权（`demo_network_permitted`）的 Demo 写请求签名前，自动刷新过期的时钟校准，避免 45 秒 TTL 边界导致无谓失败；未授权/只读 transport 仍直接拒绝，不发起任何网络请求。
- `src/factorlab/integrations/binance_futures_trading/adapter.py`：在 `submit_order` 与 `cancel_order` 中将请求发送阶段与回读确认阶段（`_confirm_write`）解耦；仅对数学上确定发生于 `_request_http` 之前的 `(DemoWriteRefused, SafetyBlocked, TimeCalibrationError, AccountReadExpired)` 清理本次新建的 placeholder 并原样重抛；一旦进入 HTTP 发送或回读阶段，任何异常仍严格保持 `UNKNOWN` 幂等保护，绝不盲目重发。
- `tools/deploy_alpaca_paper_runtime.py`：在 `finally` 中自动清理远端 `/tmp/factorlab-alpaca-*` 暂存目录，防止重复部署堆积磁盘空间。
- `tests/integrations/test_demo_constructor_clock.py`：新增 2 项回归测试（覆盖写前过期时钟刷新、刷新失败清理 placeholder、以及 POST 已发后回读时钟失败必须保留 `UNKNOWN` 的双向边界）。

### 2.3 本地定向回归测试实测结果（Python 3.13.9）

| 测试分组 | 实测结果 | 耗时 |
| --- | --- | ---: |
| 时钟校准与 Demo 网络写边界（`test_demo_constructor_clock.py`, `test_binance_demo_network.py`） | **116 passed** | 21.74 s |
| Alpaca Paper 运行时、Native V69、多实例、日历调度与公司行为（5 个测试模块） | **48 passed** | 80.15 s |
| 公共资金政策、迁移、共享记账与风控快照（`tests/trading/*capital*.py`，6 个测试模块） | **58 passed** | 30.68 s |

### 2.4 历史未发送占位符审计清理

在完成升级前 C07 一致性备份（`backup-20261126T104000Z`，验证通过）后的停服窗口内，经自动化五重不变量断言校验通过，安全移除了旧版 bug 遗留的未发送占位符 `fl-d7345f8795464a65-g-0003a-00000003`，并持久化审计收据至 `/var/lib/factorlab-demo/execute-state/p10-01-phantom-placeholder-cleanup-receipt.json`（`0600`）。其余全部历史订单、成交、资金费、预算、策略包及 7/20 USDT 归属记录保持原样。

---

## 3. 升级前后账户及账本差异报告

完整脱敏快照见 `pre-upgrade-snapshot.json`（`2026-11-26T10:31:56Z`）与 `post-upgrade-snapshot.json`（`2026-11-26T15:36:02Z`）。

### 3.1 Binance Demo 对比

| 指标 | 升级前（`10:31:56Z`） | 升级后（`15:36:02Z`） | 差异与说明 |
| --- | --- | --- | --- |
| 运行版本 / PID | `ea9cbbdd` / `652127` | `12335eb` / `767167` | 已切换至目标版本并通过 C07 维护重启 |
| 服务健康与状态 | `PAUSED` / `account_recovered=false` | `RUNNING` / `account_recovered=true` | `demo-health` 退出码 `1 -> 0`，活跃告警清零 |
| `UNKNOWN` 订单数 | `1`（`g-0003a-00000003`） | **`0`** | 历史未发送占位符已审计清理 |
| 官方 Wallet Balance (USDT) | `4999.23962561` | `4999.47647223` | `+0.23684662`（自然减仓成交盈利 + 资金费结算） |
| 官方 Margin Balance / NAV (USDT) | `4999.50555826` | `4999.47647223` | 持仓平仓后未实现盈亏归零 |
| 官方 Unrealized PnL (USDT) | `+0.26593265` | `0.00000000` | `0.04 NVDAUSDT` 多单已自然平仓 |
| 官方 Available Balance (USDT) | `4993.49522561` | `4993.54007223` | 挂单占保证金 `5.93640000` |
| 官方持仓 | `NVDAUSDT +0.04 @ 143.61` | 空仓（`0`） | 由 `g-0003a-00000005` @ `149.39` 自然平仓 |
| 官方挂单 | `0` | `1`（`g-0003b-00000001`, BUY 0.04 @ `148.41`） | 轮换至新网格后的自然挂单，且经重启完整恢复 |
| 资金费账本（`futures_funding`） | `10` 条 | **`32` 条** | 升级后自动同步并补齐全部 22 笔新官方资金费流水 |
| 累计 POST 消耗 / 剩余（上限 100,000） | `122` / `99,878` | `125` / `99,875` | 新增 3 笔自然策略单（无任何人为验收单） |
| 累计名义额消耗 / 剩余（上限 500,000 USDT） | `763.6376` / `499,236.3624` | `781.5300` / `499,218.4700` | 严格保留原有预算与历史消耗，未重置 |
| 初始权益基线 / 最大累计损失预算 | `5000.00000000` / `250 USDT` | `5000.00000000` / `250 USDT` | 实际累计回撤 `0.52352777 USDT`（远低于 250 上限） |
| `protocol.sqlite3` 订单统计 | FILLED 63, CANCELED 55, REJECTED 2, UNKNOWN 1 | FILLED 64, CANCELED 56, REJECTED 2, SUBMITTED 1 | 保留全部历史订单，无数据丢失 |
| `strategy-deployment.sqlite3` 增量迁移 | V1 表结构（6 包 / 12 切换 / 35 事件） | **Schema v2/v3 伴生表已自动创建**，原 6 包/12 切换/35 事件与 `legacy-grid` 完整保留 | 无损增量迁移 |
| 历史资金归属事件（`demo:static-shared-account:v1`） | `grid=7 USDT (NVDAUSDT)`, `portfolio=20 USDT (QQQUSDT, SPYUSDT)` | 完全一致，原封保留 | 未硬编码重置 |

### 3.2 Alpaca Paper 对比

| 指标 | 升级前（`10:31:56Z`） | 升级后（`15:36:02Z`） | 差异与说明 |
| --- | --- | --- | --- |
| 运行版本 / PID / 启动次数 | `ea9cbbdd` / `521249` / `starts=20` | `12335eb` / `767635` / `starts=22` | 经升级（21）+ 重启验收（22） |
| 健康状态 / 双流状态 | `healthy=true` / `CONNECTED` / `CONNECTED` | `healthy=true` / `CONNECTED` / `CONNECTED` | 持续健康 |
| 官方 Cash / Equity (USD) | `100000.05` / `100000.05` | `100000.05` / `100000.05` | 2026-11-26 美国感恩节休市，未伪造任何成交 |
| 官方持仓 / 挂单 | `0` / `0` | `0` / `0` | 一致 |
| 策略身份与调度状态 | `v69-original` / `natural-week` / `WAITING_NEXT_WEEK` | `v69-original` / `natural-week` / `WAITING_NEXT_WEEK` | 原 V69 语义完整保留 |
| `account.sqlite3` | `19 orders`, `18 activities`, `8 meta` | `19 orders`, `18 activities`, `8 meta` | 历史完全保留 |
| `portfolio.sqlite3` 增量迁移 | 7 表（`6 plans`, `12 orders`, `13 confirmations`, `2 claims`） | 新增公共资金政策 8 张伴生表，原数据行数零变化 | 无损增量迁移 |
| `deployments.sqlite3`（V2 Native V69 收编） | 不存在 | 自动创建（`packages=1`, `instance_adoptions=1`, `instance_deployment=1`, `strategy_registry=2`） | 自动构建 `native-v69-12335eb...zip` 并无损收编 `V69_CAUSAL / v69-original` |

---

## 4. 两市场在线运行与订单恢复证据

### 4.1 Binance Demo 实测验收（详细 JSON 见 `binance-online-acceptance.json`）

1. **自然策略在线闭环（未人为制造验收单）**：
   - `fl-d7345f8795464a65-g-0003a-00000004`（orderId `196094766`，SELL 0.04 @ `149.51` `reduceOnly=true`）-> 自然撤单改价（`CANCELED`）；
   - `fl-d7345f8795464a65-g-0003a-00000005`（orderId `196095410`，SELL 0.04 @ `149.39` `reduceOnly=true`）-> 自然成交平仓（`FILLED`）；
   - 自动轮换至 `fl-d7345f8795464a65-g-0003b`，自然提交 `fl-d7345f8795464a65-g-0003b-00000001`（orderId `196095716`，BUY 0.04 @ `148.41` `NEW`）；
   - 经 C07 维护窗口停机重启（PID `763010 -> 767167`）后，挂单 `196095716` 以原 `clientOrderId` 完整恢复，无重复下单，`unknown_count=0`。
2. **Campaign 自动创建与续期**：原窗口 `window-ac7004ae46f54624829872bfa316f28d`（2026-11-25 17:32 UTC 过期）后，升级重启即自动创建新 3 小时窗口 `window-bce684fc966547049c3600e5a2778a98`（到期 `2026-11-26T18:21:03Z`，`orders=4, consumed=3, remaining=1`），全程无人干预。
3. **动态多标的行情获取与资格校验**（基于已部署 `12335eb` 实测 `DemoSymbolRegistry` 访问官方 Demo API）：

| 标的 | 资格校验 | 杠杆/保证金 | 最小名义额 (USDT) | 实时 Ask / Mark | 最新资金费率 | 1h K线 | 7 USDT Grid 可负担 | 20 USDT Portfolio 可负担 |
| --- | --- | --- | ---: | --- | ---: | ---: | --- | --- |
| `NVDAUSDT` | 通过 | 1x CROSSED | 5 | 149.95 / 150.08 | -0.00064119 | 26 | 是 | 是 |
| `QQQUSDT` | 通过 | 1x CROSSED | 5 | 637.74 / 638.14 | +0.00051248 | 26 | 是 | 是 |
| `SPYUSDT` | 通过 | 1x CROSSED | 5 | 722.91 / 722.34 | +0.00093070 | 26 | **否**（0.01×722.91 > 7） | 是 |
| `BTCUSDT` | 通过 | 20x CROSSED | 50 | 60392.60 / 60321.61 | +0.00010000 | 26 | **否** | **否** |
| `ETHUSDT` | 通过 | 20x CROSSED | 20 | 1684.28 / 1684.20 | +0.00010000 | 26 | **否** | 是 |

4. **三类网格运行能力**：
   - **固定网格（`legacy-grid`）**：线上持续实盘模拟运行并已完成完整平仓与轮换闭环；
   - **资金费敏感网格（`B_FUNDING_SENSITIVE_GRID` / `AdaptiveGridV2`）**：在已部署 `12335eb` 环境喂入真实在线 NVDAUSDT 1h K线与 32 笔官方 `FUNDING_FEE` 流水，产出合法 V2 限价单 `BUY 0.04 @ 149.45` 且检查点持久化通过；
   - **波动率自适应网格（`C_VOL_ADAPTIVE_GRID` / `AdaptiveGridV2`）**：喂入真实在线 NVDAUSDT 1h K线与 100 小时 warmup 收盘序列，波动率带宽计算与检查点持久化通过。
5. **部分成交与状态机边界说明**：本次自然交易均为 0.04 整笔成交，未自然出现部分成交；部分成交增量记账、防重复回放与原始订单撤单恢复能力由本地与离线/协议回归测试覆盖并已验证通过。

### 4.2 Alpaca Paper 实测验收（详细 JSON 见 `alpaca-online-acceptance.json`）

1. **原 V69 策略持续运行**：`factorlab-alpaca-paper.service` 处于 `EXECUTE` 模式，已自动收编为 V2 `NativeWeeklyExecution`（`V69_CAUSAL / v69-original`），遵守自然周调度（本周首日 `2026-11-23`，信号截止 `2026-11-20`，当前 `WAITING_NEXT_WEEK`）。因 2026-11-26 为美国感恩节休市，**未伪造任何行情、成交或调仓**。
2. **合法交易日历与分钟/小时/日/周调度**（基于官方 Paper 日历接口实测）：
   - 感恩节休市日 `2026-11-26`：分钟与日调度均返回 `null`；
   - 常规开盘日 `2026-11-23`（10:31 ET）：分钟（`closed-bar:...60`）、小时（`closed-bar:...3600`）、日（`session_open:2026-11-23`）、周（`natural_week_open:2026-11-23`，`signal_cutoff=2026-11-20 21:00Z`）全部精确命中；
   - 提前收盘日 `2026-11-27`（黑色星期五 13:00 ET / 18:00 UTC 收盘）：10:35 ET 小时调度命中且 `closed_at=18:00Z`，周调度为 `null`，13:05 ET 收盘后返回 `null`。
3. **交易流与行情流恢复**：常驻服务维持 `trade_stream=CONNECTED` 与 `market.state=CONNECTED`（IEX 8 标的 `missing_symbols=[]`），并实测单服务网络中断与进程重启后双流自动重连恢复。
4. **公司行为、分红、拆股及原始订单恢复**：实测官方 `/v2/account/activities` 分页拉取 21 条流水（18 `FILL` + 0 公司行为），无 HTTP 422 报错；并在隔离 `CorporateReceiptJournal` 中验证 `DIV` 回执幂等摄入与重放保护；原 `account.sqlite3`（19 订单）与 `portfolio.sqlite3`（12 订单 / 13 确认）完整恢复。

---

## 5. 策略实例、资金归属与公共资金政策对账

1. **Binance Demo 持久化资金归属与公共资金政策验证**：
   - 线上持久化账本保留 `demo:static-shared-account:v1`：`grid_budget = 7 USDT (NVDAUSDT)`、`portfolio_budget = 20 USDT (QQQUSDT, SPYUSDT)`，合计 `27 USDT`，官方账户权益 `4999.47647223 USDT` 仅作风控与余额输入，未扩大策略资金上限。
   - 在已部署 `12335eb` 环境下实测 `CapitalPolicy(mode="SHARED_SAME_DIRECTION", budget=27 USDT)`：注册 `grid-legacy-7`（7 USDT）与 `portfolio-20`（20 USDT）后，再尝试额外分配 `1 USDT` 被精确拒绝（`ValueError: allocations exceed static account capital budget`），确认**不会重复计算账户现金或保证金**；同标的 `NVDAUSDT` 双实例共享时默认合并上限取参与实例最小值（`7 USDT`）。
2. **Alpaca Paper 多实例与共享证券隔离验证**：
   - 线上生产账本保持单一原生策略 `V69_CAUSAL / v69-original`（`deployments.sqlite3` 已记录 `instance_adoptions=1`）。
   - 在已部署 `12335eb` 隔离环境中实测双实例注册（`V69_CAUSAL` 6,000 USD + `balanced-1` 4,000 USD，预算 10,000 USD，`SHARED_SAME_DIRECTION`）：尝试额外分配 1 USD 被拒绝（`ValueError: allocations exceed static account capital budget`）；共享证券 `AAPL` 同时记录两个独立 `SymbolOwnership`，默认合并暴露上限自动收敛至 `4000 USD`。

---

## 6. 服务器资源实测数据（CPU / 内存 / 磁盘 / 日志）

| 资源项 | 升级前（`10:31Z`） | 升级后（`15:36Z`） | 备注 |
| --- | --- | --- | --- |
| 根分区 `/` 总容量 / 使用率 / 可用 | 60.95 GiB / **97.89%** / **1.27 GiB** | 60.95 GiB / **44.32%** / **33.92 GiB** | 清理无引用历史 release 后恢复充裕余量 |
| `/opt/factorlab` | 37 GiB | 14 GiB | 保留当前 + 回滚版本 |
| `/opt/factorlab-alpaca` | 13 GiB | 4.6 GiB | 保留 `12335eb` + `ea9cbbdd` + `bbda2122` |
| `/var/lib/factorlab-demo` | 78 MiB | 83 MiB | 正常增长 |
| `/var/lib/factorlab-alpaca-paper` | 498 MiB | 499 MiB | 其中 `market-stream.sqlite3` 占 497 MiB |
| `/var/backups/factorlab-demo` | 316 MiB | 366 MiB | 含升级前/后 C07 备份（13 份有效副本，14 天保留） |
| `/var/backups/factorlab-alpaca-paper` | 169 MiB | 667 MiB | 新增 `p10-01-pre-12335eb-20261126T103849Z`（498 MiB） |
| `/var/log`（含 `syslog` / `journal`） | 1.1 GiB（`syslog` 876 MiB） | 1.1 GiB | 需与 P10-02 协调长期日志压缩/瘦身 |
| 内存（887 MiB total） | used 349 MiB / available 537 MiB / Swap used 556 MiB | used 482 MiB / available 404 MiB / Swap used 522 MiB | 稳定运行无 OOM |
| 业务进程 RSS / CPU（稳态实测） | Demo Execute 17.1 MiB / TradFi 24.1 MiB / Alpaca 30.2 MiB | Demo Execute 33.7 MiB (5.1% CPU 启动后回落) / Alpaca 22.6 MiB (1.5% CPU) / TradFi 18.8 MiB (0.6% CPU) | 2 vCPU 负载低，资源充裕 |

---

## 7. 无人值守运行验证、已知问题与未完成长周期清单

### 7.1 无人值守与故障恢复实测（详细 JSON 见 `unattended-ops-acceptance.json`）

- **开机自启**：全部 3 个常驻服务（`factorlab-demo-execute`, `factorlab-demo-tradfi-observer`, `factorlab-alpaca-paper`）与 6 个定时单元（`demo-health`, `demo-daily-report`, `demo-backup`, `demo-strategy-deploy`, `demo-tradfi-health`, `alpaca-paper-health`）均为 `enabled`。
- **C07 一致性备份 + 隔离恢复演练 + 进程重启恢复**：执行 `systemctl start factorlab-demo-backup.service`（`backup-20261126T153229Z`），`rc=0`，维护停机仅 `5.756s`，14 个 SQLite 数据库全部完整性通过，`drill_restore` 10/10 校验通过且正确施加 `restore-quarantine.json` 隔离保护，重启后 `reconcile_verified=true`、`open_orders_count=1`、`unknown_count=0`。
- **健康检查与告警恢复**：`factorlab-demo-health.service`、`factorlab-demo-tradfi-health.service`、`factorlab-alpaca-paper-health.service` 实测全部返回 `rc=0`；`/var/lib/factorlab-demo/monitoring/alert-state.json` 确认 `active_alerts={}`、`consecutive_healthy_since_restart=5`。
- **每日报告**：手动触发 `factorlab-demo-daily-report.service` 返回 `rc=0`，成功刷新 `daily-report-latest.md`（2,893 bytes）。
- **数据过期自动恢复**：在感恩节休市盘口短暂静默期间实际观测到 Binance Demo 触发 `STALE_OR_FUTURE_QUOTE` 自动暂停、随后新报价到达后自动恢复 `RUNNING` 与对账。
- **非破坏性单服务网络中断与进程重启（Alpaca Paper）**：通过 `systemctl set-property --runtime factorlab-alpaca-paper.service IPAddressDeny=any` 隔离 6 秒后解除（未影响主机全局网络及 Binance Demo），解除隔离后双流恢复 `CONNECTED`（瞬时采样点 `alpaca_after_network_fault` 中 `healthy=false`），随后执行 `systemctl restart factorlab-alpaca-paper.service`，确认新进程 PID `726025 -> 767635`、`starts: 21 -> 22`、`healthy=true`、`trade_stream=CONNECTED`、`market=CONNECTED`。
- **过夜连续无人值守实测快照（`2026-11-27T01:58:12Z`，距 `T0` 约 10.4 小时）**：
  - Binance Demo（PID `767167` 全程零重启，`starts=46`）：`status=RUNNING`, `account_recovered=true`, `unknown_count=0`, `active_alerts={}`；期间自然轮换两代网格（`g-0003b -> g-0003c -> g-0003d`，累计 POST `125 -> 127`），Campaign 窗口连续 3 次全自动续期至 `2026-11-27T03:21:08Z`，凌晨 `00:05Z` 自动生成 `daily-report-2026-11-27.md`（`Result=success`）。
  - Alpaca Paper（PID `767635` 全程零重启，`starts=22`）：期间观测到休市 IEX 行情流空闲断连（`01:56Z` `StreamDisconnected -> RECONNECTING -> RECOVERING`）并在同进程免重启下自动恢复为 `CONNECTED` 与 `healthy=true`（`01:58Z`）。
  - 根分区磁盘使用率 `45.0%`（可用 `33.51 GiB`，10.4 小时净增 `0.41 GiB`）。

### 7.2 已知问题与 P10-02 协调项（不阻塞当前部署）

1. **`market-stream.sqlite3` 长期磁盘增长**：Alpaca Paper 的 `market-stream.sqlite3` 在 2 天内增长至 ~498 MiB（241 万条流事件），长期无人值守需在 P10-02 中引入按天滚动归档或保留窗口裁剪策略。
2. **`/var/log/syslog` 体积增长偏快（876 MiB）**：`factorlab-demo-execute.service` 每轮 stdout 打印包含全部 120+ 历史订单的 JSON 状态行，rsyslog 全量转存至 `/var/log/syslog`；建议与 P10-02 协调精简常态日志输出或收紧 logrotate 轮转阈值。
3. **旧版历史 release 清理自动化**：当前 `install_demo_strategy_runtime.py` 与 `install_alpaca_paper_runtime.sh` 仅追加新 release 目录而无自动保留数量（如保留最近 3 个版本）修剪，建议在 P10-02 中纳入发布后清理策略。
4. **外部 Webhook/移动端推送未配置**：`alert-state.json` 与 `alert-events.jsonl` 正常记录本地告警事件，外部 webhook 凭据按现网配置保持 `NOT_CONFIGURED`。
5. **休市/无部分成交边界**：Alpaca Paper 因 2026-11-26 美国感恩节休市尚未触发实际开盘日调仓（下次开盘为 `2026-11-27T14:30:00Z`，下次周调仓为下周一）；Binance Demo 本次自然成交为整笔成交。

### 7.3 未完成的长周期持续观测窗口清单（不宣称已通过，不阻塞其他开发任务）

持续运行证据收集已由现有每分钟健康定时器、每日报告定时器及每日 C07 备份定时器自动运行，观测基线 **`T0 = 2026-11-26T15:36:02Z`**：

| 观测窗口 | 目标截止时间 (UTC) | 当前状态 | 验收核查命令 / 依据 |
| --- | --- | --- | --- |
| **24h 窗口** | `2026-11-27T15:36:02Z` | **IN_PROGRESS（未结束）** | 核查每日报告、C07 定时备份（03:15Z）、Alpaca 2026-11-27 黑色星期五开盘与提前收盘表现、无未恢复 `UNKNOWN`/异常重启 |
| **72h 窗口** | `2026-11-29T15:36:02Z` | **IN_PROGRESS（未结束）** | 核查连续 3 次每日 C07 备份、磁盘与内存稳态趋势、Binance Demo 连续 Campaign 自动续期与资金费流水连续性 |
| **7d 窗口** | `2026-12-03T15:36:02Z` | **IN_PROGRESS（未结束）** | 核查下周一 Alpaca Paper `v69-original` 自然周调仓执行、7 天资源增长斜率及双市场零账本偏差 |

---

## 8. 可执行的回滚说明

> **核心原则**：代码回滚**绝不覆盖或回退已推进的 SQLite 业务账本**（本版本对 `strategy-deployment.sqlite3` 与 `portfolio.sqlite3` 的迁移均为伴生表增量兼容）；仅在发生严重数据库损坏时才启用离线备份灾难恢复流程。

### 8.1 回滚保留资产清单

- **Binance Demo 上一稳定代码版本**：`/opt/factorlab/releases/ea9cbbdd7519e5400df097e5661b0d6c47b6c4da`
- **Binance Demo 升级前一致性备份**：`/var/backups/factorlab-demo/backup-20261126T104000Z`
- **Binance Demo 升级后一致性备份**：`/var/backups/factorlab-demo/backup-20261126T153229Z`
- **Alpaca Paper 上一稳定代码版本**：`/opt/factorlab-alpaca/releases/ea9cbbdd7519e5400df097e5661b0d6c47b6c4da`（已记录于 `/opt/factorlab-alpaca/previous-release`）
- **Alpaca Paper 升级前完整状态备份**：`/var/backups/factorlab-alpaca-paper/p10-01-pre-12335eb-20261126T103849Z`

### 8.2 Alpaca Paper 一键代码回滚命令

```bash
ssh -i <SSH_KEY_PATH> <USER>@<VM_HOST> \
  "sudo sh /opt/factorlab-alpaca/releases/12335ebd56fda7a2d0e709176806d96e3e7b7b67/source/tools/rollback_alpaca_paper_runtime.sh ea9cbbdd7519e5400df097e5661b0d6c47b6c4da"
```

回滚后验证：

```bash
ssh -i <SSH_KEY_PATH> <USER>@<VM_HOST> \
  "readlink /opt/factorlab-alpaca/current && sudo -u factorlab-alpaca /opt/factorlab-alpaca/current/venv/bin/python -m factorlab.integrations.alpaca.cloud_runtime health"
```

### 8.3 Binance Demo 同账本代码回滚命令

```bash
ssh -i <SSH_KEY_PATH> <USER>@<VM_HOST> "sudo python3 - <<'PY'
import pathlib, subprocess
OLD = 'ea9cbbdd7519e5400df097e5661b0d6c47b6c4da'
py = f'/opt/factorlab/releases/{OLD}/venv/bin/python'
subprocess.run(['systemctl', 'stop', 'factorlab-demo-strategy-deploy.timer', 'factorlab-demo-tradfi-observer.service', 'factorlab-demo-execute.service'], check=True)
dropins = {
    'factorlab-demo-execute.service': ('20-release.conf', f'[Service]\nExecStart=\nExecStart={py} -m factorlab.integrations.binance_futures_trading.demo_grid_supervisor start --execute\n'),
    'factorlab-demo-health.service': ('20-strategy-release.conf', f'[Service]\nExecStart=\nExecStart={py} -m factorlab.integrations.binance_futures_trading.demo_cloud_runtime monitor\n'),
    'factorlab-demo-daily-report.service': ('20-strategy-release.conf', f'[Service]\nExecStart=\nExecStart={py} -m factorlab.integrations.binance_futures_trading.demo_cloud_runtime report --write-report --markdown\n'),
    'factorlab-demo-strategy-deploy.service': ('40-formal-release.conf', f'[Service]\nExecStart=\nExecStart={py} -m factorlab.integrations.binance_futures_trading.azure_strategy_deploy drive --state-root /var/lib/factorlab-demo/execute-state --inbox /var/lib/factorlab-demo/champions --mode hot\n'),
    'factorlab-demo-backup.service': ('40-formal-release.conf', f'[Service]\nReadWritePaths=/var/lib/factorlab-tradfi-observer/task-mailbox\nExecStart=\nExecStart={py} -m factorlab.integrations.binance_futures_trading.demo_cloud_backup run-scheduled --state-root /var/lib/factorlab-demo/execute-state --backup-root /var/backups/factorlab-demo --retention-days 14 --min-valid-copies 1 --recovery-directory /run/factorlab-demo-backup --task-mailbox /var/lib/factorlab-tradfi-observer/task-mailbox/tasks.sqlite3 --drill\nExecStopPost=\nExecStopPost={py} -m factorlab.integrations.binance_futures_trading.demo_cloud_backup recover-scheduled --state-root /var/lib/factorlab-demo/execute-state --backup-root /var/backups/factorlab-demo --recovery-directory /run/factorlab-demo-backup\n'),
    'factorlab-demo-tradfi-observer.service': ('40-formal-release.conf', f'[Service]\nExecStart=\nExecStart={py} -m factorlab.integrations.binance_futures_trading.tradfi_cloud --state-root /var/lib/factorlab-tradfi-observer --execute-health /var/lib/factorlab-demo/execute-state/cloud-health.json --symbols QQQUSDT SPYUSDT --poll-seconds 30 --task-db /var/lib/factorlab-tradfi-observer/task-mailbox/tasks.sqlite3\n'),
    'factorlab-demo-tradfi-health.service': ('40-formal-release.conf', f'[Service]\nExecStart=\nExecStart={py} -m factorlab.integrations.binance_futures_trading.tradfi_cloud --state-root /var/lib/factorlab-tradfi-observer --health\n'),
}
for unit, (name, body) in dropins.items():
    (pathlib.Path('/etc/systemd/system') / (unit + '.d') / name).write_text(body)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'start', 'factorlab-demo-execute.service', 'factorlab-demo-tradfi-observer.service', 'factorlab-demo-strategy-deploy.timer'], check=True)
print('ROLLED_BACK_TO', OLD)
PY"
```

回滚后验证 `account_recovered=true` 与 `unknown_count=0`：

```bash
ssh -i <SSH_KEY_PATH> <USER>@<VM_HOST> \
  "sudo systemctl start factorlab-demo-health.service && sudo jq '{pid,status,account_recovered,unknown_count,nav_usdt,open_orders}' /var/lib/factorlab-demo/execute-state/cloud-health.json"
```

---

## 9. 附：本目录证据文件清单

- [pre-upgrade-snapshot.json](pre-upgrade-snapshot.json)：升级前脱敏双市场账户、账本、服务与主机资源快照
- [post-upgrade-snapshot.json](post-upgrade-snapshot.json)：升级后脱敏双市场账户、账本、服务、资源快照与 `T0` 观测窗口基线
- [source-consistency.json](source-consistency.json)：双市场源码树、安装包与实际运行进程逐文件 SHA-256 校验记录
- [binance-online-acceptance.json](binance-online-acceptance.json)：Binance Demo 动态多标的、自适应网格 V2 与公共资金政策在线验收记录
- [alpaca-online-acceptance.json](alpaca-online-acceptance.json)：Alpaca Paper 官方日历调度、多实例、共享证券与公司行为在线验收记录
- [unattended-ops-acceptance.json](unattended-ops-acceptance.json)：systemd 开机自启、C07 备份与隔离恢复演练、每日报告、网络中断与进程重启恢复记录
