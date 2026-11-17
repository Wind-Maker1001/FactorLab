# P7-R13-F02：前瞻盘口采集调度与 GET 开销

**GET_ONLY · NO LIVE MONEY · FINANCIAL_ADVANTAGE_NOT_ESTABLISHED**

## 诊断与范围

R13-F01 冻结原始事件、门限及 INVALID_EVIDENCE / INSUFFICIENT_SAMPLE 结论保持不变。开发基线为 `4926a60c9b81d4f83245f2f04faaf729c7e50836`。

F01 的 62 次 GET 中，49 次为账户查询（7 次完整七端点账户读取），累计 HTTP 时延约 53.90 秒。其 `/fapi/v2/account` 为 canTrade 补充读取；通用无此补充的离线 fixture 每次完整 bridge 恢复为 13 GET，而 F01 同类恢复为 15 GET。行情轮询额外 3 GET。5 秒配置是完成一轮后的等待，不是固定采样周期。

存在两个不同层级的重复恢复：pending Shadow 捕获会在 Grid 完整恢复之前另做一次完整恢复；gateway 自身在成交回放前后各做一次远端 reconciliation。后者保留，用于回放后再次核对远端订单、成交费用、库存及账户事实。F02 仅共享本轮 Shadow/Grid 的完整恢复，不引入跨轮缓存。

冻结预算下，600 秒中若 HTTP 与处理耗时趋近零，5 秒完成后等待可产生约 120 轮，按 16 GET/轮已超过 600 GET；F01 形态为 18 GET/轮。不能用人为延长轮次、降低行情新鲜度或事后扩预算消除这个约束。真实窗口需要在首次请求前记录资源预检，并严格执行原预算终止规则。

## 当前进展

- 原有定向基线：39 passed，111.23 秒，HTTP 拦截，无真实请求。
- 已加入单次完整恢复之后、Grid 撤回未提交提案之前的 Shadow 捕获恢复入口。
- 新恢复定向回归：9 passed，17.00 秒。HTTP 拦截与每 GET 250 ms 虚拟时钟对比：无 pending 两条路径均为 13 GET / 3.25 秒；有 pending 从 26 GET / 6.50 秒降至 13 GET / 3.25 秒。两条路径均交付 3 次连续盘口，保留相同提案、账户状态和可知时间语义。实际本机 fixture 壁钟分别为无 pending 0.315/0.289 秒、有 pending 0.488/0.345 秒，不代表真实网络时延。
- 已覆盖恢复先于撤回、捕获中断恢复、UNKNOWN、完成订单、恢复失败及超时。首次新测试独立运行因缺少导入 wired fixture 出现 5 个 setup error；补齐后上述回归通过。
- 已集成有界逐轮计时：账户/行情 GET、HTTP 状态与 RTT、Shadow/Grid 恢复、closed-bar、SQLite COMMIT、实际等待、盘口市场与接收时间，以及 p50/p95/max。`start_to_start` 包含上一轮摘要写入开销；`loop_seconds` 到等待结束为止，不包括其自身摘要写入。原始证据逐轮保留，内存仅保留最新回合与有界分位数样本。
- 提交失败仍回滚 cursor ack；新增的 COMMIT 计时不得改变 SQLite 原事务语义。
- 首次请求前的 `resource-precheck.json` 按 F01 实测端点均值推算 24 轮、458 GET；快速网络情形会更早耗尽预算（近零工作时间调度包络 2186 GET）。实际严格保留 600 GET 上限，耗尽按原 CAPACITY_LIMIT 终止，不延长等待凑预算、不增大预算、不重启。历史均值不是时延上界。
- 综合定向回归：269 passed，153.53 秒；另有提交失败回滚定向测试 1 passed。覆盖无/有 pending、UNKNOWN、完成订单、进程重启、网络超时、过期/未来报价、缺 Bar、超间隔、策略取消/状态变化、恢复失败及连续盘口交付。全 feed 对比为无 pending 16→16 GET，有 pending 29→16 GET，两个版本均交付相同 3 个盘口。
- M19 的 180 秒观察及独立恢复已结束，释放共享账户网络窗口；F02 将在无并发 worker 的错峰条件下开始。
- TODO：执行新身份真实窗口及原 R12 离线评价。

## 最终结果

`COLLECTION_PERFORMANCE_STATUS=PENDING`

`REAL_FORWARD_WINDOW_STATUS=NOT_STARTED`

`R12_ADMISSION_STATUS=NOT_EVALUATED`

`FINANCIAL_ADVANTAGE_NOT_ESTABLISHED`

`GET_ONLY`

`NO LIVE MONEY`
