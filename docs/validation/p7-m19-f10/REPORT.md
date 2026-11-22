# P7-M19-F10 Binance Demo 资金费率权威记账与账户对账闭环

## 一、背景与基线

开发基线为 `origin/main` 的 `ef9d50734f35338b40c6f4a7413f6763be7e9b53`，在独立 worktree（分支 `feat/p7-m19-f10-funding-ledger`）完成实现与离线回归验证。

在 F09 真实 Binance USD-M Demo（`NVDAUSDT`）执行观测中：

- 订单 `194372514`（`BUY 0.04 NVDA @ 129.59 USDT`）已 `FILLED`，两笔 Maker 成交手续费均为 `0 USDT`。
- 持仓期间发生官方真实资金费率结算 `FUNDING_FEE = -0.00469722 USDT`，交易所流水 `tranId = 1768092951478927810`。
- 官方钱包余额由 `4999.97114760 USDT` 降至 `4999.96645038 USDT`。
- 因资金费率未进入 FactorLab 权威经济账本与 FIFO 归因，`reconciliation_residual = -0.0046972200 USDT`，且 `demo_fill_acceptance.py` 存在 `not funding_events` 的临时阻塞条件，导致 `DEMO_ACCOUNT_RECONCILED = false`。

F10 通过把官方已确认 `FUNDING_FEE` 纳入协议账本、`PaperStore` 权威经济账本、FIFO 归因及独立 GET 验收，在不放宽任何对账容差的前提下消除该真实经济账本缺口。

---

## 二、账本事件结构与幂等规则（F10-01）

### 1. 权威资金费事件模型 `FundingEvent`

在 `src/factorlab/trading/contracts.py` 新增独立的 `FundingEvent` 不可变事件契约（与 `OrderEvent` 并列，绝不伪装成订单成交、交易手续费或虚构仓位交易）：

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `market` | `Market` | 交易市场标识（`Market.BINANCE_USDM`） |
| `account_id` | `str` | 绑定的权威账户身份 |
| `instrument_id` | `str` | 合约标的（如 `NVDAUSDT`） |
| `tran_id` | `str` | 规范化十进制正整数交易所流水号（来自官方 `tranId`） |
| `income_type` | `str` | 固定为 `'FUNDING_FEE'` |
| `asset` | `str` | 结算资产（当前仅准入 `'USDT'`） |
| `amount` | `Decimal` | 有符号十进制金额（负值为资金费支出，正值为资金费收入，非零有限值） |
| `event_time` | `datetime` | 交易所毫秒时间戳对应的 UTC 时间 |
| `source` | `str` | 固定为 `'exchange_rest_income'` |
| `event_id` | `str` | 确定性事件主键：`funding:FUNDING_FEE:<tran_id>` |
| `metadata` | `dict[str, Any]` | 原始交易所回包（`raw`）、`event_time_ms`、`info`、`trade_id` 及观测时间 `observed_at` |

### 2. 双层持久化与幂等冲突检测

1. **协议适配器账本（`FuturesAdapter` / SQLite）**：
   - `futures_funding(event_id TEXT PRIMARY KEY, tran_id TEXT UNIQUE NOT NULL, symbol TEXT NOT NULL, asset TEXT NOT NULL, income_type TEXT NOT NULL, amount TEXT NOT NULL, event_time_ms INTEGER NOT NULL, account_id TEXT NOT NULL, payload TEXT NOT NULL)`
   - `futures_income_coverage(scope TEXT PRIMARY KEY, covered_from_ms INTEGER NOT NULL, covered_through_ms INTEGER NOT NULL, complete INTEGER NOT NULL, updated_at TEXT NOT NULL, detail TEXT NOT NULL)`
2. **控制器权威账本（`PaperStore` / `PaperState`）**：
   - `PaperStore._TYPES` 注册 `FundingEvent`，事件写入 `events` 表并持久化到 `snapshot`。
   - `PaperState` 新增 `funding: dict[str, Decimal]`（按资产累计已实现资金费）与 `funding_events: dict[str, FundingEvent]`（按 `event_id` 索引已入账事件），默认工厂兼容 F10 之前的历史快照升级。
