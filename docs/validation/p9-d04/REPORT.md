# W02 / P9-D04：历史交易状态与停牌估值合同

2026-11-12 · **W02_PARTIAL**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

三个指定停牌窗口的独立状态证据、前序真实观察及定向估值消费已闭合，共新增 **28个精确证券日的模型估值资格**。BigQuant当前公开字典明确写出 `st_status=2` 为 `*ST`，但没有取得该定义与原历史导出版本的有效期绑定，故不改变全局枚举映射。冻结LightGBM第一折新增完成 **2023-05-10、05-11、05-12三个会话**，随后在 **2023-05-15 / 000540.SZ** 的未知可消费状态处停止。整体不是完整折通过。

## 三段停牌的证据与用途

|证券及发行人|精确缺价会话|最后合格真实收盘|正式复牌|原始状态与定向消费|
|---|---|---|---|---|
|002185.SZ，天水华天科技股份有限公司|2025-09-25—10-16，10会话|2025-09-24，11.78元；正成交量|2025-10-17|停牌10日原始OHLC为空、量0、`suspended=1`；10日独立carry资格通过|
|600095.SH，湘财股份有限公司|2025-03-17—03-28，10会话|2025-03-14，6.89元；正成交量|2025-03-31|停牌10日原始OHLC为空、量0、`suspended=1`；10日独立carry资格通过|
|601615.SH，明阳智慧能源集团股份公司|2026-01-13—01-22，8会话|2026-01-12，19.68元；正成交量|2026-01-23|停牌8日原始OHLC为空、量0、`suspended=1`；8日独立carry资格通过|

每个对象均读取停牌、停牌进展和复牌三份发行人原件，共九份；不是由供应商枚举推断。原件分别明确相同证券代码、简称、停牌起始和复牌日期，均为筹划重组/发行股份购买资产事项。复牌公告说明方案仍需审议、审批或注册方可实施，不能把预案当成停牌期间已发生的换股或新增股份到账。

公告来源及公开日期：

