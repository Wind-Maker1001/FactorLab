# P6-D02：Binance TradFi 历史覆盖与离线回测可行性

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

结论：**P6_D02_HISTORICAL_DATA_READY**。NVDAUSDT 已取得本次上限内连续 90 天的真实成交日线、小时线、历史 Mark 日线和历史 Index 日线，以及 275 条独立资金费率事件。这里的 READY 是研究输入覆盖，不是策略稳健性证明或完整永续交易规则认证。

## 1. 实际取得的数据

唯一研究合约为 `BINANCE:USDM:NVDAUSDT`，来源 `BINANCE_USDM_PUBLIC_REST`、官方 `https://fapi.binance.com/fapi/v1/`。当前响应仍为 `contractType=TRADIFI_PERPETUAL`、`underlyingType=EQUITY`、USDT 报价和保证金资产，基础资产字段为 NVDA；不从代码生成现货证券身份或 USD 换算。

所有区间为 UTC 左闭右开。运行机器实际 UTC 日期为 **2026-11-10**，所以本次最后完整自然日为 11 月 9 日，而不是尚未闭合的 11 月 10 日。

| 数据 | 实际覆盖 | 有效记录 |
| --- | --- | ---: |
| 成交日线 | `[2026-08-12, 2026-11-10)` | 90 |
| 成交小时线 | 同上，首根 8 月 12 日 00:00、末根 11 月 9 日 23:00 | 2,160 |
| 成交分钟线样本 | `[2026-11-09, 2026-11-10)` | 1,440 |
| 历史 Mark Price 日线 | `[2026-08-12, 2026-11-10)` | 90 |
| 历史 Index Price 日线 | 同上 | 90 |
| 历史资金费率 | `2026-08-12T00:00:00.001Z` 至 `2026-11-09T16:00:00.001Z` | 275 |
| 本次当前 Mark/Index 快照 | `2026-11-10T17:50:34.844Z` | 1 |

日线末根实际闭合时间为 `2026-11-09T23:59:59.999Z`。各周期闭合桶缺口、规范化键重复、冲突均为 **0**；确切原始分页重复/排除计数见 [real-evidence.json](real-evidence.json)。未前向填充，未生成零成交替代桶。

**最早实际读取到 2026-08-12，不等于交易所历史从这一天开始。** 这是 90 天调查边界，边界当天仍有数据。当前 exchangeInfo 的 `onboardDate` 为 `2026-03-26T14:40:00Z`，但上市时间字段不证明该日所有历史端点仍可读取；本次不越过 90 天上限继续探测绝对历史起点。

分钟线只验证了上述一天；不能把这一结果扩大为 90 天分钟线可用，也不能把 NVDAUSDT 的结果推广到其他合约。本次无需比较合约即可回答核心问题，没有逐代码扫描。

## 2. 接口证据：文档与实际响应分开

