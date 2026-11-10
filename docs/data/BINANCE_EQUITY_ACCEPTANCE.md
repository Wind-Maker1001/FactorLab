# P6-B03 Binance Equity 验收

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

协议来源：币安官方 `binance-connector-python`，提交 `f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179`，Stocks OpenAPI 1.0.0。协议与复现命令见 [BINANCE_EQUITY.md](BINANCE_EQUITY.md)。

## 离线验证

- Python 3.14.0，NumPy 2.3.4，pandas 2.3.3，PyArrow 22.0.0，pytest 8.4.2，websockets 16.1.1。
- 新增模块 **22 passed**：REST 回执与精度、空报价、认证信息拒绝持久化及异常脱敏、只读白名单、原生数字开头证券标识、事件去重与蜡烛修订保留、目标证券能力记录、交易阶段、未闭合蜡烛隔离、连接恢复与缺口、Parquet 批次、篡改拒绝、无 Key 离线重放、正式金融准入拒绝。
- 既有 A 股校验回归 **96 passed**：`test_schema_phase1.py`、`test_schema_numeric_integrity.py`、`test_schema_ohlc_relationship.py`，覆盖证券格式、数值完整性与 OHLC 关系。
- DataHub 关联回归 **18 个用例通过**：首轮 17 个通过；分块 smoke 在按 `requirements/scalable-compute.txt` 安装 psutil 7.2.2 后单独复验通过。
- 最终 Python 3.14 验证中，新增模块与 A 股校验合计 **118 passed**；加上 DataHub，覆盖 **136** 个离线用例。

## 真实行情 smoke

2026-11-10 **10:15:56.789060–10:16:31.340922 UTC**，在显式开启网络、仅使用本地只读 API Key 的条件下完成 REST 采集及 30 秒 WebSocket 观察。

| 能力 | 实测结果 |
| --- | --- |
| 股票发现 | `exchangeInfo` 返回并保存 **7,957** 个证券，包含数字开头原生标识 |
| NVDA REST quote | bid **181.75 USD × 3,200 股**；ask **181.8 USD × 200 股**；原生接口没有事件时间，保持空值 |
| price 流 | 收到 **10** 条价格推送；持久化 **3** 条 NVDA 价格观测，交易所报告阶段均为 `PRE` / `PRE_MARKET` |
| NVDA quote 流 | 原始收到 **20** 条实时报价，去重后保存 **15** 条；最后一条 bid **181.76 × 100 股**，ask **181.9 × 300 股** |
| NVDA 5m kline 流 | 收到 **1** 条 `x=false` 蜡烛，保存到 `open_bars`；`bars` 为 **0**，未擅自闭合或补造 |
| calendar 流 | 已订阅，窗口内 **0** 条转换事件，状态为 `DOCUMENTED_NOT_OBSERVED`；不声称已实测到阶段转换 |
| 原始证据 | 保存并验证 **34** 份 REST 响应／WebSocket 帧及其来源回执 |

该次报价只是观测值，不是持久有效的当前价格。K 线其他周期、其他交易阶段、真实断线恢复和 calendar 转换未在此窗口实测；重连与各阶段处理由离线用例验证。

## 真实产物离线重放

禁止 `socket.connect` 后，成功验证全部 **34** 份原始回执，并从 Parquet 读取：

| 表 | 行数 |
| --- | ---: |
| symbols | 7,957 |
| quotes | 16（REST 1 + WebSocket 15） |
| prices | 3 |
| bars | 0 |
| open_bars | 1 |
| sessions | 0 |
| gaps | 3（观察起点、建连窗口、观察结束边界） |

快照数据版本：`d6873933696da1241cbe58838e0d25166fd3bf550fde5a9462362809c9e8c849`。原始行情、Parquet、认证材料不随源码提交。提交前已使用实际本地 Key 对暂存源码、测试、文档及 76 个本地证据／日志文件进行精确匹配检查，未发现 Key 内容；检查过程不输出 Key。

历史行情与历史规则均为 `UNVERIFIED`，无历史补造或自动回填。正式金融准入仍然阻断。
