# P7-R11：Grid 前瞻验证工程协议

**NO LIVE MONEY · BLOCKED · NOT_ESTABLISHED**

本版本冻结协议工程与评价入口，金融参数尚未批准，不启动交易或金融评价。历史 R04B `[2026-05-14,2026-08-12)`、R07 `[2026-08-12,2026-11-10)` 均已用于研究，不能重新称为未触碰 OOS。R08 的后窗账户亏损主要来自残余长期库存；闭环盈利不代表完整账户盈利。

## 身份与不可变性

入口为 `python -m factorlab.experiments.grid_forward`，复用 `ExperimentTracker` 的权威 JSON、规范化哈希、排他硬链接发布和读取校验；SQLite 只是现有可重建索引，不是第二个研究数据库。

协议身份为 `r11-<json_hash(protocol)>`。版本名、冻结时间或任意规则改变都会改变身份；再次发布同一身份会失败，不覆盖。工程 schema 为 `P7_R11_ENGINEERING_V1`，金融状态固定为 `PENDING_APPROVAL`，不能通过编辑状态字段激活。金融激活必须建立新的不可变版本，保留此版本及原结果。

[冻结证据](../validation/p7-r11/protocol.json)中的哈希保证内容绑定，不是交易所真实性或第三方时间戳证明。收集者声明的时间及 receipt 需要独立来源验证后才能用于金融资格判断。

## 金融激活前必须批准的字段

工程版本没有默认金融配置，以下字段均是激活阻断项，而不是隐含沿用历史参数：

| 类别 | 必须冻结的内容 |
|---|---|
| 策略 | Grid 源码提交 SHA、完整参数、原始策略身份；R09 是否纳入及其规则/源码版本 |
| 数据 | 数据源、NVDAUSDT 合约身份、观察频率、UTC 半开窗口 `[start,end)`、采集与可知时间证据 |
| 执行 | 闭合 Bar、只在后续观察执行订单、超时撤单确认、部分成交与风险退出语义 |
| 成本 | 统一手续费/滑点、资金费来源与结算时点；不能把事后 overlay 当作可执行现金 |
| 预算 | 初始资本、名义敞口上限、初始库存与估值、固定持有数量、滞后目标算法 |
| 会计 | NAV、回撤、FIFO、库存年龄、残余库存、闭环、成本与资金占用定义 |
| 判断 | 主要指标、统一对照、经济失败条件、最低 Bar 数及持续时间、样本结束边界 |
| 中止 | 缺失/重复/不连续数据、未确认执行和研究中止规则；不得按结果择时结束 |

R08 的 1000 USDT、零库存、1h、100h TTL 和 4/6 bps 基础成本仅是历史参考，不作为当前金融授权。本基线没有可接入的 R09 冻结研究或 M13 证据适配，本协议排除 R09，不等待外部数据运行。

## 固定的比较与会计契约

原始 Grid、固定持有、既有滞后动态敞口、现金使用相同初始资本、预先声明的敞口边界、UTC 时间轴、估值及成本口径。固定持有规模必须在开始前确定，不能以未来平均库存倒推；滞后目标只使用当时已经可知的 Grid 状态，在后续观察执行。现金假定零交易、零资金费、零利息。R09 只有在单独冻结后才能进入新版本。

NAV 包含初始资本、已实现及未实现损益，扣除费用；滑点已进入成交价，不重复扣除。资金费调整 NAV 是基础 NAV 加有符号资金费现金流。最大回撤从包含初始资本的路径计算。库存按 FIFO，年龄截至统一实验结束，残余按共同末价估值，不虚构末尾成交。完整闭环是正常 SELL 完全耗尽 BUY 批次且未被风险退出污染。

评价接口声明的指标包括期末 NAV、净收益、资金费调整净收益、回撤、平均/最大持仓敞口、已实现/未实现损益、库存年龄与剩余数量、完整闭环数及净贡献、手续费、滑点、资金费及最大资金占用。对照差异按同一资金费调整后账户净收益计算。统计推断、Paper 经济结果与交易所可执行结果必须分开；闭环数量不能单独决定成功。

工程版本只冻结这些接口契约，不输出金融指标。金融激活的 Paper/FIFO/资金费连接与外部证据内容验证留在入口的 TODO；审批金融配置后应在新版本实现，不能解除本版本的 BLOCKED 状态。

## 数据类别与时间准入

- **A / HISTORICAL_REFERENCE**：仅设计、参考或测试；评价入口拒绝。
- **B / FORWARD_PAPER**：冻结后新观察；允许检查时点证据，但不代表真实成交。
- **C / DEMO_CONFIRMED**：必须包含交易所确认的 trade/order ID 与 `EXCHANGE_CONFIRMED` 来源；影子价格触及、仅订单接受或自行构造的成交不是成交确认。

最小证据格式为 `{"kind":"FORWARD_PAPER","observations":[]}`。非空观察要求 `opened_at`、`closed_at`、`known_at`、`decision_at`、非空 `receipt` 以及布尔 `completed_bar: true`。全部时间必须显式 UTC，满足：

`frozen_at <= opened_at < closed_at <= known_at <= decision_at <= evaluated_at`

Bar 使用连续半开边界，后一 Bar 的 open 必须等于前一 Bar 的 close。重复、乱序、断档、未闭合与未来信息拒绝。`closed_at` 是区间右边界，不是历史 CSV 的结束前一微秒标签；适配时必须显式转换。Demo 类型的每个输入观察是成交证据观察，还须具有上述交易所确认字段；它不是允许把每个市场 Bar 自动视为成交的格式。

当前入口返回 `UNVERIFIED_RECEIPTS`：只校验提供的时间声明与字段，尚不授予金融样本资格。不存在金融阈值时，有观察也不能宣称样本充分或不足，输出 `NOT_ASSESSED`；空观察明确输出 `INSUFFICIENT_FORWARD_SAMPLE`。两者均 `BLOCKED`、`NOT_ESTABLISHED`、`metrics: null`。

## 可复现运行

在安装当前项目的 Python 环境中：

```sh
python -m factorlab.experiments.grid_forward freeze \
  --store /path/to/private/r11-store --version engineering-v1 \
  --frozen-at <explicit-UTC-timestamp>
python -m factorlab.experiments.grid_forward evaluate \
  --store /path/to/private/r11-store --identity <freeze-returned-id> \
  --evidence docs/validation/p7-r11/empty-evidence.json \
  --evaluated-at <explicit-UTC-timestamp-after-freeze>
python -m pytest tests/test_grid_forward.py tests/research/test_experiment_tracker.py -q
```

`evaluate` 不创建账户、下载行情或发送订单。同一协议、证据及评价时间产生相同结果；CLI 输出 JSON，由调用者保存成新证据，不回写覆盖实验定义。冻结记录可以复制到新 tracker 的 `experiments/` 中，再由现有 tracker 重建索引。

小规模测试与实际 CLI 空样本输出见 [验证说明](../validation/p7-r11/README.md)。当前成果为工程协议冻结，不是 Grid 有效性的金融结论。