官方开发者旧文档地址重定向至 [当前 Market Data 文档](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)，实取为 **HTTP 202、0 字节正文**。因此端点和参数另用 [Binance 官方 USD-M SDK 源码](https://github.com/binance/binance-futures-connector-python/blob/main/binance/um_futures/market.py) 交叉确认，源码字节摘要见 [run-evidence.json](run-evidence.json)。空文档响应不是市场历史为空。

| 端点 | 官方 SDK 依据 | 本次实际响应 |
| --- | --- | --- |
| `GET /fapi/v1/klines` | symbol、interval、startTime、endTime、limit | 1d / 1h / 1m 均有真实已闭合数据；小时分页实际返回 1,500 条，再返回 660 条 |
| `GET /fapi/v1/fundingRate` | symbol、时间范围、limit；按时间升序，SDK 注释上限 1,000 | 275 个历史事件；各时间块随后取得空终止页 |
| `GET /fapi/v1/markPriceKlines` | symbol、interval、时间范围、limit | 90 条历史 Mark OHLC，不是 premiumIndex 快照 |
| `GET /fapi/v1/indexPriceKlines` | **pair**、interval、时间范围、limit | 90 条历史 Index OHLC |
| `GET /fapi/v1/premiumIndex` | 当前 Mark / Index / funding 观察 | 1 个当前快照，继续禁止用于普通历史估值查询 |
| 独立历史 Premium Index 序列 | 本次官方参考未完成确认 | **NOT_RUN**，不能从 Mark/Index OHLC 或当前 premiumIndex 拼造 |

SDK 该版本的 Kline 注释写 max 1,000，而既有成交分页器使用的 1,500 在本次确实被接受并返回 1,500 条。**1,500 是已验证的成交单页容量，不是本次认证的所有端点统一最大值。** Mark/Index 使用 1,000 的请求上限，未做极限容量探测；当前完整官方限制和精确权重表仍需可读的版本化文档确认。

适配器支持固定 UTC 周期 `1m/3m/5m/15m/30m/1h/2h/4h/6h/8h/12h/1d`；实测只有 1m、1h、1d。其他周期以及 API 的周/月等可变周期均未在此次实证中认证。

原生开盘/闭合/结算时间保留毫秒，研究时间使用 aware UTC。请求采用 `startTime` 和 `endTime=exclusive_end-1`，每页从实际末时间前进；不因短页就停止。资金费率满页保留同时间戳 Regular/Special 的重叠边界。没有记录的空终止页、请求失败、上市前和真实缺失桶分别处理，不将空响应证明为“从未交易”。

## 3. 分阶段采集与来源完整性

原 P6-B01 的 10 根日线、30 个资金事件和 6 份原始回执直接重放并继承，没有重新请求 `[2026-10-31, 2026-11-10)` 的已验证成交/资金区间。

| 阶段 | 新请求区间 | 新日线 | 新资金事件 | 累计本轮采集请求 |
| --- | --- | ---: | ---: | ---: |
| 相邻探测 | `[2026-10-28, 2026-10-31)` | 3 | 9 | 6 |
| 扩至 30 天 | `[2026-10-11, 2026-10-28)` | 17 | 51 | 10 |
| 扩至 90 天 | `[2026-08-12, 2026-10-11)` | 60 | 185 | 14 |

以上累计含本轮 exchangeInfo 和当前快照各一次。加上独立发现 1 次、小时/分钟/Mark/Index 合计 9 次，**本次市场 API 共 24 次 HTTP 尝试，全部 200；429、5xx、传输错误、网络重试均为 0**。文档查阅和测试环境依赖安装不是市场 API 调用，未混入该数字。

使用既有 PowerShell transport、单请求 15 秒超时、并发 1；网络阶段分别限制 24 次/360 秒和 12 次/180 秒，包含重试次数和 Retry-After 等待预算，未启动重复扫描。当前 exchangeInfo 公布 `REQUEST_WEIGHT=2400/minute`；本次小批量请求远低于该额度，没有为了吞吐添加线程池。成功请求缓存和 pending 可恢复，已发布同 scope Snapshot 可离线复用，预算或错误中止不发布半成品。

日线/资金采集观察跨度 **11.024403 秒**，随后四种序列的实测墙钟 **7.750 秒**；前者含归档与解析，两者合计约 **18.77 秒**，不把发现和文档查阅计成这两个批次的实测耗时。序列批次 Python 分配峰值 **3,295,761 字节**；5 个新 Snapshot 含继承原始回执副本占 **7,168,436 字节**，不是进程 RSS 或独立原始字节量。

新载荷存放在忽略的 `results/p6-d02/` 下，沿用不可变 `runs/<generation>`、Parquet、原始回执和 manifest；Git 仅提交聚合证据、代码和报告。保存路径及固定版本如下，不能拿 `latest.json` 当研究固定版本：

| 数据 | generation（相对 `results/p6-d02/`） | data_version |
| --- | --- | --- |
| 日线/资金/快照 | `daily/runs/bb0c43caaebd4e46a612b75b9b8b2b15` | `34f804333867a581b2ab135249ca1e7a1b9acacfdec55896005f0be59fb4704d` |
| 小时线 | `hourly/runs/bddaab1cd5c943e28caa62c0fc49ef33` | `8f4861c8b3bef653b6f68cc778516e771ec7bd88678c00ad404b9111ac9ca607` |
| 分钟样本 | `minute/runs/49d054ea60754ccaa17a20bf6b568004` | `953e3f01722e33b6d4fb580a4ddf9e036f0b67ca0550037562af842ad79e9072` |
| 历史 Mark | `mark-daily/runs/360aecf86aa24b75bd0911bb451f6605` | `6ce4bbc308cebd03661a2af5fdc253c6f925b9e0a59b6273be9ebf07f7b5ab34` |
| 历史 Index | `index-daily/runs/a220c221f99745e9989b0a01ccdd6ab7` | `eb4a7f484e5fa2e1c4cd3feb80b29f335121ff968cc3360b5cdd4adf2c8666a0` |

manifest 的字节 SHA256、每张 Parquet SHA256、原始回执校验数、实际时间和查询行数均在 real-evidence.json。数据版本绑定 manifest 内容，不将其混称为 manifest 文件字节 SHA256，也不将摘要一致性当作交易所金融真实性认证。

## 4. 资金费率、规则和缺口

275 个事件包括 **274 Regular + 1 Special**；月份计数为 8 月窗口 60、9 月 95、10 月 93、11 月窗口 27。Special 实际时间为 **2026-09-10T00:00:01.002Z**。原始 rateType、费率、结算时间和事件附带 Mark 值均精确保留，不并入价格收益；所有事件均有 markPrice。

Regular 实际大部分间隔为 8 小时，但 **9 月 9 日 20:00 至 9 月 10 日 00:00** 还观察到 4 小时和 1 小时间隔；毫秒边界也有 0/1 毫秒偏移。不能按固定三次/日推断缺失或删除“额外”事件。Special 是来源报告的特殊事件，不改标 Regular；其具体触发公告与适用名义价值规则尚待绑定，不能把该事件直接声称为某账户的已扣款。

当前可核验的规则：

| 字段 | 本次观察值/含义 |
| --- | --- |
| 合约类别、状态 | `TRADIFI_PERPETUAL`、`TRADING` |
| 基础/报价/保证金资产 | `NVDA / USDT / USDT` |
| PRICE_FILTER tickSize | `0.01000` |
| LOT_SIZE minQty / stepSize | `0.01 / 0.01` |
| MARKET_LOT_SIZE minQty / stepSize | `0.01 / 0.01`，市场单 maxQty 为 `3000` |
| MIN_NOTIONAL | `5` |
| 上市字段 | `2026-03-26T14:40:00Z`，只作为本次响应中的上市声明 |
| maintMarginPercent / requiredMarginPercent | `2.5000 / 5.0000`，不是经认证的历史账户保证金档位 |
| liquidationFee | `0.015000`，不是 maker/taker 交易手续费 |

没有使用账户、密钥、杠杆设置或交易端点。当前 exchangeInfo 只证明观察时点，不认证整个 90 天规则；实际杠杆上限、风险阶梯、历史保证金版本、强平执行规则和 maker/taker 费用尚未绑定证据。

| 缺口/边界 | 日期或范围 | 分类与处理 |
| --- | --- | --- |
| 日线、小时线、Mark/Index 日线缺桶 | 已采 90 天窗口内：无 | 0 个，连续 UTC 桶已核验 |
| 分钟数据覆盖不足 | `[2026-08-12, 2026-11-09)` | 未采样，不是数据质量失败 |
| 调查边界之前 | 2026-08-12 之前 | 超出本次上限，绝对历史起点未确定 |
| 未闭合自然日 | 2026-11-10 | 不进入闭合日线，不补造 |
| 资金事件发布完整性 | 全窗口；特别是上述 9 月 9–10 日 | 返回事件全部保存，无官方历史发布水位/完整结算日历，不宣称无遗漏 |
| 历史规则/费用 | `[2026-08-12, 2026-11-10)` | 来源证据缺口，不用当前规则回填 |
| 日内 Mark/Index 路径 | 已采日线内 | OHLC 不提供先后路径或每个风险触发时刻 |
| Premium 历史端点与当前权重文档 | 本次调查 | 官方正文不可读，独立历史序列 NOT_RUN |
| 本次市场 API 中断或冲突 | 无 | 错误 0；不是用删除冲突后的结果冒充完整 |

## 5. NVDAUSDT 回测能力分级与 P6-B04 边界

| 能力 | 等级 | 当前可做的事与尚需合同 |
| --- | --- | --- |
| 行情指标研究 | **READY** | 连续闭合日线/小时线；分钟仅限一天样本 |
| 价格信号研究 | **READY** | 固定版本、UTC 顺序和观察截止可用；信号只能用当时已闭合桶，保留历史可得性未认证 |
| 简化价格收益模拟 | **PARTIAL** | 可开发不借款、无杠杆、固定研究资金上限的模拟；先明确下一桶执行、滑点/成交量约束及费用假设，不称真实账户复制 |
| 资金费率收益核算 | **PARTIAL** | 历史 Regular/Special 已具备；还需持仓有效时点、方向、合约数量和 USDT 名义价值定义，绑定特殊事件规则 |
| 保证金权益核算 | **BLOCKED** | 缺适用历史保证金、风险档位和合约规则版本 |
| 强平模拟 | **BLOCKED** | 虽然历史 Mark 日线可用，仍缺风险触发粒度/路径与适用强平规则；不能静默跳过强平 |
| 多合约共享账户 | **BLOCKED** | 只核验一个合约，缺多合约完整覆盖及统一资金/风险合同 |

**因此可以开展研究型、无杠杆的简化价格回测开发；还不能直接称其已具备真实费用/成交条件下的完整回测输入。** 加资金费率的研究同样可以开发，但必须独立事件账本，先落实上述持仓和名义价值合同。包含保证金和强平的完整永续回测目前不能做。

P6-B04 最值得先实现的是：**固定 Snapshot 输入、下一闭合桶之后执行、精确持仓数量与 USDT 名义价值、显式费用/滑点假设、Regular/Special 独立结算的无杠杆研究账本**。历史成交跨度已不是该最小范围的开发阻塞；真正阻塞完整永续引擎的是历史规则和账户风险语义，而不是再找一个当前 Mark 快照。

可优先研究滚动动量、均线/波动指标等不依赖盘口或真实杠杆的信号。单合约不能证明跨证券稳健性；一天分钟样本也不适合宣称高频策略可行。

TODO：绑定可读的版本化官方限制/权重与 Premium 文档、Special 规则公告、历史费用与保证金/强平规则；若要日内风险模拟，再有界采集匹配粒度的历史 Mark，而不是从日线内插路径。

## 6. 验证与复现

真实数据通过原生 Reader 和 BinanceResearch 重放；socket connect/create_connection 被禁止。原始响应重新归一化后与 Parquet 逐字段精确相等，保留十进制字符串、Regular/Special、原生毫秒和合约身份；6 套 Snapshot（含基线）的全部原始回执及 manifest/Parquet 版本校验通过。原始历史价格响应中的非交易量字段留在原始载荷，不生成虚假的 Mark/Index 成交量列。

最终定向离线测试 **67 passed，12.31 秒**，环境为 Python 3.14.0、NumPy 2.3.5、pandas 2.3.3、PyArrow 22.0.0，满足项目相应版本约束。覆盖预算包含重试与 Retry-After、断点缓存、已完成复用、UTC 边界、空响应、分页/冲突、资金事件类型、历史 Mark/Index 与快照分离、旧版本兼容以及身份/摘要/研究查询合同。过程中单独运行的 9 个历史任务用例已包含在 67 个中，不能相加。所有异常路径 fixture 都是合成测试，不计为真实限速或中断实证。

```bash
PYTHONPATH=src python -m pytest -q \
  tests/research/test_binance_history.py \
  tests/research/test_binance_client.py \
  tests/research/test_binance_collection.py \
  tests/integrations/test_binance_research.py

# 显式网络许可；已完成同 scope 输出会离线复用。
PYTHONPATH=src python -m factorlab.integrations.binance history \
  --allow-network --transport powershell --timeout 15 \
  --max-requests 24 --max-seconds 360 \
  --baseline docs/validation/p6-b01/nvda/runs/real \
  --end 2026-11-10 --output results/p6-d02/daily

PYTHONPATH=src python -m factorlab.integrations.binance series \
  --allow-network --transport powershell --timeout 15 \
  --baseline results/p6-d02/daily/runs/bb0c43caaebd4e46a612b75b9b8b2b15 \
  --start 2026-08-12 --end 2026-11-10 --interval 1d \
  --endpoint markPriceKlines --output results/p6-d02/mark-daily
```

小时、分钟、Index 同用 series 入口，分别选择 `klines/1h`、`klines/1m`、`indexPriceKlines/1d`；分钟日期只取上述单日。多个命令各自的预算不能自动当成一个全任务预算，应按本次请求清单分配，或复用同一个 PublicClient 预算对象。

本地产物可通过 `examples/binance_history_evidence.py --snapshot name=<固定generation> --output <证据文件>` 离线重放；不得将缺少本地载荷视为可以从聚合 JSON 还原市场数据。版本和摘要是定位/校验合同，不是市场载荷的替代品。

### 若未来需要更长跨度

本次已达到 90 天上限，没有必要用其他来源伪装补足。若未来研究需要更长样本，可另行评估 Binance 官方历史归档是否列出该原生合约并验证实际文件；本次没有证明这些归档存在。授权的底层股票未复权/复权行情也可独立用于股票信号研究，但必须保留原来源、股票身份、币种、交易日历与公司行动语义，不能冒充 NVDAUSDT 的成交、资金费率或 Mark 历史。合成样本只用于工程验证。
