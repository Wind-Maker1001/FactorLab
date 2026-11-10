# P8-02：四模型真实历史样本外 Walk-Forward

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 研究合同

本研究检验真实历史横截面预测，不运行交易账户。基线为启动时 `origin/main` 的 `f9a3ba96cc9f4d99ebf866129a09b5b4e046fdd5`。研究窗口为2021-09-13—2026-09-11，预热从2021-05-01起，标签尾部从同一来源读取至2026-09-30。固定配置见 [p8-02-real-models.json](../../../configs/research/p8-02-real-models.json)。

先实读 JoinQuant ZIP 的成员CSV、manifest，BigQuant ZIP 的原价/状态 schema 和真实分片，然后冻结配置，才执行模型。全部消费成员绑定 P3-D06 保存的 SHA256；哈希漂移直接拒绝。数据身份只证明导出字节一致，不证明历史首次可得、无修订或稳定证券主体身份。

样本按**研究首日成员的规范交易所代码 SHA256 升序取128只**，与收益无关；以后只评价各日仍在历史成员表中的这些证券，不按收益或退样表现添加替代证券。该规则节省计算并允许成员退出，不是无偏全市场抽样，也没有使用窗口末日存续名单。然而限制首日成员会排除后续新纳入者，结果只适用于这一固定起始队列。证券身份为供应商交易所代码，不是经审的历史稳定主体ID。成员数据是当前查询得到的追溯快照，不能当作历史当时已知证据。

## 特征、标签与可得时间

既有64候选定义的完整实验未执行，不改动其名称或定义，不把原价输入映射成 adj_close。[逐项可用性](legacy-factor-availability.csv)记录：32项因HFQ未认证为UNAVAILABLE，20项因本研究不消费amount/turnover等必需字段为UNAVAILABLE，12项仅依赖可用字段但不属于冻结子集，为AVAILABLE_NOT_RUN。这是本研究输入合同下的状态，不声称amount/turnover来源资产不存在。

本轮7项独立 `RAW_PRICE_EXPLORATORY` 定义经 Registry V2 受限AST执行；每项版本为 `raw-exploratory-v1`，定义全文及摘要保存于 frozen-study.json：

|特征|定义|
|---|---|
|RAW_MOM5/20/60|当日raw_close / 前5/20/60观察会话raw_close − 1|
|RAW_TREND20|当日raw_close / 最近20会话raw_close均值 − 1|
|RAW_VOL20|最近20会话的单会话原价收盘变化率样本标准差，精确实现见冻结AST与既有引擎|
|RAW_RANGE20|最近20会话的 (raw_high − raw_low) / raw_close 均值|
|RAW_VOLUME5_20|最近5会话volume均值 / 最近20会话volume均值|

RAW价格来自 `cn_stock_real_bar1d` 的 open/high/low/close；volume只用于同证券滚动比值，不把未认证单位解释为可成交容量。状态来自 `cn_stock_status` 的 suspended、st_status，要求 suspended=0 且 st_status=0。来源字段名称与软件映射不是独立历史状态认证。

所有信号特征在信号日收盘后可计算；这是计算时序，不是供应商历史分发时间证明。每证券在显式CN观察日历上形成完整网格；无效OHLC、非正量、缺失状态、停牌或ST行先置为缺失，**不填零、不前填**。额外要求信号日前含当日61个连续会话全部合格，避免窗口端点因子越过中间缺失。全部7特征完整后，在当日信号可用成员横截面做ddof=0的z-score；标准化先于未来标签可评价性筛选，未来标签不会改变历史特征或横截面标准化参数。此横截面处理不拟合跨日期参数，Ridge另有只在Train拟合的中位数和StandardScaler。

主标签为 `raw_open[t+6] / raw_open[t+1] − 1`：信号日t收盘后决策，下一观察交易日开盘起，经历5个开盘到开盘间隔，到t+6开盘成熟。entry_date=t+1、exit_date=t+6。为保守处理停牌/缺价，t至t+6共7会话全部须合格；标签在exit_date后才可进入后续训练。t+6的日状态使用完整日线状态，属于回顾性可评价性限制，不宣称开盘前已知道该日全部状态。

