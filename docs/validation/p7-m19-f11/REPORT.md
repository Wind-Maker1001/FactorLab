# P7-M19-F11 验证报告：BREAKOUT 持仓自动退出与平仓后 Grid 轮换

**状态：`F11_ENGINEERING_ACCEPTED`（离线工程验收通过；`NO LIVE MONEY`）**

所有自动化验证均在独立 worktree（`D:\Code\FactorLab-worktrees\p7-m19-f11-breakout-exit`，分支 `feat/p7-m19-f11-breakout-exit`，基线 `origin/main` `ef9d50734f35338b40c6f4a7413f6763be7e9b53`）中使用临时 SQLite、确定性时钟和离线 HTTP 模拟器完成。未驱动原官方 Binance Demo Execute，未产生真实网络 `POST`/`DELETE`，未篡改或重初始化私有账本。最终官方在线退出验收需在合并最新主线后于独占执行会话中统一开展，本报告不将离线通过冒充真实交易所成交。

---

## 一、根因复盘（F09 永久停滞问题）

F09 官方验收中，Grid 部署 `fl-d7345f8795464a65-g-00001`（区间 `[129.59, 132.21]`，`breakout='pause'`）已成交 `BUY 0.04 NVDA @ 129.59 USDT`，随后价格突破区间进入 `PAUSED / BREAKOUT`，交易所当前挂单为 `0`、`UNKNOWN=0`，账户持续授权与 Worker `desired` 均为 `RUNNING`。

原有行为形成死锁：
1. `GridExecution` 在 `breakout='pause'` 触发 `BREAKOUT` 后将状态锁死在 `PAUSED` 并撤销既有挂单，不再产生任何订单提案；`breakout='exit'` 虽存在 `EXITING` 语义，但输出的是 `OrderType.MARKET` 卖单，而 Binance Demo 网关与运行时明确只允许单向 `LIMIT GTC`。
2. `GridConsumer.quote()` 与 `GridConsumer.gate()` 仅在普通 `RUNNING` Grid 且存在新鲜闭合 Bar（`live_bar=True`）时才允许派单。
3. F08 的 `rotate_if_terminal()` 在检测到账户仍有持仓（`status['positions']` 非空）时严格拒绝轮换至下一代部署 `g-00002`。

结果：既有持仓既没有合法的 reduce-only 退出路径，又无法轮换新区间，导致仓位长期裸露于价格与资金费率风险中。

---

## 二、最小架构改动与目标状态机

遵循“停止策略开仓”与“既有持仓受控退出”解耦原则，不新建审批状态机、不绕过累计授权、不重写 Demo 网关或订单引擎，仅在既有 6 个模块进行最小必要扩展：

