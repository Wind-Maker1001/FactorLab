# P7-R01：NVDAUSDT 模型与交易策略有效性

**P7_R01_RESEARCH_REPRODUCIBLE / P7_R01_EVIDENCE_LIMITED / P7_R01_NEGATIVE_RESULT**

**INSUFFICIENT_HISTORY — NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

本次没有发现足以支持预测或交易优势的证据。Ridge 重现既有负面结果；LightGBM、XGBoost 未提供稳定样本外改善。FactorScore 不适用于本次单品种输入，不能声称四模型都已得到有效金融比较。研究可复现不等于盈利能力，也不授予正式策略准入。

## 数据与时间合同

直接读取 P6-D02 小时 Snapshot `hourly/runs/bddaab1cd5c943e28caa62c0fc49ef33`，版本 `8f4861c8b3bef653b6f68cc778516e771ec7bd88678c00ad404b9111ac9ca607`。真实 Binance USDM NVDAUSDT，来源 BINANCE_USDM_PUBLIC_REST，UTC `[2026-08-12,2026-11-10)`，2160 根已闭合小时线。未重新采集、复制或建立大型索引。已有 90 根日线版本 `34f804333867a581b2ab135249ca1e7a1b9acacfdec55896005f0be59fb4704d` 留作资产背景，不混入小时模型比较。

| 分区 | 行数 | 首信号 UTC | 末信号 UTC | 末标签退出 UTC |
| --- | ---: | --- | --- | --- |
| Train |1294|08-13 01:00|10-05 22:00|10-05 23:00|
| Validation |431|10-06 00:00|10-23 22:00|10-23 23:00|
| Test |407|10-24 00:00|11-09 22:00|11-09 23:00|

年份均为 2026。特征复用 P7-M01：1 桶收益、6 桶平均收益、24 桶波动。标签为信号后下一开盘到再下一开盘的收益；特征水位为已闭合桶。边界向后对齐 UTC 日，标签退出必须严格早于下一分区。训练预处理与模型均由 `fit_final_model` 在 Train 内拟合。模型参数、种子 42 保存在 evidence.json；树模型固定 100 棵、深度 3，单线程，无搜索。

Ridge 的历史 Test 在任务开始前已经公开；这里不是全新盲测。新增模型的固定配置不按 Test 选择。训练是本次离线操作，实际开始/结束时间在 evidence.json，历史信号时间不冒充当时部署时间。预测推理是在本次离线运行完成，Paper 提交为历史下一开盘 +1ms，假设成交 +2ms；成交详情保存 event_time。

## Walk-Forward 适用性与稳定性

现有通用 Runner 强制至少两只证券的横截面评价；原 splitter 以整月和日级 `date < entry_date < exit_date` 为合同，无法合法表达同日小时标签。本次不修改基础验证系统、不伪造横截面，而在 research 内复用训练/预处理函数做固定扩展时间窗：

- 折 1：Train 1294 行，评价 `[10-06,10-15)` 215 行。
- 折 2：Train 1510 行，评价 `[10-15,10-24)` 215 行。

边界标签清除导致两个评价窗合计 430 行；完整 Validation 为 431 行。各模型同窗比较，第二折可使用第一折已成熟的历史标签，但最终冻结模型仍只拟合原 Train，不用验证折选择参数。

| 模型 | 折1 MSE skill | 折2 MSE skill | Test MSE skill |
| --- | ---: | ---: | ---: |
| Ridge |-0.008769|0.008433|-0.007195|
| LightGBM |-0.006877|-0.037980|-0.020403|
| XGBoost |-0.028061|-0.022435|-0.021545|

skill = 1 − 模型 MSE / 零预测 MSE。没有稳定改善。未运行原横截面 Walk-Forward Runner，不将研究适配称为原系统验收。

## 预测质量

| 模型/基准 | Test RMSE | Test MAE | 三态方向准确率 | 预测/实际 Pearson |
| --- | ---: | ---: | ---: | ---: |
| FactorScore | N/A | N/A | N/A | N/A |
| Ridge |0.005560160|0.003314406|45.2088%|0.003793|
| LightGBM |0.005596500|0.003358178|47.6658%|-0.008144|
| XGBoost |0.005599629|0.003364200|47.4201%|0.005327|
| 零收益 |0.005540266|0.003302487|0.4914%|未定义|
| 简单动量（上一桶收益）|0.007853957|0.004626391|51.8428%|-0.010109|

方向采用 sign 的负/零/正三态；零预测准确率仅表示真实零收益占比，不是可交易方向判断。动量方向超过 50% 但误差明显较差，不能据此声称 Alpha。

FactorScore 合同为每日横截面 z-score；单品种不能得到有意义的横截面。标记 NOT_APPLICABLE，不添加未经验证的时间序列标准化替代品。Train、Validation、Test 全指标、Test 固定二等分摘要、分数 min/p01/p25/median/p75/p99/max 均见 [evidence.json](evidence.json)。Test 分数范围：Ridge `[-0.000544,0.001644]`，LightGBM `[-0.001442,0.002122]`，XGBoost `[-0.004396,0.005294]`。树模型极端分数没有转化为误差优势。

## Paper 收益与交易成本

详表见 [paper-table.md](paper-table.md)，必要订单/成交/权益路径及拒绝明细见 evidence.json。所有策略使用既有 `replay_history` → RealtimeAccount → PaperBroker，没有新撮合器。各自初始 1000 USDT，合成费率 0.1%、滑点 0.01%，阈值 ±0.0001、目标权重 5%。双均线只在 MA5/MA20 真交叉时改目标，动量/模型按阈值改目标，中间保持持仓。无交易基准权益恒定。

