# P7-R04B-D01：NVDAUSDT 独立历史数据认证

**P7_R04B_ASSUMPTION_BOUND_DATA_READY**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

固定窗口 `[2026-05-14, 2026-08-12)` UTC 已取得官方 Binance USD-M Public Data 的 2,160 根闭合小时成交线，90 个完整 UTC 日，与 R02 无时间重叠。不存在缺桶、重复、非法 OHLCV、零量或 high=low；没有补值。数据可以供下一阶段接口开发和假设约束研究使用；正式 16 账户执行仍须完成 R04B-E01 和未参与参数研发的来源声明。本轮未运行收益回测，未修改预注册参数或 Grid 引擎。

## 来源与网络

基线 `0d3c4a17eab5ddb6294c1f7fa183dd781c89e66f`。

REST 小规模探测显式指定 NVDAUSDT、1h、2026-05-14 首小时的 startTime/endTime、limit=1；该请求及 exchangeInfo 均 curl 28、HTTP 000、无响应。REST 连接问题未解决，没有将超时解释为数据不存在，也未继续分页。

官方 Public Data 解决了主数据获取路径：

`https://data.binance.vision/data/futures/um/monthly/klines/NVDAUSDT/1h/NVDAUSDT-1h-2026-{05,06,07,08}.zip`

四个具体对象全部 HTTP 200，ZIP 内有对应 CSV，各自官方 `.CHECKSUM` 均取得并与实际 ZIP SHA256 相符。仅下载窗口涉及月份；只保留目标 2,160 行结构化表，月边缘多余行不进入研究表。不解压到磁盘、不保留重复 CSV、不创建 SQLite。原对象保留一份，manifest 用 `external_raw_receipts` 引用其路径、URL、时点和摘要；这是现有不可变 manifest 的扩展字段，普通 `Reader.verify_raw()` 不遍历外部引用，外部 ZIP/官方 CHECKSUM 对应校验由本轮认证工具完成。不得把空的内嵌 raw_receipts 解释为已验证外部来源。

请求证据：[network-observations.json](../validation/p7-r04b/network-observations.json)、[supplementary-requests.json](../validation/p7-r04b/supplementary-requests.json)。实际 **14 次**：REST 2、主月份对象 4、官方校验文件 4、上市月份 1、辅助对象 3；重试 0，未轮换代理。连接超时 8 秒，总请求上限 20 秒，每对象上限 2 MiB。原始 UTC 时点按主机实际时钟记录为 2026-11-13，HTTP Date 亦为该日；任务日期为 2026-11-14，未改写采集时间。

## 合约与上市证据

沿用并校验 R02 留存的官方 2026-11-10 exchangeInfo 原文摘要 `bb6cc37b014e203eefccfdedf8a1258ebe3de52ac878d46c26aa40a58c642d10`。其身份为 `BINANCE:USDM:NVDAUSDT`，contractType=`TRADIFI_PERPETUAL`，quoteAsset=marginAsset=`USDT`，EQUITY 分类。新对象路径明确为 `futures/um`、NVDAUSDT，与 R02 同合约标识；当前在线身份刷新请求超时，因此这里认证的是留档身份及官方历史路径的一致性，不是新获取的当前规则。

价格是原生未复权合约每 base unit 的 USDT 报价，volume/taker-buy-volume 为 base units，quote-volume 为 USDT，count 为成交笔数；保留原始十进制字符串精度，不把股票现货或其他供应商价格拼入该表。数量单位解释延续 R02，不引入新的股票兑付或乘数断言。

取得官方 `NVDAUSDT-1h-2026-03.zip`（5,691 bytes，SHA256 见请求收据），含 130 行。该上市月份档案最早桶为 **2026-03-26 14:00–14:59:59.999 UTC**，volume=10870.22，count=11211。留档 onboardDate 为 **14:40 UTC**，落在同一个小时桶内：小时桶起点不代表 14:00 已成交，不能据此推断精确首笔成交早于上市。此为真实官方最早观测桶，不是逐笔首成交时点或全体更早月份不存在的证明。TODO：在正式研究入口开放前补齐上市公告/精确首成交交叉证据；不以 onboardDate 单独推定连续性。本轮目标窗连续性由实际 2,160 根数据独立验证。

## 内容身份与质量

完整收据：[certification.json](../validation/p7-r04b/certification.json)；不可变 manifest：[snapshot-manifest.json](../validation/p7-r04b/snapshot-manifest.json)。

- generation：`bde00b2f948842e2b8c2a00465a9f8c6`。
- data version：`352d960fad24e50f1423ff173d838a994a29603d2629652f46c7edc76e5e2a83`。
- bars.parquet SHA256：`16aa66258880efdace966880f59691a29cb74f4f87428240a5ef247ad044a2ab`。
- manifest SHA256：`a689366daf5815f4fea2726fa213197d5505ae89548083a094d39476aae3e12c`。
- 本机资产：`D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6`。

逐条检查原始时间严格递增、UTC 整小时、无重复；close time 为 open+1h−1ms 且早于采集截止；范围严格等于冻结窗口。现有 `bars` 验证十二字段、有限且合法 OHLC、非负 base/quote/taker 数量和成交笔数，额外要求 low>0。`completeness` 验证 2,160/2,160，missing_ranges=[]。真实零量、平坦桶各 0；不删除此类观测。使用原始 CSV 再次转换，两次 DataFrame 完全相等；内存重复 Parquet 序列化摘要与已发布文件相同。既有 Reader 验证 manifest 内容版本及表哈希。没有经济结果参与筛选。