| 模块 | 最小变更 | 职责与不变量 |
| --- | --- | --- |
| `src/factorlab/strategies/grid_execution.py` | 新增外部命令 `propose_limit_exit`（`exit_limit`）与 `cancel_exit`（`exit_cancel`）、持久化追踪 `exit_orders` 与 `exit_cancellations`；退出单完全成交且仓位归零时落为 `EXITED / BREAKOUT`；排除 `exit_orders` 受普通 `order_ttl` 干扰 | 复用原 Grid 单调 `delivery`、命令日志与合法命名空间（`<deployment_id>-<seq:08d>`）；保持 `deployment()` 不可变（F09 既有持久化部署零迁移兼容）；严禁 `BUY` 建仓，保留原 `breakout='exit'` 的 `MARKET` 语义与 Demo 网关拒单契约 |
| `src/factorlab/integrations/binance_futures_trading/demo_grid_bridge.py` | 新增 `breakout_exit_status()`、`_fresh_exit_book()`、`_plan_limit_exit()` 与 `step_breakout_exit()`；重放 `exit_limit`/`exit_cancel` 命令；`withdraw_unsubmitted()` 同步检查 `futures_orders` 防止误撤已入协议库订单 | 每次最多执行一次写动作（单笔 `DELETE` 或单笔 `POST`）；以新鲜可信盘口 `bidPrice` 按 `tickSize` 向下取整定价；校验 `LOT_SIZE`/`PRICE_FILTER`/`MIN_NOTIONAL`/`MAX_QUANTITY`/`MAX_NOTIONAL`/累计预算/窗口预算；实现有界撤改单（`EXIT_REPRICE_SECONDS=180`、`EXIT_MAX_ATTEMPTS=5`、两阶段先撤后 GET 确认再重发、部分成交防碎仓保护、已消费 `DELETE` 防重复撤销） |
| `src/factorlab/integrations/binance_futures_trading/demo_market_feed.py` | 补齐 5 行可选消费钩子 `set_latest_book(raw)` 与 `maintain(raw)` | 将已通过 5 秒新鲜度校验的真实 `bookTicker`（含精确 `bidPrice`/`askPrice`）透传给 `GridConsumer`，无额外 HTTP GET；即便盘口时间戳未跳变也可推进退出生命周期 |
| `src/factorlab/integrations/binance_futures_trading/demo_grid_supervisor.py` | 扩展 `GridConsumer`（`gate()`、`before_poll()`、`_accept_bar()`、`quote()`、`maintain()`、`status()`）与 `GridSupervisorRuntime.health_snapshot()` | `BREAKOUT` 后持续进行 GET 恢复与窗口自动续期；仅在 `closed_by_breakout` 受控退出路径上解除对普通开仓 `live_bar` 的依赖，同时仍强制 `desired=='RUNNING'`、`authority()`、5 秒账户/盘口新鲜度与 `collection.check()`；在 `cloud-health.json` 的 `grid.breakout_exit` 中诚实输出退出阶段与阻断诊断 |
| `src/factorlab/integrations/binance_futures_trading/demo_runtime.py` | 强化 `rotate_if_terminal()` | 除原有官方 GET `positions`/`open_orders`/`unknown` 与未回执 `dispatches` 校验外，额外核验本地权威账本 `gateway.state.positions` 为空、所有历史订单均已达终态且 `grid.active` 中无已发往交易所的活动单，确保仅在完全平仓并落账后才生成下一代 Grid |
| `src/factorlab/integrations/binance_futures_trading/execution.py` | 抽出 `fresh_mark_price()` 与 `check_price_band()`，供撤单前预检与原 `_network_admission()` 共用 | 官方标记价仍须在五秒内；预检不缓存或授权后续 POST，实际提交继续取得新标记价并执行全部正式准入 |

### 业务状态映射

```
RUNNING
  → BREAKOUT_DETECTED / ENTRY_BLOCKED  (grid.closed_by_breakout=True，禁止 BUY；若有在途 BUY 先执行 DELETE)
  → EXIT_PENDING                       (reconciled=True, UNKNOWN=0, inventory>0, open_orders=0，准备提交 reduce-only SELL LIMIT GTC)
  → EXIT_WORKING                       (活动 reduce-only SELL 在交易所挂单监测，有界冷却 180s + 最多 5 次尝试 + 防碎仓保护)
  → EXIT_RECONCILING                   (POST/DELETE 后或 UNKNOWN/write_uncertain/断网恢复期间，严禁发替代单，强制先 GET 对账)
  → EXIT_BLOCKED                       (用户 PAUSED/STOPPED/HALTED，或规则/精度/价格带/预算不满足时显式阻断，保留原仓位，不伪造平仓)
  → FLAT_CONFIRMED                     (官方 GET 确认持仓=0、挂单=0、UNKNOWN=0、所有订单终态、成交与手续费已落账)
  → ROTATE_GRID                        (复用 F08 rotate_if_terminal 轮换至下一代 g-0000N，继承原行情游标与累计预算，新 Bar 隔离)
```

---

## 三、离线回归与 C04/F08 集成验证

测试环境：Windows，Python 3.12.7，pytest 9.1.1，临时 SQLite + 离线 HTTP 模拟器（`tests/integrations/test_demo_breakout_exit_f11.py`）。

