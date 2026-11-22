# FactorLab P7-M19-F12：Binance Demo 增量订单恢复与历史终态归档验证报告

- **任务编号**：`P7-M19-F12`
- **分支**：`perf/p7-m19-f12-incremental-recovery`
- **基线提交（`origin/main`）**：`ef9d50734f35338b40c6f4a7413f6763be7e9b53`
- **验收结论**：`F12_ENGINEERING_ACCEPTED`

---

## 一、背景与真实性能根因分析

### 1.1 F09 官方 Demo 600 秒 Execute 观测复盘

在 `P7-M19-F09` 官方 Demo 600 秒运行窗口中，虽然历史终态订单仅约 6 笔且全程 `0 POST / 0 DELETE`，但观测到了严重的 REST `GET` 风暴：

- `GET /fapi/v1/order`：累计 **180 次**（耗时约 **225.9 秒**，平均单次 ~1.25 秒）
- `GET /fapi/v1/userTrades`：累计 **90 次**（耗时约 **114.3 秒**，平均单次 ~1.27 秒）
- 单次恢复耗时：约 **40 秒**（实际轮询周期被拉长至 35～40 秒）
- 600 秒窗口总计：**631 次 GET**

### 1.2 原实现 $O(N)$ 开销的精确来源

通过对 `FuturesAdapter._reconcile()` 与 `FuturesPaperGateway.recover()` 的代码路径溯源，原实现的 $O(N)$ 请求放大由以下三层叠加造成：

1. **全量历史订单逐笔遍历（`adapter.py:FuturesAdapter._reconcile`）**：
   每次调用 `reconcile()` 时，遍历 `futures_orders` 表中的**全部历史订单**（除本地未上链的 `REJECTED` 外），对每一笔历史订单无条件调用 `GET /fapi/v1/order`。
2. **有成交历史订单重复拉取成交明细（`adapter.py:FuturesAdapter.query_order`）**：
   `query_order()` 在收到 `/fapi/v1/order` 响应后，只要 `executedQty > 0`（即所有历史已成交或部分成交后撤单的终态订单），无条件再次调用 `GET /fapi/v1/userTrades?symbol=...&orderId=...` 重新拉取该订单的全部历史成交并重算累计手续费。
3. **单轮 `gateway.recover()` 的双遍 `adapter.reconcile()` 放大（`gateway.py:FuturesPaperGateway.recover`）**：
   `FuturesPaperGateway.recover()` 在重放 `OrderEvent` 前先调用一次 `self.adapter.reconcile()`（Pass 1），随后调用 `self.controller.reconcile()`，而 `DemoExecutionGateway.reconcile()` 内部又无条件调用第二次 `self.adapter.reconcile()`（Pass 2）。
   - 对于 $N$ 笔历史订单（其中 $N_{\text{filled}}$ 笔有成交）和 $A$ 笔活动订单（其中 $A_{\text{partial}}$ 笔有部分成交），单轮 `gateway.recover()` 的 REST `GET` 请求数为：
     $$\text{GET}_{\text{round}} = 2 \times \left( 5_{\text{account}} + (N + A)_{\text{order}} + (N_{\text{filled}} + A_{\text{partial}})_{\text{userTrades}} \right)$$
   - 代入 F09 场景（6 笔历史订单中 3 笔有成交，每轮 2 遍，600 秒内共 15 轮恢复）：
     - `/fapi/v1/order`：$6 \times 2 \times 15 = 180$ 次
     - `/fapi/v1/userTrades`：$3 \times 2 \times 15 = 90$ 次
     与 F09 实测日志精确吻合。

---

## 二、交易所事实契约与完整性证明

不能将恢复简单改为“只查非终态订单”。本方案基于 Binance USD-M Futures 官方接口契约与本地账本不变式，建立了可证明的完整性边界：

### 2.1 订单状态终态 vs. 经济事实终态

