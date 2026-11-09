# P5-D01：历史数据字段语义与研究质量输出

2026-11-10；main基线 `d6d51622afffb49cd5764ab89b41f025f0d62599`。

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 实际修复及消费方式

本轮修复的是诊断丢失观察事实的问题，不是覆盖供应商价格。原预检一旦发现必需字段非有限或 OHLC 关系异常，整份查询就不能进入后续覆盖分析。新增的 `research_quality` 在来源、版本、范围内容身份、字段、日期、证券及主键通过检查后保留观察事实；严格的 `INPUT_REJECTED` 和原预检失败路径继续保留。身份不匹配或重复键不作为可信观察发布。

研究消费者从原 `pipeline_preflight` 结果读取：

- `research_quality.observations`：逐证券／供应商会话的数值可计算性、正常交易观察、上市前、退市后、停牌、应有而未观察到的行情、字段缺值、非有限／非数值、原价／调整价 OHLC 关系异常、生命周期冲突及历史成员未知。
- `research_quality.fields`：每个查询字段的声明单位及来源语义；数值失败不抹去已经解释的来源单位，数值有限也不会自动证明单位。
- `receipts[].metadata.field_semantics`：DataHub 查询携带的原生字段、供应商文档单位、标准化单位、转换乘数、证据等级、适用范围及证据位置。CSMAR 原生适配器同样传递已有字典和实际转换的结果，不增加行情能力。

`numeric_calculable` 只描述该行所有必需行情数值有限、非负且满足既有 OHLC 关系，不证明长回看因子已有预热，也不证明可成交。停牌、成员身份、来源单位和正式资格分别表达；正常交易观察也不等于历史资格认证。新结果是回顾性诊断，不得作为股票池选择掩码。异常只标记发生行，未来行的异常不扩散为历史整证券隔离；原 D06 计算隔离策略未改动。

原 `coverage_matrix` 仍表示严格预检接受的输入覆盖，`research_quality` 则保留通过身份／结构检查但因数值异常被拒的观察。两者含义不同，不能把前者的查询拒绝直接解释成供应商没有原始行。没有插值、补零、改价、跨代码拼接或从未来状态反推历史。

## 单位及来源证据

复用 P4-D02 已保存的当前网页响应，不重新获取供应商网页或数据；历史载荷继续绑定 P4-03 的冻结清单。

|字段／来源|已取得证据|本轮合同处理|
|---|---|---|
|BQ `cn_stock_real_bar1d` raw OHLC|[S08](../p4-d02/sources/S08/response.bin)说明未复权价格，但未给完整经济单位合同|保留 `UNKNOWN_VENDOR_CONTRACT`，记录原生字段和价格语义；不推断 CNY/share|
|BQ `cn_stock_bar1d` adj OHLC|[S11](../p4-d02/sources/S11/response.bin)声明未复权价乘累计因子|记录供应商调整价声明；经济单位及历史适用性仍 UNKNOWN，不认证 HFQ|
|BQ volume|S08 明确“单位为股”|`vendor_documented_unit=shares`；历史归档标准化单位仍 UNKNOWN|
|BQ amount|S08 明确“单位为元”|保留原文经济单位含义，币种未明确；不直接写 CNY|
|BQ `turn → turnover`|S08 明确“小数表示，0.05 表示 5%”|记录 fraction 声明及 rename-only；不乘／除100，不据此认证历史合同|
|BQ index open|现有 schema／收据只证明物理类型及内容身份|UNKNOWN；不借用 CSMAR 指数点合同|
|CSMAR 原生 Dalyr raw OHLC、volume、amount|已有 `profiles.PROFILES/MAPPINGS` 的已审 DES 哈希；`binding.replay` 校验原始字典、表头单位及原生输入，复用既有十进制转换|传递 CNY/share、shares、CNY、乘数1及字典身份；不重新转换或二次缩放|
|CSMAR Adjprcwd|既有 `market_candidate` 检查元/股并保留候选复权语义|传递 CNY/share；明确 comparable reinvestment close，HFQ_UNVERIFIED|
|CSMAR 指数|既有 IDX 字典规定价格为 index_points，量／额分别从万股／万元乘10000|仅验证既有转换，不将其合同转用给 BQ，也不新增 CsmarAdapter 的指数能力|
|JoinQuant 成员|已有成员文件、manifest 和冻结内容身份|只表达历史正成员观察；零行仍 UNKNOWN，不认证负成员历史或 known_at|

四类证据严格分开：当前供应商文档声明（`VENDOR_DOCUMENTED_CURRENT`）、原生字典校验后可确定的标准化结果（`REPLAY_VALIDATED_DICTIONARY_AND_CONVERSION`）、仅靠类型／数值猜测（本轮不采纳）、完全未知（`UNKNOWN`）。当前文档的历史适用性独立保留 UNKNOWN。来源字节和采集角色沿用 [P4-D02 SOURCE_INDEX](../p4-d02/SOURCE_INDEX.json)，2026 年采集／导出时间不变成 2021 年已知时间。

### 23 项单位缺口及最小补证

两证券各11项，加指数open共23项，全部继续保留 `UNIT_UNDECLARED`：