### 1. F11 专项回归（全部 16 项场景 + F09 原始场景确定性回归）

| # | 验收场景 | 测试用例 | 结果 |
| --- | --- | --- | --- |
| F09 | F09 原始场景确定性回归（`fl-d7345f8795464a65-g-00001` `[129.59, 132.21]`，`0.04 NVDA @ 129.59`，`PAUSED/BREAKOUT`，挂单 0，`UNKNOWN=0`，持续授权 `RUNNING`） | `test_f11_f09_original_scenario_deterministic_regression` | PASS |
| 1 | BREAKOUT + 空仓：先撤在途 BUY，延续原 F08 轮换，零冗余 SELL | `test_f11_01_breakout_flat_continues_f08_rotation` | PASS |
| 2–3 | BREAKOUT + 持仓：阻止 BUY，按新鲜 `bidPrice` 提交合法 reduce-only SELL LIMIT GTC，核验官方格式 POST 参数与 GET `/fapi/v1/order` 回执 | `test_f11_02_03_breakout_blocks_buy_submits_exit_and_verifies_official_get_receipt` | PASS |
| 4 | EXIT 未成交：持续监测不伪造成交，冷却期内不误撤，超冷却后先撤再 GET 确认再重发，受 `EXIT_MAX_ATTEMPTS` 上限严格约束 | `test_f11_04_unfilled_exit_monitors_and_reprices_bounded` | PASS |
| 5 | EXIT 部分成交：(a) 剩余量低于 `MIN_NOTIONAL` 时防碎仓保护不撤单直至完全成交；(b) 剩余量合法（`0.03 @ 168 = 5.04 >= 5`）撤改时仅按剩余 `0.03` 下单 | `test_f11_05a_exit_partial_fill_dust_protected_until_full_fill`、`test_f11_05b_exit_partial_fill_legal_remainder_submits_only_remaining_qty` | PASS |
| 6 | EXIT 完全成交：状态转 `EXITED / BREAKOUT`、`FLAT_CONFIRMED`，不再发额外卖单 | `test_f11_06_exit_full_fill_never_submits_extra_sell` | PASS |
| 7 | EXIT 取消与成交竞态：覆盖 `DELETE` 返回 HTTP 200 `FILLED`、HTTP 400 `-2011` 及超时丢包 3 种竞态，均经 GET 确认终态与手续费，零替代卖单，随后安全轮换 | `test_f11_07_exit_cancel_fill_race[http_200_filled / http_400_unknown_order / timeout]` | PASS |
| 8 | POST / DELETE 结果 `UNKNOWN`：`EXIT_RECONCILING` 严格禁止替代 POST，仅在 GET 恢复解决 `UNKNOWN` 后继续 | `test_f11_08_post_or_delete_unknown_forbids_replacement_until_get_recovery[POST / DELETE]` | PASS |
| 9–10 | `SIGKILL` 独立子进程恢复已有退出单 + 多次重启不重复 `POST` | `test_f11_09_10_sigkill_and_multiple_restarts_recover_exit_without_duplicate_post` | PASS |
| 11 | 控制面优先级：`USER_PAUSED`（`EXIT_BLOCKED:USER_PAUSED`，显式 `resume` 后恢复退出）、`USER_STOPPED`、`RUNTIME_STOPPED`、`ACCOUNT_HALTED`、`LOSS_HALT`（触及 `max_loss` 硬熔断）均零绕过写入 | `test_f11_11_stop_halt_paused_priority_during_exit`（5 组参数） | PASS |
| 12 | 退出期间 `orders=1` 窗口自动续期与同一连续授权下全局 `posts` / `submitted_notional` / `opening_equity` 连续累计 | `test_f11_12_window_auto_renewal_and_global_budget_continuity` | PASS |
| 13 | 分批成交后余额、持仓、手续费及 `demo_fill_acceptance.fifo_evidence` 对账残差严格为 `0` | `test_f11_13_balance_position_fee_and_fifo_consistency` | PASS |
| 14 | 平仓后自动轮换至下一代并继承原 `feed_deployment_id` 行情水位与 `activated_at` 新 Bar 隔离 | `test_f11_14_rotation_after_flat_inherits_watermark_and_bar_isolation` | PASS |
| 15 | 规则/精度/价格带/预算不满足安全阻断（`EXIT_MIN_NOTIONAL` 低价与小仓、`EXIT_MIN_QUANTITY`、`EXIT_PRICE_FILTER`、`EXIT_PRICE_BAND`、`EXIT_MAX_NOTIONAL`、`EXIT_GLOBAL_BUDGET_EXHAUSTED` 笔数与名义额），零 POST、保留仓位、不伪装平仓 | `test_f11_15_rule_precision_and_budget_violations_block_safely`（8 组参数） | PASS |
| 16 | C04 systemd Execute 路径与 `demo_cloud_runtime.health()`：退出中 `alive=True`、`grid.breakout_exit.phase='EXIT_WORKING'`、`execute_healthy=False`；平仓轮换后恢复 `RUNNING`、`execute_healthy=True`、`demo_execution_ready=True` | `test_f11_16_c04_execute_path_and_health_across_exit_to_rotation` | PASS |

