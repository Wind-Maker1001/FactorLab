# P7-R12：前瞻证据准入与评价接线

**NO LIVE MONEY · FINANCIAL_PROTOCOL_PENDING_APPROVAL · ECONOMIC_ADVANTAGE_NOT_ESTABLISHED**

R12 独立验证研究输入，不修改 R11 的 `P7_R11_ENGINEERING_V1`、`PENDING_APPROVAL` 和 `BLOCKED / NOT_ESTABLISHED`。历史两个 90 天窗口均已使用；R09/R10 不是已经验证的风险改善。

## 输入与身份

入口：`python -m factorlab.experiments.grid_forward_evidence`。输入只读；显式 `publish()` 才使用既有 ExperimentTracker 发布独立 R12 experiment/run。R12 身份绑定 R11 原身份及外壳哈希、证据内容、评价时间和适配器代码身份。JSON 为权威记录，现有 SQLite 仅为索引。

`P7_R12_FORWARD_EVIDENCE_V1` 封套必须包含 `kind`、`window: [start,end]`（UTC 半开）、`max_quote_gap_seconds` 和 `source`。source 包含格式、名称、规范化 JSON 的 SHA-256 与原始 payload。摘要绑定内容而非外部真实性；独立核验必须以另外保管的可信摘要/交易所证据为基础。`max_quote_gap_seconds` 是本次工程采样完整性声明，不是金融样本阈值或交易参数。

类别：

- `HISTORICAL_REFERENCE`：仅方法调试与旧结果核对，不进入新前瞻评价。
- `FORWARD_SHADOW`：提案和离散报价，不存在实际成交收益；不能提升为 R11 `FORWARD_PAPER` 或 Demo。
- `DEMO_CONFIRMED`：要求订单/成交 ID、交易所来源与端点、成交/接收/确认时间、数量、价格、费用/资产、账户/提案绑定、完整性状态和内容摘要。字段齐全只通过结构校验，仍为 `UNVERIFIED_RECEIPT`。

R11 协议按原 tracker 外壳摘要、协议身份和工程定义逐一校验。即使重新计算摘要，也不能改变原协议的审批状态或比较规则。

## NORMALIZED_V1 与未来 M15 兼容契约

未来采集端应提交 source.format=`NORMALIZED_V1`；不导入、不等待 M15 分支。紧凑可执行例子为 [人工输入](../validation/p7-r12/synthetic-shadow.json)。payload 必须含：

- `binding`：account、instrument、strategy、version、interval_seconds、session_started_at、prior_bar_ids。先前已处理事件身份必须保留；重启缺口写入 gaps，不续期、不重新标为 live。
- `bars`：id、opened_at、closed_at、received_at、known_at、decision_at、completed_bar、live。必须冻结后开始，区间连续，闭合后接收/可知再决策，全部不晚于评价时间。
- `proposals`：id、bar_id、market_at、decision_at、captured_at、deadline、side、price、quantity、account、strategy、version、replay。时间、Bar 身份、账户与部署绑定必须一致。
- `books`：同一盘口的 market_at、received_at、bid、ask、bid_quantity、ask_quantity。市场时间严格递增，非交叉报价；候选仅使用市场和接收时间都晚于 capture 的报价。capture 前报价只是基线。
- `fills`：仅 Demo 允许；字段参见上文和 `evaluate()`。receipt 为删除 receipt 字段后的规范化 JSON 摘要。仅接受 BINANCE_USDM_DEMO、EXCHANGE_CONFIRMED、`/fapi/v1/userTrades` 声明；这不是网络真实性认证。
- `gaps`：所有已知重启/采集中断；非空阻止合格结果。
- `ends`：at、ids、reason；保留原始 deadline，终止只缩短观察，不生成补成交。

格式错误直接失败；准入错误保留 issue code。不排序修复、不删除重复、不插值、不缩短窗口后继续放行。离散采样不能证明两次报价之间没有触价；采集端声明和完整性摘要也不能独立证明不存在隐瞒的数据。

## M13 已发布证据

