# P9-D02：停牌估值与历史 ST 的限定研究消费

2026-11-11 · **P9_D02_BLOCKED：两条第一折账本恢复条件估值资格，完整三折仍阻断**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 研究恢复了什么

002408.SZ 的停牌账面估值可以进入显式选择政策的独立研究版本；000540.SZ 的定向历史 ST 身份也可以消费，但它们不共同构成执行许可。本次实际复用保存的预测重放最小阻塞窗口，并重新核验两条原保存的完整第一折账本，没有重训模型或重算因子。

|模型 / 第一折|实际最小重放|原完整账本重新核验|新增条件可估值区间|
|---|---|---|---|
|FactorScore|2023-03-13—2023-05-08，37 会话|2023-03-13—2024-03-12，243 会话|2023-04-26—2024-03-12，212 会话|
|Ridge|同上|同上|同上，212 会话|
|LightGBM|2023-05-08 / 000540.SZ 中止|没有返回完整账本|未恢复|
|XGBoost|NOT_RUN|NOT_RUN|未恢复|

243 会话包括起始现金信号日；原账本实际记录242个执行会话。新增212会话是相对于原真实观察前缀截止日2023-04-25的延伸，不是212个停牌日。两账户合计新增 **424 个条件估值账户日**，恢复 **2 个模型×第一折**的数值账本范围；没有恢复任何模型的完整三折比较。最小窗口的6个新增会话包含4个停牌会话及复牌后的2个会话，不与完整账本的212会话重复计数。

完整账本恢复属于对原保存账户的证据消费与重新核验，不宣称重新执行了整折。新重放仅覆盖最小窗口，且从原折起点保留账户前史；目标先按原完整 Test 日历生成，再截取执行范围，未把窗口末端改成清仓信号。

## 停牌估值与成交分离

独立配置显式选择 `LAST_OBSERVED_RAW_CLOSE_CARRY_ON_CONFIRMED_SUSPENSION` 版本1。消费原 P9-D01 的 `load_sidecar()`、`carry()` 及绑定文档，只允许002408.SZ在2023-04-26、04-27、04-28、05-04对已有正持仓估值。前序真实观察为4月25日7.12元；raw_close保持缺失，估值记录为 `MODEL_VALUATION_ASSUMPTION`、`nontradable=true`。其他证券/日期的缺价不能借此获得carry。

FactorScore持有13,300股，单日账面值94,696元；Ridge持有13,600股，单日96,832元。两账户共 **8个采用假设的账户日**，清单见 [valuation-policy.json](valuation-policy.json)。5月5日恢复真实观察收盘6.76元，不继续carry。

三种资格分别保留：`OBSERVED_MARKET_PRICE`、`MODEL_VALUATION_ASSUMPTION`、`UNVALUABLE_POSITION`。仅观察政策下，两条账本仍因缺价阻断；显式carry政策下，逐持仓价格、股数、现金、费用和权益核对通过。估值没有进入 raw_open、raw_close 或执行价格；最小重放中停牌证券成交为0。原保存的完整账本从未被改写。

本次7.12元与原执行器诊断账本沿用的旧mark一致，因此 **NAV金额变化为0**。恢复的是估值的明确语义和证据资格，不是发现了停牌日真实价格。整条条件净值路径包含估值假设，不能把停牌日标成真实市场收盘，也不能以复牌恢复观察抹去此前假设。

## 历史 ST 与剩余执行障碍

只消费000540.SZ / 2023-05-08 / is_st。原码 `st_status=2` 保留；其他未知枚举和没有定向证据的日期不被全局映射为false。P9-D01公告公开时间保守上界为4月29日上海日终，早于原5月5日15:00信号截止，且发行人规定5月5日起实施退市风险警示。来源版本、文档身份及精确证券/日期/字段均绑定。

ST身份已知不等于5月8日开盘交易状态已事前认证。独立适配明确保留该日执行权限 `UNKNOWN`，不使用事后日成交或价格存在性生成正常交易许可。LightGBM真实重放仍触发：

```text
SchemaError: execution status is missing for 2023-05-08/000540.SZ
```

因此未产生该账户的拒单回执或卖出成交，也未宣称返回了其部分账户账本。原执行器的fail-fast顺序不变。

另外使用同一原 `PriceLimitSchedule` 对真实输入单独诊断：前收0.63元、开盘0.60元、ST的5%限制与价格刻度得到跌停下限0.60元，`sell_blocked=true`。这是规则诊断，不冒充实际账户拒单；即便未来取得合法交易状态，现有保守日线规则也不会因当日有成交量而允许该卖单。具体订单资格仍是 **ORDER_EXECUTABLE_UNVERIFIED**。

最小窗口在该明确障碍停止。XGBoost第一折、四模型后续两折以及原两个股票基准后续完整执行均未开展，不能将NOT_RUN解释为零收益。没有采用事后正常交易语义绕过这个边界。

## 与冻结 P9-01 的对比