| 维度 | 交易所事实与风险 | F12 完整性契约 |
| --- | --- | --- |
| **订单状态终态** | Binance 订单状态 `FILLED`、`CANCELED`、`EXPIRED`、`REJECTED` 为状态机终态；`NEW`、`PARTIALLY_FILLED`、`PENDING_CANCEL` 为非终态。 | `PENDING_CANCEL` 显式映射为本地非终态 `SUBMITTED` 并置 `reconcile_required=True`，绝不提前归档。 |
| **仅凭写响应 / WS 推送的终态不可信** | `POST /fapi/v1/order` 的同步 `WRITE_ACK` 或单次 WebSocket 推送可能尚未落盘完整 `userTrades` 手续费明细，或存在取消与成交竞态（cancel-fill race）。 | 订单进入 `ARCHIVED_TERMINAL` 必须满足 `last_authority_source == 'GET'`，即至少经过权威 `GET /fapi/v1/order` 对账确认；仅由 `WRITE_ACK` 或 WS 推进的终态订单归为 `ECONOMIC_INCOMPLETE`，继续留在实时对账队列。 |
| **迟到成交与取消竞态** | 撤单请求返回 `CANCELED` 前后，撮合引擎可能刚刚产生成交；或者 `/fapi/v1/order` 已显示 `executedQty > 0`，但 `/fapi/v1/userTrades` 尚未返回完整成交明细。 | 归档必须同时满足：<br>1. `trades_reconciled == 1`：当 `executedQty > 0` 时，`/fapi/v1/userTrades` 返回的成交数量之和必须精确等于 `executedQty`，且所有成交均含有效 `USDT` 手续费；<br>2. **多次权威确认（`min_terminal_confirmations = 2`）**：连续至少 2 次权威 `GET` 观测到完全一致的终态、`executedQty` 和 `cumulative_fee`；<br>3. **终态驻留窗口（Dwell Window）**：自首次权威 `GET` 观测到终态起，经过至少 `terminal_dwell_seconds = 60s`（对于 `CANCELED` / `EXPIRED` 为 `cancel_race_dwell_seconds = 120s`）且期间无任何状态/成交/费用漂移，才允许置 `archived = 1`。 |
| **迟到成交或手续费事后修正** | 若归档后交易所罕见地迟到补录成交或修正手续费，会导致账户钱包余额（`walletBalance`）或持仓数量（`positionAmt`）与本地已知成交变化不符，或在增量成交流中出现新 `tradeId` / 修改后的 `commission`。 | 双重发现机制：<br>1. **实时层账户基线突变触发（Reactive Account-Delta Trigger）**：每轮实时恢复比对权威账户快照（`wallet_balance` + `positions`）与上轮基线 `account_baseline`；若发生变动且当前实时层未捕获新成交，**立即在同一轮次触发增量历史审计**；<br>2. **历史层重叠窗口与滚动抽检（Scheduled Overlap & Rolling Audit）**：周期性回扫最近 `trade_overlap_ids = 50` 条 `userTrades`（校验 `qty` / `price` / `commission` / `commissionAsset` 是否与已存收据一致），并按 `rolling_order_batch_size` 滚动抽检已归档订单。一旦发现差异，立即将该订单反归档（`archived = 0, reconcile_required = 1`），生成修正 `OrderEvent` 入账。 |

### 2.2 接口查询窗口、分页与保留期边界

1. **`GET /fapi/v1/userTrades` 分页与时间窗口约束**：
   - Binance 限制单次 `limit <= 1000`；`startTime` 与 `endTime` 跨度不得超过 **7 天**；**`fromId` 不能与 `startTime` / `endTime` 或 `orderId` 同时传入**。
   - `fetch_user_trades_paginated()` 严格遵守上述约束：
     - 当使用 `fromId` 增量拉取时，绝不附带 `startTime` / `endTime`，通过 `fromId = max(1, last_trade_id - overlap_ids + 1)` 建立有界重叠窗口，单页满 `page_limit` 时以 `last_id + 1` 自动翻页。
     - 当使用 `startTime..endTime` 范围查询且跨度超过 7 天时，自动切分为 `<= 7 天` 的子窗口，子窗口内部若满页则推进 `cursor_start = last_time_ms` 并依赖跨页 `(id)` 去重。
     - **防静默截断（Fail-Closed）**：单页内部若出现重复 `trade.id`，或跨页重叠区同一 `trade.id` 的关键经济字段（`orderId`、`qty`、`price`、`commission`、`commissionAsset`）不一致，或达到 `max_pages` 仍未读尽，或游标停滞，立即抛出 `DataQualityError`，拒绝推进水位并阻断交易准入。
2. **`GET /fapi/v1/order` 历史保留期边界**：
   - Binance USD-M Futures 会自动清理 **超过 3 天且无成交（`executedQty == 0`）的 `CANCELED` / `EXPIRED` 订单**（查询返回 `-2013 Order does not exist`），且标准 REST 接口仅保留 **最近 90 天** 的历史订单与成交。
   - **区分“留存期外正常清理”与“留存期内异常丢失”**：
     - 对已归档订单的滚动复核：若返回 `-2013`，且属于（a）无成交的 `CANCELED` / `EXPIRED` 订单且距终态超过 3 天，或（b）已成交终态订单且距终态超过 90 天，则记录 `pruned_by_exchange_retention` 并保持本地权威归档账本，**不误判为 `UNKNOWN`**；
     - 反之，若在交易所承诺留存期内（3 天内的撤单或 90 天内的成交单）返回 `-2013`，属于持久记录与交易所事实不一致，立即将该订单反归档降级为 `UNKNOWN`（`reconcile_required = True`），并将 `history_audit_status` 置为 `FAILED`，使控制器立即进入 `RECONCILIATION_REQUIRED` 拒绝交易状态。
   - **超过 90 天离线断档的完整性保护**：
     - 若 `trades:<SYMBOL>` 水位的 `last_trade_time_ms` 距当前时间超过 90 天，且离线期间账户基线发生未解释变动，REST 接口已无法证明 90 天前的成交流完整性；`audit_history()` 立即记录 `coverage_complete = False` 并抛出 `DataQualityError`，保持可恢复的拒绝交易状态，绝不静默漏账。

---

## 三、架构与实现方案（F12-01 ～ F12-05）

### 3.1 F12-01：四类订单恢复状态机

在 `src/factorlab/integrations/binance_futures_trading/recovery.py` 中实现 `classify_orders()`，将所有订单按持久化元数据划分为四类：

