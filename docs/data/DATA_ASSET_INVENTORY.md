# P3-D01：真实数据资产盘点

审查日期：2026-11-08。主线基线：`d165c65a17c1ff4b7d174a063a6607589cd38522`（本轮 HTTPS fetch 已核对）。

**P3D01_PARTIAL — NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

可访问资产已经足够开展限定范围的回顾性数据诊断与信号探索；不等于具备正式历史收益回测资格。PARTIAL 指未逐行诊断所有可访问表、未完成全市场跨源复权对照，并非已发现的数据全部不可用。

## 1. 来源定位与检查边界

为避免公开私人绝对路径，以下使用目录别名。别名对应本地目录的原有名称；复现时由操作者显式传入路径，不复制供应商原件。

|别名|实际定位依据与相对位置|本轮状态|
|---|---|---|
|BQ|已发现的 `BigQuant Raw`，含 stock、index、factor、industry、common、metadata、fund、future、option、cbond 共10个 ZIP|stock.zip 的目录、schema、收据及指定数据实际可读；其余部分仅目录/schema检查|
|CS|已发现的 `CSMAR Raw`；当前55个文件|目录可读；原生覆盖复用后续 catalog，实际重读已绑定的487行派生候选|
|FD|已发现的 `FactorLabData`|catalog、inventory_v2、candidates、validation 均可读|
|CN|既有 inventory_v2 索引定位的 `CNRDS Raw`|81个已索引文件当前存在；本轮财报覆盖复用已有资料，不宣称81个文件均已逐行读取|
|TDX|inventory_v2/files.csv 的 vipdoc 精确路径|9,374个已索引 .day 当前存在；实际读取1个文件，不能外推全库覆盖|
|M30|inventory_v2/files.csv 的 BaiduNetdiskDownload 精确路径|192个已索引文件当前存在；包含第三方30分钟数据，不等同于通达信原生分钟线|
|FE|既有 `FACTORLAB_FINANCIAL_EVIDENCE_20261028`|既有金融证据报告及原始定位清单可读；复用历史结果，不刷新冻结收据|

未在本轮有界目录、既有来源清单中定位到可绑定的**聚源、HBN、独立上交所/深交所全量日历与行情库、通达信原生分钟库**，状态为 **NOT_LOCATED_IN_CHECKED_SCOPE**，不是全盘不存在的证明。没有观察到权限拒绝；未来遇到拒绝应记录 **NOT_ACCESSIBLE**，不能与缺失混同。CNRDS、聚宽、聚源不是同一来源。

旧 inventory_v2 的9,697文件/13.83 GiB属于2026-10-24历史快照，未包含后增BigQuant和新增CSMAR文件；不能当作当前全资产总量。本轮只按旧索引确认9,697条路径仍存在，没有扫盘或重新哈希整库。

## 2. 本轮实际逐行检查

机器统计：[p3-d01-statistics.json](p3-d01-statistics.json)。原始行未进入报告或统计文件。

### 2.1 五年历史成员 × BigQuant 行情

输入：`FD/validation/joinquant_csi500_full_20261024/csi500_full_history.zip` 的 `members_daily.csv`；`BQ/stock.zip` 的 `stock/cn_stock_real_bar1d/*.parquet`。

- 范围：**2021-09-13—2026-09-11**，1,211个观察日期。
- 实际读取61个月分片，共6,417,495条物理行情行；质量统计仅针对932个历史成员候选代码过滤后范围，不是全市场质量认证。
- 历史成员：605,500行、605,500个唯一 `(date, stock_code)` 键、932个交易代码；重复超额0。沿用原表，不将代码当稳定证券ID。
- 候选代码的行情：1,091,414行、同数唯一 `(date, instrument)` 键、930个代码；重复超额0。这一分母包含不在成员期间的行情，不能与成员日直接比较。
- 按实际成员日连接：**605,208条匹配，292条键缺失**；缺失涉及 `300114.SZ`、`600068.SH`，分布于292个日期。只做 `.XSHG → .SH`、`.XSHE → .SZ` 字面后缀转换，没有跨代码拼接或来源优先级选择。
- 匹配行中open/high/low/close各缺564值，amount缺564，pre_close缺1。缺值原因未逐条归属，保持UNKNOWN，不自动视为停牌、零价格或无收益。
- 全候选代码行情中OHLC各缺1,647值、amount缺1,647、pre_close缺61；其他已检查列缺值0。非空数值无±inf，非空价格无非正值及OHLC顺序异常，volume/amount无负值。这些规则不检验极端涨跌、单位或经济合理性。
- 同名字段schema：date为timestamp，instrument/name为string，volume为int64，deal_number为int32，价格/amount/turn/复权及涨跌停字段为double。schema未证明价格调整语义、换手率是比例还是百分数、数量和金额单位；正式合同仍UNKNOWN。

历史成分是追溯查询结果。上述连接覆盖不是PIT批准，也没有把缺失的两个代码从中证500事实名单中删除。

### 2.2 中国石油2017—2018：两源对照

