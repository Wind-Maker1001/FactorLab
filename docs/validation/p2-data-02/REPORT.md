# P2-DATA-02：公司行为与目标仓位执行集成

`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

实际基线：`967f9201ce349676b9ad3f2719c162176ffab9fd`。2026-11-07 获取最新
`origin/main` 后创建 `codex/p2-data-02-corporate-action-execution`，工作区为
`D:/Code/FactorLab-worktrees/p2-data-02`。参考提交 `305b7c4` 是该基线的祖先。
不修改 PR #10/#11、冻结比较器、Golden、历史测试清单或任何金融批准条件。

## 交付结论与接口

公司行为已经接入 `run_target_positions(..., dividend_plan=plan)` 的实际订单循环。
订单仍由原有 `AShareExecutionBacktester._execute_order()` 执行，复用手续费、整手、
停牌、涨跌停和 `PositionBook` 的 FIFO/T+1。单证券的其他目标仓位策略直接使用相同接口。
原有 NaN=持有、0=退出、正权重=空仓时进入的语义保持不变；本次没有增加连续再平衡。

新增独立入口 `run_integrated_software_backtest(bars, calendar, dividend_plan, config, ...)`
复用 `moving_average_signals()`，策略和买入持有基准分别使用相同执行器及独立账户状态。
原有 `run_software_backtest()` 对非空公司行为和变化复权比例的拒绝，以及真实数据 CLI
的正式金融准入拒绝均保留。

```python
from factorlab.backtest.target import run_target_positions
from factorlab.strategies.dual_ma import run_integrated_software_backtest
from factorlab.data.corporate_actions.execution_evidence import (
    DividendExecutionPlan, EventSupplement, DividendTax,
)

