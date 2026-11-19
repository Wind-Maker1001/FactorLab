# P7-R13-F03　实时行情调度与账户恢复

当前验收为 **BLOCKED**。签名官方 Demo 窗口因 CLOCK_SKEW 在启动阶段关闭，独立的公开行情探测在第三轮触发 STALE_POLL。离线结果支持缩短普通轮次和连续补数，但签名账户恢复、实时闭合 Bar 和盘口的连续联动尚未在官方 Demo 建立。M19 的真实订单生命周期验收仍需该联动。

基线为 `794650c`，包含 [#203](https://github.com/Wind-Maker1001/FactorLab/pull/203) 和 [#201](https://github.com/Wind-Maker1001/FactorLab/pull/201)。采集运行使用 `97198b434cd48a509db2b987f036f35686821121`。Binance 官方 USDⓈ-M 文档列出的 Demo REST 主机为 `demo-fapi.binance.com`，与仓库固定主机一致。[官方 REST 环境](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info)

## 耗时链与修复

F02 的 12.149 秒普通轮次包含 9.417 秒 Grid 恢复和约 2.73 秒行情 poll。恢复内 14 次账户 GET 累计 8.557 秒，恢复 book GET 约 0.600 秒。feed 的 /time、klines、book RTT 分别为 0.552、1.569 和 0.568 秒。controller、protocol 和 feed 的 COMMIT 约为 5.4、21.9 和 3.8 毫秒，证据 fsync 约 155 毫秒，调度等待为零。整轮开始到结束包含恢复时间，而 5 秒 STALE_POLL 从恢复结束后取得的交易所 /time 锚点计算。/time 响应到拒绝又经过 2.174 秒，因此第一次收到锚点时，按采集机时钟算已至少旧约 2.826 秒。原始日志没有保留 serverTime 的值，当时的盘口接收年龄则为 3.618 秒。[F02 原始事件](../p7-r13-f02/window-a/events.csv)

- 两个订单日志都为空时，Demo 网关直接调用现有 controller reconciliation。它每轮都重新读取账户、持仓、挂单、配置与适用的交易状态，并用同一个现有核验入口更新本地账户投影。这省去第一遍远端读取和空回放事务。只要任一日志有订单身份，包括历史终态，就执行原来的回放前后双核验，继续检查迟到成交、手续费和持仓差异。提交、撤单及 UNKNOWN 恢复路径保留原有检查。
- feed 从同一个通过时钟校验的 /time 计算当前闭合边界。持久 Bar 游标正好等于边界时，待交付的闭合区间为空，该轮仅取盘口。需要 Bar 时，klines 请求只覆盖待交付的闭合区间。原 5 秒锚点、盘口接收及交付前校验继续适用。新增计时沿用 F02 单轮遥测，区分 /time 接收年龄、行情批次年龄、行情 poll 和历史回放。
- Grid 的连续模式每个恢复轮次最多顺序处理四个既有上限的历史页。页校验连续区间后逐 Bar 持久化确认，失败后的未确认后缀由原游标续接。历史页不读盘口，使用只能推进策略历史的回调，且撤回未提交的历史提案。把最新闭合区间留给下一轮的 /time 和盘口校验，也避免批量重放消耗已经取得的盘口有效期。有限 Shadow 实验仍使用原有 BACKFILL_LIMIT 和中断规则。5 秒轮后等待、30 秒最大报价间隔、配置的请求和证据预算继续原样执行。

## 离线定向测试与对照

Python 3.14 的定向回归为 **153 passed**，覆盖实时行情、连续补数与重启、落后时钟下历史 Bar 的交易隔离、行情过期、游标 COMMIT 回滚、Shadow pending 恢复、真实订单协议的网络拦截、重启、UNKNOWN、部分/完整成交及原始限价。另在实际 main 源码和修复源码各运行同一性能夹具，均为 **2 passed**。

每个场景包含六轮，均交付六个盘口和两个闭合 Bar。其中四轮游标已经追平。网络拦截夹具的账户 v3 响应含 canTrade，每次完整账户观察为六个 GET。F02 的官方 Demo 响应需额外读 v2，故相同空日志路径预期是 14→7 个账户 GET，签名窗口本次在进入恢复前停止。Pending 场景持有一个模拟交易所的部分成交挂单，六轮仍逐轮执行原有订单和成交核验。两个源码版本得到相同的现金可用额 994.07408 USDT、权益 999.99708 USDT、持仓 0.02、累计成交额 2.92 USDT、累计费用 0.00292 USDT 和 PARTIALLY_FILLED 状态。普通场景两边均为已对账空仓。

表中每格为 p50 / p95 / max，时间单位为秒。离线 HTTP 模型每次 GET 固定 250 毫秒，墙钟列实测拦截传输和本地 SQLite，未等待真实网络。候选 Pending 的恢复墙钟波动与其账户 GET 计数保持一致的结果一起解读。

| 每轮阶段 | 普通 main | 普通 F03 | Pending main | Pending F03 |
| --- | --- | --- | --- | --- |
| 账户 GET | 12/12/12 | 6/6/6 | 16/16/16 | 16/16/16 |
| 行情 GET（含恢复盘口） | 4/4/4 | 3/4/4 | 4/4/4 | 3/4/4 |
| 账户 HTTP 模型 | 3/3/3 | 1.5/1.5/1.5 | 4/4/4 | 4/4/4 |
| 行情 HTTP 模型 | 1/1/1 | .75/1/1 | 1/1/1 | .75/1/1 |
| 全轮 HTTP 模型 | 4/4/4 | 2.25/2.50/2.50 | 5/5/5 | 4.75/5/5 |
| 恢复墙钟 | .120/.207/.207 | .072/.096/.096 | .243/.337/.337 | .256/.374/.374 |
| 行情 poll 墙钟 | .027/.126/.126 | .031/.111/.111 | .036/.130/.130 | .031/.141/.141 |
| Bar 回调墙钟 | 0/.058/.058 | 0/.044/.044 | 0/.036/.036 | 0/.044/.044 |
| SQLite COMMIT 墙钟 | .176/.212/.212 | .104/.144/.144 | .248/.424/.424 | .247/.471/.471 |
| 全轮墙钟，不含等待 | .187/.228/.228 | .111/.162/.162 | .279/.467/.467 | .298/.515/.515 |
| 配置的轮后等待 | 5/5/5 | 5/5/5 | 5/5/5 | 5/5/5 |

历史补数回归以每页 2 Bar 的上限模拟 11 Bar 落后。首轮固定边界依次读取四页并持久化 8 根，重启后再推进 2 根历史 Bar，随后在新一轮交付最后一根和盘口。整个过程交付顺序连续。将同样的轮次放大到默认 120 Bar/页时，单个完整历史轮次可推进至多 480 Bar，实际每轮耗时取决于交易所响应和逐 Bar 持久化。

## 签名官方 Demo 窗口

独立实验 `p7-r13-f03-realtime-recovery-a` 于北京时间 2026-11-19 23:05:16 开始，23:05:23 关闭。使用现有已绑定 Demo 凭证，以独立本地 Preview 状态和会话锁隔离。预算 600 秒、600 GET、20 提案、5 MiB，轮后等待 5 秒，盘口间隔上限 30 秒。

共 **3 GET、0 POST、0 DELETE**。启动 /time 和 v3/account 返回 HTTP 200，RTT 约 4.71 和 1.77 秒。随后 positionRisk 以 HTTP 400 / CLOCK_SKEW 返回，RTT 约 0.79 秒。请求预算守卫按既有中断规则拒绝继续发送，外层记录 `request after interruption prohibited`。HTTP 错误发生于初始化，无进入 poll_round 的样本。有效盘口、合格闭合 Bar 和连续交付样本均为零，行情年龄和单轮阶段分位数无样本。该窗口没有触发 STALE_POLL 或 BACKFILL_LIMIT。

原始 `events.csv`、`summary.json` 和输出保留在工作树的 `local-p7-r13-f03/window-a/`。Git 中的[原始 HTTP 事件](demo-events.csv)和[签名窗口摘要](demo-summary.json)可重放该结论。

## 独立公开行情探测

为区分签名启动故障和行情批次自身的延迟，使用同一源码和固定 Demo 主机，另启一次 155 秒预算的无凭证探测。它使用同一个生产 feed、5 秒行情门限和轮后等待、30 秒最大报价间隔，以及同一个 GET-only 请求守卫。将签名窗口用掉的 3 GET 从公用的 600 GET 上限扣除。该探测没有 Grid 或账户恢复，合格 Bar 指在探测启动后开盘的闭合 Bar。

实际于北京时间 23:09:20 开始，23:10:12 因 STALE_POLL 关闭。**9 GET、0 POST、0 DELETE**，HTTP 状态全部为 200。两个有效盘口的市场时间间隔为 9.028 秒，接收间隔为 8.459 秒，接收年龄为 4.380 和 3.811 秒。唯一闭合 Bar 在探测前开盘，用于预热，合格闭合 Bar 为 **0**。第三轮关闭使连续交付止于两个盘口。两次官方观测合计 **12 GET、0 POST、0 DELETE**。没有 BACKFILL_LIMIT，公开探测的游标来自新状态目录。

| 行情轮 | GET | 行情耗时（秒） | /time 接收年龄（秒） | 批次年龄（秒） | 盘口接收年龄（秒） | 实际等待（秒） | 交付 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 3 | 2.575 | 2.424 | 4.088 | 4.380 | 5.005 | 一盘口、一预热 Bar |
| 2 | 2 | 3.436 | 2.417 | 2.977 | 3.811 | 5.006 | 一盘口 |
| 3 | 3 | 10.904 | 2.433 | 5.118 | 3.693 | 0 | STALE_POLL |

第二轮确实省去了无新闭合区间的 klines GET。第三轮跨过 23:10 的闭合边界，需要 GET klines。/time 的 HTTP 调用耗时 8.207 秒，响应中的锚点到达时已旧 2.433 秒。随后 klines 和 book 的 RTT 合计 2.658 秒，批次最终超过 5 秒门限，尽管盘口自身较新，仍在交付前关闭。三轮行情耗时的 p50/p95/max 为 3.436/10.904/10.904 秒，含等待的整轮为 8.442/10.906/10.906 秒，feed COMMIT 为 0.003897/0.013716/0.013716 秒。启动 exchangeInfo 另用约 25.299 秒。[公开原始事件](public-events.csv) · [逐轮摘要和分位数](public-summary.json)

M19 所需的签名账户恢复和连续实时输入仍待观察。空日志重复读取消除后，带真实订单日志的账户恢复仍保留双核验和原查询开销。现有 5 秒行情门限下，交易所时钟 GET 接收年龄与跨分钟串行市场请求的尾部延迟仍是两个独立的直接问题。

## 复现

```powershell
python -m pytest -q tests/integrations/test_demo_market_feed.py tests/integrations/test_demo_grid_supervisor.py tests/integrations/test_forward_recovery_scheduling.py tests/integrations/test_forward_round_telemetry.py tests/integrations/test_r13_forward_observation.py tests/integrations/test_forward_observation.py tests/integrations/test_binance_demo_grid_network.py tests/integrations/test_binance_demo_grid_bridge.py
python -m pytest -q -s tests/integrations/test_forward_recovery_scheduling.py -k measure_full_round
# 用 git archive 导出 794650c 的 src 到空目录，令 PYTHONPATH 指向导出的 src
python -m pytest -q -s -o 'pythonpath=' tests/integrations/test_forward_recovery_scheduling.py -k measure_full_round
python docs/validation/p7-r13-f03/summarize_window.py docs/validation/p7-r13-f03/demo-events.csv new-demo-summary.json
python docs/validation/p7-r13-f03/summarize_window.py docs/validation/p7-r13-f03/public-events.csv new-public-summary.json
```
