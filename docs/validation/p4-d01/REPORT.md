# P4-D01：601857.SH 历史 PIT/HFQ 证据闭环研究

2026-11-09；研究窗 **2017-01-03—2018-12-28**，2017为预热，2018为研究。基线及本次fetch核对的 `origin/main`：`e27d2f893a9f432ea67f80a69f4e9300a9796a13`。

**PARTIAL — NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

原价观察、原生转换、四次现金条款及部分当前来源身份能够闭合局部条件；历史分发/修订、独立身份/状态和正式复权合同仍未闭合。当前材料支持按来源分离的回顾性 RAW 数值调查，不能据此生成合格历史收益标签、认证HFQ或进入正式金融研究。

## 一、证据覆盖的分母

[证据矩阵](bundle/EVIDENCE_MATRIX.csv)有38个“条件×期间”项：13 PASS、2 FAIL、19 UNKNOWN、4 NOT_APPLICABLE。四个不适用项仅是四次现金分配的新增股份上市日；不把未知的送转配股标作不适用。

- 全38项中局部PASS比例为 **13/38＝34.21%**；排除已证明不适用的4项后为 **13/34＝38.24%**。
- 这是本任务明确列出的条件计数，不是独立事件数、金融认证完成度或概率；现金条款、PDF原件、知识时间分别计项，不能相加成多个经济事件。
- 原价观察覆盖：两源各487个日期，其中2017年244、2018年243。BQ独立于个股行的CN日历表也有487日；相对于该供应商日历两源为487/487。独立交易所日历覆盖尚UNKNOWN，不能外推为已证明官方全会话覆盖。
- 四次现金事件中，条款证据4/4（2017两项继承D04镜像调查），本地官方PDF字节与历史收据匹配2/4；历史首次公开同版、供应商首次分发及完整修订链均未闭合。
- 正式准入仍为0；原有金融门禁在本次D05读取器执行中拒绝两源诊断。

机器证据放在 `bundle/`：[原始证据索引](bundle/evidence-index.json)、[本次D05读取器观察](bundle/local-observations.json)、[新增有界扫描](bundle/additional-observations.json)、[内容绑定收据](bundle/receipt.json)。索引区分本次核验字节、继承结果、私有原件和归档成员；仓库上游以固定基线Git blob原始字节绑定，私有原件以实际文件/归档成员原始字节绑定。仓库上游核验仅还原检出的CRLF为LF并比对已绑定blob摘要，发布包核验不依赖Git历史或联网；新扫描取得原始blob仍需固定基线对象。目录级`.gitattributes`冻结本次发布包字节，避免Windows检出换行转换破坏收据。公开文件只含摘要、坐标、哈希和差值，不含商业证券日原值。`--verify`检查提交的聚合证据与仓库上游绑定；重新核验私有原件须执行有路径参数的扫描，二者不是同一覆盖范围。

## 二、本次实测与既有结论的边界

本次重新运行未修改的 `audit_p3_d05.py`，实际读取现有CSMAR prepared-v4及已绑定native/lineage、BQ窗口行情/状态/日历并调用既有正式门禁。本次另外复用D06 `Archive` 读取器，检查目标窗BQ调整价候选、分红/配股/因子，以及中证500日成员与变更；对其所消费载荷的收据哈希不匹配立即拒绝。补充绑定CS事件/因子原生文件，重核两份2018保存PDF的文件头、实际内容SHA256与历史收据。

D02—D04的原件调查、D05官方目录响应、D06较晚成员包、D07原运行结果是**继承证据**，不是本次重跑。D06目录中实际入口是 `README.md`，没有 `REPORT.md`；索引绑定实际文件。D07的两源目标查询、版本及BQ完整所选分片/收据绑定与本次观察核对一致，不只是报告中版本字符串相同。

没有重复下载2018公告或重扫原生大XLSX/ZIP；旧ZIP和工作簿哈希是已保存来源引用，不标作本次大文件复核。2017官方端点在D05已有HTTP 200挑战页的失败证据，本次沿用，不将挑战页当PDF，也不重复尝试访问挑战页。

## 三、原始行情、产品、字段与转换链

### CSMAR

版本为 `sha256:d77417709364d664f1041418c51a434e836a72cbebbc53f7b2158556478ccb64`，含义是prepared-v4 manifest内容身份，不是2017历史vintage。

