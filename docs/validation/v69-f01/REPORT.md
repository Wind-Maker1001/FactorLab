# V69-F01 — 市场发现完成，交易执行独立验收

基线：origin/main `5e9f29a`。本 PR 市场发现范围已完成，可独立审查；不修改共享执行层。通用 Demo 执行准入由 [F03 / PR #239](https://github.com/Wind-Maker1001/FactorLab/pull/239) 负责。QQQUSDT 当前为 **DEMO_LISTED_NOT_EXECUTED**，不宣称成交或账户验收完成。

## 八标的覆盖矩阵

| 股票 | 主网 EQUITY/TradFi | Demo EQUITY/TradFi | Demo 行情 | 分类 / 缺失原因 |
|---|---|---|---|---|
| AAPL | AAPLUSDT | 无 | 未请求 | MAINNET_ONLY：Demo 全量索引无对应合约 |
| SNDK | SNDKUSDT | 无 | 未请求 | MAINNET_ONLY：Demo 全量索引无对应合约 |
| BA | 无 | 无 | 未请求 | NOT_LISTED：两环境全量索引均无对应股票合约 |
| C | 无 | 无 | 未请求 | NOT_LISTED：CUSDT 为 COIN，排除 Citigroup 映射 |
| JPM | JPMUSDT | 无 | 未请求 | MAINNET_ONLY：Demo 全量索引无对应合约 |
| GE | 无 | 无 | 未请求 | NOT_LISTED：两环境全量索引均无对应股票合约 |
| TQQQ | TQQQUSDT | 无 | 未请求 | MAINNET_ONLY：Demo 全量索引无对应合约 |
| QQQ | QQQUSDT | QQQUSDT，TRADING | 盘口、深度、100 根小时 Kline、标记价格、10 条历史资金费率 | DEMO_LISTED_NOT_EXECUTED |

覆盖结论严格限定于捕获快照，不代表未来上市情况。对应关系依据 `baseAsset`、`underlyingType=EQUITY`、`underlyingSubType=TradFi` 及合约原始字段，而非仅凭名称。**发行主体、法律底层资产和 ETF 份额类别未独立确认**；QQQ 及其余候选均为 EXCHANGE_METADATA_ONLY。不能将元数据匹配写成底层身份认证完成。

## Demo 全量候选：24 = 20 + 2 + 2

| exchangeInfo 状态 | 数量 | 合约 |
|---|---:|---|
| TRADING | 20 | TSLAUSDT、HOODUSDT、INTCUSDT、CRCLUSDT、AMZNUSDT、MSTRUSDT、COINUSDT、PLTRUSDT、EWJUSDT、PAYPUSDT、METAUSDT、NVDAUSDT、GOOGLUSDT、SPYUSDT、QQQUSDT、MVLLUSDT、KORUUSDT、SOXLUSDT、SOXSUSDT、MUUSDT |
| PENDING_TRADING | 2 | BMNRUSDT、DISUSDT |
| SETTLING | 2 | FAKEKR000660USDT、FAHOODUSDT |

这是一份官方 Demo 股票/ETF **元数据候选清单**。TRADING 仅是交易所状态，不等于实际可成交；另外四个条目不能计入 TRADING。FAKEKR000660USDT、FAHOODUSDT 尤需后续身份核查，不据此推定对应真实股票。完整字段见 demo_equity_tradfi.json。

## QQQ 规则及公开行情

捕获规则：TRADIFI_PERPETUAL；报价及保证金币种 USDT；pricePrecision=5、quantityPrecision=2；价格 tick=0.01000，数量最小值和 step 均为 0.01；MIN_NOTIONAL=5 USDT。precision 与 tick/step 分开记录。

2026-11-24T15:03:42.223458Z 保存的盘口 bid=640.91000、ask=643.55000。按当时 ask 和 LOT_SIZE/MIN_NOTIONAL 计算，最小数量 0.01，指示性买单名义金额 **6.4355000 USDT**。这是历史快照计算，不是当前账户的可执行保证；杠杆 brackets、保证金、价格带及实时准入仍待执行验收。

同批标记价格 641.94000000 USDT，lastFundingRate=0.00043091（0.043091%）；历史 funding 返回 10 条。所有五种公开行情响应保存在 qqq_market_evidence.json；这是原始响应覆盖，不宣称小时线连续性或全部闭合已完成质量验收。未对其余七个无 Demo 合约的标的重复探测。

## 证据保留与复核

沿用仓库 [CI 原始下载外置实践](../ci-perf-03/README.md) 以及 [仅保留小型 JSON 的做法](../../architecture/factor_registry_v2.md)：两份完整 exchangeInfo 原始文件共 3,566,625 字节、约 14.7 万行中的主要部分，不再放在分支最终树中。原件保留于本机仓库外 `D:/Code/FactorLab-archives/v69-f01-public-snapshots/`；evidence_manifest.json 记录原文件字节数、SHA-256、官方 URL 和外置位置。**尚无远程持久产物链接**；外置原件不是新克隆仓库即可取得的依赖。普通后续提交也不会清除早期 Git 提交中的大对象，本次不重写已发布分支历史。

仓库保留 mainnet_listing_evidence.json、demo_listing_evidence.json：全量合约身份索引（含非股票冲突项）支持审查缺失结论；八标的匹配行、CUSDT 行和全部 Demo EQUITY/TradFi 行按原字段原值保留，支持审查身份标签、交易状态、合约类型、结算币种、精度及 filters。它们是 JSON 语义原始行，不是 HTTP 原始字节。来源 SHA-256 不能单独证明摘录完整性；有外置原件时可逐行对比。摘录字节 SHA-256 固定在 manifest 中，脚本会校验完整索引数量、唯一性和 Demo 清单一致性。原始 exchangeInfo 获取时间未记录；serverTime 不冒充获取时间。

## 分离验收与复现

`validation.json` 分别记录 `market_discovery.status=COMPLETE`、`trading_execution.status=NOT_EXECUTED`。市场发现完成的定义仅为快照覆盖调查和 QQQ 真实公开行情采集，不包括独立发行主体确认或成交验证。

没有提交订单，手续费未知；杠杆限制、账户权益/持仓、买入成交、reduce-only 平仓及重启恢复对账均未执行。执行阶段由 F03 能力支持，仍须协调 Azure 账户和订单所有权；本 PR 不等待 F03 成交才进入审查，也不设置模拟交易人工审批步骤。不修改 campaign、现有策略持仓或云端服务。

`python tools/validate_tradfi_demo.py` 离线校验已保留证据并重建映射、状态清单和 validation；不再联网刷新固定证据。`python -m unittest discover -s tests/validation -p test_v69_f01.py` 检查覆盖分类、20/2/2 分组、原件摘录绑定及篡改拒绝。新快照应保存为新批次，避免覆盖本次历史证据。

?????3 ? unittest ???????????/SHA-256??? 956 ?? Demo 744 ???????????????????????? 345 KB?
