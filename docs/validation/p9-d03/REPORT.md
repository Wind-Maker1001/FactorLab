# P9-D03：000540停牌估值证据与Stage A恢复

2026-11-11 · **P9_D03_EVIDENCE_ACCEPTED** · **Stage A：PASSED**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

000540.SZ在2023-05-04的限定估值证据成立。发行人公告确认当天正式停牌，停牌前最后真实成交收盘为2023-04-28的0.66元。这个价格现在可以作为具名政策下的独立模型估值输入，而不是5月4日的真实收盘价。原始行情仍为空，执行器及交易规则不变。

复用P9-02的两条Stage A隔离路径后，现金、持仓、权益及费用审计均通过。随后仅运行 **LightGBM第0折从原折起点2023-03-13至2023-05-09的38会话前缀**，保留冻结目标和调仓日程，该范围完成并通过独立账本审计。证据验收、Stage A通过和这个有限Stage B结果是三个独立结论；没有把前缀称为完整折研究。

## 证据如何闭合

启动时工作区干净，已fetch的`origin/main`为`0cf5404510b497cf2ad0d974d21a04119219a0df`。已阅读并复用合并的PR #84、#88、#91、#92、P9-02报告、既有Sidecar、共享账户执行器及估值审计接口。原P9-D01/D02及P9-02的冻结报告不修改。

### 正式停牌及证券身份