输入：`FD/candidates/csmar-601857-2018-20261105/prepared-v4`，以及BigQuant同证券同期真实价格表。

|项目|本轮实际结果|
|---|---|
|证券及日期|601857.SH；2017-01-03—2018-12-28|
|CSMAR候选 / BigQuant|各487行、487个唯一日证券键；重复0；日期外连接双方缺失均0|
|CSMAR候选缺值与基础异常|所有字段缺值0；价格非正/OHLC顺序/负量负额/±inf均0|
|两源OHLC及volume|487对逐字段数值完全一致|
|两源amount|4对数值不完全相等，最大绝对差 `5.960464477539063e-08`；保留差异，不自动舍入或裁定原因|
|单位|CSMAR已审映射为CNY/股、股、CNY；BigQuant此范围数值一致不代替其单位合同|
|已有信号范围|2017年244个预热观测，2018年243条研究观测；旧信号诊断5金叉/6死叉仅引用已有收据|

较早的 `DUAL_MA_2018_DATA_STATUS.md` 只针对当时两份2019年以后ZIP。后续新增原始日行情导出 `000057802` 已提供2015—2018原生观测；不能继续声称本地不存在2018行情。原始扫描与信号测试不重复执行。

两源数值相同不证明独立上游、历史可得性或HFQ认证。本轮没有把BigQuant adjusted字段与CSMAR Adjprcwd直接视作同算法复权价。

### 2.3 BigQuant公司行为与复权事件表

以下为**本轮全表实际读取**；不同表的date不是统一时间语义，不可直接相加或当交易日历。

|表|行数 / 候选唯一键数|证券数|date观察范围|主要缺值/证据问题|
|---|---:|---:|---|---|
|cn_stock_dividend|35,508 / 35,508|5,414|2016-11-08—2026-11-06|bonus_rate缺35,207；conversed_rate缺31,176；cash_before_tax缺390；cash_after_tax缺402；有publish/register/ex_date，无现金支付日字段|
|cn_stock_allotment|71 / 71|67|2017-04-11—2026-08-21|allot_listdate缺1；publish/register/exright日期及配股价格/比例无缺值；单位/参与缴款规则未认证|
|cn_stock_adjust_factor|69,310 / 69,310|5,363|2016-11-08—2026-08-19|字段缺值0；type/method存在，但算法、绝对锚点、因果可得性及事件全集未证明|

前两表候选键为 `(date,instrument)`，因子表为 `(date,instrument,type,method)`，候选键重复超额均0；不是供应商认证的主键合同。空送转/现金字段不能直接解释为0；不因有记录就推定实施完成、没有撤销或修订。

## 3. 其余资产的已知覆盖与UNKNOWN

### 3.1 CSMAR：复用原生catalog，不重复扫描大ZIP

依据：`FD/candidates/csmar-601857-2018-20261105/inventory/catalog.json`。以下是**已有原生扫描结果**，不是本轮重新哈希/逐行验证。行数为物理出现次数，范围是所选日期字段的观测范围，不保证期间连续。

|表|工作簿|物理行数|代码数|已有观察范围|研究用途/未决项|
|---|---:|---:|---:|---|---|
|TRDNEW_Dalyr|15|13,242,853|5,943|2015-01-01—2026-10-20|RAW OHLCV/amount、Adjprcwd及状态候选；全市场唯一键数、跨分片重复和缺值UNKNOWN|
|TRDNEW_Co|1|6,025|6,025|1991-04-03—2026-10-16|上市/公司身份当前导出，不等于历史身份版本|
|TRDNEW_Cptl|1|72,305|5,890|1991-02-26—2026-10-20|现金/送转/配股候选；不能用除息日代登记或支付日|
|TRDNEW_Capchg|1|144,074|6,035|1991-04-03—2026-10-20|股本存量观察，不自动映射为持有人获赠股份|
|TRD_AdjustFactor|1|62,523|5,890|1991-02-26—2026-10-20|因子事件观察，不是完整每日或可复算HFQ证书|

其他已发现表包括IDX_Idxtrd、IDX_Chgsmp、IDX_Smprat、IDX_Smpinfo、IDX_Idxstk、前/后复权行情、利润表FS_Comins、资产负债表FS_Combas、直接/间接现金流表及银行类财报、估值、融资融券、大笔交易。除既有针对性扫描外，它们的全表起止时间、唯一键、交易日覆盖、单位、缺失异常、历史修订均UNKNOWN；文件名只用于定位，不用来认证覆盖。

IDX_Chgsmp已有六导出2,632物理行、1,318不同事件键；导出之间存在重叠，不能合并后将行数称为独立事件。既有时间字段统计表显示IDX_Smprat所扫描成员覆盖2020—2026，但不是全部工作簿的无重复全集声明。

### 3.2 BigQuant：完整导出收据不等于完整数据验证

所有stock表收据总行数和schema保存在JSON。导出的查询窗多为2016-11-07—2026-11-06；日期仅为WHERE范围，不是实际最早/最晚观测。