1. **`UNKNOWN`（优先级最高）**：
   - 包括 `confirmed_status == 'UNKNOWN'`、`reconcile_required == 1`（例如 `POST` 超时/断网导致 ACK 丢失、启动恢复时非终态订单待核验、或历史审计发现异常）。
   - 必须通过原 `origClientOrderId` 调用 `GET /fapi/v1/order` 查证；当存在 `UNKNOWN` 时，`submit_order()` 与 `TradingController` 严格禁止重发或提交新订单。
   - 若单次 `GET /fapi/v1/order` 返回 `-2013`（可能因交易所异步落盘延迟），保持 `UNKNOWN` 与 `reconcile_required = True`，绝不自行伪装成无订单或已撤单。
2. **`ACTIVE`（活动挂单）**：
   - 状态为 `NEW`、`PARTIALLY_FILLED`、`PENDING_CANCEL`（本地 `SUBMITTED` / `PARTIALLY_FILLED`）。
   - **F11 优先级兼容**：在 `ACTIVE` 队列内部，按 `(0 if (reduce_only and side == SELL) else 1, client_order_id)` 排序，确保活动 `reduce-only SELL` 平仓挂单始终优先对账。
3. **`ECONOMIC_INCOMPLETE`（状态虽终结但经济事实未完整或未过驻留期）**：
   - 状态虽为 `FILLED` / `CANCELED` / `EXPIRED` / `REJECTED`，但满足以下任一条件：
     - `last_authority_source != 'GET'`（仅收到 `WRITE_ACK` 或 WS 推送，未经权威 `GET` 核实）；
     - `executedQty > 0` 但 `trades_reconciled == 0`（成交明细数量与 `executedQty` 不一致，或手续费缺失/非 `USDT`）；
     - 权威 `GET` 确认次数 `< 2`；
     - 检测到时钟回拨（`clock_rollback_count > 0`）；
     - 未满终态驻留时间（`FILLED` < 60s，`CANCELED`/`EXPIRED` < 120s）。
   - 此类订单在实时恢复层继续每轮执行权威 `GET` 对账，直到所有条件满足后才自动晋升为 `ARCHIVED_TERMINAL`。
4. **`ARCHIVED_TERMINAL`（已归档终态订单）**：
   - 已保存完整的订单终态、交易所 `orderId`、累计成交量、累计 `USDT` 手续费、最后成交 ID/时间及至少 2 次权威 `GET` 确认并渡过驻留期（或本地校验拒绝从未上链的 `LOCAL_PREFLIGHT` 订单）。
   - 实时交易准入恢复层**彻底跳过**对 `ARCHIVED_TERMINAL` 的 `/fapi/v1/order` 与 `/fapi/v1/userTrades` 请求。

### 3.2 F12-02：跨进程持久化恢复元数据与原子水位提交

在现有 SQLite 数据库（复用 `futures_orders`、`futures_events`、`futures_receipts` 与 `PaperStore`）中新增两张轻量元数据表：

1. **`futures_order_recovery`**：
   - 主键 `client_order_id`，索引 `(archived, reconcile_required, confirmed_status)` 与 `(symbol, exchange_order_id)`。
   - 持久化字段：`symbol`、`exchange_order_id`、`confirmed_status`、`raw_status`、`reconcile_required`、`is_reduce_only`、`side`、`cumulative_filled_qty`、`cumulative_fee`、`fee_asset`、`trades_reconciled`、`trade_count`、`last_trade_id`、`last_trade_time_ms`、`terminal_confirmations`、`terminal_first_get_at`、`last_authoritative_get_at`、`last_authority_source`、`archived`、`archived_at`、`archive_reason`、`clock_rollback_count`、`updated_at`。
2. **`futures_recovery_state`**：
   - 持久化全局与分品种水位：
     - `global`：`realtime_rounds`、`audit_round`、`rounds_since_audit`、`last_realtime_reconcile_at`、`last_history_audit_at`、`history_audit_status`、`history_audit_error`、`rolling_cursor_client_order_id`、`clock_rollback_count`、`pruned_by_exchange_retention`。
     - `account_baseline`：上次实时恢复确认的 `wallet_balance` 与 `positions` JSON。
     - `trades:<SYMBOL>`：`last_trade_id`、`last_trade_time_ms`、`last_scan_at`、`coverage_complete`、`coverage_note`。
     - `income:<SYMBOL>:<INCOME_TYPE>`：`last_tran_id`、`last_income_time_ms`、`last_scan_at`、`cumulative_income`、`event_count`。

**崩溃一致性保证（Atomic Event-Before-Watermark Contract）**：
- `sync_order_recovery_on_save()` 与 `sync_order_recovery_on_apply()` 与 `futures_orders`、`futures_events`、`futures_receipts` 的写入处于**同一个 SQLite 事务（`with self._connect() as conn:`）**中。
- 在 `audit_history()` 与 `sync_income_incremental()` 中，所有发现的迟到成交、费用修正或资金费事件**必须先完成 `OrderEvent` 入账 / `handler` 落盘，最后才更新 `futures_recovery_state` 水位**。若中途进程崩溃或写入中断，重启后水位仍停留在旧位置，重放具有幂等性，绝不会出现“水位已前进但经济事件未入账”。