发行人中天金融集团股份有限公司2023-36号《关于公司股票交易被实施退市风险警示暨公司股票停牌的公告》，签署日2023-04-28，公开公告日2023-04-29，原件为[发行人公告PDF](https://pdf.dfcfw.com/pdf/H2_AN202304291586106477_1.pdf)。复用P9-D01保存的原件并验证SHA256：

`55f64543cc636130a1befa013868b1a4c37f7f82511bab1642f90cd54cc2e456`

本次重新读取PDF文字，确认以下条款：

- 2023-05-04停牌一天，2023-05-05开市起复牌。
- 自5月5日起实施退市风险警示，简称由中天金融变为*ST中天。
- 证券代码无变动，仍为000540；证券种类为人民币普通股A股。
- 复牌后的日涨跌幅限制为5%。

公告公开时间只有日期，沿用上海时区4月29日日终的保守上界，不声称精确盘中发布时间。该上界早于5月4日估值和5月5日退出信号，但晚于4月25日买入信号。没有用公告倒改买入信号，也没有把2026年本地取得时间当作历史供应商分发时间。

### 最后真实观察、填充值和旧mark

本次定向取证只读BigQuant四个4—5月价格/状态分片，过滤两条既有Stage A路径所需行；000540调查窗口为4月25日至5月5日，5月8—9日仅用于既有拒单复现。分片字节与原P8-02输入manifest绑定。

|来源|2023-04-28|2023-05-04|2023-05-05及边界|
|---|---|---|---|
|BigQuant原生日线/状态|收盘0.66，成交量361,474,595股，未停牌|OHLC=null，成交量0，suspended=1|正成交量、suspended=0，恢复真实观察估值|
|CSMAR原生日线|收盘0.66，Filling=0，成交量与BigQuant一致|四价重复0.66，但Filling=1、成交量0|Filling=0、有成交；Trdsta=1与发行人*ST身份冲突，不消费该状态|
|CNRDS 2023首个目标代码分片|目标行缺失|目标行缺失|保持SOURCE_MISSING，不补造交叉验证|
|通达信既有资产索引|无sz000540.day|无目标原件|SOURCE_MISSING_IN_EXISTING_INVENTORY，不推断其他存储不存在|

CSMAR及CNRDS均重新读取原生工作簿字节，验证既有分片身份；仅顺序读取代码前缀，到超过000540即停止，分别121,859和30,670行。CSMAR的Filling字典身份也保存于[evidence.json](evidence.json)。没有把非节假日填充当成交观察，也没有把五一假期填充当新观察。

BigQuant目标窗口中，停牌前最后一条正成交量、有效真实收盘确实为4月28日，价格0.66元，与CSMAR真实观察一致。4月28日至5月5日的供应商exdr标记为0、adjust_factor不变，公告明确代码不变，未发现会使本次原价延续失效的证券身份或除权口径冲突。此核验不升级HFQ或完整公司行为账务认证。

因此三者必须分开：**5月4日真实raw_close缺失；CSMAR的0.66是供应商填充；执行器的0.66原本只是旧mark。** 本次独立证据使最后一种数值取得限定模型估值资格，并没有为前两种数据改写含义。没有根据复牌后价格选择carry价格。

## 唯一新增权限

[valuation-policy.json](valuation-policy.json)显式启用：

`000540.SZ / 2023-05-04 / valuation_mark`

政策仍为`LAST_OBSERVED_RAW_CLOSE_CARRY_ON_CONFIRMED_SUSPENSION`版本1。新增候选把独立停牌原件嵌入该估值候选的证据绑定，不对000540增加可消费的`suspension_confirmed`、`trade_status`或其他日期字段。

Sidecar只增加这一精确键；前序观察日从原有单一4月25日约束改为按获准证券精确绑定，002408仍只能使用4月25日，000540只能使用4月28日。候选同时绑定证券、日期、字段、原始空值、最后真实观察胶囊及其价格分片字节、独立停牌公告和复牌日期。缺证据、身份漂移、错误日期或错误价格均拒绝。

研究消费仍要求已有正持仓、持仓数量不因停牌变化、供应商停牌与原价缺失、当天无成交、引擎mark与独立核准价格一致，以及具名政策显式启用。返回`raw_close=null`、`nontradable=true`、`valuation_status=MODEL_VALUATION_ASSUMPTION`。对Stage A的138,800股，5月4日条件账面值为91,608元；不是可变现报价或当日真实市场价格认证。

现有`qualify_valuations()`仅增加可选的逐证券观察映射，继续使用现有`carry()`和`audit_ledger()`。观察价、模型假设、未核准估值三种状态不合并；原始行情不回写，执行器、费用引擎及ST跌停规则不修改。

## Stage A：恢复了什么

直接复用P9-02的`stage_a()`及原配置，而不是重新设计最小持仓路径：

- 002408的四个既有停牌会话继续通过核准，复牌恢复真实观察。
- 000540沿用4月25日信号、4月26日买入138,800股、5月5日退出信号。5月4日只有一个新增核准估值会话，停牌期间没有成交。
- 5月8日仍由既有5%跌停规则拒绝卖单，原因`sell_price_limit`；当天没有成交或新增费用，持仓继续存在。
- 5月9日没有自动重试成交，仍持有138,800股；5月5日至5月9日现金保持899,984.0512元。
- 唯一买入总费用79.9488元，佣金29.9808、滑点49.968、印花税0。逐笔重算通过，没有重复收费。

两条路径的现金、累计成交股数、权益及费用守恒全部通过，估值审计采用独立核准价格，不把未知价格当0。[stage-a-result.json](stage-a-result.json)保存必要派生收据；完整账户明细保持私有。

## Stage B：冻结日程的最小前缀

Stage A通过后，只启动`B_lightgbm / fold 0 / 2023-03-13—2023-05-09`，38个来源会话，从原折起点重放共享账户，不以局部新建仓替代真实组合路径。

冻结的128证券集合仍在原起始成员日确定；现有输入读取接口增加可选窗口，只读取该前缀涉及的来源分片及证券，不重算因子。候选预测文件SHA256仍为：

`c8f18f07fde5521c340b9ad3c4364469a855481fc5f0c4b12d93a6ab20e6ad3f`

原LightGBM目标Parquet和调仓决策CSV分别与P9-01公开保存的私有产物哈希核对后，再按前缀日期切片。**不以截短日历调用目标生成器**：原生成器会把窗口末端视为完整折终点，生成提前清仓目标。本次预执行身份门槛实际捕获并拒绝了这一差异；现有执行入口增加可选冻结目标计划，默认原行为不变。

该前缀完成；现金、股数、权益、费用和核准估值审计通过，只有000540/5月4日一个模型估值证券日，整体估值状态为`CONDITIONAL_ON_CONFIRMED_SUSPENSION_CARRY`。窗口末端不强制清仓。没有在本次执行范围中发现新的缺价、身份或状态阻断。

本次在这个最小范围停止：完整第0折、其他模型、后续折及Stage C均未启动，不发布完整折绩效。原三折和预测保持冻结，也未加载保存模型重新训练。后续授权不能由本次一个证券日的成功自动推导。[remaining-blockers.json](remaining-blockers.json)分别记录已消除的定向阻断、执行范围边界及仍未满足的金融合同。

## 验证与复现

定向及直接相关回归覆盖：新增精确范围及相邻日期拒绝、独立停牌公告缺失/漂移、前序观察日和身份绑定、正既有持仓、原价不回写、政策显式启用、逐证券观察映射、复牌恢复、跌停拒单与不自动重试、固定费用、现金/股数/权益守恒、Stage A失败禁止Stage B，以及前缀不重写终点目标。最终 **103 passed / 5.39秒**；代码身份和私有产物摘要见[verification.json](verification.json)。没有运行全仓库CI矩阵。

真实入口共调用4次：首次在Stage A之前因原生行未排序的日期切片失败；第二次Stage A通过，Stage B窗口成员/日历范围不匹配；第三次Stage A通过，预执行目标一致性门槛阻止了截短日历的终点目标变化；第四次使用有界读取及冻结目标计划，完成两阶段限定执行。失败输出保留私有，没有在失败尝试中重训或运行完整折。测试开发中另有一项过强的“不能有任何卖出”断言失败：固定权重调仓可以合法小额减仓；该测试改为核对冻结目标和未终止持仓，而不是禁止正常调仓。

单个研究进程、归档串行读取、数值线程1；同时最多另一个定向pytest进程，未提高4工作进程/6GiB预算。没有扫描全市场、全量因子计算或参数搜索。

```sh
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
python -I -B -X utf8 tools/research/run_targeted_valuation.py \
  --original '<EXISTING_P8_02_PRIVATE_OUTPUT>' \
  --study '<EXISTING_P9_01_PRIVATE_OUTPUT>' \
  --bigquant-root '<EXISTING_BIGQUANT_ARCHIVE_DIRECTORY>' \
  --evidence-root '<EXISTING_P9_D01_PUBLIC_SOURCE_DIRECTORY>' \
  --membership-zip '<EXISTING_JOINQUANT_MEMBERSHIP_ZIP>' \
  --csmar '<EXISTING_CSMAR_2023_DAILY_ZIP>' \
  --cnrds '<EXISTING_CNSP_ZIP>' \
  --inventory '<EXISTING_ASSET_INVENTORY_CSV>' \
  --output '<NEW_PRIVATE_DIRECTORY>'

python -I -B -X utf8 -c "import sys; sys.path.insert(0,'src'); import pytest; sys.exit(pytest.main(['-q','-p','no:cacheprovider','tests/research/test_p9_market_evidence.py','tests/research/test_p9_02_recovery.py','tests/research/test_oos_execution.py','tests/research/test_shared_portfolio.py']))"
```

商业供应商原始行、完整价格序列、冻结预测及完整交易/持仓账本保留私有。公开文件只保留限定观察、公告身份、政策及必要派生收据。原价研究仍为`TOTAL_RETURN_UNAVAILABLE`，不授予实盘许可。