3. **幂等与冲突判定（`funding_identity_key`）**：
   - 身份主键：`event_id = funding:FUNDING_FEE:<tran_id>`。
   - **重复 GET / 跨进程恢复 / 重启重放**：当 `event_id`（或 `tran_id`）已存在且 `(market, account_id, instrument_id, tran_id, income_type, asset, amount, event_time)` 完全一致时，`PaperStore.record_event` 返回 `False`，`PaperBroker.apply_funding` 不重复变动 `cash[asset]` 与 `funding[asset]`。
   - **同身份内容冲突**：当相同 `tran_id` / `event_id` 对应的金额（`amount`）、资产（`asset`）、合约（`symbol`）或事件时间（`event_time`）存在任意不一致时，立即抛出 `DataQualityError('funding event identity conflict')`，拒绝静默覆盖并断开适配器对账状态。

---

## 三、官方 GET 恢复接线与覆盖范围（F10-02）

### 1. 分段与分页查询 `FuturesAdapter.income_window`

- 仅使用官方签名 `GET /fapi/v1/income`（`incomeType='FUNDING_FEE'`）及既有网络/时钟错误处理，**零写请求（0 POST / 0 DELETE）**。
- **不以单次 `< 1000` 条无窗口返回认定完整历史覆盖**：所有覆盖推进均必须显式携带 `startTime` 与 `endTime`。
- **时间窗口分段**：当 `[start_ms, end_ms]` 跨度超过 `INCOME_MAX_WINDOW_MS`（7 天）时，自动切分为连续子窗口逐段拉取。
- **确定性分页与边界去重**：
  - 单页返回数量 `< limit`（默认 `1000`）时当前子窗口完成。
  - 单页返回数量 `== limit` 时，以该页最大毫秒时间戳 `max_ts` 作为下一页 `startTime = max_ts` 继续拉取，并通过已收集的 `tran_id` 集合对 `max_ts` 毫秒边界上的记录严格去重。
  - 若整页全部落在同一毫秒（`max_ts == cursor` 且无新 `tran_id`）、翻页丢失边界锚点或超过最大页数 `max_pages`，立即抛出 `DataQualityError`，绝不标记 `complete=True`。
- **严格回包与资产校验**：对非列表回包、缺失/非法 `tranId`、非法 `time`、区间外时间戳、非有限/零金额、非 `USDT` 资产，均抛出 `DataQualityError`。

### 2. 历史补录与增量重叠衔接 `FuturesAdapter.sync_funding`

- **历史起点推导**：综合显式 `start_time`、账本最早订单/回执/成交/资金费活动时间 `_earliest_activity_ms()` 以及已持久化的 `covered_from_ms`，取最小时间戳作为 `target_origin_ms`。若发现更早的历史活动起点，自动向前补齐 `[target_origin_ms, covered_from_ms]`。
- **增量重叠回看**：在已有完整覆盖 `[covered_from_ms, covered_through_ms]` 基础上，每次增量同步从 `max(target_origin_ms, covered_through_ms - INCOME_OVERLAP_MS)`（默认回看 1 小时）拉取至当前 `end_ms`，确保迟到入账的资金费流水可被无损补录，且已入账事件通过 `tran_id` 幂等去重。
- **空同步水位持久化**：成功完整读取后，即使没有新增事件，只要 `covered_through_ms` 推进，也通过已有事务与覆盖记录 UPSERT 持久化；新 Session 从最近一次已确认水位继续增量重叠扫描。
- **故障保持**：当 `/fapi/v1/income` 发生超时或网络异常时，保留既有 SQLite 账本与 `PaperState` 不变，`adapter.reconciled` 置为 `False`，待后续 `recover()` 重试恢复。

---

## 四、经济账本、FIFO 归因与风险约束一致性（F10-03）

### 1. 钱包、权益与风险约束无双重扣减

- 官方 `/fapi/v3/account` 返回的 `walletBalance` 已经包含已结算的 `FUNDING_FEE`。
- 本地 `PaperBroker.apply_funding` 在重放 `FundingEvent` 时同步更新 `state.cash['USDT'] += event.amount` 与 `state.funding['USDT'] += event.amount`，从而使本地账本现金与官方 `walletBalance` 重新对齐（由 `DemoExecutionGateway._cash_consistent` 与 `_funding_consistent` 共同校验）。
- 账户总权益与 NAV：
  $$\text{final\_wallet} = \text{opening\_cash} + \text{realized\_trade\_pnl} - \text{actual\_fees} + \text{realized\_funding}$$
  $$\text{final\_equity} = \text{final\_wallet} + \text{unrealized\_pnl}, \quad \text{final\_nav} = \frac{\text{final\_equity}}{\text{opening\_cash}}$$