### 2. 既有套测回归（C04 / F08 / Grid / MarketFeed / Runtime / Receipts）

- `tests/integrations/test_demo_breakout_exit_f11.py`：30 passed
- `tests/integrations/test_demo_grid_supervisor.py` + `test_demo_execute_systemd.py` + `test_binance_demo_grid_bridge.py` + `test_binance_demo_grid_network.py` + `test_binance_demo_fill_faults.py` + `test_demo_continuous_campaign.py` + `test_forward_observation.py`：176 passed（含最终代码单独复跑 106 passed）
- `tests/research/test_grid_execution.py` + `tests/integrations/test_demo_cloud_runtime.py`：67 passed
- `tests/integrations/test_demo_market_feed.py` + `tests/integrations/test_demo_market_collection.py`：56 passed
- `tests/integrations/test_binance_demo_runtime.py` + `tests/integrations/test_demo_get_recovery.py` + `tests/integrations/test_demo_get_recovery_window.py`：33 passed
- `tests/integrations/test_demo_receipts_m19.py` + `test_demo_reconciliation_m14.py` + `test_demo_observability.py` + `test_demo_constructor_clock.py`：94 passed
- 累计离线回归：**456 passed, 0 failed**

---

## 四、实际无法自动退出的交易所与 Demo 约束汇报（交付项 7）

基于 Binance USD-M Demo `NVDAUSDT` 现行合约规则（`stepSize=0.01`、`minQty=0.01`、`tickSize=0.01`、`MIN_NOTIONAL=5 USDT`、`PERCENT_PRICE=[0.97, 1.03]`）与现有 Demo 安全护栏（仅 `LIMIT GTC`、`MAX_QUANTITY=0.04`、`MAX_NOTIONAL=7 USDT`、`max_loss=1 USDT`、`max_open_orders=1`），以下客观约束会导致自动退出被安全阻断（输出明确 `EXIT_BLOCKED` / `HALTED` 诊断，绝不伪造平仓或放开 `BUY`）：

1. **`MIN_NOTIONAL = 5 USDT` 下限（低价或部分成交碎仓）**：
   - 对 `0.04 NVDA` 仓位，一旦实时买一价跌破 `125.00 USDT`（`0.04 × 124.99 = 4.9996 < 5.00 USDT`），任何新限价单均不满足 `MIN_NOTIONAL`，将触发 `EXIT_BLOCKED:EXIT_MIN_NOTIONAL`。
   - 若卖单部分成交后因交易所过期或外部撤单导致剩余 `0.01–0.03 NVDA`（例如 `0.02 × 130 = 2.60 < 5.00 USDT`），新卖单同样无法满足 `MIN_NOTIONAL`（F11 已对在途部分成交卖单实施防碎仓保护：只要挂单仍在交易所且剩余量拆撤后会低于 `MIN_NOTIONAL`，拒绝主动撤单，保留原挂单等待成交）。
