# P3-D05：601857.SH 2017—2018 最小真实历史证据闭环

审查日期：2026-11-09。基线：`51e6975`（本轮 `git fetch origin` 后的 `origin/main`）。P3-11、P3-12、P3-13 分别已由 #35（`ef6b6d1`）、#34（`9e9317f`）、#33（`176d60a`）合并。

**P3D05_PARTIAL — NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

可继续按来源分离的 **RAW 回顾性特征探索**；不能推进合格历史收益标签、公司行为执行或正式真实回测。主要剩余障碍是历史版本/首次分发、独立官方状态和日历、事件正文级全集及复权合同，而非 RAW 数值无法计算。

证据：[真实诊断](real.json)、[官方响应及全部标题清单](official-responses.json)、[逐事件矩阵](ACTION_MATRIX.csv)、[逐条件及最小外部证据](CONDITIONS.csv)。PASS 只适用于该行所列事实；UNKNOWN 不当成 FAIL 的证据，也不当成已满足。正式门禁 FAIL 表示实际拒绝，不表示门禁软件失败。

## 1. 两源与原生证据

CS＝现存 CSMAR `prepared-v4`；BQ＝现存 BigQuant `stock.zip`；EV＝已保存公告与收据。原件不复制入库，逐行金额只存私有诊断附件。公开报告仅保存日期、差值、源版本、哈希和坐标，不保存可重构行情序列。没有融合或指定权威源。

|项目|本轮观察|
|---|---|
|两源日期键|各487个唯一键；集合相同，重复0|
|2017预热实际边界|2017-01-03—2017-12-29，244条|
|2018研究实际边界|2018-01-02—2018-12-28，243条|
|OHLC及volume|487对逐字段精确一致|
|amount|4对精确不等；保留 FAIL，不舍入覆盖|
|基础质量|两源RAW六字段缺值、±inf、非正价格、OHLC顺序异常及负量负额均0|
|经济异常|UNKNOWN；基本不等式检查不认证极端涨跌经济合理性|

CS原生目标日期范围实际有519行：`Filling=0` 487行、`Filling=2` 32行，原生重复键0。既有准备器排除后者；其余487个键与 prepared 完全相同。原生完整候选文件另有窗口外边界记录，本轮不把填充行计作交易观察，也不将其解释成停牌或官方休市证明。原生金额十进制按既有映射转 binary64 后与 prepared 的487项比较失配0。

### 精确差异定位与处置

`Δ = BQ amount − CS prepared amount`；以下不是供应商原始绝对金额。四项各相差1 ULP，仅说明**当前二进制表示距离**，不证明供应商精度或成因。

|日期|Δ（精确十进制表示）|CS原生坐标：TRDNEW_Dalyr2.xlsx / sheet1|
|---|---:|---:|
|2017-02-27|-0.0000000298023223876953125|987376|
|2017-05-03|-0.0000000298023223876953125|987423|
|2018-10-12|+0.000000059604644775390625|987800|
|2018-12-13|+0.0000000298023223876953125|987844|

BQ定位为 `stock/cn_stock_real_bar1d/<对应月份首日>_<月末>.parquet`，键 `(date, instrument=601857.SH)`；成员完整哈希在 `real.json → sources.bigquant.source.provenance.selected_archive_binding`。CS每项原生值哈希和物理行在 `native.amount_locations`。

CS已审 DES 映射为价格 CNY/股、volume股、amount元，三者乘数均1；合同版本由 `src/factorlab/data/csmar/profiles.py` 固定，日表字典摘要 `12b4277c15ae5fa2f6b329480ff9e5df5cfe4903208bc84871751e5c29ad6a98`。BQ schema 的amount是double、volume是int64，但未携带经济单位、交易范围或舍入合同。**处置：差异保留，成因UNKNOWN，BQ单位/精度UNKNOWN。** 下一步只需该表字段字典及金额计算/精度说明，不需重下两年行情。

本轮实际绑定 CS manifest 和所读 native/lineage 文件，以及BQ新读状态/停牌/快照/日历分片。复用已有原生 ZIP 引用而不重哈希大ZIP：