- **002185**：2025-045号[停牌公告](https://pdf.dfcfw.com/pdf/H2_AN202509241749841477_1.pdf)，公告日2025-09-25；2025-046号[进展公告](https://pdf.dfcfw.com/pdf/H2_AN202510091758970180_1.pdf)，公告日10-10；2025-048号[复牌公告](https://pdf.dfcfw.com/pdf/H2_AN202510161763333821_1.pdf)，公告日10-17。
- **600095**：临2025-013号[停牌公告](https://pdf.dfcfw.com/pdf/H2_AN202503161644424709_1.pdf)，公告日2025-03-17；临2025-014号[进展公告](https://pdf.dfcfw.com/pdf/H2_AN202503211645608333_1.pdf)，公告日03-22；临2025-020号[复牌公告](https://pdf.dfcfw.com/pdf/H2_AN202503281648738377_1.pdf)，公告日03-29。
- **601615**：2026-004号[停牌公告](https://pdf.dfcfw.com/pdf/H2_AN202601121816954404_1.pdf)，公告日2026-01-13；2026-005号[进展公告](https://pdf.dfcfw.com/pdf/H2_AN202601191818114481_1.pdf)，公告日01-20；2026-009号[复牌公告](https://pdf.dfcfw.com/pdf/H2_AN202601221818287212_1.pdf)，公告日01-23。

[state-contracts.json](state-contracts.json)保留公告PDF身份、公告日期、镜像展示/收录时间，三者分列。初始公告镜像均在停牌前一晚已展示或收录，政策中的公开时间上界取该日期日终；这不是交易所精确发布时间，也不是历史供应商首次分发时间。全部新增政策仅允许事后研究账面估值，不能用于历史信号筛选。

直接从八个必要价格/状态月分片投影三个代码的必要列，分片SHA256逐一与原P8-02 manifest绑定。前序收盘是在停牌之前按正价格、正成交量确定的最后真实观察，不使用复牌价格选择carry。三个前序观察日至复牌边界的供应商 `exdr=0`、调整因子不变；公告代码与简称连续，未观察到使该限定原价延续失效的身份、除权或权益实施冲突。完整公司行为/支付链仍未认证，不把这个限定检查解释成HFQ或总收益认证。

按冻结历史成员归档的实际日期键核对，分别为 **10/10、10/10、8/8成员日**；既有Sidecar对这三段覆盖均为0。三个窗口在第一折之外，现存第一折FactorScore/Ridge账户不能证明这些后续日期的持仓。实际冻结账户暴露保持未评估；下述隔离正持仓验证不冒充原模型持仓。

## `st_status=2` 的实际调查结论

读取[BigQuant股票状态字段字典](https://bigquant.com/data/datasources/cn_stock_status)，取得原始页面及SHA256。页面明确说明：

> ST 标记。0 为正常，1 为 ST，2 为 *ST。

页面还注明 `is_risk_warning` 在2014-04-21之前与 `st_status>0` 一致，之后不能互相替代。当前页面的这些字面定义已经取得，不再将“2没有任何公开解释”作为结论。

但原 `metadata.zip/catalog.json` 仅提供目录来源及2026-11-07检查日期；`stock/cn_stock_status/schema.json` 来自 `zero_row_query`，只列 `int8` 类型，没有枚举或历史生效版本。原分片receipt绑定查询、字节与导出时间，没有历史字典版本。**当前网页定义不能单独认证所有历史250个证券日的枚举合同。** 保留000540的38日、000046的189日、600466的23日及其中26个成员日的既有分母，不重新扫描全市场。

全局适配器不改，原始 `st_status` 不覆盖；不将2直接映射成ST、停牌或不可交易。没有新增任何 `is_st` 日期例外，也未扩展000540已有5月4日估值或5月8日状态政策。

## 得到核准的估值合同

[valuation-policies.json](valuation-policies.json)按三证券列出全部28个来源会话，每项绑定证券、精确日期、前序真实收盘胶囊、原分片/manifest身份、独立停牌原件、独立复牌原件、政策方法和版本、用途。

复用 `LAST_OBSERVED_RAW_CLOSE_CARRY_ON_CONFIRMED_SUSPENSION / v1`、`Valuation`、`load_sidecar()`、`carry()` 与 `qualify_valuations()`。Sidecar许可只增加这28个 `valuation_mark` 精确键及其前序观察日期；初始停牌与复牌文件分别校验，不借初始公告推定最终复牌。

语义仍严格分离：

- **OBSERVED_RAW_CLOSE**：前序真实成交观察和复牌后的真实观察。
- **MODEL_VALUATION_ASSUMPTION**：28个指定停牌日的独立账面carry，`raw_close=null`。
- **UNVERIFIED_VALUATION**：范围外、缺证、来源漂移或条件不满足时不授予carry。
- **NONTRADABLE**：获准模型mark不提供成交价格或交易资格，停牌订单仍由共享执行器拒绝。

三条隔离路径均以停牌前已存在的正持仓进入窗口，并在首个停牌会话尝试退出。共享执行器以 `suspended` 拒单，28个会话无停牌成交；持股不变，估值与核准价格一致，复牌后回到真实观察。现金、股数、权益及冻结费用审计全部通过。完整持仓/交易明细保持私有，只公开限定审计收据。

## 冻结第一折真正推进到了哪里

原完整预测文件、原LightGBM第0折目标Parquet、调仓决策CSV均按P9-01公开摘要验证；不重训、不重新生成因子或目标，不以截短日历制造清仓。窗口请求仍为原折 **2023-03-13—2024-03-13（右开），243来源会话**，128证券身份及必要来源分片保持原样。

首次批量调用在5月15日因状态未知停止，没有返回中途账本。随后复用现有 `start_session()` 前向逐会话接口，使原目标在原信号收盘提交、下一会话开盘执行，并在首个异常处停止；执行器与费用代码没有更改。原38会话目标及交易、持仓、NAV、拒单、目标对照的既有字段逐项一致。逐会话接口新增的派生 `gross_security_turnover` 列不作为旧账本字段；开发时直接全列比较曾因此失败，随后改为严格比较旧账本全部原字段，而不是放宽数值容差。

新完成并通过资格与账本核验的只有 **2023-05-10、05-11、05-12**。旧38会话仅作身份核对，不计入新增成果。原单日000540估值假设仍包含在累计账本里，新三会话不增加其估值政策范围。

**下一真实阻断为2023-05-15 / 000540.SZ：**原状态 `st_status=2 / suspended=0 / is_risk_warning=1`，现有适配器的 `is_st=null`，执行器报 `execution status is missing for 2023-05-15/000540.SZ`。该日没有被解释成停牌或正常可成交，也不以当前网页定义放行。失败开盘可能已写入进程内部分订单行；验收账本只保留上一完整收盘及之前的记录，不收录失败当日部分行，不继续重试。

因此完整第一折仍需历史版本化枚举合同，或独立核验后精确到该日的状态政策。预检已经标出的000540自5月19日起缺价仍是后续潜在边界，不把尚未执行到的日期称为新的实际账户阻断。三个2025—2026年停牌窗口的估值闭合也不能消除2023年的状态阻断。详见[bounded-execution-results.json](bounded-execution-results.json)与[remaining-blockers.json](remaining-blockers.json)。

## 验证、来源和复现

定向测试 **136 passed / 7.79秒**，覆盖精确日期、边界拒绝、初始/复牌文件漂移、错误前序观察/价格、非正持仓、已观察价格不得carry、不能授予信号/交易权限，以及既有状态、费用、执行、账本回归。三条真实隔离路径与完整折有界前向重放另有实际消费收据。

最终单研究进程运行3.25秒，测得峰值工作集 **321,511,424字节（约306.6MiB）**；与定向pytest最多同时两个Python进程，数值线程1。全部四次尝试、原件和私有输出合计 **2,838,002字节（约2.71MiB）**，未复制原始冲突证据、未创建数据库索引或新环境。[verification.json](verification.json)保存来源、代码、私有收据身份、测试和资源数值；这里只公开必要单点观察、政策与聚合账本证据。

基线main为 `92c2516e408ddbd76cd94cdf1c3b8178d81c3d1f`，复用已合并PR #88/#92/#94及P9-D02预检、P9-D03、DQ-03。DQ-C01 PR #104以开放状态精确HEAD `bb35dfad42a5a7187f3c50780e7d7fb1649d5de5:docs/validation/dq-c01/REPORT.md` 只读引用，属于 **UNMERGED_EVIDENCE**，不合并。

```sh
python -I -B -X utf8 tools/research/investigate_p9_d04.py \
  --output '<NEW_PRIVATE_ACQUISITION>' --metadata '<BIGQUANT_METADATA_ZIP>'
python -I -B -X utf8 tools/research/inspect_p9_d04_sources.py \
  --output '<NEW_PRIVATE_NATIVE>' --acquisition '<PRIVATE_ACQUISITION>' \
  --manifest '<P8_02_INPUT_MANIFEST>' --stock '<BIGQUANT_STOCK_ZIP>'
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
python -I -B -X utf8 tools/research/run_p9_d04.py \
  --original '<P8_02_PRIVATE>' --study '<P9_01_PRIVATE>' \
  --previous '<P9_D03_RUN_4>' --native '<PRIVATE_NATIVE>' \
  --bigquant-root '<BIGQUANT_ROOT>' --membership-zip '<FROZEN_MEMBERSHIP_ZIP>' \
  --evidence-root '<P9_D01_PUBLIC_SOURCE_DIRECTORY>' --output '<NEW_PRIVATE_RUN>'
python -I -B -X utf8 -c "import sys; sys.path.insert(0,'src'); import pytest; sys.exit(pytest.main(['-q','-p','no:cacheprovider','tests/research/test_p9_d04.py','tests/research/test_p9_market_evidence.py','tests/research/test_p9_02_recovery.py','tests/research/test_oos_execution.py','tests/research/test_shared_portfolio.py']))"
```

**TODO：**取得历史版本状态合同，或先为5月15日实际阻断补精确发行人状态证据；随后只对真正触及的后续持仓缺价逐项核准。PIT、HFQ、公司行为完整账务及真实订单可执行性继续按各自合同处理，不能由28个估值证券日或三个新增会话授予正式金融准入。