# plan 必须绑定已经审阅的 snapshot、原始事件、版本和逐字段证据。
# 合成完整构造示例见 tests/research/test_dividend_execution.py 的 plan_for()。
# 真实事件税务目前只接受 UNKNOWN；合成税率不适用于真实账户。
result = run_target_positions(targets, bars, calendar, dividend_plan=plan)
```

## 每日顺序与统一对账

1. 开盘前仅释放实际支付日期早于当天、且税务合同允许的资金。
2. 使用 `available_cash` 执行上一收盘产生的订单，结算成交与费用。
3. 除息日确认应收；当日支付只有日期证据时，在订单之后入账。
4. 登记日依据当日成交完成后的真实收盘持仓锁定权益及买入批次。
5. 使用 `raw_close` 估值。没有强制期末清仓。

同一事件登记日必须早于除息日，除息日不晚于支付日。登记、除息必须有完整持仓观测；
支付可以发生在非交易日或研究期之后。非交易日保留 `payments.actual_date`，
在下一观测日开盘前体现此前已支付资金，另记 `observed_on`，不改写事件支付日期。
同日支付一律不能用于当日开盘订单；未提供可验证日内时间的更激进规则尚不支持。

主执行器只有一份可用现金；公司行为状态机返回新增现金差额，独立保存应收和受限现金，
并以事件 ID 防止重复支付。卖出、失败订单、重试均不会重置登记权益。

每日可复算字段：

| 字段 | 含义 |
|---|---|
| `opening_available_cash` | 当日开盘可用余额，不包含当日日期型付款 |
| `available_cash` | 收盘可交易现金 |
| `restricted_cash` | 已支付但税务未知的税前红利，不供交易 |
| `cash` | `available_cash + restricted_cash` |
| `receivable_gross` | 尚未支付的税前应收 |
| `recognized_receivables` | 已知合成税率时为税后应收；税务未知时按税前列示 |
| `market_value` | 实际股份 × `raw_close` |
| `portfolio_value` | `cash + market_value + recognized_receivables` |
| `equity_net` | 税务未知且有非零权益后为缺失值，不报告精确税后权益 |
| `tax_accrued` | 显式合成税率于除息日确认的累计税额 |
| `equity_before_tax_charges` | `portfolio_value + tax_accrued`，仅账务桥接，不是假设再投资税前策略收益 |
| `trade_cash_flow` / `dividend_cash_flow` | 成交含费用流量 / 本观测日新增可用红利流量 |
| `lots` | 买入日期与剩余股份的不可变快照 |

`strategy_nav/strategy_return` 基于上述账面权益；`net_status=UNKNOWN` 时不能解读为税后表现。
权益锁定、除息确认、支付分别输出 `entitlements`、`recognitions`、`payments`。
现有离线 `cash_dividend_ledger()` 保留，作为税前复算对照，不在订单循环后再次入账。

## 证据补充合同

`CorporateAction`、`EventCoverage`、`SourceSnapshot`、既有 snapshot 存储不变。
`EventSupplement` 单独保留原始事件和来源版本；执行事件必须保持原始事件 ID、证券、
来源 ID/哈希、类型和合成标记，增加一个修订号并具有严格更晚的可得时间。
不覆盖 CSMAR 的 `PROPOSED` 候选，不填充固定登记日期。

审阅后提交一个独立修订及关联 JSON `SourceArtifact`。JSON 至少包含：

```text
original_sha256      = sha256(json_bytes(original_event))
implemented_sha256   = sha256(json_bytes(reviewed_revision))
source_version       = 原始来源版本
fields.<field>       = {value, source_sha256, locator, available_at}
```

执行必需字段包括登记、除息、支付日期、现金金额、状态、公告日期、币种、基准、规则、
原始条款以及现金事件的显式零股份条款和参考价。来源文件必须全部绑定 snapshot artifacts，
每次执行先校验实际文件字节/哈希；关联 JSON 精确绑定原始与修订内容。
字段证据不得晚于修订的可得时间，修订必须在登记日上海时间 15:00 前可得。
日期型可得时间使用既有当日末上界，不擅自视为开盘前可得。
原始文件取得时间仍保存在 `SourceArtifact.acquired_at`，不伪装历史取得时间。
未解决的 `snapshot.issues`、遗漏来源、重复修订、缺日期全部拒绝。

`COMPLETE` 还要求其来源文件实际包含证券、范围、完整修订 ID 枚举和
`completeness_statement`；且覆盖声明在首日开盘前可得。仅给 COMPLETE 标签无效。
这是可审阅、可重放的证据合同，哈希与声明本身不构成独立金融认证；本 API 不批准市场数据。
本最小执行合同不自动处理登记后才知悉的修订或追溯事件，必须另外补证并明确解析修订。

## 税务与不支持事件

支持税前金额、`UNKNOWN`、以及只针对 `synthetic_only=True` 事件的显式合成平税率。
合成合同为 `DividendTax(event_id, "SYNTHETIC_EXPLICIT", "0.1", "SYNTHETIC_FLAT_RATE_V1")`。
税前验收显式使用合成税率 0，并不推定真实投资者免税。
未知税务不作净额推算，不把税前付款自动转为可用资金。

现有持仓本来就有 FIFO 买入日期和剩余股份；新增 `lot_snapshot()` 只读导出，
连同逐日快照、权益快照和成交历史保留未来持有期税务证据。
尚未实现个人/机构身份、持有期红利税、出售后补扣、托管支付条件的完整规则。
不把两类投资者统一为同一税率，也不搭建完整证券清算平台。

送股、转增、配股、拆合股、复杂组合、代码变更等均明确失败。
`TRDNEW_Capchg` 的股本存量绝不转换成投资者持仓变动。

## 人工复算

案例一：初始 10000 元；1 月 2 日以 100 元买 100 股，无费用，合成税率 0。
1 月 3 日登记，1 月 4 日除息，每股 2 元，1 月 8 日支付。

| 估值日 | 持股 | 原价 | 可用现金 | 应收 | 股票市值 | 总权益 |
|---|---:|---:|---:|---:|---:|---:|
| 01-02 / 01-03 | 100 | 100 | 0 | 0 | 10000 | 10000 |
| 01-04 / 01-05 | 100 | 98 | 0 | 200 | 9800 | 10000 |
| 01-08 收盘 | 100 | 98 | 200 | 0 | 9800 | 10000 |

01-08 开盘余额仍为 0；01-09 才能用这 200 元。支付不再额外增加收益。
详见 `evidence/manual-*.csv`，与既有离线税前股息账本逐日一致。

案例二专门隔离资金时间：所有交易原价均为 2 元；初始 200 元；01-03 买 100 股并登记；
01-04 卖出得 200 元，保留每股 2 元的红利权利；01-05 收盘发出再次买入目标。

| 支付与税务 | 01-08 开盘现金 | 01-08 可买数量 | 说明 |
|---|---:|---:|---|
| 01-05 支付，合成税率 0 | 400 | 200 股 | 卖出 200 + 已付红利 200 |
| 01-08 支付，合成税率 0 | 200 | 100 股 | 当日红利只能收盘后可用 |
| 01-06 周六支付，合成税率 0 | 400 | 200 股 | 保留周六实际日期，周一观察并可用 |
| 01-05 支付，税务未知 | 200 | 100 股 | 另有受限现金 200，不进入预算 |

该固定价案例只验证软件资金路径，没有模拟除息价格经济效果，不能用作真实收益样本。
每笔买卖、费用为 0、现金与应收均可见 `evidence/*-nav.csv`、`*-trades.csv` 和摘要。

## 中国石油 2018 边界

重放 `E:/CSMAR Raw` 原始 Cptl ZIP，复用原解析器；核对候选 prepared-v4 的 18 个文件哈希。
归档官方公告 PDF 重新读取并验证哈希、登记/除息/付款日期及金额。完整诊断见
`evidence/real-2018-diagnostic.json`，不提交私有原始行、价格、ZIP 或 XLSX。

| 登记日 | 除息/支付日 | 官方税前每股 | 官方 PDF SHA256 |
|---|---|---:|---|
| 2018-06-20 | 2018-06-21 | 0.06074 | `9e26fe2216c5264e809b92cfe4cb8c0e47c14961d203a9c2889d4f1285361f51` |
| 2018-09-20 | 2018-09-21 | 0.08880 | `79b1e9ee30b0a3e7a71df9e803c33a620169d61646ed2f77b8367e366b0653c6` |

CSMAR 原始公告日期分别比公告落款晚一天，诊断中同时保留。取得/可得时间为 2026 年，
不能从公告的 2018 年日期推定已经具有历史 PIT 证据。
原始事件仍缺登记日、显式零条款、参考价及已验证规则，状态仍为 PROPOSED。
两次公开分红的佐证不证明全年没有其他公司行为，覆盖仍为 PARTIAL。
244 个预热观测、243 个研究观测、MA5/MA20 的 5 次金叉与 6 次死叉已复现。
`Adjprcwd` 仍是信号候选，不认定为通过 HFQ/PIT；原入口继续拒绝变化的复权比例。

真实收益：**BLOCKED / NOT_RUN**。除了以上事件证据，官方交易日历、历史证券身份、
逐日状态及价格复权资格、账户税务/托管条件和独立正式金融批准仍需按既有合同完成。
本次没有新增例外。

## 验证与复现

新增集成测试覆盖登记前后交易、应收/支付、未知税、合成税率、现金预算、周末付款、
重复防护、重试、输入顺序与未来扰动、原价隔离、无事件兼容、双均线/基准和证据拒绝。
Python 3.13/3.14 的结果、选择与实现哈希见 `evidence/test-summary.json`。
每个版本：新增集成 42 项均通过；相关执行/策略/股息/安全回归 202 项通过；
仓库声明的核心测试及前向金融门禁 471 项通过（这两组包含重叠测试，不相加为独立数量）。
完整日志和 XML 保存在 worktree 的 `local-validation`，摘要绑定其 SHA256。
使用仓库未修改的 `tools.ci.validation_layers` 排除已冻结历史节点，不重复复审 Legacy。
初次扩大到历史 market_admission 收集节点触发缺少旧工作流错误，XML 保留在
`local-validation/related-313.xml`、`related-314.xml`；同一工作流在开发基线也不存在。

```powershell
& D:/Environments/factorlab-py314/Scripts/python.exe -B -X utf8 -m pytest tests/research/test_dividend_execution.py -q
# 验收每个分组的精确命令保存在 evidence/test-summary.json；额外全量非历史测试可使用：
& D:/Environments/factorlab-py313/Scripts/python.exe -B -X utf8 -m pytest -p tools.ci.validation_layers --current-layer-inventory=local-validation/selection-313.json -q
& D:/Environments/factorlab-py314/Scripts/python.exe -B -X utf8 docs/validation/p2-data-02/reproduce.py --output local-validation/recomputed
# 授权本地数据可选诊断（不执行真实收益回测）：
& D:/Environments/factorlab-py314/Scripts/python.exe -B -X utf8 docs/validation/p2-data-02/reproduce.py --output local-validation/recomputed --candidate E:/FactorLabData/candidates/csmar-601857-2018-20261105 --source-root 'E:/CSMAR Raw'
```

下一步最小任务：为两次已佐证事件制作审阅后的逐字段修订关联，同时取得完整范围清单与
历史可得时间证据；税务另行定义投资者身份/持有期/补扣合同。复杂公司行为仍需独立执行合同。
补证前维持真实收益阻断，可继续独立验收合成软件能力。
