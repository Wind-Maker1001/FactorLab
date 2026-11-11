# BigQuant 导出批次语义询证 / Export-batch semantic attestation request

**REQUEST_PREPARED_NOT_SENT**

收件渠道：由数据账户持有人通过其已授权的 BigQuant 技术支持渠道提交。当前没有发件、工单编号或供应商答复。仅请求供应商确认自身数据产品，不请求审核研究项目或授予研究资格。

## 中文正文

主题：请确认 cn_stock_real_bar1d 于 2026-11-07 导出批次的字段口径及版本

BigQuant 技术支持团队：

我们持有通过账户授权 DAI 接口于 2026-11-07 导出的 `cn_stock_real_bar1d`。本次询证只涉及 2023-03-13 至 2024-03-12 的行情观察，位于附件列出的 13 个月度分片内。分片本身覆盖 2023-03 至 2024-03 整月；研究窗口不等于分片全月范围。

附件提供查询、导出时间、分片及原始收据 SHA256、原 P8-02 冻结输入 Manifest 身份和零行查询 schema，不包含行情记录、账户凭证或 Token。请按附件身份确认以下问题：

1. 该导出批次适用哪个数据集版本或语义修订版本？如没有版本号，能否正式确认这些指定查询/时间/哈希对应批次的字段定义？
2. `open/high/low/close` 是否均为交易所未复权的实际成交日价格，而非前复权、后复权、供应商填充或模型估值？请说明日线成交观察的聚合范围及已知例外。
3. 本范围 OHLC 是否为人民币元/股？`pre_close/upper_limit/lower_limit` 的货币和计价单位分别是什么？无涨跌幅限制占位值如何区分？
4. 全日停牌或无成交是否保留记录？OHLC、量、额分别为空还是零？保留的前收/限价是否仅为参考字段，不代表当日成交？
5. `volume` 是否为股，`amount` 是否为人民币元，是否为当日累计成交量额？如日线与交易所公开统计存在时段、聚合、修订或展示精度差异，请说明规则。
6. 是否存在任何自动前值填充、后值回填或其他价格填充？适用于哪些字段和状态？
7. 是否会追溯修订历史 OHLC？请说明修订原因、版本留存、通知和本导出快照已包含的修订范围；未复权并不自动表示历史不修订。
8. 当前公开原价字典 https://v6.bigquant.com/data/datasources/cn_stock_real_bar1d 是否适用于附件的 2026-11-07 批次？页面内 `version="official"` 表示发布类别还是可识别的语义版本？
9. 如上述定义曾变化，请提供适用的导出/快照生效区间、受影响的行情观察区间及变更记录。我们不要求证明 2023 年网页文字与今天相同，而要求确定 2026 年实际导出快照的经济语义。
10. 能否提供有版本/生效日期的正式文档、维护记录或由可识别供应商支持人员作出的批次确认，明确数据源全名、适用范围、字段语义、单位、停牌/填充及修订政策？

另请单独说明是否保留历史首次分发时间或逐次 vintage；如无法提供，请明确未知。该问题与本次回顾性原价批次确认分开，不以导出时间替代历史首次可得时间。

请在回复中引用附件 Manifest SHA256 与 13 个收据 SHA256，或以可复核的清单/附件唯一识别本批次。逐个检查原始商业价格值并非本次请求。

## English text

Subject: Semantic/version attestation for the 2026-11-07 cn_stock_real_bar1d export batch

Dear BigQuant Technical Support,

We hold an account-authorized DAI export of `cn_stock_real_bar1d` made on 2026-11-07. This request concerns observations from 2023-03-13 through 2024-03-12 within the 13 monthly partitions identified in the attachment. The files cover full months March 2023 through March 2024; our observation window is narrower.

The attachment contains queries, export timestamps, partition and original receipt SHA256 hashes, the original P8-02 frozen-input Manifest identity, and a zero-row-query schema. It contains no commercial price rows, account credentials or tokens. Please confirm:

1. Which dataset/semantic revision applies to this exact batch? If no revision number exists, please provide an attributable batch-specific attestation against the listed queries, timestamps and hashes.
2. Are all `open/high/low/close` values exchange unadjusted observed daily transaction prices, without forward/back adjustment, vendor filling or model valuation? Please identify the aggregation scope and exceptions.
3. Are OHLC denominated in CNY per share? What currency and price units apply to `pre_close/upper_limit/lower_limit`, and how are no-limit placeholders identified?
4. For full-day suspension or no trade, is a row retained? Are OHLC, volume and amount null or zero? Are retained previous-close and limit fields references rather than same-day observed trades?
5. Is `volume` measured in shares and `amount` in CNY, accumulated over the trading day? Please explain any aggregation/session, revision or display-precision differences from exchange statistics.
6. Is any automatic forward fill, backward fill or other price filling performed? Which fields and states does it affect?
7. Can historical OHLC be revised retrospectively? Please describe revision policy, version retention, notices and revisions already embodied in this export snapshot.
8. Does the current public raw-price dictionary at https://v6.bigquant.com/data/datasources/cn_stock_real_bar1d apply to this 2026-11-07 batch? Does embedded `version="official"` denote a publication category or an identifiable semantic revision?
9. If definitions changed, please provide applicable export/snapshot effective intervals, affected observation intervals and change records. We need the semantics of the actual 2026 export, not proof that a 2023 webpage had identical wording.
10. Please supply a versioned formal document, maintenance record or attributable supplier confirmation identifying the full datasource name, batch scope, field definitions, units, suspension/filling and revision policies.

Separately, please state whether historical first-distribution timestamps or vintages are retained. If unavailable, please say so. Export time is not historical first availability; retrospective raw-price batch semantics and point-in-time availability are separate questions.

Please identify this batch by the attached original Manifest SHA256 and 13 receipt SHA256 values, or by an equivalently reproducible list. We are not requesting a review of our project or a row-by-row price certification.

## 最小附件及发送边界

直接附上 `batch-identity-manifest.json`：13 个原始分片路径、原查询、逐分片导出时间、Manifest SHA256、分片/收据 SHA256 与 schema 均在其中。原 Manifest 字节 SHA256 为 `ed00e76b3a7b0ea7da1f79fe96a06bcbfb3ab2781b2b7ab75f58c74aef8f5405`；这是原始字节身份，不要求供应商认定研究项目名称的任何含义。

必要时附 `official-source-excerpts.txt`，指明当前定义。完整行情 Parquet、原工作簿、账户页面、访问令牌及认证头均不附送。若支持人员确需查询凭证或账户信息，由账户持有人在私密支持渠道另行决定，不放入公开 PR。

收到回复后，保存原消息/工单、供应商身份、回复时间、附件及其原件哈希，逐项标明明确确认和未回答项；未答复不得视为同意。TODO：取得上述正式批次确认后再评估范围合同，当前不扩大价格资格。