2. **`MAX_NOTIONAL = 7 USDT` / `plan.max_margin = 7 USDT` 上限与反碎仓死区**：
   - 当 `0.04 NVDA` 价格涨破 `175.00 USDT`（`0.04 × 175.01 = 7.0004 > 7.00 USDT`）时，全量单笔卖出超出 Demo 单单名义额与保证金上限；在 `(175, 250)` USDT 价格区间内，即使拆为 `0.03 + 0.01`，剩余 `0.01` 仍低于 `5 USDT`，反碎仓校验会阻断为 `EXIT_BLOCKED:EXIT_MAX_NOTIONAL`。仅当价格回落至 `<= 175 USDT` 或达到 `>= 250 USDT`（可拆为两笔合法 `0.02`）且网关 `collateral <= max_margin` 时才可自动退出。
3. **`PERCENT_PRICE` 价格带偏离**：
   - 若盘口点差异常拉宽导致 `bidPrice < markPrice × multiplierDown`（如 `0.97 × markPrice`），限价退出会被网关 `Demo price band` 预检拦截并安全阻断为 `EXIT_BLOCKED:EXIT_PRICE_BAND`，待盘口回到保护带后自动重试。
4. **累计授权与 `max_loss = 1 USDT` 硬熔断**：
   - 对 F09 `0.04 NVDA @ 129.59 USDT` 仓位，若价格跌破约 `104.59 USDT`（亏损达 `1.00 USDT`），账户将触发全局风控硬熔断 `HALTED`；若持续授权的累计 `total_orders` 或 `total_notional` 耗尽，则触发 `EXIT_BLOCKED:EXIT_GLOBAL_BUDGET_EXHAUSTED`。F11 严格遵守不绕过 `HALTED` 与累计预算的原则。
5. **仅 `LIMIT GTC` 成交不确定性与有界撤改上限**：
   - Demo 网关禁止 `MARKET` 订单，限价卖单依赖真实买盘成交；单部署最多尝试 `EXIT_MAX_ATTEMPTS = 5` 次（冷却 `180s`），超限后触发 `EXIT_BLOCKED:EXIT_RETRY_LIMIT_EXCEEDED`（或保留最后一笔活动挂单继续监测），防止无界 `POST`/`DELETE` 消耗预算。若某笔 `DELETE` 预留已消耗但网络丢包导致原单仍在交易所，则标记 `EXIT_CANCEL_ALREADY_CONSUMED` 并继续监测原单，不重复发 `DELETE`。
6. **对 F09 真实持仓的可执行区间结论**：
   - 在当前 F09 持仓（`0.04 NVDA @ 129.59 USDT`）下，只要合并主线后重启独占官方 Execute 进程时实时 `NVDAUSDT` 买一价处于 `[125.00, 175.00]` USDT 且位于 `PERCENT_PRICE` 保护带内、累计授权与 `1 USDT` 亏损预算仍有余量，F11 即可无需人工审批地自动提交合法 reduce-only `SELL LIMIT GTC` 退出单，并在官方 GET 确认平仓落账后自动轮换至 `fl-d7345f8795464a65-g-00002`。

---

## 五、PR #224：Extended CI 四项回归与 Codex P2 修复（2026-11-22）

### F07 独立时钟回归