### 3.3 F12-03 & F12-04：双层恢复分离与增量历史查询

1. **第一层：实时交易准入恢复（Tier-1 Real-Time Trading Admission Recovery）**
   - 每次调用 `adapter.reconcile()` / `gateway.recover()` 时执行：
     - 拉取 5 个实时账户读集接口（`account`、`balance`、`positionRisk`、`openOrders`、`leverageBracket`），保持 **5 秒新鲜度约束（`max_snapshot_age_seconds <= 5.0`）**；
     - 交叉核对交易所 `openOrders` 与本地已归档订单：若任何本地 `ARCHIVED_TERMINAL` 订单意外出现在交易所 `openOrders` 中，立即反归档并优先执行 `query_order()`；
     - 仅对 `UNKNOWN`、`ACTIVE`（优先 `reduce-only SELL`）与 `ECONOMIC_INCOMPLETE` 订单执行 `GET /fapi/v1/order` 及必要的 `GET /fapi/v1/userTrades`；
     - 检查 `account_baseline` 是否发生无法由本轮实时成交解释的持仓或钱包变化，若有则立即触发增量 `audit_history()`；
     - **消除网关层双遍冗余网络请求**：`FuturesPaperGateway.recover()` 在 Pass 1 获取权威 `exchange` 快照并重放 `dirty_keys` 后，将新鲜的 `exchange` 快照直接传入 `controller.reconcile(exchange=exchange)`（Pass 2 在 5 秒窗口内直接复用同一快照完成一致性校验），使稳态账户接口每轮从 10 次降至 5 次，活动订单查询不再翻倍。
     - **SQL 级一致性索引（`order_consistency_index`）**：`gateway.recover()` 与 `DemoExecutionGateway.reconcile()` 通过单条 SQL 直接读取全部订单的轻量摘要（状态、累计成交量、均价、累计手续费、最后序列号），无需在每轮恢复中反序列化上千条已归档订单的完整 JSON 与历史事件列表。

2. **第二层：历史经济账本复核（Tier-2 Historical Economic Ledger Audit）**
   - 由 `adapter.audit_if_due()` 按有界周期（默认每 `audit_interval_rounds = 12` 轮或 `audit_interval_seconds = 300s`，或当 `history_audit_status != 'OK'` 时）在后台/准入前按计划触发：
     - **增量成交流扫描**：通过 `GET /fapi/v1/userTrades` 以 `fromId = max(1, last_trade_id - 50 + 1)` 扫描增量与重叠窗口成交；比对每笔成交的 `qty`、`commission`、`commissionAsset` 与本地收据，若发现迟到成交或手续费修正，立即定位所属订单并调用权威 `query_order()` 更新账本；
     - **有界滚动抽检**：每次审计仅抽取 `rolling_order_batch_size`（默认 5 笔）已归档交易所订单调用 `GET /fapi/v1/order`，以游标 `rolling_cursor_client_order_id` 循环覆盖全量历史，单次审计开销恒为 $O(\text{batch\_size})$。
     - **准入联动（无第二套 Supervisor）**：若历史审计发现无法自动修复的账本冲突，自动将目标订单置为 `UNKNOWN`（`reconcile_required = True`）并将 `history_audit_status` 置为 `FAILED`，直接复用现有 `TradingController` 的 `RECONCILIATION_REQUIRED` 机制阻断交易准入。

### 3.4 F12-05：与 F10 / F11 / F08 / C04 的边界兼容性

- **F10（资金费率恢复）**：在 `recovery.py` 与 `adapter.py` 中提供独立的 `income()`、`income_paginated()` 与 `sync_income_incremental(symbol=..., income_type='FUNDING_FEE', handler=...)`，维护独立的 `income:<SYMBOL>:FUNDING_FEE` 水位（按 `(incomeType, tranId)` 重叠去重），**绝不将资金费混入 `OrderEvent.cumulative_fee`**。
- **F11（活动 `reduce-only SELL` 对账优先级）**：`classify_orders()` 对 `ACTIVE` 订单按 `is_reduce_only and side == 'SELL'` 置顶排序。
- **F08（Grid 轮换判定）& C04（独占进程锁与停止语义）**：未修改任何网格轮换、风险预算、持续授权或进程锁逻辑。

---

## 四、6 / 100 / 1000 历史订单性能与正确性对比

使用确定性离线交易所传输层与临时 SQLite 数据库（脚本：`docs/validation/p7-m19-f12/benchmark.py`，原始数据：`docs/validation/p7-m19-f12/measurements.json`），在 **6 笔、100 笔、1000 笔历史终态订单** 三种规模下进行完整对比。

每种规模均包含：
- 已成交终态订单（`FILLED`，占历史订单的 $2/3$，均含 `USDT` 成交费用）
- 已撤单终态订单（`CANCELED`，占历史订单的 $1/3$，含未成交撤单与部分成交后撤单）
- 活动挂单（`NEW`，包含 1 笔 `BUY` 与 1 笔 `reduce-only SELL`）
- 部分成交活动挂单（`PARTIALLY_FILLED`，含成交费用）
- `UNKNOWN`（`POST` 超时 ACK 丢失后通过 `GET` 恢复，验证 `post_count == 1` 零重发）
- 历史归档订单的迟到成交与手续费更新（`hist-0001` 迟到成交 + 手续费补录）