1. 两行情表分别提供版本绑定的 OHLC 经济单位、币种／计价基准、缩放及适用历史范围；调整价还需明确价格语义，正式 HFQ 算法／锚点仍走既有资格流程。
2. 原价表提供 volume 的股／手与缩放、amount 的币种和金额尺度、turn 的小数／百分数及分母口径，明确适用于冻结的2021年9—11月归档版本。已有当前页面的股／元／小数声明可直接用于询证，不需要重下数据。
3. 指数表提供 open 的指数点／基期／缩放及适用版本，不借股票价格或另一供应商字段补合同。

TODO：取得版本适用证据后，沿用已有单位转换和绑定合同，只补充证据所覆盖的字段／范围；在此之前不清除 UNKNOWN。

## 真实小窗口与预检变化

复用 [P4-03 冻结清单](../p4-03/input-definition.json)，两证券 `600363.SH`、`601868.SH`，输入2021-09-13—2021-11-22，研究2021-09-28—2021-11-12。保持原 source/kind/version/security、范围内容哈希、13个原始身份条目和窗口；没有重新盘点或接受新的 pins。

最终真实结果在 [final-receipt.json](final-receipt.json) 和 [final-INPUT_MATRIX.csv](final-INPUT_MATRIX.csv)：88个证券／会话格子，79行行情数值可计算；79个正常交易观察、9个上市前无行情、53个无正成员观察的身份 UNKNOWN。53 = 600363 的44日 + 601868 上市前9日，并不是53项供应商缺数。上市前9日不要求供应商补行情，预热不足仍是研究窗口本身的限制。

预检仍 **FAIL，26项**：23项单位、1项公司行为稳定身份映射、1项601868自身预热、1项600363研究段成员未知。工程失败数量没有减少；可用性改善是研究消费者能够知道哪些数值可计算、哪些单位只有当前供应商声明、哪些空格是证券尚未上市，而不是把26项强行清零。正式请求仍 REJECTED。

## 既有10行异常与292个缺失键的解释边界

以下直接引用已保存的 [D06 diagnostics](../p3-d06/diagnostics.json) 与 [D02 缺失报告](../../data/P3_D02_MISSINGNESS_REPORT.md)，不宣称本轮重新读取全部异常原件或重算五年78因子：

- 10行原价及调整价 OHLC 关系异常位于声明研究日期之外，涉及600363、601198；原 D06 整证券阻塞1,517成员日是回顾性计算下界，不是每行都不能计算或历史可知选股规则。本轮定向测试验证逐行异常分类、保留输入以及尾部异常不污染此前观察；本小窗口没有这10行异常。
- 292个缺失键中，283个300114/302132是历史交易代码与供应商回溯命名的身份冲突，已有候选行情及CSMAR逐日对照不授权自动改码；剩余9个600068与已记录退市边界相关，不要求补造退市后的价格。观察级上市快照的退市日采用原预检的包含端点规则，不能冒充官方退市日的交易有效性认证。
- 564个已存在键的OHLC／amount空值与停牌事件关联，不是292个缺失键，也不把四列重复相加；CSMAR其中554个键为非节假日填充，不能作为真实成交覆盖BQ空值。余下10个CSMAR目标键缺席仍UNKNOWN；它们不是上述10行OHLC关系异常。
- 保留605,500成员日、604,644完整原始OHLC、605,208状态键、598,112共同有限的原基线；这些是上游已保存观察，不是本轮新计算。

## 定向验证与复现

最终定向测试 **122 passed / 46.99秒**，记录见 [validation-tests.xml](validation-tests.xml)。覆盖异常／缺值查询仍保留观察、主键／日期／证券／来源身份拒绝、上市前／退市后不索取行情、停牌与数值分离、未知成员、未来行异常隔离、单位证据层级及消费者传递；复用原有正式准入拒绝测试和原生CSMAR单位转换测试。

首次新增测试的导入路径导致收集失败（0项执行），改为现有 tests.research 包路径后，第一轮118项通过。最终结果以 validation-tests.xml 为准；同一测试集合的多轮执行不相加。pandas在既有查询及观察拼接处对包含全空列的输入发出3条 FutureWarning，未改变观察或准入结果。TODO：统一上市快照可空日期精度，消除混合精度／全空列拼接对未来pandas推断行为的依赖。

在仓库根目录使用已有依赖的Python 3.13环境，设置 `OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2`：

```sh
# 复用冻结pins，只读已有归档。成功输出真实FAIL报告后预期退出1。
python -I -B -X utf8 tools/data/audit_p4_03_controlled_inputs.py --manifest docs/validation/p4-03/input-definition.json --bigquant-root '<BQ>' --membership-zip '<MEMBERSHIP_ZIP>' --output '<NEW_RECEIPT.json>' --matrix '<NEW_MATRIX.csv>'

python -I -B -X utf8 -c "import sys; sys.path.insert(0, 'src'); import pytest; sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/research/test_p5_d01_data_quality.py', 'tests/research/test_p4_01_pipeline_inputs.py', 'tests/research/test_p4_03_controlled_inputs.py', 'tests/research/test_datahub.py', 'tests/research/test_qualification_admission_boundaries.py', 'tests/research/test_csmar_source_binding.py::test_native_roundtrip_exact_units_lineage_and_determinism', 'tests/research/test_csmar_source_binding.py::test_index_units_are_explicit_ten_thousand_and_not_percent']))"
```

所有诊断输出使用新文件名；不提交商业证券日价格。没有新增CI工作流、审批规则、测试矩阵或数据质量框架；没有修改策略、优化器、收益计算、PIT/HFQ正式门禁或DataHub路由架构。