- 产品/原生表：CSMAR `TRDNEW_Dalyr`，代码 `Stkcd`、交易日期 `Trddt`。`Opnprc/Hiprc/Loprc/Clsprc → raw_open/raw_high/raw_low/raw_close`；`Dnshrtrd → volume`，`Dnvaltrd → amount`。
- 已审DES摘要：`12b4277c15ae5fa2f6b329480ff9e5df5cfe4903208bc84871751e5c29ad6a98`。代码内合同价格为CNY/股、量为股、额为CNY，乘数均1，见 `src/factorlab/data/csmar/profiles.py`。目标prepared没有取得合格的换手率输入。
- 身份链：原生ZIP/工作簿历史引用 → native与lineage Parquet → manifest声明 → `market/bars.parquet` → 当前受控诊断。原生日行情ZIP旧引用为 `e599361d89553976017539424285aded264d5a6a0e2b75261e0e627e9c51b290`，工作簿成员旧引用为 `3947db283db81bdb7f23e9e8977bd45855bb83f4528575739c95ebac28abaec8`。
- 本次窗口native共519行，`Filling=0` 487行、`Filling=2` 32行；既有准备器排除填充行，未填充键与prepared相等，重复键0。不能把32行填充解释成交易、停牌或官方休市证据。
- 原生金额十进制按既有映射转换binary64后，487项与prepared失配0；不据此推定供应商原始算法。manifest、所读native/lineage、事件/因子文件及prepared文件的本次字节绑定见索引。

### BigQuant

原价来自 `stock.zip!stock/cn_stock_real_bar1d/<月份边界>.parquet`，键为 `(date,instrument)`，目标 `601857.SH`。读取窗口覆盖24个月分片。OHLC直接映射 `raw_*`，保留volume、amount；不取调整价覆盖原价，不插值、不填零。

所选行情分片/收据/schema绑定版本为 `sha256:eb816b086d75984f726bd436a9d479a35571922914dcf5d952bb43b172a4c4d6`，**不是整个stock.zip摘要**。每个成员与收据摘要在 `local-observations.json → sources.bigquant.source.provenance.selected_archive_binding`。schema中的价格、amount、turn为double，volume为int64；schema不足以证明经济单位、时段范围、舍入精度或换手率比例/百分数。

调整价候选来自独立表 `cn_stock_bar1d`，本次目标487行，OHLC、adjust_factor和turn均无空值；这里只闭合“候选字段可读且有值”。其语义、HFQ算法及历史版本仍UNKNOWN。公司行为为 `cn_stock_dividend`、`cn_stock_allotment`、`cn_stock_adjust_factor`；事件表date、publish_date、register_date、ex_date不能当作同一时间角色。

两源证券代码、日期键与本次内容身份能够关联；供应商首次可得时间、历史同版载荷、初版/修订/撤销父子关系仍为 **UNKNOWN**。2026采集或导出时间不倒填历史known_at。

## 四、四个金额差异：位置闭合，原因未闭合

两源487对OHLC与volume本次精确比较未见数值差异。amount有4对精确不等，仍保留FAIL；没有舍入吞差、择一为真或融合表。

`Δ＝BQ amount−CS prepared amount`，仅公布差值而非商业原值：

|日期|Δ（精确十进制）|CS原生位置：TRDNEW_Dalyr2.xlsx / sheet1|
|---|---:|---:|
|2017-02-27|−0.0000000298023223876953125|987376|
|2017-05-03|−0.0000000298023223876953125|987423|
|2018-10-12|+0.000000059604644775390625|987800|
|2018-12-13|+0.0000000298023223876953125|987844|

BQ坐标为对应月原价分片和目标日期/代码。四项均为当前二进制表示的1 ULP，原生值摘要及坐标见 `native.amount_locations`。

|归因候选|现有证据约束|结论|
|---|---|---|
|经济单位不同|CS映射乘数为1；487对量及OHLC一致不能替代BQ单位合同|BQ单位仍UNKNOWN，不能认证或完全排除|
|十进制精度/浮点转换/截断|四差均1 ULP；CS native→prepared转换失配0|表示路径是候选，不证明BQ何时舍入/截断|
|原始字段定义、时段不同|BQ schema只有类型，未给成交额涵盖交易范围|UNKNOWN|
|后续修订|无目标记录初版和更正链|UNKNOWN|
|日期、代码或交易归属|当前键集合相同、无重复；未发现键错位|不能认证供应商时区/交易时段合同|

**原因UNRESOLVED。** 最小动作是R03四记录与字段合同核验，不需要重下两年全量行情。

