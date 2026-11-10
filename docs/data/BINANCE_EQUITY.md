# Binance Equity 股票现货只读数据

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 协议依据

协议依据为币安官方 [Stocks SDK](https://github.com/binance/binance-connector-python/tree/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks)，固定提交 `f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179`，OpenAPI 1.0.0。适配器不依赖该 SDK，尤其不引入其交易、账户、免责声明签署、listen-key 或代币化资产操作。

- [REST 市场数据定义](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/rest_api/api/market_data_api.py)：`GET /sapi/v1/equity/market/exchangeInfo`，不传 symbol 返回全部活跃股票；`GET /sapi/v1/equity/market/quote` 返回单证券 bid/ask，可能约有 5 秒缓存延迟。只需 `X-MBX-APIKEY`，不需要 Secret 或签名。未知证券的发现结果为空列表；无报价时响应体为空，不是零价格。
- [WebSocket 流定义](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/websocket_streams/streams/market_streams_api.py)：`wss://nbstream.binance.com/equity/stream`，仅发送 `SUBSCRIBE` 市场流 RPC。`price` 为全部活跃股票的约 3 秒快照；`NVDA@quote` 为单证券最优 bid/ask，最多每 200ms 一次；`NVDA@kline_5m` 为蜡烛更新；`calendar` 仅在交易阶段变化时推送，不是心跳。股票代码保留大写，月线 `1M` 不可变为 `1m`。
- K 线周期仅支持 `5m / 1h / 1d / 1w / 1M`。这些定义证明订阅协议存在，不等于某次连接已实际收到数据。
- **历史行情能力未证实**：不提供历史 REST K 线、补齐或历史规则查询，不用永续合约替代股票现货。

## 本地采集

安装 `pip install '.[binance-equity]'`。仅在本地进程环境中配置 `BINANCE_EQUITY_API_KEY`，权限只开启 Reading。不要把 Key 写入命令参数、代码、日志、测试 fixture、报告或 Git；不需要 API Secret。

所有输出目录必须是新目录；原始响应、回执和 Parquet 属于本地行情产物，不应提交到 Git。

```sh
python -m factorlab.integrations.binance_equity discover --allow-network --output /local/equity-discovery
python -m factorlab.integrations.binance_equity quote --symbol NVDA --allow-network --output /local/nvda-quote
python -m factorlab.integrations.binance_equity stream --symbol NVDA --streams price quote kline calendar --interval 5m --seconds 60 --allow-network --output /local/nvda-live
python examples/binance_equity_nvda.py --seconds 60 --allow-network --output /local/nvda-smoke
python -m factorlab.integrations.binance_equity replay --input /local/nvda-smoke --table quotes
```

`stream` 仅连接公开市场流，不读取 Key；REST 发现和报价读取环境变量。HTTP 使用 Python 的系统/环境代理设置；WebSocket 使用 websockets 的系统/环境代理发现。断线期间只记录缺口并有限重连，不执行历史回填。`--seconds` 限制观察时间；首次建连、断线及结束边界单独记录。没有更新不等于断流：报价可能不变，calendar 可能无阶段转换。

## 研究语义与重放

- 证券类型为 `STOCK_SPOT`，价格 USD，数量为股。价格/数量采用精确十进制字符串，毫秒时间采用可空整数；不会自动转换 float 或生成复权价格。
- REST quote 不提供事件时间，因此 `event_time` 留空，不能用采集时间冒充。WebSocket quote 的 `T` 为来源时间，`E` 为服务端推送时间；价格的 `t` 可空。每条数据另有 `acquired_at` 和 `raw_receipt`。
- 仅按交易所报告的 `mp` / calendar 阶段区分 `CLOSED / OVERNIGHT / PRE_MARKET / REGULAR / POST_MARKET`。没有阶段字段的报价与 K 线保留 `UNKNOWN`，不套用 24/7 日历，也不按机器本地时间猜测交易时段。股票支持盘前盘后或夜间交易，不意味着当前正在该时段交易。
- exchangeInfo 是观测时点的规则快照，`rule_observed_at` 与历史生效时间分开。`tradabilityUpdateTime` 只说明交易方向变化时间，不证明所有规则自该时刻生效。
- 只将 `x=true` 的收到蜡烛写入 `bars`；其他写入 `open_bars`，不自动闭合。跨交易阶段的 K 线不强行归入某个阶段。未收到的区间不生成价格、成交量或零成交蜡烛。
- 有来源时间的相同内容在最近 10,000 个事件窗口内去重；同一来源报价／K 线仅推送时间变化时不重复入表，无时间价格快照保留每次观测。内容实际变化仍保留，闭合 K 线修订是带事件时间和采集时间的独立观察版本，不覆盖旧观察，也不保证每个开盘时间只有一行。研究使用者需要按观察截止时间选择版本。所有原始帧仍保存，去重不改变原始证据。接口未提供完整序列号，缺口表不构成完整性保证。
- `manifest.json` 固定 schema、协议来源版本、能力观测状态、来源回执和 Parquet 内容摘要。`Reader` 验证内容版本，返回既有 `QueryResult` / `SourceMetadata`；离线重放不创建网络客户端。`INTERRUPTED_OR_FAILED` 快照可用于诊断，不能当作完成采集。
- 数据仅供未准入研究观察，不改变 A 股 DataHub 校验、PIT/HFQ 证明要求或正式金融准入。

## 最小验证

```sh
python -m pytest tests/integrations/test_binance_equity.py -q
```

离线测试不请求币安。真实 smoke 必须显式传入 `--allow-network`，能力状态 `DOCUMENTED_NOT_OBSERVED`、`MESSAGE_WITHOUT_TARGET`、`OBSERVED` 分别表示文档存在但尚未收到、收到消息但没有目标证券、实际收到目标事件；REST `NO_QUOTE` 不算成功读取 bid/ask。能力记录中的 `messages` 为去重后成功解析的推送计数，原始到达数应从原始帧统计。市场关闭或观察窗口不足时，不能将未收到 K 线写成已验证。

验收实测见 [BINANCE_EQUITY_ACCEPTANCE.md](BINANCE_EQUITY_ACCEPTANCE.md)。
