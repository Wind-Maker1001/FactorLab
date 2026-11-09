# P3-D07：多证券真实数据准备与证据边界

2026-11-09；集成主线基线 `03504c59b487e1f91160378c661809ea605fe509`，D06 合并提交 `f3cbdb54d51780eb394b1a30b326914a4965ad89`。

**PARTIAL — NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

批处理已实际消费两个窗口、七个独立来源／证券查询。它复用 DataHub、单证券 preflight、Registry V2 原价诊断和治理层冲突比较；工程检查结果不是 `QualificationReport`，不能交给正式入口充当资格包。没有改动正式准入条件、原14因子、64候选或模型数学行为。

## 上游边界

已同步正式 main；PR [#41](https://github.com/Wind-Maker1001/FactorLab/pull/41) 于2026-11-09合并。复用 DATAHUB、DATA_GOVERNANCE、D02—D05、P3-08 和 Registry V2 的现有合同，使用既有 Python 3.13 环境。

D06 的 `join_members`、`field_profile`、`market_quality`、`factor_diagnostics`、`label_availability`、`window_profiles` 提取到 `factorlab.diagnostics.research_coverage`，原 CLI 仍导出相同函数入口。计算主体未改；`window_profiles` 增加可选候选参数，默认仍为原有12/3/3和24/3/3。没有复制78因子或 splitter。工具脚本还包含归档及资源统计，因此只共享计算部分，不将整个脚本提升为公共模块。

`batch_readiness(..., research_inputs=...)` 调用共享研究诊断。统一 CLI `audit_p3_d07.py --bigquant-root ... --membership-zip ...` 通过受控 `d06_readiness_adapter` 复用 D06 的 `Archive` 和既有 `SelectedTableAdapter`，不引入数据存储框架。每个消费分片校验收据，按来源/版本隔离。成员日、上市退市、ST/停牌、调整价、基准及日历带独立内容身份；公司行为沿用 `CorporateActionAdapter` 返回的事件版本包绑定，未提供时 UNKNOWN，不根据行情推导公司行为。

研究输入逐项要求显式 source/version；保留 effective_date、source_kind、内容身份及原始元数据。导出时间不转换为 known_at；当前回顾性输入的历史首次可得时间和历史版本仍 UNKNOWN。观察级成员/代码和上市退市边界不是独立认证 PIT 身份。缺行先按已有边界分类，无法确定历史资格时返回 UNKNOWN，不判定供应商漏数。

因子只消费至声明研究截止日，标签端点消费至显式输入截止日；区间外未来异常不能整证券隔离历史结果。保留 D06 的整证券隔离和原值，不修复异常。历史长度不足的候选窗口为 UNKNOWN，不缩短窗口、不放松 purge/embargo。工程数值、来源可追溯、历史身份、正式金融资格分别输出；`research_window` 保留金融窗口 UNKNOWN，实际工程结果在 `research_diagnostics`。下列两次真实数据诊断及其 JSON 是原运行证据，本次接口集成不重新宣称五年计算。

## 真实运行观察

### 上市／退市边界：2021-09-13—2021-10-29

[real-csi500.json](real-csi500.json) 消费 BigQuant 两个月行情分片，以所选证券观测日期并集作为28日诊断网格。研究段从9月28日起，共19日。网格并非官方日历，也不能发现所有来源共同缺失的会话；三代码只是有界诊断对象，不宣称其在整个窗口都是中证500成员。

|证券|行情行|预热行|研究行|网格缺日|RAW工程网格检查|
|---|---:|---:|---:|---:|---|
|600068.SH|0|0|0|28|FAIL|
|601868.SH|19|0|19|9|FAIL|
|600363.SH|28|9|19|0|PASS|

前两项 FAIL 是相对于声明的共同网格缺观测，**不证明应有上市前／退市后行情**。D02 已记录600068退市及601868上市边界；不能补价、跨代码拼接或把9日缺口自动变成数据供应商失误。补证首先是历史身份、成员和日历／缺值语义，而非下载“不存在”的价格。

601868、600363各有13个研究信号日存在t+1／t+6原价端点，末6日缺后续端点。600068没有行情，端点诊断 UNKNOWN。观察到端点不代表历史标签版本已成熟；复权open、基准和状态未取得资格，不生成收益标签。9日预热也不满足所有长回看因子，缺值保留。

### 双源多证券：2017-01-03—2018-12-28

[real-multisource.json](real-multisource.json) 使用 CSMAR 既有 prepared-v4 和 BigQuant 24个月分片。两者来源、版本、哈希分别保留。

|来源／证券|行情行|2017预热|2018研究|RAW工程检查|
|---|---:|---:|---:|---|
|BigQuant / 601857.SH|487|244|243|PASS|
|BigQuant / 600000.SH|487|244|243|PASS|
|CSMAR prepared / 601857.SH|487|244|243|PASS|
|CSMAR prepared / 600000.SH|0|0|0|FAIL|

CSMAR prepared 是限定601857的已准备包。另一代码缺失只说明该包没有覆盖，不能外推原生CSMAR库缺数据。三个有行情的查询均有237个研究信号日具有两端原价观察，最后信号日2018-12-20、退出观察2018-12-28。

601857两源仍有 **4个amount精确数值差异**，OHLC和volume未观察到值差异。600000的CSMAR缺行产生487×6＝2,922个字段缺失对照项，不是2,922个独立经济事件。比较结果只统计精确值／缺失，不认证单位一致、指定权威源或返回融合表。

明确版本保持为：

- CSMAR prepared-v4：`sha256:d77417709364d664f1041418c51a434e836a72cbebbc53f7b2158556478ccb64`。
- BigQuant 2017—2018分片绑定：`sha256:eb816b086d75984f726bd436a9d479a35571922914dcf5d952bb43b172a4c4d6`。

BigQuant 所读分片逐个核对收据SHA256；版本由分片、收据和schema绑定构造，不是整库哈希。实际输入清单和四个消费实现文件的哈希在各JSON中。CSMAR沿用既有prepared原生绑定，不重复扫描XLSX或下载数据。获取／导出时间不充当历史首次可得时间。

## 六门禁与四态

|条件|本轮结果|含义|
|---|---|---|
|RAW|4查询PASS、3查询FAIL|仅基础数值及声明网格完整性；不是源合同已认证|
|ADJUSTMENT|UNKNOWN|HFQ算法、锚点、独立参考和修订链未绑定|
|STATUS|UNKNOWN|未绑定历史ST、停牌、上市／退市资格证据|
|PIT|UNKNOWN|成员知识时间、供应商首次分发及历史修订未闭合|
|SOURCE_BINDING|UNKNOWN|哈希是内容身份，不是独立上游事实认证|
|FORMAL|FAIL|诊断对象不具备既有可重放资格包，保持阻断|

PASS 只表示该行的明确工程条件；实际重复／缺失／异常为 FAIL；未执行或缺证据映射为 UNKNOWN，并保留旧preflight outcome。NOT_APPLICABLE 仅用于确实不适用的条件，本轮没有将任何未观察的历史条件标成不适用。门禁是诊断摘要，不伪造正式qualification阶段结果。

## 最小候选研究窗口：引用D06，不冒充本轮重算

`main-integration.json` 固定正式 D06 合并提交及三个聚合文件身份，保留原始覆盖、端点、源质量和最小候选逐阶段数据，标记 `MERGED_MAIN_RECEIPT` / `REUSED_UPSTREAM_RUN_NOT_RECOMPUTED`。旧 `real-csi500.json → upstream_d06` 保留原运行时身份，不重写为新计算。原始逐因子定义／字段／回看矩阵为正式主线 `docs/validation/p3-d06/factors.csv`；本次未重算五年78因子。

D06范围为2021-09-13—2026-09-11，1,211日、932代码、605,500成员日。输入预热／尾部范围为2021-01-13—2026-10-11，1,388会话。原14及64候选全部定义均有有限输出，共同有限598,112成员日，再要求股票及基准t+1／t+6端点为597,723，再要求其结构筛选代理为597,623。这些是供应商调整价候选下的工程数值；HFQ、turnover单位、历史身份和正式可得性仍未认证。

D06最小12／3／3月候选第0折：

|阶段|名义日期（右端不含）|切分后成员日|日期数|代码数|14／64／78共同有限|再要求端点|再要求结构代理|
|---|---|---:|---:|---:|---:|---:|---:|
|训练|2021-09-13—2022-09-13|117,500|235|600|115,395|115,286|115,252|
|验证|2022-09-13—2022-12-13|27,000|54|500|26,655|26,628|26,628|
|测试|2022-12-13—2023-03-13|29,000|58|500|28,429|28,401|28,401|

purge／embargo均为5日。D06另发现输入范围10条OHLC关系异常，涉及600363、601198，整体隔离两代码的1,517成员日；其数值覆盖是下界，不是历史时点选股规则，不能用未来异常修补历史成员。正式准入成员日仍为0，**当前没有合格金融研究窗口**。共享实现现已正式接入，固定收据与小型合成集成测试分别提供历史运行结果和当前接口一致性证据，不将合成测试当作真实金融证据。

## 最小补证与优先级

机器补证范围在每份JSON的 `gaps`，进一步收窄的请求见 [minimum-evidence.csv](minimum-evidence.csv)。RAW缺网格证据先归第4类，不能自动归为可补行情。

1. **本地解析／映射**：CSMAR原生catalog已有多证券资料，扩展prepared覆盖属于本地候选准备；先核对目标原生行与既有parser，当前600000覆盖UNKNOWN。BigQuant status、上市快照、公司行为和成员导出也已有本地资料；可补观察级绑定，不会解除历史认证。本次已接入正式 D06 受控适配器与既有源元数据合同；TODO：为尚未提供的官方/独立历史身份、公司行为全集及供应商历史版本补齐外部证据。
2. **实际取数**：仅在资格／字段合同证明所需观测应存在，且现有本地副本不含时提出精确取数请求。当前不授权重下完整BQ／CSMAR库，也不索取上市前或退市后行情。
3. **供应商时间／修订**：分别向CSMAR、BigQuant索取目标载荷历史版本、首次分发、时区／精度、撤销替换关系；2026收据不能替代。中证500还需起点成员版本及变更的历史同版分发材料。
4. **官方／独立证据**：600068／601868过渡、300114／302132代码命名历史、独立交易会话、ST／停牌／上市／退市及单位；公司行为需全类别原始材料、修订链、算法锚点和独立复权参考。现有2018公告原件可复用，2017原件及全集证据按D04／D05缺口继续补。
5. **当前无法解除**：缺正式证据和确定性源绑定合同的金融准入。软件通过、行情存在、因子有限都不能替代认证。

## 定向测试与清理

原D07新增11项继续覆盖：多证券隔离、缺行／空值及固定分母、异常不修复、冲突使用同一次读取且不融合、显式版本失配、未来行扰动不改变历史结果、重复键、缺字段继续批处理、多个窗口拒绝、双证券读取、收据字节修订拒绝。正式门禁负向测试使用未修改的 `assert_formal_market_admission`。

本次增加共享D06逐因子/窗口输出对照、统一批处理调用、来源/版本/内容身份拒绝、历史成员缺证据、上市前缺行归因、缺调整价不替换、未来区间异常、受控归档收据、公司行为版本/时点引用、缺ST/停牌状态及短窗口测试。首轮75项中3项失败，暴露D06默认24/3/3候选不适用于短输入；修正为D07显式选择足够历史的候选后78项通过。加入事件/状态证据测试后，Python 3.13定向组合 **80 passed / 45.88秒**，见 `integration-tests.xml`。默认D06 CLI计算、14/64/78定义及金融门禁均保留。

- 首次测试命令误在父目录运行，退出4、0 tests；不是软件测试失败。误生成的空JUnit已清理。
- 定向套件首轮58 passed（31.25秒）；增加缺字段及收据测试后61 passed（16.31秒），见 `targeted-tests.xml`、`final-tests.xml`。
- 补充资格优先于补价的断言后，新增套件11 passed（1.89秒），见 `classification-tests.xml`。
- 本次受控适配器与统一CLI补充回归2 passed（4.99秒），见 `adapter-cli-tests.xml`；完整CLI实际输出包含共享78因子统计及正式主线固定收据。此处行情为合成归档夹具，不宣称重新完成真实五年研究。

仅提交聚合JSON、补证清单、JUnit及软件，不包含商业库证券日原值。过渡JSON和误目录空JUnit已清理；没有新缓存或大型归档。

## 复现

从本PR工作树根目录运行；`python` 指向已有依赖的解释器，`<BQ>`／`<FD>`为已有数据根，输出指定新文件。环境变量 `OMP_NUM_THREADS`、`OPENBLAS_NUM_THREADS`、`MKL_NUM_THREADS` 均为2。

```sh
python -I -B -X utf8 tools/data/audit_p3_d07.py --stock-zip '<BQ>/stock.zip' --symbols 600068.SH 601868.SH 600363.SH --start 2021-09-13 --end 2021-10-29 --research-start 2021-09-28 --research-end 2021-10-29 --upstream-d06-ref f3cbdb54d51780eb394b1a30b326914a4965ad89 --output <new-csi500.json>
python -I -B -X utf8 tools/data/audit_p3_d07.py --stock-zip '<BQ>/stock.zip' --csmar-candidate '<FD>/candidates/csmar-601857-2018-20261105/prepared-v4' --symbols 601857.SH 600000.SH --start 2017-01-03 --end 2018-12-28 --research-start 2018-01-01 --research-end 2018-12-28 --output <new-multisource.json>
python -I -B -X utf8 -c "import sys; sys.path.insert(0, '<repo>/src'); import pytest; sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/research/test_real_data_readiness.py', 'tests/research/test_candidate_preflight.py', 'tests/research/test_qualification_admission_boundaries.py']))"
```

`--upstream-d06-ref` 默认固定正式合并提交，仅接受当前集成分支祖先；读取固定Git对象的聚合JSON／CSV，不执行引用对象中的源码。

完整共享诊断入口（消费现有本地资产，输出新文件）：

```sh
python -I -B -X utf8 tools/data/audit_p3_d07.py --bigquant-root '<BQ>' --membership-zip '<FD>/validation/joinquant_csi500_full_20261024/csi500_full_history.zip' --symbols 600068.SH 601868.SH 600363.SH --start 2021-01-13 --end 2023-03-22 --research-start 2021-09-13 --research-end 2023-03-13 --output <new-integrated.json>
```

`end` 是标签端点读取边界，研究窗口阶段右端仍不含；不同来源不自动拼接。未传研究资产时旧 RAW CLI 继续工作，但不会伪造78因子/成员/状态结果。