### 4.1 核心性能与正确性指标总表（12 项必测指标）

| 对比维度 | 6 笔历史订单<br>原实现 (`baseline`) | 6 笔历史订单<br>优化后 (`F12`) | 100 笔历史订单<br>原实现 (`baseline`) | 100 笔历史订单<br>优化后 (`F12`) | 1000 笔历史订单<br>原实现 (`baseline`) | 1000 笔历史订单<br>优化后 (`F12`) |
| --- | --- | --- | --- | --- | --- | --- |
| **1. 稳态每轮 GET 总次数** | **36.0** | **8.0** (-77.78%) | **348.0** | **8.0** (-97.70%) | **3348.0** | **8.0** (-99.76%) |
| **2. 稳态每轮 `/fapi/v1/order` 次数** | **16.0** | **2.0** (-87.50%) | **204.0** | **2.0** (-99.02%) | **2004.0** | **2.0** (-99.90%) |
| **3. 稳态每轮 `/fapi/v1/userTrades` 次数** | **10.0** | **1.0** (-90.00%) | **134.0** | **1.0** (-99.25%) | **1334.0** | **1.0** (-99.93%) |
| **4. 稳态单轮恢复耗时（离线本地）** | **0.1504 s** | **0.0403 s** (3.73x) | **1.1870 s** | **0.0523 s** (22.68x) | **12.7215 s** | **0.2486 s** (51.17x) |
| **4b. 按 F09 真实网络 RTT (~1.25s/GET) 折算单轮网络耗时** | ~45.0 s | ~10.0 s | ~435.0 s | ~10.0 s | ~4185.0 s (~70 min) | ~10.0 s |
| **5. 交易准入账户快照新鲜度** | `0.0s <= 5.0s` (`True`) | `0.0s <= 5.0s` (`True`) | `0.0s <= 5.0s` (`True`) | `0.0s <= 5.0s` (`True`) | `0.0s <= 5.0s` (`True`) | `0.0s <= 5.0s` (`True`) |
| **6. 经济事件总数（协议事件 / 控制器订单事件 / 成交事件）** | `10 / 17 / 6` | `10 / 17 / 6` | `104 / 205 / 68` | `104 / 205 / 68` | `1004 / 2005 / 668` | `1004 / 2005 / 668` |
| **7. 成交累计数量 (`total_filled_quantity`)** | `5.0` | `5.0` | `59.5` | `59.5` | `584.5` | `584.5` |
| **8. 手续费累计金额 (`total_fee_usdt`)** | `0.5000` | `0.5000` | `5.9500` | `5.9500` | `58.4500` | `58.4500` |
| **9. 持仓 / 钱包 / 权益 (`position / wallet / equity`)** | `3.0 / 499999.5000 / 499999.5000` | `3.0 / 499999.5000 / 499999.5000` | `1.5 / 499994.0500 / 499994.0500` | `1.5 / 499994.0500 / 499994.0500` | `1.5 / 499941.5500 / 499941.5500` | `1.5 / 499941.5500 / 499941.5500` |
| **10. UNKNOWN 恢复结果（状态 / POST 次数 / 恢复 GET 数）** | `SUBMITTED / post=1 / GET=38` | `SUBMITTED / post=1 / GET=9` | `SUBMITTED / post=1 / GET=350` | `SUBMITTED / post=1 / GET=9` | `SUBMITTED / post=1 / GET=3350` | `SUBMITTED / post=1 / GET=9` |
| **11. 独立进程重启后的幂等性（状态 / 重启 GET 数）** | `idempotent=True / GET=40` | `idempotent=True / GET=11` | `idempotent=True / GET=352` | `idempotent=True / GET=11` | `idempotent=True / GET=3352` | `idempotent=True / GET=11` |
| **12. 历史完整性审计覆盖与单次审计开销** | 无独立分层（每轮全扫） | 单次审计 `10 GET`（`5 order + 5 userTrades`），重叠窗口 `50 trades`，2 次审计覆盖全量 | 无独立分层（每轮全扫） | 单次审计 `10 GET`（`5 order + 5 userTrades`），重叠窗口 `50 trades`，20 次审计滚动覆盖全量 | 无独立分层（每轮全扫） | 单次审计 `10 GET`（`5 order + 5 userTrades`），重叠窗口 `50 trades`，200 次审计滚动覆盖全量 |

### 4.2 实时轮询成本 vs. 周期性历史审计成本对比

