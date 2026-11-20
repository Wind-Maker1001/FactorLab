# P7-M19-F02：Grid 执行输入时钟接线

基线：`203d35dfc592c299455f7a253f04037effdf3cd3`（PR #211）。

## 原因与修复

Grid Worker 原先让 `GridConsumer` 使用本机 UTC，并让 feed 使用独立公开客户端的交易所校准。这样 Bar 的闭合/新鲜度与恢复资格门槛、账户/订单风控分别处于不同的时间来源；本机快慢 20 秒可能改变 Bar 准入。

Worker 现在将 `bridge.session._now` 传入 Consumer 的 `clock` 与 Feed 的 `receipt_clock`，将 `bridge.session.client.demo_time` 传入 Feed 的 `exchange_time`。Preview 与 Execute 使用同一接法。Session 沿用 #211 的交易所锚点和单调时钟投影；Feed 的有效期预留与签名 GET 的校准检查负责刷新，异常仍进入原有暂停与恢复路径。

`FRESH_CLOSED_BAR_REQUIRED` 的解除仍要求运行权限有效、非历史补数、闭合时间晚于启动/恢复门槛、Bar 年龄小于一个周期，并且源身份尚未处理。历史预热/补数、崩溃后的源事件重放不能升级为新交易信号。策略订单 TTL 仍按原有闭合 Bar 序列推进；没有引入新的有效期政策。

只修改 Worker 的三个接线参数及其定向测试。盘口/账户 5 秒门限、5000 毫秒 recvWindow、串行订单准入、写前持久化、UNKNOWN 防重发、原始 LIMIT 价格、SELL reduceOnly、交易所成交确认和 Preview 禁写均保持原实现。

## 离线验证

Python 3.13，213 passed（205.33 秒）：

```text
python -m pytest tests/integrations/test_demo_grid_supervisor.py \
  tests/integrations/test_demo_market_feed.py \
  tests/integrations/test_binance_demo_grid_bridge.py \
  tests/integrations/test_binance_demo_grid_network.py \
  tests/integrations/test_binance_demo_execution.py \
  tests/integrations/test_binance_demo_network.py -q
```

新增 12 个 Worker 入口级用例覆盖 Preview/Execute 的本机 ±20 秒、运行中墙钟 ±40 秒跳变、校准过期刷新/读取失败后的断开、盘口过期、批次超过 5 秒、账户事实延迟及 kill switch。HTTP 拦截夹具保留真实 Session、Consumer、Feed、Bridge、Controller 和网络准入；校准时间和响应时间戳由独立确定性时钟推进，Preview 的 POST/DELETE 均被拒绝。Execute 在新鲜 Bar 与盘口到达后只发送一个原价 LIMIT，并在继续轮询时保留挂单而不制造成交。

同批原有用例覆盖历史补数、过期 Bar 信号、重复 Bar、游标和进程恢复、挂单、UNKNOWN、写前持久化、部分成交、reduceOnly 反向单及真实写入前的风控。过期盘口/批次和账户延迟的新增用例仍然拒绝写入，未以减少暂停为由放宽风控。

## 一次官方 GET-only Preview

官方主机 `demo-fapi.binance.com`，仅 NVDAUSDT，独立 Preview 状态。沿用既有 600 请求及 5 MiB 观测预算；Worker 窗口 155 秒、轮后等待 5 秒，初始化至退出实耗 187.757 秒，低于 600 秒上限。121 次 GET，0 POST，0 DELETE。原始事件保存在独立本地观测目录，公开摘要见 [preview-evidence.json](preview-evidence.json)。

以下均为 2026-11-20 UTC：

| 输入/转换 | 交易所时间 | Consumer 接收时间 | 结果 |
| --- | --- | --- | --- |
| 启动前已闭合 Bar | 09:41–09:42 | 09:42:33.265101 | 序号 1；保留 FRESH_CLOSED_BAR_REQUIRED，撤回本地提案 |
| 账户异常后恢复时已闭合 Bar | 09:42–09:43 | 09:43:08.922444 | 序号 2；闭合早于恢复门槛 09:43:07.804067，仍不准入 |
| 新鲜闭合 Bar | 09:43–09:44 | 09:44:02.878332 | 序号 3；live_bar=true，reason=null，Grid RUNNING，1 个本地提案 |
| 随后盘口 | 09:44:02.067 | 09:44:02.890848 | live_bar=true；无订单写入 |
| 随后盘口 | 09:44:15.397 | 09:44:15.777912 | live_bar=true；无订单写入 |
| 随后盘口 | 09:44:26.263 | 09:44:27.068570 | live_bar=true；无订单写入 |

共交付 9 个盘口、3 根 Bar；STALE_POLL=0。五次校准均首样本通过，普通刷新相对前锚点的漂移在相邻不确定区间内；交易所相对本机偏移约 −2.05 至 −2.07 秒。

窗口出现以下阻塞，不能标记 `M19_EXECUTION_INPUT_READY`：

- `/fapi/v3/positionRisk` 返回 HTTP 400 / -1021 / CLOCK_SKEW，RTT 5.730398 秒。当次签名不确定性 273.602 毫秒、校准年龄 11.430 秒、墙钟跳变约 0.017 毫秒，recvWindow 仍为 5000 毫秒。请求总 RTT 超过窗口，但现有证据不能将总 RTT 等同于入站延迟。Worker 按现有路径重新校准并暂停，下一轮重新对账；未将该请求解释为本机墙钟域错误。
- 后段 `/fapi/v1/openOrders` 读取失败，耗时 8.584506 秒，无 HTTP 状态记录；Worker 暂停并撤销实时资格。下一恢复轮次观察到 31.839 秒盘口接收间隔，触发 R13_QUOTE_GAP，最后状态为 RECONCILE_REQUIRED。

上述账户网络/恢复问题交集成阶段处理，没有修改协议、恢复策略或连续性门限，也没有启动第二次官方窗口。观察证明新鲜闭合 Bar 可以进入 Grid 更新并产生提案，但未满足无时钟错误且持续稳定交付的完整条件。没有真实订单或成交实测，不声明 `DEMO_LIFECYCLE_VERIFIED`。

NO LIVE MONEY。