M13 begin 的 market_at 是 Bar 的排他 close 边界，但已发布 CSV 没有独立 Bar open、closed 标志、known_at 和 live 提交凭据。因此适配器不从 source 字符串倒推缺失证明，不把 begin 时间当成 known_at。

唯一真实提案发生在 `2026-11-15T12:27:19.763402Z`，早于 R11 冻结 `12:27:55.334651Z`；后续报价不能把旧提案提升为冻结后提案。复现结果保留 `INVALID_TIMELINE`、`M13_BAR_KNOWLEDGE_NOT_PUBLISHED` 等原因，合格工程指标与金融指标均为 null。原 CSV 不复制，输入记录绑定其紧凑事件摘要。

## 输出语义与工程指标

时间/完整性、样本、receipt 和金融准入分别输出。合法声明为 `VALID_ENGINEERING_OBSERVATION`；时间倒置为 `INVALID_TIMELINE`，其他完整性错误为 `INVALID_EVIDENCE`。零提案或完整性失败为 `INSUFFICIENT_SAMPLE`；合法非空输入为 `NOT_ASSESSED`，因为最低金融样本阈值未批准。真实性始终 `UNVERIFIED_RECEIPT`，金融始终 `NOT_FINANCIALLY_ADMITTED`，metrics 始终 null。

完整输入才输出工程指标：有效 Bar/提案数；Bar 覆盖秒数除窗口秒数；完整报价覆盖提案数除总提案数；中断次数；观察触价（BUY midpoint <= limit，SELL 反之）；盘口候选（BUY ask <= limit，SELL bid >= limit）；未观察到触价的右删失比例；capture 到共同窗口/deadline/已记录终止三者最早边界的持续时间；`(limit / bid_or_ask - 1) * 10000` 距离；已采样 midpoint 相对 limit 的绝对偏离范围。

覆盖要求首尾及相邻报价间隔不超过声明 max_quote_gap_seconds。失败时只有 diagnostics 输入计数与缺口信息，不输出修补过的指标。价格偏离仅为采样范围，不是持仓浮亏、连续路径极值或可成交价格。

## 金融评价准备，未激活

`financial_contract()` 返回阻断项；`financial_preflight()` 检查未来声明是否包含精确四组对照和共同条件，不审批凭据、不运行账户，即使 DECLARED_ONLY 也不激活。

未来必须另行冻结批准版本，包含策略代码/参数、风险版本、数据身份、窗口/频率、资本/初始库存/名义上限、统一费用与滑点、实际资金费结算、固定持有事前数量、滞后目标可知时间、订单超时/部分成交/撤单/风险退出、最低 Bar 和持续时间、经济失败条件及共同终止时间。R09 必须单独冻结，不能隐式纳入 R11。

复用边界：Paper `run_experiment` 产生账户与订单证据；其旧 end 过滤为 inclusive，未来适配必须事先转换到半开窗口。FIFO `attribution` 要求 result.json、trades.csv、daily.csv 与权威 PaperStore 成交相互一致，不能由影子报价制造这些文件。资金费复用 cashflow 和 drawdown，以实际结算时持仓和现金流接线，不能将历史 overlay 视为可执行现金。既有 exposure 对照必须由批准的资本、固定持有规模与滞后目标驱动，不能沿用历史硬编码。

NAV 包含初始资本、已实现/未实现损益并扣费；滑点在成交价中不重复扣除。回撤路径包括初始资本。残余库存共同末价估值，不虚构清仓；持仓年龄截至共同结束。FIFO 完整闭环与风险退出污染单列，资金费、手续费、滑点及资金占用分别归因。

未来判断必须同时回答：

1. 共同资本/敞口的 Grid 与三类对照账户差异，而非闭环数量。
2. 完整闭环与残余库存的 FIFO 贡献，库存尾部是否继续主导亏损。
3. 风险退出前后暴露时间与同等暴露对照，区别减仓与收益质量改善。
4. 成本及实际资金费是否抵消闭环利润。
5. 数据完整性、样本持续时间和预注册失败条件是否足以支持判断；不足则继续 NOT_ESTABLISHED。

无真实合格前瞻样本时不改变金融状态。复现命令和实际测试结果见 [验证记录](../validation/p7-r12/README.md)。
