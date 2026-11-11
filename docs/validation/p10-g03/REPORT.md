# P10-G03 BigQuant 原价补证与冻结研究消费

**P10_G03_PARTIAL**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

BigQuant 的 `002408.SZ` 在 **2023-04-24、04-25、05-05、05-08** 四个会话取得版本绑定的 `OBSERVED_RAW` 资格。四个会话各有四项原始 OHLC，共 **16 个价格对象**。资格限回顾性研究的 `EXECUTION / CNY/share`。P10-G02 的实际价格入口接受了这四行，价格、证券日期和其余投影值逐项一致。完整第一折的消费资格仍未闭合。

基线为 `origin/main` 的 `9af38d53cc35296e8e1e0c99e99d12da90d40bb7`，包含已合并的 #105、#106、#107。冻结 `B_lightgbm` 第 0 折使用 `[2023-03-13, 2024-03-13)`、243 个来源会话、128 个冻结证券身份。完整窗口对应原 Manifest 中 13 个月度价格分片及配套状态与日历。默认回放的价格读取上限为 2023-05-08，`--requalify-fold0` 请求整折价格输入。价格门禁检查所读行，范围由消费者的输入决定。经核对，原 LightGBM 目标中此证券有 49 个目标行，其中 2 个落在获准日期。

## 证据如何绑定到价格

`stock.zip` 中的 `stock/cn_stock_real_bar1d` 两个 2023 年 4、5 月 Parquet 成员逐一匹配原 P8-02 输入 Manifest 与各自的 DAI 导出收据。收据记录对 `cn_stock_real_bar1d` 的直接查询，导出时间分别为 2026-11-07 12:20:29 和 12:20:30 UTC。原 `schema.json` 为零行查询，实际 Parquet footer 的 OHLC、`adjust_factor`、`pre_close`、限价及金额为 `double`，成交量为 `int64`。`date` 为无时区日观察键，`instrument` 为带交易所后缀的证券键。合同记录 schema、分片、收据和原 Manifest 的标识或 SHA256，证券保持原代码。见 [来源合同](source-price-contract.json) 和 [批准范围](qualified-scope.json)。

