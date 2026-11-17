# P7-R13-F02：前瞻盘口采集调度、GET 开销与真实窗口

**GET_ONLY · NO LIVE MONEY · FINANCIAL_ADVANTAGE_NOT_ESTABLISHED**

R13-F01 的原始事件、冻结门限及 INVALID_EVIDENCE / INSUFFICIENT_SAMPLE 结论保持不变。F02 的开发基线为 `4926a60c9b81d4f83245f2f04faaf729c7e50836`，真实采集源码为 `0f3b29ce717b2d93cb944f3cc2e800cf8015ea45`，采集实现内容摘要为 `ea41193e45d966805228dc9f76c734d2101a5ae180039615199fba9cb8d21116`。

## 耗时诊断与最小优化

F01 的 62 次 GET 中，49 次为账户查询（7 次完整七端点账户读取），累计 HTTP 时延约 53.90 秒。`/fapi/v2/account` 是 canTrade 补充读取；无此补充的离线 fixture 每次完整 bridge 恢复为 13 GET，而 F01 同类恢复为 15 GET。行情轮询额外 3 GET。配置的 5 秒是完成一轮后的等待，不是固定采样周期。

有 pending 时，Shadow 原先先完整恢复一次，再由 Grid 完整恢复一次。F02 复用同轮完整恢复，在 Grid 撤回未提交提案之前恢复持久化的 Shadow 捕获；无 pending 不做提案级恢复。gateway 在成交回放前后各做一次远端 reconciliation 的行为保留，因此 UNKNOWN、成交费用、库存及账户状态仍经历原有双观察核对。没有跨轮缓存、跳过历史订单查询或放宽行情新鲜度。

`poll_round` 原始事件与 summary 提供账户/行情 GET 数、HTTP 状态/限流/失败和 transport RTT、Grid/Shadow 恢复、closed-bar、SQLite COMMIT、实际等待、盘口市场/接收时间以及 p50/p95/max。`start_to_start` 包含上一轮摘要写入；`loop_seconds` 到等待结束为止，不包括其自身摘要写入。HTTP RTT 不包含签名构造、响应解析及证据 fsync。controller、protocol、feed 的 COMMIT 与整个 transaction 分开计时；cursor 的提交失败仍回滚 ack。

原始证据逐轮保留在原字节预算内，内存仅保留最新回合与有界分位数样本；没有后台遥测服务、持续日志或新增数据库索引。来源身份、Bar 完成与可知时间、去重及中断留痕不变。新增诊断事件不产生研究报价，也不能消除 R12 中断拒绝。

## 离线对比与回归

`performance-comparison.json` 使用拦截 HTTP 与每 GET 250 ms 虚拟时钟。相同账户、报价与提案输入产生相同恢复经济状态和 3 个实际 feed 交付盘口：

| 场景 | 原恢复 GET | F02 恢复 GET | 含行情整轮 GET | 恢复虚拟 HTTP 时间 |
| --- | ---: | ---: | --- | --- |
| 无 pending | 13 | 13 | 16 → 16 | 3.25 → 3.25 秒 |
| 有 pending | 26 | 13 | 29 → 16 | 6.50 → 3.25 秒 |

虚拟时间是 fixture 的确定性请求耗时；本机恢复壁钟另列在 JSON 中，不代表真实网络性能。普通轮次没有因本次共享恢复而减少 GET。

综合定向回归 **269 passed，153.53 秒**，另有提交失败回滚测试 **1 passed**。覆盖无/有 pending、UNKNOWN、完成订单、进程重启、网络超时、过期/未来报价、缺 Bar、报价超间隔、策略取消/状态变化、账户恢复失败和连续盘口。保留未恢复不得决策、恢复捕获先于撤回、无未来数据与冻结窗口不可重启等风险约束。首次独立运行新增测试有 5 个 wired fixture setup error，修复导入后通过；扩大为真实 feed 交付时发现 fixture 的 broker 时钟未同步，统一虚拟时钟后综合回归通过。

## 首次请求前资源预检

`resource-precheck.json` 在真实请求前记录不变的 600 秒、600 GET、20 提案、5 MiB、5 秒完成后等待、30 秒最大报价间隔。按 F01 实测端点均值推算约 24 轮、458 GET；快速网络情形会更早耗尽预算（近零工作时间的调度包络为 2186 GET）。历史均值不是时延上界。实际始终保留 600 GET 上限，耗尽按原 CAPACITY_LIMIT 终止，不延长等待凑预算、不扩预算、不重启。

M19 的实际观察及独立恢复结束后释放共享账户网络窗口，F02 随后开始。没有与 M19 worker 并发，也未调整其 runtime、binding 或交易授权状态。

## 唯一真实窗口