| 工作层级 | 触发频率 | 每次请求构成（1000 笔历史归档 + 2 笔活动挂单，其中 1 笔部分成交） | 请求复杂度 |
| --- | --- | --- | --- |
| **实时交易准入恢复（Tier-1）** | 每一轮实时轮询（或每次下单前准入） | `5` 次账户读集（`account`, `balance`, `positionRisk`, `openOrders`, `leverageBracket`）+ `2` 次活动订单 `/fapi/v1/order` + `1` 次部分成交活动订单 `/fapi/v1/userTrades` = **8 次 GET** | $O(A + U + I)$，与已归档历史订单总数 $N_{\text{archived}}$ **完全无关（$O(1)$）** |
| **异常驱动增量审计（Tier-1 Reactive）** | 仅当账户钱包/持仓基线发生实时层未解释变动时立即触发 | 在 Tier-1 的 `8` 次 GET 基础上，增加 `1` 次增量 `/fapi/v1/userTrades(fromId=...)` + 受影响订单的 `/fapi/v1/order` + `/fapi/v1/userTrades(orderId=...)` + `5` 笔滚动抽检 = **17 次 GET**（耗时 `0.738s`，而原实现需 `3352 次 GET`） | $O(\Delta_{\text{changed}} + B)$ |
| **周期性历史账本审计（Tier-2 Scheduled）** | 默认每 12 轮实时恢复或每 300 秒触发 1 次 | `1` 次增量 `/fapi/v1/userTrades(fromId=...)`（重叠窗口 50 条）+ `5` 笔滚动抽检已归档订单（`5` 次 `/fapi/v1/order` + 对应的 `/fapi/v1/userTrades`）= **10 次 GET**（折合每轮实时恢复均摊仅 **~0.83 次 GET**） | $O(B)$，其中 $B = \text{rolling\_order\_batch\_size} = 5$ |

---

## 五、13 类故障与边界场景回归验证

在 `tests/integrations/test_demo_incremental_recovery_f12.py` 中新增了 16 个确定性回归测试，完整覆盖全部 13 类故障场景与规模不变式：

| 故障/验证场景 | 对应测试用例 | 验证结果 |
| --- | --- | --- |
| 1. `POST` ACK 丢失 + `GET /order` 暂时返回 `-2013` 未找到 | `test_f12_post_ack_lost_and_transient_2013_never_reposts` | **PASS**（保持 `UNKNOWN`，阻止第二次 `POST`，后续 `GET` 恢复后精确收敛且 `post_count == 1`） |
| 2. 订单已终态但经济事实未完整（仅 `WRITE_ACK` / WS 或缺 `userTrades` 手续费） | `test_f12_terminal_order_with_incomplete_economics_cannot_archive` | **PASS**（归类为 `ECONOMIC_INCOMPLETE`，拒绝归档，直至权威 `GET` 补齐 `USDT` 手续费并满足确认次数与驻留窗口） |
| 3. `GET /userTrades` 分页、重叠窗口去重与冲突检测 | `test_f12_user_trades_pagination_overlap_and_late_fill_audit` | **PASS**（多页 `fromId` 自动翻页、重叠区幂等去重、7 天以上时间跨度自动切窗、同 ID 负载冲突/页内重复立即抛出 `DataQualityError`） |
| 4. 延迟成交（Late Fill）与迟到手续费修正（Late Fee Revision） | `test_f12_user_trades_pagination_overlap_and_late_fill_audit` | **PASS**（已归档订单发生迟到成交或手续费更新时自动反归档，补录 `OrderEvent` 并更新累计成交与累计手续费） |
| 5. 查询时钟回拨（Clock Rollback） | `test_f12_clock_rollback_blocks_archival_until_monotonic_recovery` | **PASS**（检测到时钟回拨时 `clock_rollback_count` 递增，阻断归档并维持 `ECONOMIC_INCOMPLETE`，直至时钟恢复单调并重新完成驻留确认） |
| 6. 中途崩溃 / 恢复状态写入中断（Atomic Crash Consistency） | `test_f12_mid_audit_crash_does_not_advance_watermark_before_events` | **PASS**（在 `OrderEvent` 写入前注入崩溃异常，验证 `trades:<SYMBOL>` 水位绝不提前推进，恢复后重新审计完整入账且零漏账） |
| 7. 独立进程重启幂等性与稳态 $O(1)$ 查询保持 | `test_f12_process_restart_preserves_archived_skip_and_idempotency` | **PASS**（重启后已归档终态订单保持归档跳过，仅重核活动订单，且不产生重复 `recovery:...` 事件） |
| 8. 历史审计发现持久记录与交易所不一致（留存期内 `-2013` vs 3 天/90 天留存期外正常清理） | `test_f12_archived_order_retention_pruned_vs_in_window_2013` | **PASS**（留存期外 `-2013` 记录 `pruned_by_exchange_retention` 保持归档；留存期内 `-2013` 立即反归档为 `UNKNOWN` 并使控制器进入 `RECONCILIATION_REQUIRED`） |
| 9. 历史审计发现已归档订单成交量倒退/篡改 + 账户活动挂单与本地归档状态不匹配 | `test_f12_history_audit_persistent_mismatch_and_open_order_resurrection` | **PASS**（成交量倒退立即触发 `DataQualityError` 并标记 `UNKNOWN`；本地已归档订单若出现在交易所 `openOrders` 中立即在 Tier-1 反归档修复） |
| 10. F10 独立资金费（`GET /fapi/v1/income`）增量恢复与水位幂等 | `test_f12_independent_funding_fee_incremental_recovery_f10_boundary` | **PASS**（分页与重叠去重拉取 `FUNDING_FEE`，崩溃不推进 `last_tran_id`，且绝不混入订单 `cumulative_fee`） |
| 11. F11 活动 `reduce-only SELL` 对账优先级 | `test_f12_reduce_only_sell_priority_preserved_f11_boundary` | **PASS**（活动 `reduce-only SELL` 订单始终排在 `ACTIVE` 对账队列首位） |
| 12. 超过 90 天离线且账户发生未解释变动的完整性边界保护 | `test_f12_90_day_retention_gap_with_unexplained_account_change_fails_closed` | **PASS**（拒绝静默漏账，标记 `coverage_complete = False` 并抛出 `DataQualityError` 阻断准入） |
| 13. 6 / 100 / 1000 历史订单规模下的请求复杂度与账本一致性回归 | `test_f12_scale_6_100_1000_orders[6]`, `[100]`, `[1000]` | **PASS**（三种规模下稳态每轮均为 `8 GET`、`/fapi/v1/order = 2`、`/fapi/v1/userTrades = 1`，账本 100% 一致） |