BigQuant 将这张表的 OHLC 定义为交易所公布的未复权成交行情，将 `adjust_factor` 定义为累计后复权因子，`cn_stock_bar1d` 用未复权价格乘此因子得到后复权价。四个通过对象的原分片因子均异于 1。深交所归档以证券、交易日期和开、高、低、收命名字段返回同一 A 股四日观察，16 项按十进制精确一致。发行人公告确认 `002408` 对应齐翔腾达的人民币普通股，深交所当期交易规则以人民币每股计价。这三部分证据和分片字节共同支撑精确日期上的 `CNY/share` 原价合同。现行字典的表级口径只用于解释字段，其页面版本与整个原导出批次的生效映射仍待补证。[BigQuant 未复权表](https://v6.bigquant.com/data/datasources/cn_stock_real_bar1d)、[BigQuant 后复权表](https://bigquant.com/data/datasources/cn_stock_bar1d)、[深交所归档行情](https://www.szse.cn/market/trend/archive/index.html)、[发行人 2023-047 号公告](https://disc.static.szse.cn/download/disc/disk03/finalpage/2023-05-04/389dda70-b1bf-4df3-a410-fb32c67d47b7.PDF)、[深交所 2023 年交易规则](https://www.szse.cn/lawrules/rule/repeal/rules/t20230217_598773.html)。

供应商字典以股、元描述 `volume`、`amount`，并将 `upper_limit`、`lower_limit` 定义为未复权限价。无涨跌幅限制时存在占位值。四个有成交日双方均有正成交量，其中两日的量差超过深交所万股列的展示精度。数量、金额与限价均保留资格 UNKNOWN。交易规则允许除权息参考价成为行情的前收盘和涨跌幅计算基准，因此 `pre_close` 与上一真实成交收盘可不同。定向样本中的 `exdr` 为零，源批次的除权参考价构造、调整链锚点、精度、前复权口径与公司行为日覆盖仍待补证。[深交所 2023 年交易规则](https://www.szse.cn/lawrules/rule/repeal/rules/t20230217_598773.html)、[P9-D02 预检](../p9-d02-preflight/REPORT.md)。

同一证券 4 月 26、27、28 日和 5 月 4 日的全日停牌已由公告闭合。四个 BigQuant 行的 OHLC 和成交额为空，成交量字段均记录为零，前收和涨跌停参考字段仍在。现有三源证据显示 CSMAR 以 `Filling=1` 提供重复价，CNRDS 的重复价对应缺失量额。这些日期的成交价资格保留为未通过。既有 Sidecar 的持仓延续估值仍作为独立估值对象处理，停牌原价空值保持原样。[P9-D01 停牌与估值证据](../p9-d01/REPORT.md)。

`date` 对应来源交易日，日线投影没有逐笔交易时刻。发行人披露日、供应商导出时间和 2026-11-12 的取证日分别记录。历史供应商首次可得时间继续为 UNKNOWN，合同的 `available_at` 为空。明确传入历史 cutoff 时，这四个对象也会被拒绝。

## 消费结果

`replay_source_result` 在 Manifest 和已验分片身份命中时装配现有 `PRICE_CONSUMPTION_V1`。P10-G02 先校验原字段名和证券日期身份，再经 `require_fields` 检查用途、单位和范围，随后核对日键、四个原始 binary64 价格及原成交量的摘要。该摘要把实际通过的投影绑定到取证对象，成交量参与身份核对。用相同列名换成另一组价格会触发投影拒绝。

Stage A 读取原消费者的定向投影，四行获准价格通过。混入原停牌行、指定历史 cutoff、替换 Manifest 或价格值均被拒绝。另用既有用途门禁确认 `EXECUTION` 资格不会授予 `BOOK_VALUATION`。定向回归覆盖错误分片、原字段身份变更、单位未知、证券日期越界、填充和模型估值、以及 P10-G02 的四个既有日期身份拒绝案例。[消费结果](consumption-results.json)。

完整输入仍包含合同以外的行，Stage B 待价格覆盖闭合。新增合格范围为四个真实价格输入行，本轮没有新增冻结成交、现金或持仓账本。原预测、候选预测、LightGBM 目标及调仓决策均匹配原冻结摘要。成交规则、费率、滑点和独立估值继续由原执行器与 Sidecar 处理。合并基线的执行状态障碍仍是 2023-05-15 的 `000540.SZ`。P9-D05 的独立状态补证和整折接入由 [PR #109](https://github.com/Wind-Maker1001/FactorLab/pull/109) 跟踪，交付准备时仍为开放状态。[剩余缺口](remaining-blockers.json)。

## 验证与资源

既有 Python 3.13 环境中，三个定向测试文件 **71 项通过，2.44 秒**。真实 Stage A 含原冻结文件身份核对用时 **2.293 秒**，峰值工作集约 **196 MiB**。定向工作涉及 5 个原 Parquet 成员，共约 11.50 MiB 成员载荷，三次不同目的的读取合计约 34.48 MiB。保留的证据原件约 1.14 MiB，测试临时载荷约 29 KiB。本地数据验证单进程，Arrow 和数值工作线程限制为 1。[验证收据](verification.json)。

复现 Stage A 时，使用原 P8-02 Manifest 和 BigQuant 原归档，调用 `load_source` 投影 `002408.SZ` 的 2023-04-24 至 05-08，保留返回的分片身份。以 [批准范围](qualified-scope.json) 中四个日期构建 `replay_source_result`，用原 P9-D01 Sidecar 中的独立版本 pin 调用 `qualified_replay_prices`。用同一对象的八行投影验证范围拒绝。公开收据只发布身份、范围、检查结果和资源观测。