- 持续授权（Continuous Authorization）亏损上限仍基于权威账户权益事实判定，不会因本地账本记录 `FundingEvent` 而产生二次扣减。

### 2. 交易手续费与资金费独立、FIFO 持仓成本零污染

在 `src/factorlab/experiments/grid_fifo.py`（`attribution` 与 `attribute_events`）中：

- 资金费不进入 `OrderFill` 或 FIFO `Lot` / `Match`，**不改变**任何订单成交数量、成交价格、持仓均价（`entry_price`）、剩余持仓成本（`residual_cost`）、换手额（`turnover`）或订单累计手续费（`actual_fees`）。
- 各项损益指标严格分离并满足恒等式：
  - `realized_trade_pnl = normal_sell + break_even + stop_loss`（纯成交已实现损益）
  - `realized_trade_net = realized_trade_pnl - actual_fees`（扣除交易手续费后的已实现成交净损益）
  - `trading_net = realized_trade_pnl + residual - actual_fees`（不含资金费的交易净损益）
  - `realized_funding = sum(event.amount for event in funding_events)`（独立统计的已实现资金费损益）
  - `strategy_net_pnl = economic_net = trading_net + realized_funding`（含资金费的策略总经济净损益）
  - `wallet_change = final_wallet - opening_cash = realized_trade_pnl - actual_fees + realized_funding`
  - `reconciliation_residual = reported_equity - final_equity`

---

## 五、独立验收修复与 F09 残差归零验证（F10-04）

### 1. `demo_fill_acceptance.py` 验收逻辑升级

- 移除原先“`/fapi/v1/income` 存在任意 `FUNDING_FEE` 即令 `DEMO_ACCOUNT_RECONCILED = False`”的临时限制。
- 使用 `adapter.income_window(start_time=start_ms, end_time=end_ms)` 完整拉取验收窗口内的交易所流水，按 `incomeType` 分类核对：
  - `COMMISSION`：与目标订单已确认成交手续费核对（`DEMO_FEE_RECONCILED`）。
  - `FUNDING_FEE`：逐笔核对交易所流水与本地 `adapter.funding_events()` / `controller.store.funding_events()` 的 `(tran_id, symbol, asset, amount, time)` 是否完全一致、无未入账流水（`unbooked_events == []`）、且 `funding_coverage` 完整覆盖验收窗口（`DEMO_FUNDING_RECONCILED`）。
  - 其他不支持的 `incomeType`（如 `TRANSFER`、`INSURANCE_CLEAR`、`COMMISSION_REBATE` 等）：继续放入 `unaccounted_income` 并阻断 `DEMO_ACCOUNT_RECONCILED`。
- `DEMO_ACCOUNT_RECONCILED` 要求 `DEMO_FEE_RECONCILED`、`DEMO_FUNDING_RECONCILED`、`DEMO_POSITION_RECONCILED`、`reconciliation_residual == 0`、`reported_equity_difference == 0` 且 `unaccounted_income == []` 同时成立。

### 2. F09 确定性离线回归验证

在 `tests/integrations/test_demo_funding_ledger_f10.py::test_f09_deterministic_funding_residual_reconciliation` 中精确复现 F09 账户事实：

| 指标 | F10 修复前（未入账） | F10 修复后（权威入账） |
| --- | --- | --- |
| `opening_wallet` | `4999.97114760 USDT` | `4999.97114760 USDT` |
| 订单 `194372514` 成交 | `BUY 0.04 NVDA @ 129.59 USDT`（2 笔 Maker 成交） | `BUY 0.04 NVDA @ 129.59 USDT`（2 笔 Maker 成交） |
| `actual_fees` | `0 USDT` | `0 USDT` |
| 交易所 `FUNDING_FEE` (`1768092951478927810`) | `-0.00469722 USDT`（未入账） | `-0.00469722 USDT`（已入账） |
| `realized_funding` | `0 USDT` | `-0.00469722 USDT` |
| `wallet_change` | `0 USDT` | `-0.00469722 USDT` |
| `final_wallet` / 官方 `walletBalance` | `4999.97114760` vs `4999.96645038 USDT` | `4999.96645038 USDT`（精确一致） |
| `reconciliation_residual` | `-0.00469722 USDT` | `0 USDT` |
| `DEMO_FEE_RECONCILED` | `true` | `true` |
| `DEMO_FUNDING_RECONCILED` | `false` | `true` |
| `DEMO_ACCOUNT_RECONCILED` | `false` | `true` |
| 独立进程重启对比 `DEMO_RESTART_RECOVERY_VERIFIED` | — | `true` |

