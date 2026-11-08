# P3-08：真实候选受限诊断

基线：`e1ebe0dec9eb0b585972565167eff0c29a7f1d62`。

**P308_ENGINEERING_ACCEPTED — NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

工程验收表示受限诊断入口正确，不代表金融数据获得正式研究准入。真实窗口已实际运行，结果见 [real.json](real.json)；#29固定提交的ResearchPipeline拒绝路径已实际执行，见 [pipeline-refusal.json](pipeline-refusal.json)。

定向回归首轮115 passed、1 failed，失败为Python 3.14环境缺少可选psutil（[原始结果](targeted-tests.xml)）；该唯一失败项在已有、带psutil的Python 3.13环境补跑1 passed（[结果](partitioned-smoke.xml)），没有改依赖或跳过测试。新增入口最终16项测试全部通过（[结果](preflight-final.xml)），包括RAW通道不受无效调整价字段污染及非有限open不计为成熟观察。小型独立合成示例实际结果见 [fixture.json](fixture.json)，不冒充供应商数据。

## 本轮真实窗口观察

CSMAR prepared-v4 与 BigQuant 所选2017—2018月分片分别读取。两源各487个唯一证券日期键，其中2017预热244条、2018研究243条；日期集合相同。各源RAW OHLCV/amount没有缺值、重复、非有限值、基础OHLC顺序异常或负成交量额。这些是本轮重读所选候选的观察，不是原始487日文件重新审计或独立官方交易日历认证。各自观察日期作为诊断网格，独立日历仍UNKNOWN。

精确数值对照报告4个amount冲突；OHLC与volume未观察到差异。不归因为已证明的浮点或舍入规则，不选覆盖源，单位与语义资格仍UNKNOWN。

|来源|本轮明确版本|
|---|---|
|CSMAR prepared-v4|`sha256:d77417709364d664f1041418c51a434e836a72cbebbc53f7b2158556478ccb64`，manifest绑定；实际核对bars、events及observed_sessions三个文件哈希|
|BigQuant窗口|`sha256:eb816b086d75984f726bd436a9d479a35571922914dcf5d952bb43b172a4c4d6`，所选行情分片、收据及schema的排序内容绑定；不是整个stock.zip哈希|

CSMAR原生重放及catalog来源沿用prepared-v4，不重复扫描原始表。BQ只读取与目标窗相交的行情分片，再过滤目标证券；未扫描五年成员或全部公司行为表。各源版本与载荷哈希分别保留。

### RAW因子可计算性

以下各因子在**每个来源各自**的2018年243观察日上均有243个有限样本，缺失率0、非法值0。计算输入包含2017预热，报告分母仅为2018研究窗口。结果是原价特征，不是总收益或策略回报。

|因子|定义|所需先前观察数|
|---|---|---:|
|RAW_MOM5|raw_close / lag(raw_close,5) - 1|5|
|RAW_MOM20|raw_close / lag(raw_close,20) - 1|20|
|RAW_MOM60|raw_close / lag(raw_close,60) - 1|60|
|RAW_PRICE_MA20|raw_close / rolling_mean(raw_close,20) - 1|19|
|VOLUME_RATIO|rolling_mean(volume,5) / rolling_mean(volume,20)|19|

既有MOM20、VOL20、PRICE_MA20、AMIHU20依赖调整价，TURN20依赖未提供的合格turnover，本入口均报告NOT_AVAILABLE。财务/行业/情绪类无本窗口合格输入，不进入本通道。定义哈希、源版本、证券代码和身份证据引用都保留；证券代码本身不升级成已认证稳定身份。

### 标签边界与公司行为

5日预测期限沿用既有标签函数：t收盘特征，t+1开盘进入，t+6开盘退出。243个研究信号日中237个有两端原价观察，末6个缺少窗内退出观察；最后可观察信号为2018-12-20，对应2018-12-28退出。未生成标签数值，复权open与基准不具备本通道所需资格，label_generation=NOT_RUN。上述日期只是回顾性数值观察边界，历史版本成熟/首次分发/修订仍UNKNOWN。

本轮重读CSMAR事件候选4条，均为现金候选，4条缺登记日、覆盖PARTIAL且执行BLOCKED；本地available_at记录候选准备时间，不能替代历史首次分发。BQ事件字段对照复用[P3-D03既有证据](../../data/P3_D03_ACTION_EVIDENCE.md)：四组现金金额及除息日匹配，但CS Annodt与BQ publish_date四次均不同，CS缺登记日、BQ缺支付日。BQ因子8行是4日期×2方向，不是8个经济事件。上述BQ事件对照未在本轮重复扫描。