## 五、成员身份与交易状态

本次从现有 `index.zip` 读取2017—2018 `cn_stock_index_component`，过滤中证500 `000905.SH`，观察到487个日期、243,493行；目标 `601857.SH` 成员行 **0**。同窗 `cn_stock_index_change` 目标动作也是 **0**。这些数字是当前供应商查询观察；243,493行不是“每天500”的认证，也不把它强制补成243,500。

因此，现有BQ历史资料**覆盖了目标日期范围，但未包含该证券的成员观察区间**。D06所用JQ完整成员包始于2021-09-13，与目标窗无交集；D06的三段旧日期抽查及2021以后成员不能替代2017—2018基线。当前证据既不能认证该证券是中证500成员，也不能仅凭0行正式认证整个窗口非成员。应取得官方起点及完整适用变更链/有范围证明的非成员材料；没有待证事实支持将601857强行加入历史中证500。

交易状态方面，本次BQ状态487行，键与原价相同、无重复或缺值：st_status、is_risk_warning、suspended原始码均为0；price_limit_status均为2；exdr=1有4行、0有483行。CS未填充行Trdsta=1.0有487项。停牌原因表目标0行、基本信息为当前快照。这些是观察覆盖，不在缺字典时推断枚举，不证明历史无ST/无停牌或持续上市。

独立材料目前支持的是公告中的代码/名称及四次登记/除息安排；缺窗口全段官方身份沿革、ST及其他特殊处理、停复牌、独立交易所会话和版本知识时间。BQ CN日历是同供应商独立表，不是独立官方来源。

## 六、公司行为与HFQ链条

### 四次实际现金候选

|除息/计划支付日|税前元/旧股|登记日|文档落款|SSEDATE披露标签|BQ publish_date|
|---|---:|---|---|---|---|
|2017-06-22|0.03801|2017-06-21|2017-06-16|2017-06-16|2017-03-31|
|2017-09-15|0.06926|2017-09-14|2017-09-07|2017-09-08|2017-08-25|
|2018-06-21|0.06074|2018-06-20|2018-06-12|2018-06-13|2018-03-23|
|2018-09-21|0.08880|2018-09-20|2018-09-13|2018-09-14|2018-08-31|

本次再次观察CS现金候选4条、BQ分红4条、现金/除息日期配对4组；CS因子4行、BQ因子8行（4日×2方向），不是8个经济事件。金额配对沿用D03的1e-14二进制表示容差，不是金融认证容忍政策。