原价变化不含现金分红、送配股/换股权益及完整公司行为校正。Top10减Bottom10未来原价变化差仅为预测诊断，**不是可实现投资收益**，没有共同资金、成本、涨跌停排队、容量或借券核算。

## 时间拆分与模型比较

既有 ExpandingWalkForwardSplitter 与 WalkForwardRunner 直接负责拆分及拟合。初始Train15个月、Validation3个月、Test12个月、步进12个月，最多3折。Train尾部purge6会话，Validation尾部embargo6会话；实际标签exit_date必须严格早于下一阶段首日，不能仅依赖固定天数。每折模型只在该折Train拟合，不用Validation重拟合；每模型仅一组固定参数，Validation保留为独立诊断，没有超参搜索或早停。后续Train可以使用此前已成熟的历史Test标签。

A组为7项共同原始特征；B组复用TrainFactorSelector，只在已purge的Train中按覆盖、方向与相关冗余选至多4项；C为固定20日动量FactorScore参考。B的绝对RankIC和方向一致率阈值预先设为0，只做合法质量、排序和去冗余，不把通过阈值说成统计显著性。B由Train符号定向，A/C固定正向，故B−A同时包含维度与方向变化，不能单独归因为去冗余的因果效果。

四模型均由现有工厂构造，版本 `legacy-v1`。种子20261110、线程1；树模型100棵，其他参数见冻结配置。FactorScore对z-score等权，Ridge内部预处理只拟合Train，两树模型既有中位数插补器也只拟合Train；本研究完整案例规则避免FactorScore默认中性零路径消费缺失。所有9条组/模型路径的Test证券、日期、标签必须完全相同，失败即拒绝比较，不事后求交集。

每日等权Spearman RankIC；ICIR为每日RankIC均值/样本标准差、不年化。按年和按折分别统计；模型差异使用共同日期配对RankIC，Bartlett Newey–West lag20标准误描述日期序列相关性，不把全部证券日当IID，不搜索多重p值。行业历史分类和available_at证据尚未成立，因此跨行业稳定性为 **NOT_RUN**，不使用当前行业快照回填历史。

TODO：在正式PIT、稳定主体、历史行业版本及完整公司行为合同补齐后单独评估正式资格与可执行性，不升级本次导出的研究身份。

## 验证与复现

定向研究合同和既有Walk-Forward筛选集成 **41 passed / 33.76秒**，另补精确t+1/t+6价格点及成熟日期测试 **1 passed / 1.44秒**。合成fixture仅验证时间因果合同，不参与真实金融结果。首次测试为13通过、1失败：测试错误地要求未来标签改变后全数据identity仍相同；已改为只比较冻结的Train选择、方向、质量与预测，数据identity本来就应随标签内容改变。

```sh
PYTHONPATH=src python -B -m pytest -q tests/research/test_real_model_study.py tests/research/test_walk_forward_factor_selection.py
python -I -B -X utf8 tools/research/run_real_model_walkforward.py \
  --config configs/research/p8-02-real-models.json \
  --bigquant-root '<EXISTING_BIGQUANT_ARCHIVE_DIRECTORY>' \
  --membership-zip '<EXISTING_JOINQUANT_MEMBERSHIP_ZIP>' \
  --output '<NEW_PRIVATE_OUTPUT_DIRECTORY>'
```

输出目录独占创建，重复运行须用新目录；不联网、不重下载、不复制原始行情。源分片在读取时验证P3-D06绑定身份。原始供应商行、标准化样本外预测Parquet及带本机路径Tracker均留本机；公开派生汇总和预测文件摘要用于核对。预测保留signal_date、symbol、prediction、model_id、fold、data_identity、feature_identity，以及标签端点日期，未来执行器可消费，不建设在线服务。

## 实际输入、切分与成本

本轮实际使用2021—2026候选资产，**不需要退回2016—2018样本**。JoinQuant原始成员文件实读611,500行、1,223日期（延伸至2026-09-30）；固定研究窗的首日队列128证券保留 **95,541成员日**。本轮不是对原605,500成员日全量重新建模。股票池每天收缩/变化，不能将首日128只说成每天128只。