---

## 六、受影响测试与离线回归结果

本机环境 `D:\Environments\factorlab-py314\Scripts\python.exe`（Python 3.14.0, pytest 8.4.2），全部测试均使用离线响应与临时 SQLite：

1. **F10 专项离线回归（`tests/integrations/test_demo_funding_ledger_f10.py`）**：**27 passed**
   - 单笔负资金费入账（`test_single_negative_funding_booking`）
   - 正资金费收入入账（`test_positive_funding_income_booking`）
   - 相同 `tranId` 重复 GET 幂等（`test_repeated_get_same_tran_id_idempotent`）
   - 跨进程重启后不重复记账（`test_restart_and_cross_process_no_duplicate_funding`）
   - 迟到资金费补录与重启增量识别（`test_late_arriving_funding_backfill_and_restart_delta`）
   - 同一事件身份金额/资产/时间/标的冲突拒绝（`test_funding_identity_conflict_rejected_without_overwrite` x4）
   - 异常回包格式与不支持资产拒绝（`test_malformed_or_unsupported_asset_funding_rejected` x6）
   - 历史补录与当前增量查询衔接（`test_historical_backfill_bridges_to_incremental_sync`）
   - `income` 分页、同毫秒饱和页、超页上限、丢边界锚点及覆盖不足拒绝（`test_income_incomplete_pagination_and_insufficient_coverage`）
   - 暂时网络超时保留原状态并在重试后恢复（`test_transient_network_failure_preserves_state_and_retries`）
   - 资金费不污染成交手续费与 FIFO 持仓成本（`test_funding_does_not_pollute_fees_or_fifo_cost_basis`）
   - 钱包、权益、NAV 及风险亏损上限无双重扣减（`test_wallet_equity_nav_and_risk_no_double_deduction`）
   - 无资金费账户保持原行为（`test_zero_funding_account_preserves_behavior`）
   - 不支持的其他 `incomeType` 阻断账户级对账（`test_unsupported_income_types_block_account_reconciliation` x4）
   - F09 `-0.00469722 USDT` 确定性残差归零回归（`test_f09_deterministic_funding_residual_reconciliation`）
   - F10 前历史 SQLite 快照无损升级与资金费回填（`test_pre_f10_persisted_journals_upgrade_and_backfill_funding`）
2. **受影响核心与集成回归套件**：**511 passed**
   - `tests/test_grid_fifo.py`
   - `tests/trading/test_paper_control.py`
   - `tests/integrations/test_binance_futures_trading.py`
   - `tests/integrations/test_demo_reconciliation_m14.py`
   - `tests/integrations/test_binance_demo_execution.py`
   - `tests/integrations/test_binance_demo_network.py`
   - `tests/integrations/test_demo_get_recovery.py`
   - `tests/integrations/test_demo_get_recovery_window.py`
   - `tests/integrations/test_demo_receipts_m19.py`
   - `tests/integrations/test_binance_demo_fill_closed_loop.py`
   - `tests/integrations/test_binance_demo_runtime.py`
   - `tests/integrations/test_demo_continuous_auth.py`
   - `tests/integrations/test_demo_grid_supervisor.py`
   - `tests/integrations/test_demo_strategy_supervisor.py`
   - `tests/integrations/test_demo_execute_systemd.py`
   - `tests/integrations/test_demo_observability.py`
   - `tests/integrations/test_demo_Fault_injection.py`
   - `tests/integrations/test_demo_order_lifecycle.py`

---

## 七、工程验收结论与在线状态区分

- **工程验收状态**：`F10_ENGINEERING_ACCEPTED = true`
  - `FUNDING_FEE` 已作为独立权威经济事件持久化并重放。
  - 重复 GET、跨进程恢复与重启严格幂等，同身份内容冲突显式拒绝。
  - 订单成交手续费（`DEMO_FEE_RECONCILED`）与账户级资金费（`DEMO_FUNDING_RECONCILED`）独立统计、分别验收。
  - F09 已知 `-0.00469722 USDT` 资金费残差在确定性离线回归中精确归零。
  - 账户级对账不再因已完整入账的资金费而错误失败，未入账资金费与不支持的 `incomeType` 仍严格阻断。
  - 全部 538 项受影响离线回归测试通过。