新身份为 `p7-r13-f02-forward-feed-scheduling-a`，冻结 UTC 起止为 **2026-11-17T04:05:22.619813Z—04:15:22.619813Z**。实际于 **04:05:53.405100Z** 记录 `STALE_POLL` 中断，**04:05:53.412989Z** 完成退出。没有续期或重启。

实际为 **44 GET、0 POST、0 DELETE，0 个交付盘口、0 个 Bar receipt、0 个提案、0 次实际交易所成交**。所有 HTTP 请求正常返回；这是行情新鲜度合同触发的中断，不能描述为已证实的网络断连。原始 `interruption` 与逐轮摘要保留 `STALE_POLL`；`window_end`/采集 summary 的 `STOPPED` 是 supervisor 清理后的状态，不替代中断原因。

唯一一轮耗时 **12.148512 秒**，Grid 完整恢复 **9.416630 秒**；其中 14 个账户 GET 共 **8.557045 秒**，4 个行情 GET（含恢复用 book）共 **3.288696 秒**，HTTP 合计约占整轮 **97.5%**。controller/protocol/feed COMMIT 分别约 **0.005433 / 0.021877 / 0.003751 秒**，轮内证据 fsync **0.155032 秒**，实际调度等待 **0 秒**。详见 `performance-diagnostics.json`；只有一轮时分位数均为该单样本值。

盘口市场时间为 **04:05:49.787000Z**，接收时间为 **04:05:53.405100Z**，年龄 **3.618100 秒**。但整批 poll 相对于 `/time` 锚点的年龄已超过原有 5 秒门限，因此在交付 Bar/quote 前拒绝。该时间记录只是原始接收诊断，不计作有效盘口。整轮小于 30 秒不能代替行情新鲜度通过；pending 共享恢复没有在本轮触发，不能据此宣称真实普通轮次已经优化成功。

## R12 独立评价

正式评价使用实际 UTC `2026-11-17T04:15:47.936910Z`，晚于原冻结截止，结果为 **INVALID_EVIDENCE / INSUFFICIENT_SAMPLE**，拒绝码为 `DECLARED_OBSERVATION_GAP`。规范化 gaps 原样包含 `STALE_POLL` 和 `EARLY_WINDOW_END`。有效窗口、有效盘口及合格前瞻配对均为 0；报价触价、潜在 crossing、右删失提案和实际成交均为 0。没有提案时右删失比例不适用，不能称为 0% 或 100%。

独立空目录重放得到相同计数、拒绝码及证据摘要 `cd91eab8ed2b133bbf72b184d659e0f4dd5a2ce4351135029b0eaf678b193b7a`。原始事件规范内容摘要为 `07f329b95b6001218804aee394cd77a886b812b666f3755cd310811d19a9c4a2`。完整结果在 `normalized/window-1.json` 与 `normalized/summary.json`。性能优化只证明 pending 路径减少重复恢复，未建立真实有效采集窗口或研究准入。

```bash
PYTHONPATH=src python -m factorlab.experiments.grid_forward_observation \
  --events docs/validation/p7-r13-f02/window-a/events.csv \
  --protocol docs/validation/p7-r11/protocol.json \
  --evaluated-at 2026-11-17T04:15:47.936910Z \
  --output /path/to/new-independent-audit
```

Draft PR 为 #203。Codex bot 已审查 `8277e43`，未报告问题。CI 已通过可信风险选择、Linux/Windows locking 和 Python 3.14 package；Fast、扩展软件分片及 CI control review 在检查时仍待完成。不自动合并。

## 复现

```bash
PYTHONPATH=src python -m pytest -q \
  tests/integrations/test_forward_recovery_scheduling.py \
  tests/integrations/test_forward_round_telemetry.py \
  tests/integrations/test_demo_observability.py \
  tests/integrations/test_forward_observation.py \
  tests/integrations/test_r13_forward_observation.py \
  tests/integrations/test_demo_market_feed.py \
  tests/integrations/test_demo_grid_supervisor.py \
  tests/integrations/test_binance_demo_grid_bridge.py \
  tests/integrations/test_binance_demo_grid_network.py \
  tests/trading/test_paper_control.py \
  tests/test_grid_forward_evidence.py tests/test_grid_forward_observation.py \
  --junitxml=/path/to/new-targeted-tests.xml
```

本地 SQLite 执行状态不作为研究证据发布；交付保留原始 CSV、采集 summary、性能摘要、测试结果和 R12 规范化文件，不包含凭证。

`COLLECTION_PERFORMANCE_STATUS=PARTIAL_PENDING_RECOVERY_IMPROVEMENT`

`REAL_FORWARD_WINDOW_STATUS=INTERRUPTED_STALE_POLL`

`R12_ADMISSION_STATUS=INVALID_EVIDENCE / INSUFFICIENT_SAMPLE`

`FINANCIAL_ADVANTAGE_NOT_ESTABLISHED`

`GET_ONLY`

`NO LIVE MONEY`