| 策略 | 订单/成交 | 费用 USDT | 净收益 | 最大回撤 | 相对 MA 净收益百分点 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Ridge |55/55|2.584384|-0.837702%|-1.014725%|-0.522917|
| LightGBM |61/61|2.877347|-0.832248%|-0.948467%|-0.517463|
| XGBoost |65/65|3.062549|-0.951220%|-1.085352%|-0.636436|
| 动量 |191/191|8.965598|-1.436556%|-1.471201%|-1.121771|
| MA5/MA20 |27/27|1.317043|-0.314785%|-0.355030%|0|
| 无交易 |0/0|0|0%|0%|0.314785|

相对无交易的收益差等于各自净收益。订单差额过滤拒绝：Ridge 27、LightGBM 17、XGBoost 14，均 TARGET_ORDER_FILTER；动量和 MA 为 0。固定目标下反复调仓造成小差额请求，不能把正式信号数当成交数。成交名义额/初始权益约为 2.584/2.877/3.063/8.966/1.317 倍；这里是双边累计换手定义。

单纯加回手续费后，各交易策略收益仍为负：Ridge 约 -0.5793%、LightGBM -0.5445%、XGBoost -0.6450%、动量 -0.5400%、MA -0.1831%。这不是重新模拟零成本账户的反事实，滑点和仓位路径仍在；已有结果不能归结为“原本正 Alpha 仅被手续费吃掉”。

Paper 评价有 408 个事件步：预测标签 407 个，最后额外信号可在最终开盘执行但没有再下一开盘标签。各策略沿用 P7-M01 相同回放边界；不把交易步数误报为预测样本数。末端持仓按最终开盘代理估值，不强制平仓；持仓时间比例按每步成交后是否非零计算，非平均资金权重。短样本年化 Sharpe 仅描述，不作显著性判断；无交易 Sharpe 未定义。

### 精确成交累计后的持仓时间比例

按订单分别以 Decimal 计算累计成交、增量成交、持仓与成交名义额，只在输出研究指标时转为 float。重复累计确认的增量为零；不同订单的 BUY/SELL 净额在完整平仓时精确归零，不采用浮点容差。

| 策略 | 持仓事件步 / 总事件步 | 修正后持仓时间比例 |
| --- | ---: | ---: |
| Ridge |318/408|77.94%|
| LightGBM |295/408|72.30%|
| XGBoost |283/408|69.36%|
| 动量 |198/408|48.53%|
| 无交易 |0/408|0.00%|
| MA5/MA20 |171/408|41.91%|

既有 float 累计将部分空仓步误计为持仓，原 Ridge 79.17%、LightGBM 80.64%、动量 74.51% 已更正。动量 48.53%、LightGBM 72.30% 与审查报告一致，数值由原始成交确认重新计算，未固定输出常数。

本次仅运行证据发布入口，读取原研究结果和既有 SQLite 事件；未训练模型、未重放交易、未改数据窗口、参数、阈值或执行假设。与修复前证据逐字段比较，仅上述三个持仓比例以及成交名义额/换手率的末位浮点舍入改变；XGBoost、无交易完全不变。所有预测、订单、成交、费用、权益路径、净收益、回撤、波动率、Sharpe、拒绝原因及基准收益差均未改变。重新生成并核对后，NEGATIVE_RESULT 继续成立。

定向测试 **7 passed，1.54 秒**，新增小数多次部分成交、重复累计确认、多订单 BUY/SELL、完整平仓及后续空仓步回归，同时核对精确名义额和换手率。

## 缺失的真实成本证据与结论强度

REAL_BINANCE_HISTORY / HYPOTHETICAL_PAPER_EXECUTION。开盘为历史估值代理，100 单位假设流动性不是交易所真实成交。历史价差、实际深度/冲击、账户手续费档位：NOT_VERIFIED。规则为已有快照而非历史 PIT 规则。

P6-D02 有 275 个资金事件，不能写成历史费率不存在；其 Special 适用规则、账户名义价值与实际资金扣款未绑定，本轮不计资金结算，状态 NOT_VERIFIED_NOT_APPLIED。没有补造资金费率。净收益仅指本轮合成手续费及滑点后的 Paper 收益，不是完整永续成本收益。

90 天、单一标的、约 17 天 Test 和两个短验证窗不足以作统计稳定性认证，标记 INSUFFICIENT_HISTORY。当前证据支持 **NEGATIVE_RESULT**：没有可验证的稳定预测优势，没有优于无交易或 MA 的机器学习交易表现。MA 比模型低换手、较低亏损及回撤，但同样未证明盈利。

**不建议以盈利候选身份进入更长时间前向验证。** 若目标是检验负面结论的跨阶段稳定性，可以保留这些冻结配置做低成本、更长历史或无资金前向观察；本轮不扩采、不挖因子、不搜阈值。研究结论不是市场永远不可预测的证明。

## 复现

使用已有含 numpy/pandas/sklearn/lightgbm/xgboost/pyarrow 的环境，无需新环境。工作目录为仓库根：

```bash
PYTHONPATH=src python -m factorlab.research.binance_model_study \
  --hourly-snapshot <P6-D02-root>/hourly/runs/bddaab1cd5c943e28caa62c0fc49ef33 \
  --output <new-study-directory>
PYTHONPATH=src python -m factorlab.research.binance_model_evidence \
  --study <study-directory> --output <evidence-directory>
PYTHONPATH=src python -m pytest tests/research/test_binance_model_study.py -q
```

实际环境为 `D:/Environments/factorlab-py313/Scripts/python.exe`，LightGBM 4.6.0、XGBoost 2.1.4。原始账本及逐预测 CSV 留在忽略的 `results/p7-r01/completed-study`，提交聚合证据和必要交易详情，不重复提交账本。定向测试结果见 targeted-tests.xml。