- CS manifest：`d77417709364d664f1041418c51a434e836a72cbebbc53f7b2158556478ccb64`。
- CS 原生日行情ZIP历史引用：`e599361d89553976017539424285aded264d5a6a0e2b75261e0e627e9c51b290`；工作簿成员：`3947db283db81bdb7f23e9e8977bd45855bb83f4528575739c95ebac28abaec8`。
- BQ所选行情分片/收据/schema版本：`eb816b086d75984f726bd436a9d479a35571922914dcf5d952bb43b172a4c4d6`，不是整个stock.zip的哈希。

## 2. 公司行为逐事件证据

本轮复用D03定向读取器：CS现金候选4条、BQ分红候选4条，4组金额/除息日一致；CS因子4行，BQ因子8行（4日×2方向）。候选配股0行、送股/转增字段各4个空值，**均不等于“没有此类行为”**。浮点金额对照沿用D03的1e-14表示容差，不是金融容忍政策。

|除息日|含税元/旧股|登记日|计划支付日|CS Annodt / SSEDATE|BQ publish_date|形成日期（落款）|条款证据|
|---|---:|---|---|---|---|---|---|
|2017-06-22|0.03801|2017-06-21|2017-06-22|2017-06-16|2017-03-31|2017-06-16|2017-025：D04镜像p1/p2/p4；本轮官方索引|
|2017-09-15|0.06926|2017-09-14|2017-09-15|2017-09-08|2017-08-25|2017-09-07|2017-041：D04镜像p1/p2/p4；本轮官方索引|
|2018-06-21|0.06074|2018-06-20|2018-06-21|2018-06-13|2018-03-23|2018-06-12|2018-023：既有发行人原件p1—3|
|2018-09-21|0.08880|2018-09-20|2018-09-21|2018-09-14|2018-08-31|2018-09-13|2018-029：既有发行人原件p1—3|

2018文本本轮重读：p1金额，p2登记/除息/支付与领取方式，p3税务及落款。四项登记/支付条款补证只进入证据矩阵，**不回填候选或修改执行资格**。现金事件的新增股份上市日为 NOT_APPLICABLE，仅限这四次现金事件。