[冻结收据](frozen-study.json)记录94,300成员日通过历史与特征完整性规则，**94,198证券日、1,211日期**同时具有合法特征和标签；特征覆盖率98.7011%，给定特征的标签可评价率99.8918%，全部成员日的标签可评价率99.7216%。单项特征在成员日上的缺失率约0.2355%—0.5286%，见收据逐项值。完整案例额外排除包括61会话连续性和零方差横截面，故不能把单项缺失率直接当作最终样本损耗。

128证券完整预热/尾部网格上有5,008条raw_open缺失/非有限OHLCV行、376条停牌来源行、4,633条状态缺失/未知行，共5,401条不合格网格行；这些类别可重叠，不是研究成员日分母。该投影未发现有限OHLC关系异常。没有静默取最新、去重、填充或跨源覆盖，重复键直接拒绝。交易日为BigQuant trading_days中market_code=CN的来源会话，不升级为正式交易所日历认证。

所有时间窗为左闭右开；实际Test止于2026-03-12，2026-03-13以后因不够下一完整12月Test窗不纳入，**不是根据收益截断**。

|折|Train名义窗|Validation名义窗|Test名义窗|Train / Validation / Test证券日|Test日期|
|---|---|---|---|---:|---:|
|0|2021-09-13—2022-12-13|2022-12-13—2023-03-13|2023-03-13—2024-03-13|32,993 / 5,112 / 20,335|243|
|1|2021-09-13—2023-12-13|2023-12-13—2024-03-13|2024-03-13—2025-03-13|55,116 / 3,640 / 15,322|241|
|2|2021-09-13—2024-12-13|2024-12-13—2025-03-13|2025-03-13—2026-03-13|71,180 / 3,062 / 13,341|242|

Train标签最晚成熟分别为2022-12-12、2023-12-12、2024-12-12，严格早于各折Validation；Validation标签最晚为2023-03-10、2024-03-12、2025-03-12，严格早于Test。精确purge/embargo日期保存于冻结收据。扣除预留会话后，没有额外跨边界标签，不能将0条额外移除理解为未检查边界。

四模型全部完成，A/B各四模型、C一个固定参考，共 **9条路径、27次折拟合**。Test共 **726日期、48,998证券日、107只不同证券**；每日有效横截面47—100只。9条路径的Test键与标签摘要完全相同。全程耗时201.38秒（包含投影读取、特征、Train选择、拟合、阶段诊断与文件输出），不做跨Python矩阵。Python3.13，NumPy2.3.4、pandas2.3.3、PyArrow22.0.0、scikit-learn1.7.2、LightGBM4.6.0、XGBoost2.1.4；精确版本见[result.json](result.json)。

## 样本外结果与研究结论

**P8_02_REAL_WALKFORWARD_COMPLETED**：四模型在共同真实样本上完成三折严格时间前进评价；该状态不是正式金融准入。

|组/模型|RankIC|ICIR|正RankIC日期比例|Top10−Bottom10原价变化差（bps）|
|---|---:|---:|---:|---:|
|A FactorScore|−0.04151|−0.18911|44.35%|+6.44|
|A Ridge|+0.03141|+0.14561|54.27%|−9.98|
|A LightGBM|+0.01254|+0.07968|54.55%|+8.96|
|A XGBoost|+0.02102|+0.12182|56.06%|+11.68|
|B FactorScore|+0.04271|+0.18939|56.06%|+0.10|
|B Ridge|+0.03651|+0.16040|54.55%|−11.49|
|B LightGBM|+0.01517|+0.10358|53.17%|−0.36|
|B XGBoost|+0.01834|+0.11429|54.41%|−6.74|
|C 20日动量|−0.02767|−0.11833|47.11%|−2.40|

上表各行均为726有效横截面、48,998证券日、0个缺失RankIC日。[每日](daily.csv)、[分折](by-fold.csv)、[按年](by-year.csv)与[全部配对差异](paired.csv)保留原始精度。bps为标签价格变化差乘10,000，不是费用后策略收益。

### 哪些模型显示跨时间的预测能力？