- 2017-025、2017-041：条款继承D04镜像及D05官方索引；两份官方PDF原件仍UNKNOWN。D05保存的HTTP 200响应实为挑战页，摘要 `f18a68b7fb0966ab2a40fecbe9c1b9d46bbf6eb48d4ae29a4f74bb3b90a6a75a` 不能绑定公告正文。
- 2018-023：[发行人PDF](https://www.petrochina.com.cn/petrochina/gsgg/201806/667ae738ac3142a6a3260b3e55a90ab6/files/a78ca662a1f04a4797210e962a6f6bf8.pdf)，本次保存字节与旧收据摘要 `9e26fe2216c5264e809b92cfe4cb8c0e47c14961d203a9c2889d4f1285361f51` 一致。
- 2018-029：[发行人PDF](https://www.petrochina.com.cn/petrochina/gsgg/201809/b3d3e80c3ebe45398e844d3ae80b4368/files/332296d36d624cbf9f72fe4c61bcdba5.pdf)，本次保存字节与旧收据摘要 `79b1e9ee30b0a3e7a71df9e803c33a620169d61646ed2f77b8367e366b0653c6` 一致。

2018公告p1是分配方案，p2是登记/除息/支付与领取方式，p3是税务及落款。2018-023的0.06074含普通0.02489及特别0.03585。现金经公司直接支付或中登派发，未指定交易者暂保管；公告计划支付日不是每账户到账证据，不把BQ cash_after_tax推广为所有账户净额。

### 四种时间不能替代

`effective_at`按本行实施事实记除息日；登记日、计划支付日另列。`announced_at`仅保留SSEDATE日期标签，精确对外公开时间UNKNOWN。落款与目录ADDDATE是其他观察字段，既有ADDDATE时区/公开含义尚未认证。`known_at`始终UNKNOWN，必须由版本绑定的历史可得性证据闭合。

本地CS事件观察 `2026-11-05T04:50:10.674852+00:00`、BQ分红导出 `2026-11-07T12:22:03+00:00`、2018 PDF旧获取时间（分别 `2026-11-05T04:41:28.676079+00:00`、`2026-11-05T04:41:28.676097+00:00`）均只保留原角色。BQ publish_date明显早于实施公告标签，可能属于提案/业绩阶段，准确语义待供应商证明，不能覆盖CS Annodt或当作实施公告首次公开。

### 可核查的局部关系，不是实际供应商算法

既有D03手算候选在“仅有该现金事件”假设下使用前收盘P、税前现金D：`E=P−D`，局部前向比率 `q=E/P`，局部后向比率 `1/q`。若假设窗前局部锚点1，则后向累计为 `B_t=∏(1/q_i)`，候选后复权为 `raw_price_t×B_t`。这只是明示假设的局部链，不是已认证绝对HFQ，也不是实际现金再投资收益。

D03观察CS六位展示因子与该候选相容；BQ price_gap与“现金先舍入至分”候选接近，但这也可能来自参考价舍入或其他定义，四事件不能识别算法。四组CS/BQ因子均不同。CS Adjprcwd字典含现金红利再投资语义，不能等同简单price_gap链。本次没有拟合乘数、挑选残差最小规则或重写供应商价格。

当前可核查路径为：原生日行情坐标/哈希 → 前收盘候选 → 公告现金及实施日 → CS/BQ分开的事件与因子 → 独立表调整价候选。尚断开的边是供应商实际公式、内部精度、累计方向与窗前依赖、绝对锚点、事件修订的重算规则，以及独立HFQ价格参考。公告独立支持事件条款，不能替代独立调整价/因子参考，故 **HFQ_UNVERIFIED**。

### 未观察类别不等于无事件

本次BQ配股目标0行；bonus_rate、conversed_rate在四条分红中各4个空值。D03既有股本变更目标窗未见行、未有完整拆并股专门表绑定。现金、送股、转增、配股、拆并及其他价格影响行为的全集/修订覆盖均不能由这些零行或空值闭合，不臆造事件，也不默认无事件。

D05保存官方当前目录2017年90条、2018年68条，共158标题，当前端点分页完整；标题定位到四份A股实施公告，未见标题明确指向其更正/取消。正文尚未全类别核查，历史删除替换及2019以后追溯修订未闭合，因此158标题不构成事件全集证明。

## 七、正式认证还缺什么，下一步如何推进

PIT主要阻塞是两源目标记录的原始历史版本、首次分发及修订撤销链，以及独立历史证券身份、状态、日历、成员/非成员及公告知识时间。HFQ主要阻塞是正文级完整事件/更正链、2017官方原件、供应商实际算法与绝对锚点、窗前依赖及独立调整价参考。

[最小补证清单](MINIMUM_REQUESTS.csv)给出10个定向请求：R01/R02限两源目标窗的分发/版本；R03限四金额记录与字段合同；R04核中证500历史身份；R05/R06为官方状态和会话；R07/R08为算法锚点与小样本独立参考；R09只补2017两份原件；R10为目标证券全类别正文和追溯修订。请求精确到证券、时间、字段、版本与时间证明；没有全市场十年重取需求。

可以继续推进的是R03供应商字段核验、将现有158标题对应正文做目标类别/修订关系调查，以及在取得版本合同后对本地候选做受控映射。TODO：按R01—R10补齐未闭合原件和合同；未取得证据前保持矩阵UNKNOWN，不补历史known_at、不将0行变成无事件、不生成正式资格包。

## CI 发布字节修复（2026-11-09）

可信运行 [42629544576](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42629544576) 的 Fast 与 Python 3.13 Shard 2 在发布包验证中报 `artifact hash mismatch: local-observations.json`。原 HEAD `d614c9aeeaa7f9962c3b7ab27a0e5610b69f3aad` 的该 Blob 为80,326字节、1,927个LF，SHA-256为 `a701c9dbb8ce8a15a505a6c7662423e16f69b6c2262fecee3606213795a55624`；工作区为82,253字节、1,927个CRLF，摘要恰为收据已固定的 `f1f8842fc29d31dc025901d338e34c88d882ad66eaeacdc5238f735166f3d31e`。逐字节比较证明唯一差异为CRLF→LF，JSON解析结果相等；`additional-observations.json` 同样受影响。

本地 `core.autocrlf=true`，JSON文本写入使用平台默认换行，观察文件则按原字节复制。目录级 `* -text` 已冻结发布字节，但原提交仍含两个LF Blob，与工作区及收据的CRLF表示不一致；普通 `git add` 未更新这些被判定未修改的文件，须用 `git add --renormalize` 按现行属性重新写入索引。修复保留收据及全部证据语义、来源身份，仅将两个Blob恢复为既有收据绑定的原始字节，不重新序列化JSON、不重算或改写收据、不放宽核验器。篡改拒绝、固定基线来源身份和发布核验不依赖Git历史的合同保持不变。

Python 3.13全新独立检出（`core.autocrlf=false`）先在原HEAD复现两项同样失败（2 failed / 2.07秒）；检出修复提交后，四个artifact的工作区、Git Blob、索引和新检出字节均逐一匹配原收据。两个失败测试分别为 **1 passed / 1.19秒**（[字节与来源绑定](ci-byte-binding-test.xml)）及 **1 passed / 1.18秒**（[无历史依赖](ci-no-history-test.xml)），完整新增套件 **30 passed / 1.30秒**（[新检出结果](ci-fresh-checkout-tests.xml)）。独立运行发布包 `--verify` 返回 `PASS / LOCAL_ENGINEERING_EVIDENCE_BINDINGS_ONLY`；金融边界及38项矩阵计数不变。完整套件保留并通过篡改负例、同版本标签下分片收据替换拒绝、固定基线身份、LF/CRLF兼容和禁止访问Git历史的回归。

## 八、复现与受控验证

使用现有Python 3.13环境及pandas/pyarrow/pytest。`CS`为既有prepared-v4，`BQ`为现有BigQuant Raw，`EV`为该CS候选已有evidence目录；原件不复制入仓库。所有输出使用新路径，在可信仓库根目录启动解释器：

```sh
# 第一步：复用未修改D05读取器，重读限定窗口；私有输出只留本地。
<PY313> -I -B -X utf8 tools/data/audit_p3_d05.py \
  --csmar-candidate <CS> --stock-zip '<BQ>/stock.zip' \
  --common-zip '<BQ>/common.zip' --evidence <EV> \
  --output <NEW_OBSERVATIONS.json> --private-output <NEW_PRIVATE_AMOUNTS.json>

# 第二步：核对D07来源身份，验证CS原件、BQ新增分片收据及保存PDF；输出新目录。
OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 \
<PY313> -I -B -X utf8 tools/evidence/audit_p4_d01.py \
  --observations <NEW_OBSERVATIONS.json> --candidate <CS> \
  --bigquant-root <BQ> --evidence <EV> --output <NEW_BUNDLE_DIR>

# 仅检查已提交聚合包/上游内容绑定，不冒充重新读取私有数据库。
<PY313> -I -B -X utf8 tools/evidence/audit_p4_d01.py \
  --verify docs/validation/p4-d01/bundle

<PY313> -I -B -X utf8 -c "import sys; sys.path.insert(0, 'src'); import pytest; sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/evidence/test_p4_d01_evidence.py', 'tests/evidence/test_p3_d05.py', 'tests/evidence/test_p3_d03.py', 'tests/research/test_qualification_admission_boundaries.py']))"
```

首轮定向组合72项通过，随后增强D07的目标查询/分片收据精确身份核对，增加同版本标签下收据被替换的负例。测试覆盖：原件/聚合哈希漂移、版本混用、公告与实施日期分离、缺known_at不补值、冲突不融合、未知类别不判无事件、HFQ/PIT缺证不PASS、四态分类、挑战页不冒充PDF，以及未修改正式门禁的实际拒绝。增强来源身份绑定后的定向组合 **73 passed / 29.36秒**，见 [final-tests.xml](final-tests.xml)。随后将RAW两源版本在矩阵中显式列出（与聚合报告版本分离），对应新增套件 **27 passed / 1.39秒**，见 [source-version-tests.xml](source-version-tests.xml)。发布绑定时发现15个上游文档的Windows检出字节与Git blob不同，改用固定基线blob作为原始内容身份；加入LF/CRLF兼容及实际内容变更拒绝测试后，新增套件 **29 passed / 2.02秒**，见 [portable-binding-tests.xml](portable-binding-tests.xml)。发布核验进一步与Git历史解耦，浅检出拒绝访问历史对象的回归加入后，最终新增套件 **30 passed / 1.37秒**，见 [publication-tests.xml](publication-tests.xml)。