2018两份原件及历史获取收据沿用[D03](../../data/P3_D03_ACTION_EVIDENCE.md#3-两份2018实施公告复用)，未重下：

- [2018-023官方PDF](https://www.petrochina.com.cn/petrochina/gsgg/201806/667ae738ac3142a6a3260b3e55a90ab6/files/a78ca662a1f04a4797210e962a6f6bf8.pdf)：`9e26fe2216c5264e809b92cfe4cb8c0e47c14961d203a9c2889d4f1285361f51`；获取 `2026-11-05T04:41:28.676079+00:00`。
- [2018-029官方PDF](https://www.petrochina.com.cn/petrochina/gsgg/201809/b3d3e80c3ebe45398e844d3ae80b4368/files/332296d36d624cbf9f72fe4c61bcdba5.pdf)：`79b1e9ee30b0a3e7a71df9e803c33a620169d61646ed2f77b8367e366b0653c6`；获取 `2026-11-05T04:41:28.676097+00:00`。

### 2017原件阻塞与新增目录覆盖

[六月官方PDF](https://www.sse.com.cn/disclosure/listedinfo/announcement/c/2017-06-16/601857_20170616_1.pdf)及[九月官方PDF](https://www.sse.com.cn/disclosure/listedinfo/announcement/c/2017-09-08/601857_20170908_1.pdf)均转到static.sse.com.cn，HTTP 200但返回gzip压缩HTML/JavaScript cookie挑战页，不是PDF。原始响应各3,872字节，摘要均为 `f18a68b7fb0966ab2a40fecbe9c1b9d46bbf6eb48d4ae29a4f74bb3b90a6a75a`；六月取得 `2026-11-09T03:49:18.537460+00:00`，九月 `2026-11-09T03:49:18.534649+00:00`。这些哈希**仅绑定挑战页**。发行人2017目录返回403、总公告目录返回412；未找到可用发行人PDF。故2017官方原件仍UNKNOWN，条款证据仍明确继承D04镜像，而非升级为官方PDF核验。

新增的[上交所官方目录接口](https://query.sse.com.cn/security/stock/queryCompanyBulletin.do)按601857、完整自然年、空关键词、reportType=ALL查询。安全类型等全部实际参数保存在 `official-responses.json` 收据URL中。

|当前响应|2017|2018|
|---|---|---|
|total / 返回行 / 唯一URL|90 / 90 / 90|68 / 68 / 68|
|页数 / 当前页|1 / 1|1 / 1|
|记录日期边界|2017-01-11—2017-12-29|2018-01-13—2018-11-26|
|本地取得UTC|2026-11-09T03:49:42.777731+00:00|2026-11-09T03:49:42.787315+00:00|
|原始响应SHA256|`c14da760e858b520bdf770e1bc66c23c14af24eb56fdeabd43b8679a8ab8c3b3`|`37f1123208e207c83bd8d0840206078b108d2fc2767bb1cc0dc0d327bb776083`|

当前端点分页完整性PASS。158个标题已检查，恰有上述4份A股分红实施公告；未见标题明确指向其更正/取消的记录。标题中的2017修订涉及公司章程及议事规则。存在泛称H股公告、股东划转、可交换债等，尚未进行全部正文级事件分类。**标题/当前目录完整性不等于公司行为全集**，也不排除已删除、替换、未收录公告及2019以后追溯修订。TODO：按当前目录核对正文级类别和修订关系，再取得历史删除/替换链；不得据此将零行填成无事件。

### 四种时间分离

形成日期是文档落款；SSEDATE是当前目录披露日标签，ADDDATE四项分别为 `2017-06-15 18:40:18`、`2017-09-07 18:05:11`、`2018-06-12 16:56:53`、`2018-09-13 16:09:33`。ADDDATE时区及公开语义未认证，六月2017早于落款，不拼成虚构时序。精确首次公开、历史同版、供应商首次分发均UNKNOWN。BQ publish_date与CS Annodt四次不同是**阶段差异/语义待证**，不强制统一。

CS候选本地观察时间 `2026-11-05T04:50:10.674852+00:00`、BQ分红导出 `2026-11-07T12:22:03+00:00`及本次取得UTC分别保留，均不是2017/2018历史available_at。

公告计划支付不是所有账户实际到账；未指定交易者暂由中登保管，集团股东直接支付。个人/基金持股期税、QFII与沪股通代扣及居民企业自缴不同；不将BQ cash_after_tax套用所有账户，不生成现金执行账本。

## 3. PIT / HFQ / STATUS / FORMAL

|条件|状态|本轮缩小的缺口 / 仍需的最小证据|
|---|---|---|
|证券身份与代码映射|UNKNOWN|CS公司当前记录及BQ快照均可读，官方实施公告支持代码/名称；缺稳定ID、历史有效区间及供应商命名版本|
|独立于个股观测的日历|PASS|BQ common/trading_days 的CN表487个唯一日；两年244/243，较两源行情无漏/多日|
|独立官方交易日历|UNKNOWN|CN为同供应商独立表，不是独立交易所来源；需SSE会话及临时休市调整|
|逐日状态观察覆盖|PASS|BQ状态487行、无重复/缺值、与价格键相同|
|历史停牌/ST/退市/上市资格|UNKNOWN|BQ st_status/risk_warning/suspended原始码0各487；price_limit_status=2共487，exdr=1共4、0共483；CS未填充Trdsta=1.0共487。保留原码，不认证码义或PIT；停牌表目标0行不构成无停牌证明；当前基本信息不替代历史逐日上市/退市序列|
|价格/状态/事件历史vintage|UNKNOWN|2026快照和收据已绑定，缺历史版本载荷、首次分发及修订撤销链|
|原价/调整价/因子来源定义|UNKNOWN|RAW来自CS原生日表及BQ real_bar1d；CS Adjprcwd/Adjprcnd、BQ bar1d/adjust_factor不能只按名字认作同合同|
|复权算法/锚点/舍入/事件依赖|UNKNOWN|沿用D03关系诊断，不拟合认证；需算法版本、方向、绝对锚点、前置事件范围、税务/再投资/精度及历史修订规则|
|正式金融准入|FAIL|两源诊断实际调用既有assert_formal_market_admission均REJECTED|

新读BQ状态、停牌、基本信息与日历分片/收据/schema摘要在 `real.json → security_status/calendar`，各新读载荷与收据SHA声明比较均为true。逐日状态只公开聚合，未提交状态明细。来源一致不证明独立上游。

## 4. 最小研究窗口资格

|用途|资格|决策|
|---|---|---|
|RAW特征可计算性|PASS|每源RAW_MOM5/20/60、RAW_PRICE_MA20、VOLUME_RATIO各243个有限样本；仅回顾性数值探索|
|调整价特征可计算性|UNKNOWN|现有P3-08相关因子NOT_AVAILABLE；不能以候选调整价替代合格输入|
|历史标签|UNKNOWN|5日标签t+1开盘进入、t+6开盘退出；237个有两端原价观测、末6个无窗内退出。最后候选信号2018-12-20，退出2018-12-28；只是观察边界，不是合格收益标签|
|公司行为执行|UNKNOWN|具体现金条款有补证，全集/历史知识/账户结算未闭合；执行保持BLOCKED|
|正式金融准入|FAIL|既有门禁拒绝；不得进入正式真实回测或收益宣称|

未生成标签、未训练策略、未执行真实回测。RAW可计算不向标签、执行或正式准入传递资格。所有门禁与共享ResearchPipeline保持原样。

TODO：首先索取两源目标窗口的版本/首次分发/修订合同和BQ字段说明；取得2017两份官方归档原件及正文级完整事件/更正链；补SSE日历和历史证券状态；申请调整价研究前再闭合HFQ算法与锚点。细化闭合条件见CONDITIONS.csv。无需采购全库或重复下载现有2018原件。

## 5. 验证与复现

使用现有Python 3.13.9及依赖，真实限定窗口诊断完成。首轮报告发生顶层status被状态子对象覆盖的命名冲突，数据诊断本身完成；已改为security_status并重跑。初始输出保存在私有证据目录，不当作最终报告。

定向验证首批22 passed（新增6项及P3-08现有16项），见[targeted-tests.xml](targeted-tests.xml)；公告摘要器补充后9项最终定向结果见[notice-final-tests.xml](notice-final-tests.xml)。合成回归覆盖1 ULP不吞差、缺失不补零、重复拒绝、实际日期边界、分片/证券过滤、坏质量不获RAW PASS、不完整分页、响应哈希漂移与HTTP 200挑战页不冒充PDF。真实两源正式拒绝检查通过；它不把金融准入结果改成PASS。

路径别名：`CS`＝prepared-v4，`BQ`＝BigQuant Raw，`EV`＝CS候选既有evidence，`LOCAL`＝本次私有原件目录。输出必须是新路径。运行可信仓库脚本，不从下载目录启动解释器：

```sh
<PY313> -I -X utf8 tools/data/audit_p3_d05.py \
  --csmar-candidate <CS> --stock-zip <BQ/stock.zip> \
  --common-zip <BQ/common.zip> --evidence <EV> \
  --output <new-public-summary.json> --private-output <new-private-amount-details.json>

<PY313> -I -X utf8 tools/data/summarize_p3_d05_notices.py \
  --index-dir <LOCAL/sse-index-2017-034942397261> \
  --index-dir <LOCAL/sse-index-2018-034942397261> \
  --pdf-dir <LOCAL/sse-20170616-034918253489> \
  --pdf-dir <LOCAL/sse-20170908-034918253489> \
  --output <new-official-summary.json>

<PY313> -I -X utf8 -c "import sys; sys.path.insert(0, '<repo>/src'); import pytest; sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/evidence/test_p3_d05.py', 'tests/research/test_candidate_preflight.py']))"
```

私有原件集合别名为LOCAL，原件每次响应各存独立目录的response.bin/receipt.json，脚本在仓库tools/data，原件未执行。金额细节在私有附件，未发布。不可再生公告响应、失败取证及首次采集收据保留；本轮未创建虚拟环境或解压供应商归档，pytest禁用缓存。源码导入产生的可再生__pycache__在交付前清理。
