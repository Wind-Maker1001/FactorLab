# P3-D02：CSI500 历史行情缺失诊断

研究窗口：2021-09-13—2026-09-11。基线：`a64ebab03b3e7ca99fbb47fbd41a2161cad36d98`；P3-D01 证据提交：`3046f0d762628dac4b17f3df3c683b7e38977b9c`，PR #24 未合并。

**P3D02_RESOLVED_PARTIAL — NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

已精确定位全部292个缺失键和565个任一诊断字段为空的证券日；已确认代码变更、退市/承接上市边界及564个停牌关联事件。283个代码差异有原生CSMAR逐日OHLC一致性支持，554个停牌空值有CSMAR非节假日填充合同支持。剩余10个CSMAR目标键缺席、供应商字段/历史改码合同、指数移除与历史可知时间仍按下文保持UNKNOWN；没有应用行情修复。

## 计数与定位

|对象|本轮实际提取|
|---|---:|
|原始成员日分母|605,500|
|匹配行情|605,208|
|缺失键|292|
|匹配行 open/high/low/close/amount 各列空值|564|
|匹配行 pre_close 空值|1|
|OHLC 任一列空值的唯一证券日|564|
|上述六个字段任一列空值的唯一证券日|565|

564 不是四列 OHLC 空值数之和；四列合计为 2,256 个字段空值，实际影响 564 个证券日。pre_close 的单独异常是 601868.SH 在 2021-09-28 的记录。

逐行明细仅保留于本地私有诊断目录 `p3-d02-20261108-v2`：

- `missing_keys.csv`：全部292键，原始 stock_code、字面转换后 instrument、成员 CSV 物理行定位；不存在的行情记录没有伪造源行坐标。
- `null_fields.csv`：565个证券日及各字段原值，包含 ZIP 成员、零基行坐标和 receipt 路径。
- `null_status_comparison.csv`：逐证券日状态和独立停牌原因对照，保留双方物理源坐标。
- `missing_candidate_comparison.csv`：仅供核查的候选代码记录，不用于重新计算匹配率。
- `source_receipts.json`：来源导出时间、查询窗口和既有版本收据。未重新哈希供应商整库，收据中 SHA 声明不等于本轮重新验证。
- `continuous_intervals.json`：每证券连续异常区间；日历基准为原成员表的1211个观测日期，不冒充独立交易所日历。
- `per_key_evidence.jsonl`：292个缺失键及565个空值证券日的统一证据账本，分别保存原代码/候选代码存在性、源行、停牌记录、CSMAR原生记录、公告来源、有效时间与UNKNOWN。

成员来源为 `csi500_full_history.zip/members_daily.csv`；行情来源为 BigQuant `stock.zip/stock/cn_stock_real_bar1d/`。仅做 `.XSHG→.SH`、`.XSHE→.SZ` 转换，无跨代码拼接。明细中的 CSV 行号从含表头的第2行起；Parquet 坐标是整个分片内零基行号。

## 缺失键归因

|证券|数量|连续观测成员日期区间|可证实观察|原因裁定|
|---|---:|---|---|---|
|300114.SZ|283|2023-12-11—2025-02-14|同日302132.SZ候选记录283条；发行人公告确认2025-02-17才启用新代码|CONFLICT：供应商回溯代码与历史交易代码边界；代码变更本身CONFIRMED|
|600068.SH|9|2021-09-13—2021-09-27|发行人公告确认2021-09-13退市，所读窗口相交分片最后原代码记录2021-09-10|CONFIRMED退市日及之后；指数移除及完整换股执行仍UNKNOWN|

292个缺失键分布于292个日期。身份细节、源物理坐标与补证要求见 [身份边界](P3_D02_IDENTITY_BOUNDARIES.md)。候选行情的存在不能证明应当补回，也不能证明成员表本身错误。

## OHLC 空值与停牌证据

564个证券日的 OHLC/amount 同时为空，成交量与成交笔数均为0；BigQuant 状态记录全部 suspended=1，且在独立 `cn_stock_suspend` 表存在同日停牌原因记录：