- **官方账户在线复验区分**：
  - 本次交付严格遵循边界约束，**未修改本机原私有账本，未启动第二个 Execute Owner，未发送任何交易所写请求**。
  - 官方 Demo 账户在线最新状态的复验留待合并后由唯一授权的在线会话通过只读 GET 恢复与 `demo_fill_acceptance` 执行。

---

## 八、PR #225 Codex P2 修复：空同步覆盖水位（2026-11-22）

基线 HEAD：`66769a7191c6ef7f55d8de34cc3235efe1d10340`。原 Review：[Persist the coverage watermark when an empty sync advances it](https://github.com/Wind-Maker1001/FactorLab/pull/225#discussion_r4657676289)。

### 根因与修复

`_sync_funding()` 的 `need_db_write` 原先只考虑新事件、首次覆盖、不完整覆盖与覆盖起点扩展，遗漏了覆盖终点推进。成功空同步因此只更新 `_funding_coverage_cache`，重启后会从旧 SQLite 水位重复扫描。

在原判断中加入 `new_through_ms > coverage['covered_through_ms']`，继续复用 `self.store.transaction()` 与 `INSERT ... ON CONFLICT DO UPDATE`。事件校验与分页完成后才进入事务，事件插入与覆盖推进仍保持原子性；覆盖水位不回退，同一水位没有新增事件时不写库。金额、历史事件身份、持仓、手续费与交易所请求路径均未修改。

### 新增定向回归（6 项）

| 场景 | 证据 |
| --- | --- |
| A：成功空同步与 Session 重开 | 建立 16 天完整历史覆盖，时钟推进 10 分钟，`newly_recorded == ()`；直接读取 SQLite 验证水位推进，关闭并重开 DemoSession 后仍保留新水位；下一次 GET 仅扫描默认 1 小时重叠窗口。 |
| B：空闲账户连续进程重启 | 5 次独立 Python 子进程，各推进 6 天；重开前后读取 SQLite 水位，每次只需一个真实 7 天窗口内的 GET，扫描起点始终为上次持久水位减 1 小时。测试将 `INCOME_MAX_WINDOWS` 缩至 2，以较短模拟时间检验窗口预算，始终未触发上限。原有 1 笔资金费、订单事件、现金、持仓、钱包与平仓账户 NAV 保持不变。 |
| C：失败保持与恢复（3 项） | 分别注入网络 GET 超时、分页超过上限、非有限金额；SQLite 完整覆盖记录与资金费事件不变，对账关闭。恢复后重新覆盖旧水位后的未确认区间，3 笔待确认事件全部入账并推进水位。 |
| D：同水位幂等 | 同一终点重复同步以及请求更早终点时，`db.total_changes` 不变，完整 SQLite 覆盖记录不变，水位不回退。 |

新增测试均仅使用离线响应与临时 SQLite，并断言所有交易所请求为 GET。修复前先运行定向测试，A/B 在 SQLite 水位断言处复现失败（2 failed、4 passed）；应用修复后全部通过。

### 本次离线验证

环境：`D:\Environments\factorlab-py314\Scripts\python.exe`，Python 3.14.0 / pytest 8.4.2。

1. 新增定向测试：**6 passed, 27 deselected**（10.12 秒）。
2. 完整 F10 专项（原 27 项 + 新增 6 项）：**33 passed**（74.53 秒）。
3. 直接受影响的恢复、执行、对账与经济账本回归：**114 passed**（44.71 秒）。
4. `git diff --check` 通过。

可复现命令（在本 worktree 使用上述 Python 执行）：

```text
python -m pytest tests/integrations/test_demo_funding_ledger_f10.py -k "empty_sync_persists or idle_funding_watermark or failed_funding_sync_keeps or same_funding_watermark" -q --tb=short
python -m pytest tests/integrations/test_demo_funding_ledger_f10.py -q
python -m pytest tests/integrations/test_demo_get_recovery.py tests/integrations/test_demo_get_recovery_window.py tests/integrations/test_demo_reconciliation_m14.py tests/integrations/test_binance_demo_execution.py tests/integrations/test_binance_demo_fill_closed_loop.py tests/test_grid_fifo.py tests/trading/test_paper_control.py -q
```

本次合计覆盖 **147 项不同测试**，未重新运行上文基线交付的全部 538 项。未启动官方 Demo Execute，未访问或修改原台式机私有账本，未发送交易所写请求。新 HEAD 的托管 CI 与可信合并门禁状态另在原 PR Review 回复中记录，以 GitHub 对该提交的状态为准。