原 HEAD `f624deeeb0e247df1559764adb251b9b7d460332` 对应 [失败 CI](https://github.com/Wind-Maker1001/FactorLab/actions/runs/45010767011)：Python 3.13、3.14 的 shard 0 分别在 `test_forward_recovery_scheduling.py` 失败四项。本机先在未修改代码时复现 **4 failed, 7 passed**，调用链均为 `DemoMarketFeed._poll()` → `GridConsumer.set_latest_book()` → `DemoSession._fresh_book()` → `STALE_QUOTE`。

Feed 已根据自己的交易所时间范围验证五秒新鲜度、单调行情水位、标的和合法买卖价。新增钩子再次套用 Session 的另一时钟，错误拒绝 F07 的独立时钟模拟。现改为 `set_latest_book()` 复用 `_book()` 解析结构、标的、正数买卖价及非交叉盘口；Feed 代码和五秒时限均未改动。实际退出时 `_fresh_exit_book()` 仍检查五秒账户读集（墙钟及单调时钟）与盘口新鲜度、水位；最终 POST 仍经过 `DemoSession.submit_intent()`、原 controller、网关及 dispatch guard 的正式准入。

修复后原文件 **11 passed**，四项参数案例全部通过，包含 `fewer_gets[False/True]`、`full_round[ordinary/exchange_pending]`。保留原有 GET 优化断言，普通行情路径无新增 GET。

### P2：保留无法合法替代的工作 SELL

撤改候选满足原有冷却、尝试次数、数量、反碎仓及预算条件后，在写入 `exit_cancel` 和发出 DELETE 之前读取一次官方 `/fapi/v1/premiumIndex`。撤单前和正式 POST 共用网关 `check_price_band()`，使用当前规则 `PERCENT_PRICE` 同时检查替代限价和提交时采用的 ask。标记价缺失、未来或超过五秒、价格带规则缺失或价格越界时保留原 SELL。重启重放的待撤命令也必须重新通过此检查。

- 审查原例：原 SELL `143`，bid `140`、ask `145.10`、mark `145`、`multiplierDown=0.97`，下限 `140.65`；结果 **DELETE=0、无替代 POST、原 SELL 持续工作**。
- 价格恢复至 bid `141`、ask `141.10`、mark `141.05` 后：一次合法 DELETE → GET 确认 → 一次替代 POST，不重复撤单。
- 撤单确认后、最终提交前将 mark 再变为 `150`：原正式网关拒绝替代 POST，保留真实持仓并报告 `EXIT_PRICE_BAND`；条件恢复后可重新提交。
- 新增十项回归覆盖上述情形、ask 越带、缺失价格带规则、标记价缺失/过期/未来、重启待撤命令、账户和盘口过期。
- 请求计数断言：仅在可撤改候选上增加必要的一次标记价 GET；等待 GET 对账期间零额外标记价 GET，实际 POST 保留原来的一次标记价 GET；每次最多一个写动作。180 秒、最多五次尝试、部分成交防碎仓、UNKNOWN 恢复均保留。

### 本次定向验证与环境边界

Windows / Python 3.12.7，全部使用临时 SQLite 和离线 HTTP 模拟器。先运行 F07，再运行 F11，然后运行相关套测。未重复全部研究回测。

- F07：**11 passed**。
- F11：**40 passed**（原 30 项加新增 10 项）。
- 受影响套测：**534 passed, 1 deselected**，随后在恢复 Full access 的会话中独立补跑凭据测试 **1 passed**。覆盖 MarketFeed / MarketCollection / GridConsumer、Demo 网关执行及准入、bounded quote / preparation、Grid bridge / network、GET recovery / window、runtime / constructor clock / continuous campaign、fill faults、离线 Execute systemd / cloud runtime、receipts / reconciliation、forward observation 及 GridExecution；本次合计 **586 passed**，无待验证的本地排除项。
- 本地默认 pytest 临时目录不可访问，使用 `D:/Code/f11-test-temp/`；子进程通过显式 `PYTHONPATH=<worktree>/src` 导入本次源码。
- `test_current_user_credential_available_in_independent_process` 需要真实 Windows Credential Manager；首次受限会话返回 Windows 错误 `1312`，故从该批次排除。恢复 Full access 后使用原测试独立补跑 **1 passed**，未使用 mock 或放宽准入；GitHub Windows CI 仍执行完整原测试。
- GitHub 上的双 Python Extended software、实际分片覆盖、离线 systemd 和 `FactorLab trusted merge gate` 必须绑定推送后的新 HEAD 核对；本报告的本地结果不代替该门禁。

未操作原台式机私有交易状态，未启动官方 Demo Execute，未合并 PR。