旧 R02 bars SHA256 仍为 `562ba9e01c2ca050132a10519b3b514112b2f36e7837ffa838a72ce9bf00b7b4`，manifest 仍为 `1762a0f98483e75a9a70c0d149fac9f408fb206eb4ba4093200ee7aad5be454f`；旧路径只读，未复制行情。预注册 Git 内容 SHA256 仍为 `7a599b39d13f8cc009a8a37ecc82c3375e09ed587d4997684e39853767fcc43c`。Windows autocrlf 使工作区字节不同，校验同时比较 HEAD blob 哈希与仅规范 CRLF 的工作区内容；没有改写锁或配置。

## 历史经济信息边界

辅助对象内容检查见 [auxiliary-evidence.json](../validation/p7-r04b/auxiliary-evidence.json)。仅用五月具体对象确认可获取性，不宣称已认证全窗：

| 分类 | 本轮证据及后续用途 |
|---|---|
| A 历史事实 | 五月 funding CSV 93 行，含 calc_time、funding_interval_hours、last_funding_rate；首事件 interval=8h，缺 rateType，不自行推定全部 Regular/Special。五月 mark/index 各 744 行小时 OHLC。三对象均有实际 ZIP/CSV 内容及摘要。 |
| B 冻结假设 | 原预注册基础/压力 fee=4/8 bps、slippage=6/12 bps；交易 close 估值、全额抵押、参与率等保持原定义，不声称为真实账户费率或盘口认证。 |
| C 规则快照 | R02 留档 exchangeInfo 的合约身份及过滤器；不能回填为全窗历史规则。 |
| D 未知历史 | 全窗 funding 事件完整性水位、Special 分类、小时 mark/index 全窗认证、过滤器变更、保证金档位、特殊时段及公司/合约事件、maker/taker 等级优惠。 |

因此采用 **ASSUMPTION_BOUND_DATA_READY**，不升级历史规则完整认证。资金费不倒填、不伪装已纳入 Paper 账本反馈；mark/index 的占位零量不是成交量。TODO：按研究需求另行有界取得辅助全窗及历史规则证据。

## 资源与复现

下载前预算见 [resource-budget.json](../validation/p7-r04b/resource-budget.json)：主表 2,160 行，REST 满页预计 2 页，原始响应估计 0.31–2 MiB，Parquet 0.10–0.60 MiB，临时 10 MiB，总预留 100 MiB。追加身份/checksum/辅助阶段固定 8 次、无重试，仍在该磁盘预算内。认证时可用磁盘 **96,070,832,128 bytes**；实际新资产总占用 **376,765 bytes**（原始对象、校验文件、HTTP 头、一个 Parquet 和 manifest/指针）。所有写入限于新目录；旧表仅读取 1 次用于哈希/行数验证，不全量复制。内存读取目标四个月 ZIP、一次 March 及三个 May 辅助 ZIP；无重复解压文件。此占用是实际保留文件大小，不是操作系统块设备 I/O 计数。

失败部分仅保留紧凑错误收据；空失败目录不承载行情。回滚限于本轮新目录，既有 R02 和其他 P6 资产不可删除。Git 只含工具、报告、身份收据，不含行情本体。

已有含 pandas/pyarrow 的 Python，无需新建环境：

```bash
python -I tools/p7_r04b_sources.py --assets D:/Code/p7-r04b-official-payloads --output docs/validation/p7-r04b
python -I tools/p7_r04b_certify.py --repository . --assets D:/Code/p7-r04b-official-payloads
```

采集命令要求新的空输出路径，复现采集时替换为独立路径；认证工具复用已有 supplementary URL 收据，避免重复请求。重新发布生成新的不可变 generation，不要求采集时点或 generation ID 相同；相同原始数据的转换内容须一致。首次认证因 CRLF 锁比较中止，修正为 Git 内容比较后用同一缓存继续，未追加网络请求。

## R04B-E01 最小开发范围

现有 `grid_lifecycle.run_study` 仍按 R03 四臂运行，压力组依赖基础结果触发，并引用 A/C；不能只改 design JSON 冒充冻结的两臂矩阵。现有 `grid_paper` buy-hold 仍为旧参考入口，闭环经济归因缺口仍存在。

1. 显式 R04B 入口：仅 B/D×三模式+cash/common-budget buy-hold，两成本情景无条件执行，固定 16 账户，绑定本 snapshot/version 和预注册锁。
2. 参考策略：首个已完成 bar 的 p0 与事前成本计算 floor_0.01(min(1,1000/(p0×(1+slip)×(1+fee))))，每单≤0.1、总库存≤1，共同现金/挂单预留/参与率，最早下一完成 bar 成交；不按未来收益缩放。
3. 权威成交事件 FIFO：按真实部分数量分配买卖成本，剔除被 RISK_EXIT 污染的批次，分别报告闭环毛收益/费用/滑点/净收益、退出与残余库存，和总账 NAV 对账，滑点不重复扣减。
4. 输出共同事前风险预算与实际 EXPosure 指标（仍标 EXPOSURE_NOT_MATCHED），绑定历史辅助事实/模拟假设边界；补齐来源独立性声明及上市交叉证据后再开放正式执行。

TODO（R04B-E01）：以上接口及经济归因在独立策略任务实现，本数据任务不修改交易引擎。

## 交付引用

分支 `research/p7-r04b-historical-data`；独立 Draft PR，不自动合并。Commit、PR URL 与 CI 结果以提交记录及 PR checks 为准；数据认证本身不等同 CI 或正式研究准入。