Ridge的排序关系最一致：A三折RankIC为 **0.03186 / 0.03308 / 0.02931**，B为 **0.04276 / 0.03270 / 0.03404**，均为正。B FactorScore也三折均正，为 **0.04119 / 0.04247 / 0.04449**。A等权正向评分三折均负；简单正向20日动量后两折为负。这里证据支持“Train学习方向的重要性”，不支持“随意等权正向价格因子天然有效”。

树模型较不稳定：A LightGBM为0.03252 / −0.00846 / 0.01339；A XGBoost为0.04282 / 0.00767 / 0.01243。B XGBoost第一折0.05452，后两折降到−0.00109 / 0.00136；不能用其第一折的较高值代表可重复优势。

按日历年看，A/B Ridge在2023、2024、2025及2026已测部分均为正，但B FactorScore在2026已测43日期转为−0.01272，B LightGBM与B XGBoost在2025及2026已测部分均负。2023仅198日、2026仅43日，不把它们称为完整年度结果；跨行业稳定性未验证。

### 复杂模型是否优于简单评分，筛选是否改善结果？

在共同A特征上，三个拟合模型的总体RankIC均优于固定正向FactorScore；但对比Train定向的B FactorScore，Ridge与两树模型均未超过其0.04271。B FactorScore减B Ridge的每日配对均值为 **0.00620，HAC20标准误0.00763**；差异没有形成清晰优势证据。A Ridge减A XGBoost为0.01039、标准误0.01227。**结果不支持复杂度带来稳定增益。**

B−A的RankIC变化分别为FactorScore **+0.08423**、Ridge **+0.00510**、LightGBM **+0.00262**、XGBoost **−0.00268**；相应配对HAC20标准误为0.03117、0.00673、0.00709、0.00703。筛选明显改变FactorScore方向，但对已能拟合方向的模型改善很小且不一致，XGBoost反而下降。没有开展事后阈值调整或只报告改善模型。

### 数据质量和收益口径会不会改变结论？

**会改变经济解释，尤其是尾部价格诊断。** Ridge正RankIC却有负Top/Bottom差，B FactorScore最高总体RankIC却只有约0.10bps原价差，说明全横截面排序与尾部平均价格变化是不同估计量，不能把正RankIC直接换算为获利策略。

冻结实现还保留相邻收盘绝对跳变超过20%的候选路径作为诊断，未据此重拟合或择样主结果。Test有63证券日命中该候选路径，全部研究共同样本有168条。统一剔除这些Test路径后，9条路径仍共享48,935证券日、726日期：[敏感性结果](jump-path-sensitivity.csv)中A/B Ridge的RankIC为0.03157/0.03655，排序结论接近主结果；但A FactorScore的Top/Bottom差从 **+6.44bps变成−2.77bps**，B FactorScore由 **+0.10bps变成+9.31bps**。这证明少量异常/公司行为候选足以影响尾部均值，不能宣称其为稳健投资收益。20%只是候选阈值，会漏掉常规除息等事件，也不识别跳变原因；敏感性分析不是公司行为清洗或认证。

因此，可重复的有限证据是Ridge及Train定向评分的弱正排序关系；复杂模型没有稳定优越性，筛选没有统一改善拟合模型。历史PIT、稳定证券主体、收缩队列、公司行为和原价口径限制共同阻止将这一观察提升为真实可交易Alpha。

## 产物身份与追踪

[input-manifest.json](input-manifest.json)绑定消费分片与首日128代码；[frozen-study.json](frozen-study.json)绑定配置、定义、数据与样本。标准化预测本机保存440,982行（9×48,998），SHA256为 `19b0fab33b8cb247ba7b34bd0721c8119757464f16822ac95f111bae16f5243c`；共同Test摘要为 `cee338e4cdf231d7a9567e888ac6d3d633b8112480ffefcde617a620a1317e2e`。

每条模型路径均有ExperimentTracker experiment/run，共18个不可变JSON记录。源码摘要绑定实际执行文件，不把尚未包含新文件的基线提交冒充处理代码；Tracker文件摘要见[verification.json](verification.json)。从已落盘预测独立重算每日RankIC和Top/Bottom指标，与保存daily.csv在 `rtol=1e-10 / atol=1e-12` 下相同。没有重跑模型调参。