2018两份官方实施公告的保存原件定位、页码及哈希见P3-D03；CS日期比公告落款晚一天，不选任一日期作为已认证首次公开时间。后续[P3-D04 PR #28](https://github.com/Wind-Maker1001/FactorLab/pull/28)固定提交`923c7eac74253cf69fb18081164f7bca8e198560`进一步定位2017两份实施索引与镜像条款，但2017官方原始PDF字节和完整历史修订/分发链仍未闭合。该PR只读参考，不导入其分支内容。

### 金融门禁

本轮两源诊断对象传入既有`assert_formal_market_admission`均拒绝。另从#29固定提交`9283a6e82183ad589aa5d4e34e86ee35c2f0446c`只读加载ResearchPipeline，使用实际CSMAR bars与明确标记的辅助schema空占位，真实调用未替换的门禁，得到`REJECTED_BEFORE_DATASET_BUILD`。此负向测试不是构建完整研究包：为满足该管线的末端后续会话前置条件，拒绝测试的research_end为2018-12-27；主诊断窗仍截至12-28。未调用builder、训练、优化或交易执行。

### 最小外部补证

1. 两源分别绑定价格/证券身份/状态的历史版本、首次分发时刻、时区精度和撤销替代链；2026导出与本地准备时间不能回填为2017/2018可得。
2. 独立官方会话日历、稳定身份和历史状态、价格量额字段单位/精度与使用授权。
3. 复用2018官方原件，补2017官方原始PDF版本；目标两年全类别事件索引、提案/取消/更正及空结果覆盖，四组事件的登记/支付与实际到账边界。
4. 若后续申请调整价研究，补算法版本、绝对锚点、先前事件依赖、再投资/舍入和历史修订合同；若申请执行，再补账户税务、现金结算和交易资格。不得用数值拟合替代这些证据。

## 接口边界

`factorlab.diagnostics.candidate_preflight.preflight` 读取明确来源与版本，返回聚合 `CandidateDataDiagnostics`，不提供资格 provider。DataHub 的重复键拒绝保留为 FAIL，不另行去重。治理层 `diagnose_tables` 复用既有逐字段冲突规则，不返回融合表。

RAW_MOM5/20/60、RAW_PRICE_MA20 使用 Registry V2 的受限 AST；VOLUME_RATIO 复用原有定义。表达式引擎需要完整 whitelist，未提供且未使用的字段保留 NaN，绝不把 RAW 重命名成 adj_close。既有 HFQ 类因子及无合格换手率输入的因子标记 NOT_AVAILABLE。

标签仅检查既有函数的 t+1/t+h+1 日期和原价未来观察覆盖，不计算收益标签；观察到退出日不等于该历史版本当时可得。历史首次分发、修订链、身份、独立日历、公司行为全集和复权资格保留 UNKNOWN/UNVERIFIED。

## 复现

使用已有安装依赖的 Python，运行仓库中的可信脚本；不要从供应商归档/解压目录启动解释器。

```sh
python -I -X utf8 tools/data/candidate_preflight.py --fixture --output <new-fixture.json>
python -I -X utf8 tools/data/candidate_preflight.py --csmar-candidate <FD/candidates/csmar-601857-2018-20261105/prepared-v4> --stock-zip <BQ/stock.zip> --output <new-real.json>
```

在真实命令增加`--pipeline-ref 9283a6e82183ad589aa5d4e34e86ee35c2f0446c`可同时复现只读固定版本的管线拒绝检查；该提交须已在本地Git对象库中。它不checkout、不修改#29工作树，也不复制管线源码入本PR。

定向测试使用当前工作树源码（以下`<repo>/src`替换为本工作树绝对路径）：

```sh
python -I -X utf8 -c "import sys; sys.path.insert(0, '<repo>/src'); import pytest; sys.exit(pytest.main(['-q', 'tests/research/test_candidate_preflight.py', 'tests/research/test_data_governance.py', 'tests/research/test_datahub.py', 'tests/research/test_factor_registry_v2.py', 'tests/research/test_qualification_admission_boundaries.py']))"
```

分区DataHub smoke依赖既有可选psutil；本次已有Python 3.13环境用于该项，没有运行跨Python全矩阵。

## 验收对应

|要求|实际证据与结果|
|---|---|
|1. 真实数据进入入口|两源ACCESSIBLE，各487日；real.json|
|2. 明确版本，不融合|各自内容绑定、版本失配拒绝；merged=false|
|3. 来源冲突报告|4个amount精确值差异；保留双方版本|
|4. 缺值不补零|缺行/缺字段fixture保留研究分母及非零缺失率|
|5. 因子来源与身份|每个定义保留源/版本/数据哈希/证券代码/身份证据；身份资格UNKNOWN|
|6. 标签成熟与边界|1/5/20日期限测试，t+h+1；真实5日末6观察无退出覆盖|
|7. 不默认零公司行为|4个部分覆盖现金候选；空候选fixture仍UNKNOWN|
|8. 缺首次分发保持UNKNOWN|真实候选及fixture均不由当前获取时间推断历史可得|
|9. 正式门禁拒绝|两源对象拒绝；固定#29管线在build前拒绝实际CSMAR bars|
|10. 小fixture及真实定向复现|fixture.json、real.json、固定源哈希与上述命令|

公开诊断仅保存聚合统计、源版本和哈希，测试收据保留实际失败及补跑结果。私有原件和逐行候选不入库。输出路径必须新建，已有结果不覆盖。