此外，仓库现有的全部相关集成测试套件（共 **402 个测试用例**，涵盖 `test_demo_reconciliation_m14.py`、`test_binance_futures_trading.py`、`test_binance_demo_execution.py`、`test_binance_demo_network.py`、`test_binance_demo_fill_closed_loop.py`、`test_binance_demo_fill_faults.py`、`test_demo_receipts_m19.py`、`test_demo_get_recovery.py`、`test_binance_demo_grid_network.py`、`test_demo_grid_supervisor.py`、`test_demo_execute_systemd.py`、`test_demo_continuous_campaign.py`、`test_demo_constructor_clock.py`、`test_demo_cloud_runtime.py`、`test_demo_observability.py`）全部通过。

---

## 六、交付必答问题汇总

### 1. 原来的 $O(N)$ 恢复开销来自哪些接口？
来自 `FuturesAdapter._reconcile()` 对 `futures_orders` 表中**全部历史订单**逐笔调用 `GET /fapi/v1/order`，以及 `query_order()` 对每笔 `executedQty > 0` 的历史订单再次调用 `GET /fapi/v1/userTrades?orderId=...`；同时 `FuturesPaperGateway.recover()` 在单轮恢复中先后调用了 **2 次** `adapter.reconcile()`（Pass 1 预同步 + Pass 2 `controller.reconcile()`），导致每笔有成交的历史终态订单在每轮恢复中消耗 `2 次 /fapi/v1/order + 2 次 /fapi/v1/userTrades`，每笔无成交的历史撤单消耗 `2 次 /fapi/v1/order`。

### 2. 优化后每轮实际查询哪些订单？
在实时交易准入恢复（Tier-1）中，每轮仅查询：
1. **5 个实时账户读集接口**（`account`、`balance`、`positionRisk`、`openOrders`、`leverageBracket`），且在 `gateway.recover()` 的 5 秒窗口内复用同一权威快照，不再双遍请求；
2. **`UNKNOWN` / 写入不确定订单**（`reconcile_required == True` 或状态未知）；
3. **`ACTIVE` 活动挂单**（`NEW`、`PARTIALLY_FILLED`、`PENDING_CANCEL`，其中 `reduce-only SELL` 优先）；
4. **`ECONOMIC_INCOMPLETE` 经济信息尚不完整的终态订单**（尚未经过至少 2 次权威 `GET` 确认、成交明细或 `USDT` 手续费未对齐、或尚未渡过 60s/120s 终态驻留窗口的订单）；
5. **异常唤醒订单**：若实时 `openOrders` 中意外包含本地已归档订单，或实时账户余额/持仓基线发生未解释变动触发增量 `userTrades` 扫描发现归档订单变动，仅针对该变动订单唤醒查询。

### 3. 已确认终态如何归档？
订单必须在 `futures_order_recovery` 表中同时满足以下 6 项条件才会被标记为 `archived = 1`（`ARCHIVED_TERMINAL`）：
1. 订单状态为交易所权威终态（`FILLED`、`CANCELED`、`EXPIRED`、`REJECTED`）且 `reconcile_required == 0`；
2. 最后确认来源为权威 `GET`（`last_authority_source == 'GET'`，排除仅 `WRITE_ACK` 或仅 WebSocket 推送）；
3. 成交与手续费明细已完整对齐（`trades_reconciled == 1`：当 `executedQty > 0` 时，`/fapi/v1/userTrades` 累计成交量精确等于 `executedQty`，且所有成交手续费均为有效 `USDT` 金额）；
4. 连续权威 `GET` 观测到相同终态、成交量与手续费的次数 `terminal_confirmations >= 2`；
5. `clock_rollback_count == 0`（无未恢复的时钟回拨）；
6. 自首次权威 `GET` 观测到该终态起已渡过驻留窗口（`FILLED` / `REJECTED` $\ge 60\text{s}$；`CANCELED` / `EXPIRED` $\ge 120\text{s}$ 以覆盖撤单与成交竞态）。