|停牌原因原文|证券日|
|---|---:|
|临时停牌|10|
|拟筹划重大资产重组|53|
|配股|12|
|重大事项|279|
|重要事项未公告|34|
|重要事项未公布|5|
|重要公告|171|
|合计|564|

因此可确认这些记录有来源明确的停牌事件关联，不是仅凭 suspended 推断。**停牌事件证据为 CONFIRMED；“供应商合同规定停牌日必须将原始价格置空”的因果合同仍 UNKNOWN。** schema 为零行查询获得的字段名称和类型，不包含该语义合同。未把空值归因为涨跌停，未将零成交量转换为零价格或零收益。

pre_close 单独缺失记录的 suspended=0，OHLC存在；不得归入上述停牌空值集合。发行人[上市公告书](https://static.cninfo.com.cn/finalpage/2021-09-27/1211161840.PDF)确认601868.SH于2021-09-28上市，基本信息快照行4160同样记载该日。故上市首日边界CONFIRMED；BigQuant该字段在换股上市首日的缺值合同仍UNKNOWN，不能直接取招股价或600068上一日价格代替。CSMAR的Dretwd/Dretnd首日前价规则不等于其PreClosePrice字段合同，更不能移用于BigQuant。

## 数据源对照

复用既有catalog，读取2019—2022及2023—2026两个原生回报率ZIP中与异常证券有关的10个工作簿。XLSX顺序存储使查询实际经过9,495,295个物理数据行，只保留目标异常键与明确候选代码，没有重新做全库统计或下载；查询读取成本约为几十分钟。每个工作簿复用P3-02 `native.workbook/shared_strings/sheets/rows`，验证所读工作簿成员哈希，未重新哈希大ZIP；catalog的ZIP哈希仍是既有绑定声明。原生单元格保持原始字符串，物理sheet/row、字段名称/单位见私有 `csmar_target_rows.jsonl` 和 `csmar-query-receipts.json`。

|异常集合|CSMAR原生目标键返回|对照裁定|
|---|---:|---|
|300114.SZ缺失283键|283|CSMAR保留历史旧代码；四列OHLC全部与BigQuant新代码候选一致，绝对容差1e-8；CONFLICT是代码表示，不是行情数值冲突|
|600068.SH缺失9键|0|NOT_AVAILABLE；与正式退市边界相容，不据此补造行情|
|564个OHLC空值证券日|554|554个全部Filling=1；CONFLICT为来源空值/填充值表示差异，不等于真实成交矛盾|
|余下OHLC空值证券日|10|NOT_AVAILABLE：所查原生工作簿未返回键，原因UNKNOWN|
|601868上市首日pre_close空值|1|CSMAR有前价字段值，CONFLICT为字段表示；不据此覆写BigQuant|

共返回838个唯一目标证券日，无重复冲突。此处 `csmar_status=CONFIRMED` 只指原生记录存在且重复值无冲突，不表示证券身份、可交易性或金融事实正式认证。

原始 `TRDNEW_Dalyr[DES][xlsx].txt` 字段合同明确：`Filling=0`为正常交易不填充，`1`为非节假日填充，`2`为节假日填充；`Trdsta=1`是正常交易/ST分类语义，不是独立的停复牌标志。554个停牌关联行虽有价格，但必须携带Filling=1，不能当成真实成交观测。CSMAR `PreClosePrice`是交易所接收的除权昨收；Dretwd/Dretnd的上市首日招股价规则属于回报率计算，不能混用。

未返回的10个OHLC空值键集中在000959.SZ的2026-03-27—2026-04-10区间（按观测成员日期连续）；BigQuant有“重大事项”停牌原因。CSMAR该局部缺席的原因仍UNKNOWN，不扩大为整证券或整个年度无覆盖。

这些记录把554个空值的跨源差异具体定位到“BigQuant空值、CSMAR非节假日填充”，并支持283个缺失键为供应商代码命名空间差异。BigQuant空值合同、历史改码合同及当时可知身份仍保持UNKNOWN。

已定向读取 BigQuant `common.zip/common/trading_days`：包含pre_close单点在内共653个异常日期全部存在CN市场交易日记录，物理定位与收据保留在 `target_calendar_rows.csv`、`calendar-query-receipts.json`。这是同一供应商日历，不是独立交易所证据；区间计算仍明确采用成员日期，不以周末/工作日推断开市。原始状态资料按异常涉及证券查询，没有重做全市场十年统计。

## 缺失敏感性与最小修复

缺失键占原分母 **0.0482246%**，OHLC空值占 **0.0931462%**。两集合不相交，共856个证券日，保留分母后有604,644个成员日存在完整OHLC（99.8586292%）。这只是字段覆盖，不是因子可用、可交易或收益可计算的认证。

OHLC空值涉及70只证券、435个日期、88段连续观测成员日区间。键缺失或OHLC空值合计涉及652个日期，每日最多4个成员不可用，完整OHLC成员数最低496/500。

- **横截面因子覆盖**：标记不可用观测，仍报告500原成员分母；使用成交额、复权或更长回看窗口的因子可能损失更多样本。
- **股票池筛选**：停牌与退市应作为有时效状态，不通过删除成员重写历史股票池。
- **RankIC**：单日仅按该日OHLC完整性可提供496—500个候选；实际有效N还依赖因子、标签期和身份资格，不能把604,644当成已验证RankIC样本数。
- **Walk-forward**：按窗口交集报告缺失，回看/前瞻标签会传播缺失。未预设fold或计算未经验证的有效训练量。
- **可交易性**：564个停牌相关证券日不能以补价假设可成交；退市或换股应由公司行为与结算合同处理。

最小方案不是直接修行情：以已绑定的300114/302132生效日区分历史交易代码与供应商查询代码，补齐历史代码合同；以已确认的600068退市日和1:4.4337换股比例继续补齐登记/上市、指数移除证据；为564个停牌关联空值绑定字段合同；为pre_close单点绑定上市首日语义。每项补证到位后另行生成有来源的修复候选，不覆盖原数据，不填零、不前填、不删证券。

## 最小复现

在独立输出目录执行；参数均由操作者传入，不公开供应商原始记录：

```sh
python -I -X utf8 tools/data/audit_p3_d02.py --stock-zip '<BQ>/stock.zip' --membership-zip '<FD>/validation/joinquant_csi500_full_20261024/csi500_full_history.zip' --private-output '<NEW_PRIVATE_DIR>'
python -I -X utf8 tools/data/summarize_p3_d02.py --private-root '<PRIVATE_DIR>' --catalog '<BOUND_CATALOG>'
python -I -X utf8 tools/data/query_p3_d02_csmar.py --private-root '<PRIVATE_DIR>' --catalog '<BOUND_CATALOG>' --source-root '<CSMAR_ROOT>'
python -I -X utf8 tools/data/p3_d02_sensitivity.py --private-root '<PRIVATE_DIR>' --membership-zip '<MEMBERSHIP_ZIP>'
python -I -X utf8 tools/data/query_p3_d02_calendar.py --private-root '<PRIVATE_DIR>' --common-zip '<BQ>/common.zip'
python -I -X utf8 tools/data/finalize_p3_d02.py --private-root '<PRIVATE_DIR>' --public-statistics '<AGGREGATE_JSON>'
python -I -m pytest -q tests/evidence/test_p3_d02_diagnostic.py
```

`--extracted-private-input` 仅用于显式复用中断提取留下的两份异常明细，不代表独立重验来源；此次 v2 复用 v1 已写出的缺失/空值集合，后续源状态查询使用原始只读ZIP。最终账本共857个异常证券日条目；基线计数由finalizer逐字段严格核对，结果EXACT。

确定性fixture共8项通过：纯后缀转换、不隐式合并候选代码、键缺失与多列空值区分、只有suspended不足以归因、重复键拒绝、未分区快照、连续观测日分段、跨行组源坐标与原生记录冲突/缺席分类。原始数据只读，未改动605,500分母，未产生正式策略收益。