保存的196,112行候选预测文件身份保持：

`c8f18f07fde5521c340b9ad3c4364469a855481fc5f0c4b12d93a6ab20e6ad3f`

没有直接交易P8-02未来标签筛选后的原样本。四模型配置、Top10、每五会话时钟、原折边界、共享资金、费用、滑点与限制均保持原合同。三条已尝试路径的完整目标与冻结目标精确相同；两条成功最小重放的共同成交也精确相同。发布阶段另外逐字段比较最小重放的持仓和NAV（含现金）与原保存账本，结果一致。

复用的账户核算检查被提取为 `audit_accounting()`；旧 `audit_ledger()`仍要求真实收盘，不更改原研究的默认资格。独立政策核对只在原核算检查之后验证分离的估值对象，没有复制第二套撮合或制造行情通过审计。

对应证据见 [evidence-consumption.json](evidence-consumption.json)、[account-reconciliation.csv](account-reconciliation.csv)、[restored-scope.csv](restored-scope.csv)。剩余阻塞由“两条停牌估值缺口 + 一条ST/执行状态障碍”缩小为后者的执行资格缺口，未升级来源的PIT/HFQ身份。

## 可以发布的范围

可以发布两条第一折的**条件估值账本核对结论、明确标注假设的数值研究范围、原观察前缀及定向规则诊断**。[conditional-daily-nav.csv](conditional-daily-nav.csv)提供两条已核验路径的组合级净值，每行标明整条路径包含估值假设，并单列当日是否实际使用假设；不包含商业证券明细。本交付不将它们汇总为四模型完整绩效。

四模型完整三折净收益、年化收益、Sharpe、回撤、Calmar及模型胜负仍不能发布为有效比较，机器结果保持null。两条恢复路径也不据此获得真实可成交、正式金融准入或投资者总收益资格；原价账本仍缺完整公司行为，总收益维持 `TOTAL_RETURN_UNAVAILABLE`。原P9-01保持BLOCKED并原样保留。

验收问题的直接答案：**002408限定账面估值可以消费；000540定向ST身份可以消费；跌停限制严格保留；恢复两条第一折共424个新增条件账户日；完整三折与总收益仍不可发布；没有证据不支持的虚拟停牌成交，也没有000540被强行卖出的成交。**

TODO：取得000540.SZ / 2023-05-08满足执行合同的历史状态及事前可得证据后，在另一个独立版本检验剩余阻断；现有跌停限制和订单可成交证据要求保持不变。

## 来源、资源与复现

启动核实PR #84已合并，提交 `6c953aae0c9c81301c2a7f7e9254a1ee8a64fd8c`；远端main为 `09d1f97c6ab8175ec9f569d58b72aa319bb053b1`，已包含#83/#84/#85。本版本不覆盖P9-01/P8冻结输出，不修改DataHub正式合同、模型工厂或金融门禁。

使用Python3.14、NumPy2.3.4、pandas2.3.3、PyArrow22.0.0，与原保存数值环境一致。单进程串行，预算最多4进程、8GiB软上限；没有模型训练或全量因子重算。仅投影原128证券、第一折需要的月度价格/状态分片及来源日历；公共文档、原始值与manifest身份核验后消费。来源身份与私有结果摘要已写入机器产物，完整商业数据、预测和账户明细留在私有输出。

```sh
python -I -B -X utf8 tools/research/run_evidence_consumption.py \
  --study '<FROZEN_P9_01_PRIVATE_OUTPUT>' \
  --original '<FROZEN_P8_02_PRIVATE_OUTPUT>' \
  --source-root '<P9_D01_PUBLIC_DOCUMENT_DIRECTORY>' \
  --stock '<EXISTING_STOCK_ZIP>' --bigquant-root '<BIGQUANT_ARCHIVE_DIRECTORY>' \
  --output '<NEW_PRIVATE_DIRECTORY>' --requalify-fold0

python -I -B -X utf8 tools/research/publish_evidence_consumption.py \
  --study '<NEW_PRIVATE_DIRECTORY>' --original-study '<FROZEN_P9_01_PRIVATE_OUTPUT>' \
  --output '<NEW_PUBLIC_DIRECTORY>'

python -I -B -X utf8 -c "import sys; sys.path.insert(0,'src'); import pytest; sys.exit(pytest.main(['-q','-p','no:cacheprovider','tests/research/test_evidence_consumption_study.py','tests/research/test_p9_market_evidence.py','tests/research/test_oos_execution.py','tests/research/test_shared_portfolio.py']))"
```

最终定向与直接相关回归 **89 passed / 4.78秒**。运行历史保留于 [verification.json](verification.json)：首次在输入核验阶段发现manifest默认指向错误版本并拒绝，第二次在混合Decimal/float估值求和时失败；修复为原P8 manifest及显式数值聚合，并增加混合估值回归后成功。两次失败都没有授予未知状态执行许可。成功运行在预期LightGBM阻断点正常完成证据输出，后续未盲目重试。