### 4. 迟到成交和手续费如何发现？
通过两层互补机制发现并入账：
1. **实时层账户基线突变检测（毫秒～秒级响应）**：每轮 Tier-1 恢复将权威账户快照的 `wallet_balance` 和 `positions` 与 `futures_recovery_state['account_baseline']` 对比。由于任何迟到成交或手续费修正必然改变账户钱包余额或持仓，一旦发现当前 Tier-1 活动订单成交无法解释该账户变化，**立即在当轮触发 `audit_history()`**，通过 `GET /fapi/v1/userTrades(fromId=...)` 捕获迟到成交或手续费变更，将对应历史订单反归档并生成 `OrderEvent` 入账。
2. **历史层周期性重叠窗口扫描与滚动抽检**：`audit_if_due()` 周期性以 `fromId = max(1, last_trade_id - 50 + 1)` 重叠回扫最近 50 笔成交（检验是否有新增 `tradeId` 或既有 `tradeId` 的 `qty`/`price`/`commission` 变动），同时按 `rolling_order_batch_size = 5` 滚动抽检已归档订单的 `/fapi/v1/order`。

### 5. `UNKNOWN` 如何保证不重复写入？
1. 当 `POST /fapi/v1/order` 发生超时、断网或异常时，`submit_order()` 在同一事务内将订单持久化为 `confirmed_status = UNKNOWN, reconcile_required = 1`，并固化唯一的 `client_order_id`；
2. 只要数据库中存在任何 `UNKNOWN` 或 `reconcile_required == 1` 的订单（通过 `futures_order_recovery` 索引查询），`FuturesAdapter.submit_order()` 与 `TradingController` 均直接拒绝提交任何新订单；
3. 恢复路径对 `UNKNOWN` 订单仅调用只读的 `GET /fapi/v1/order(origClientOrderId=...)`，绝不重新调用 `POST`；
4. 若某次 `GET /fapi/v1/order` 暂时返回 `-2013`（订单尚未在交易所查询节点可见），`query_order()` 捕获异常并继续保持该订单为 `UNKNOWN`（`reconcile_required = 1`），绝不自行判定为“无此订单”或“已撤单”。在基准测试与故障回归中，所有 `UNKNOWN` 场景的 `POST` 总次数严格恒为 `1`。

### 6. 对 1000 笔历史订单的性能改善是多少？
在 1000 笔历史终态订单（含 667 笔历史成交单、333 笔历史撤单）+ 2 笔活动挂单场景下：
- **稳态每轮总 `GET` 请求数**：从 **3348.0 次** 降至 **8.0 次**（**减少 99.76%**）；
- **稳态每轮 `/fapi/v1/order` 请求数**：从 **2004.0 次** 降至 **2.0 次**（**减少 99.90%**）；
- **稳态每轮 `/fapi/v1/userTrades` 请求数**：从 **1334.0 次** 降至 **1.0 次**（**减少 99.93%**）；
- **稳态单轮恢复耗时**：离线本地执行从 **12.7215 秒** 降至 **0.2486 秒**（**提速 51.17 倍**）；若按 F09 实测网络 RTT（~1.25 秒/GET）折算，单轮网络请求耗时从 **~4185 秒（约 70 分钟）** 降至 **~10 秒**；
- **周期性历史审计开销**：单次计划审计仅需 **10 次 GET**（耗时 `0.1294 秒`），按默认 12 轮触发 1 次折算，每轮均摊仅增加 **0.83 次 GET**。

### 7. 是否仍存在不能证明历史完整性的交易所接口边界？
存在以下三项由 Binance USD-M Futures 官方 REST 接口自身限制决定的物理边界，已在代码中全部实现 **Fail-Closed（拒绝静默漏账）** 处理：
1. **超过 90 天的历史离线断档**：Binance 标准 REST 接口（`/fapi/v1/order`、`/fapi/v1/userTrades`、`/fapi/v1/income`）仅保留最近 90 天数据。若系统停机超过 90 天且期间账户持仓或余额发生变动，REST 接口无法取回 90 天前的成交明细。此时 `audit_history()` 会将 `coverage_complete` 置为 `False` 并抛出 `DataQualityError`，使系统保持可恢复的拒绝交易状态（`RECONCILIATION_REQUIRED`）。
2. **超过 3 天的无成交撤单/过期单清理**：Binance 会在 3 天后清理 `executedQty == 0` 的 `CANCELED` / `EXPIRED` 订单（查询返回 `-2013`）。因此这类订单必须在 3 天留存期内完成至少 2 次权威 `GET` 确认并归档；若一笔 `UNKNOWN` 订单在停机超过 3 天后才首次恢复且返回 `-2013`，系统无法仅凭 `-2013` 证明其“从未成交”还是“从未上链”，因此继续保持 `UNKNOWN` 拒绝交易，直到结合 `userTrades` 与账户基线核实。
3. **同一毫秒内超过 1000 笔成交的极端时间窗分页**：当使用 `startTime..endTime`（而非 `fromId`）首次引导历史成交且同一毫秒内成交超过 `limit = 1000` 时，基于时间戳的游标无法在同毫秒内切分。因此日常增量恢复全部采用单调递增的 **`fromId` 游标**（不受同毫秒条数限制），且分页器内置了游标停滞与 `max_pages` 截断检测，一旦触及上限立即 Fail-Closed 报错。