|表|分片|收据声明行数|本轮读取级别|
|---|---:|---:|---|
|cn_stock_bar1d|121|10,853,543|schema/收据；未逐行检查调整价|
|cn_stock_real_bar1d|121|10,853,543|上述五年范围及601857两年实际读取，未诊断全十年|
|cn_stock_status|121|10,853,543|schema/收据；有ST、risk_warning、suspended、price_limit_status、exdr，历史可得性UNKNOWN|
|cn_stock_limit_price|121|10,825,886|schema/收据；与行情行数不同，不能假定全覆盖|
|cn_stock_suspend|121|134,005|schema/收据；suspend_period/reason不能未经合同用于补值|
|cn_stock_name_change|11|18,422|schema/收据；非稳定证券ID证书|
|cn_stock_basic_info / cn_stock_st|各1|5,924 / 5,896|无查询窗的快照，不能冒充历史序列|
|cn_stock_static_data|11|3,088,301|schema/收据；财务/身份语义待单独审查|

index.zip发现指数行情、成分、权重、变更、概念成分与行情；industry.zip发现行业分类、成分、变更及行业指数行情；factor.zip发现估值、基础/行业因子、MACD和高频命名因子；common.zip发现交易日、节假日、股票与退市证券目录。仅确认表/schema存在，逐行覆盖/单位/历史可得性UNKNOWN。未发现本轮能认证的主力资金、供应链或情绪序列；不能从高频因子名推断分钟原价已具备。

### 3.3 CNRDS、分钟、通达信和公告

- CNRDS原件清单包含CNSP日回报、基金持股、财报、财报披露时间FRDT、分红及其他公司资料。旧FRDT扫描269,999行：会计期末1999-06-30—2026-03-31；ActlDt缺108，非空范围1999-07-16—2026-04-30。ActlDt不是供应商首次可得时间；其他财报全量主键/修订链未检验。
- 第三方30分钟ZIP名称涉及2021—2025，2026有日文件；名称范围不当作实际覆盖。本轮未逐行检查分钟表或确认授权分发。
- TDX实际样本 `bj899050.day`：34,432字节，按32字节记录读出1,076条，日期整数20220429—20261009。仅证明这一文件结构可读；不证明全A股、分钟或复权可得。
- 已有CSI官方公告/附件、发行人公告及中国石油2018两份实施公告可用于字段补证；不等于官方完整行情/状态库。中国石油公告落款日与CSMAR Annodt各差一天，保留两值。官方PDF获取日期是2026年，不改写成2018年历史可得证据。

## 4. PIT、HFQ与来源身份

四种时间分列：事件生效时间、公告公开时间、供应商历史可得时间、实际获取时间。BigQuant receipt.exported_at_utc是导出时间；CSMAR字典哈希是内容版本；文件mtime是文件系统属性。三者均不能填历史available_at。publish_date或公告落款日也不能替代供应商首次分发和修订历史。

复用FE/CERTIFIABLE_SCOPE_ASSESSMENT.md及FD预准入重算：冻结窗932代码、605,500成员日；1,006变更动作，803公告标签差异原因UNKNOWN；历史稳定ID、known_at、真实实施和授权的认证动作各0。既有HFQ重点896事件中8有实施条款支持，888仍缺相应实施原件；135金额差异与34规则差异可能重叠，不加总。零事件区间不是全类别无事件证明。现有可完整认证证券日与持仓区间仍为0。

原始身份采用已有manifest/receipt的来源+成员+物理坐标/版本。既有JQ包SHA256为 `81d9b765a899be086e68cd1c6e6f0a181eddfe41e77f84e83e2a93feb94b4506`；CSMAR候选manifest既有绑定为 `d77417709364d664f1041418c51a434e836a72cbebbc53f7b2158556478ccb64`。这些值明确是历史收据引用，本轮没有重复计算大文件SHA或认证源独立性。BigQuant逐分片收据含query/rows/bytes/SHA及导出时间，SHA声明未重新计算核对；本轮实际Parquet读取与行数保存在JSON。

## 5. 复现与结果边界

使用现有Python、pandas和pyarrow，隔离读取，不运行供应商文件中的代码：

```text
python -I -X utf8 tools/data/audit_p3_d01.py --stock-zip <BQ/stock.zip> --membership-zip <FD/validation/joinquant_csi500_full_20261024/csi500_full_history.zip> --csmar-candidate <FD/candidates/csmar-601857-2018-20261105/prepared-v4> --inventory-root <FD/inventory_v2> --start 2021-09-13 --end 2026-09-11 --output <新的统计JSON路径>
```

输出必须是新文件。脚本不解压到磁盘、不调用收益执行器、不修改来源、不做去重/填补/跨代码映射。首轮读取因误认成员列名失败，实际表头为stock_code；修正后完成，失败运行未生成统计文件。后续只增加两源和事件表诊断，没有重扫已完成的五年行情。

优先补证与研究方案分别见 [FINANCIAL_EVIDENCE_GAPS.csv](FINANCIAL_EVIDENCE_GAPS.csv)、[RESEARCH_WINDOW_PLAN.md](RESEARCH_WINDOW_PLAN.md)。
