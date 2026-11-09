# P3-D07：多证券真实数据准备与证据边界

2026-11-09；主线基线 `d2f52c05ddaf86f493ae38c792aaf961ff652bb0`。

**PARTIAL — NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

批处理已实际消费两个窗口、七个独立来源／证券查询。它复用 DataHub、单证券 preflight、Registry V2 原价诊断和治理层冲突比较；工程检查结果不是 `QualificationReport`，不能交给正式入口充当资格包。没有改动正式准入条件、原14因子、64候选或模型数学行为。

## 上游边界

执行前已获取最新 `origin/main`。PR [#39](https://github.com/Wind-Maker1001/FactorLab/pull/39)（D05）与 [#41](https://github.com/Wind-Maker1001/FactorLab/pull/41)（D06）均为 OPEN，mergeCommit/mergedAt 均为空。参考分别固定在 `f13baf947e3b65e06864548d3f8bc83c9fe043f8`、`0372cbaa2fa096ba19944b96d77f014e566a3371`，未导入其代码。

复用 DATAHUB、DATA_GOVERNANCE、D02—D04 和 P3-08 的现有合同；D05、D06 只作为未合并的证据参考。既有兼容环境 Python 3.13 被复用，没有新建环境。

本轮独立可交付的是批量原价诊断、四态门禁摘要、来源冲突保留和补证范围。TODO：#41 合入后，将其现有多证券因子／窗口分析器接入批处理；当前 `research_window.status=UNKNOWN`，不另写一套窗口或78因子计算器。历史成员、ST／停牌／上市／退市、公司行为及复权版本的自动逐项诊断仍需相应源合同绑定，当前输出 UNKNOWN；不是这些历史事实不存在。

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

`real-csi500.json → upstream_d06` 保存固定提交、三个聚合文件哈希、覆盖、端点、源质量和最小候选逐阶段数据。原始逐因子定义／字段／回看矩阵为该提交的 `docs/validation/p3-d06/factors.csv`；本轮未重算其78因子。

D06范围为2021-09-13—2026-09-11，1,211日、932代码、605,500成员日。输入预热／尾部范围为2021-01-13—2026-10-11，1,388会话。原14及64候选全部定义均有有限输出，共同有限598,112成员日，再要求股票及基准t+1／t+6端点为597,723，再要求其结构筛选代理为597,623。这些是供应商调整价候选下的工程数值；HFQ、turnover单位、历史身份和正式可得性仍未认证。

D06最小12／3／3月候选第0折：

|阶段|名义日期（右端不含）|切分后成员日|日期数|代码数|14／64／78共同有限|再要求端点|再要求结构代理|
|---|---|---:|---:|---:|---:|---:|---:|
|训练|2021-09-13—2022-09-13|117,500|235|600|115,395|115,286|115,252|
|验证|2022-09-13—2022-12-13|27,000|54|500|26,655|26,628|26,628|
|测试|2022-12-13—2023-03-13|29,000|58|500|28,429|28,401|28,401|

purge／embargo均为5日。D06另发现输入范围10条OHLC关系异常，涉及600363、601198，整体隔离两代码的1,517成员日；其数值覆盖是下界，不是历史时点选股规则，不能用未来异常修补历史成员。正式准入成员日仍为0，**当前没有合格金融研究窗口**。#41未合并也阻止本分支直接复用其构造器；只读证据引用不等于集成已完成。

## 最小补证与优先级

机器补证范围在每份JSON的 `gaps`，进一步收窄的请求见 [minimum-evidence.csv](minimum-evidence.csv)。RAW缺网格证据先归第4类，不能自动归为可补行情。

1. **本地解析／映射**：CSMAR原生catalog已有多证券资料，扩展prepared覆盖属于本地候选准备；先核对目标原生行与既有parser，当前600000覆盖UNKNOWN。BigQuant status、上市快照、公司行为和成员导出也已有本地资料；可补观察级绑定，不会解除历史认证。TODO：复用相应主线adapter合同，而非复制D05/D06未合并读取器。
2. **实际取数**：仅在资格／字段合同证明所需观测应存在，且现有本地副本不含时提出精确取数请求。当前不授权重下完整BQ／CSMAR库，也不索取上市前或退市后行情。
3. **供应商时间／修订**：分别向CSMAR、BigQuant索取目标载荷历史版本、首次分发、时区／精度、撤销替换关系；2026收据不能替代。中证500还需起点成员版本及变更的历史同版分发材料。
4. **官方／独立证据**：600068／601868过渡、300114／302132代码命名历史、独立交易会话、ST／停牌／上市／退市及单位；公司行为需全类别原始材料、修订链、算法锚点和独立复权参考。现有2018公告原件可复用，2017原件及全集证据按D04／D05缺口继续补。
5. **当前无法解除**：缺正式证据和确定性源绑定合同的金融准入。软件通过、行情存在、因子有限都不能替代认证。

## 定向测试与清理

新增11项覆盖：多证券隔离、缺行／空值及固定分母、异常不修复、冲突使用同一次读取且不融合、显式版本失配、未来行扰动不改变历史结果、重复键、缺字段继续批处理、多个窗口拒绝、双证券读取、收据字节修订拒绝。正式门禁负向测试使用未修改的 `assert_formal_market_admission`。

- 首次测试命令误在父目录运行，退出4、0 tests；不是软件测试失败。误生成的空JUnit已清理。
- 定向套件首轮58 passed（31.25秒）；增加缺字段及收据测试后61 passed（16.31秒），见 `targeted-tests.xml`、`final-tests.xml`。
- 补充资格优先于补价的断言后，新增套件11 passed（1.89秒），见 `classification-tests.xml`。

仅提交聚合JSON、补证清单、JUnit及软件，不包含商业库证券日原值。过渡JSON和误目录空JUnit已清理；没有新缓存或大型归档。

## 复现

从本PR工作树根目录运行；`python` 指向已有依赖的解释器，`<BQ>`／`<FD>`为已有数据根，输出指定新文件。环境变量 `OMP_NUM_THREADS`、`OPENBLAS_NUM_THREADS`、`MKL_NUM_THREADS` 均为2。

```sh
python -I -B -X utf8 tools/data/audit_p3_d07.py --stock-zip '<BQ>/stock.zip' --symbols 600068.SH 601868.SH 600363.SH --start 2021-09-13 --end 2021-10-29 --research-start 2021-09-28 --research-end 2021-10-29 --upstream-d06-ref 0372cbaa2fa096ba19944b96d77f014e566a3371 --output <new-csi500.json>
python -I -B -X utf8 tools/data/audit_p3_d07.py --stock-zip '<BQ>/stock.zip' --csmar-candidate '<FD>/candidates/csmar-601857-2018-20261105/prepared-v4' --symbols 601857.SH 600000.SH --start 2017-01-03 --end 2018-12-28 --research-start 2018-01-01 --research-end 2018-12-28 --output <new-multisource.json>
python -I -B -X utf8 -c "import sys; sys.path.insert(0, '<repo>/src'); import pytest; sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/research/test_real_data_readiness.py', 'tests/research/test_candidate_preflight.py', 'tests/research/test_qualification_admission_boundaries.py']))"
```

D06参考提交须存在于本地对象库；可先 `git fetch origin pull/41/head`。该选项只读取固定Git对象的聚合JSON／CSV，不执行上游源码。去掉该选项即可独立运行批处理。
