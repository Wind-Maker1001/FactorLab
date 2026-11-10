# P9-D01：定向交易状态与停牌估值证据

2026-11-11 · **P9_D01_PARTIAL**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 两个障碍分别解决到哪里

002408.SZ 的四个缺价会话属于正式停牌；能够建立“确认停牌期间沿用最后真实观察收盘价”的独立候选估值政策，但不能恢复不存在的日度成交收盘价。000540.SZ 当日发生过交易，且实际有原始价格与供应商状态记录；原错误来自 *ST 原码在适配层丢失，并非整条来源记录缺失。事前 *ST 身份及5%限制已有发行人公告支持，但交易当日状态的事前可得性、盘中中断及具体卖单成交能力仍为 UNKNOWN。候选证据不提供执行许可。

## 002408：为什么缺少四个真实收盘价

[2023-022停牌公告](https://static.cninfo.com.cn/finalpage/2023-04-26/1216579098.PDF)规定：要约收购期限4月25日届满，结果需要进一步确认，股票自 **4月26日开市起停牌**，于要约收购结果公告日开市起复牌。[2023-047复牌公告](https://static.cninfo.com.cn/finalpage/2023-05-05/1216746035.PDF)再次确认停牌起点，并明确 **5月5日上午开市起复牌**。因此来源日历中的4月26、27、28日和5月4日均落在停牌区间。第一份公告没有预先确定5月5日；完整终点是第二份公告回顾性闭合的事实。公告只有日期，本任务不把它们的日期上界当作当日开盘前已知时间。

|来源|4月25日|四个停牌会话|5月5日|
|---|---|---|---|
|BigQuant 原价与状态|收盘7.12，有成交|OHLC=null、volume=0、suspended=1|开盘7.13、收盘6.76，有成交；suspended=0|
|CSMAR 原始日行情|收盘7.12、Filling=0|四列价格7.12，但 **Filling=1、成交量0**|收盘6.76、Filling=0，有成交|
|CNRDS 2023目标分片|收盘7.12，有成交|四列价格7.12，但 **成交量/金额空值**|收盘6.76，有成交|
|通达信原生DAY|收盘整数712|四个日期均无记录|收盘整数676|

CSMAR字典明确 Filling=1 是非节假日填充，不是正常交易观察。CNRDS的重复价格也没有成交证据，不能替代BigQuant缺失的真实raw_close；空成交量不等于0。通达信以既有原生解析器的候选除数100解释整数，只作为辅助观察，不升级其单位、历史可得性或调整认证。

**最后可验证真实观察收盘价是2023-04-25的7.12元/股。** BigQuant、带单位字典的CSMAR及CNRDS一致。停牌期间没有发现可用的真实成交记录；这与正式全日停牌及BigQuant缺价相容，不解释成供应商必须提供真实收盘价的缺陷。公告中的 **7.14元要约价**、5月5日复牌价格以及后续价格均不进入停牌raw_close。

### 有限既有持仓估值

政策为 `LAST_OBSERVED_RAW_CLOSE_CARRY_ON_CONFIRMED_SUSPENSION`，版本1。仅用于已经持有的股份；每次返回独立估值对象，`raw_close=null`、`valuation_status=MODEL_VALUATION_ASSUMPTION`、`nontradable=true`，并绑定4月25日观察、价格来源身份及停牌公告。估值对象没有开盘价或成交价字段。

FactorScore原有 **13,300股**，每个停牌会话账面值 **94,696元**；Ridge原有 **13,600股**，账面值 **96,832元**。四会话、两账户共8个受影响账户日。每天受影响权益比例及1%估值价格变动的权益敏感性见 [valuation-sensitivity.csv](valuation-sensitivity.csv)。分母直接引用已保存的诊断账户权益，且同样条件于该carry假设，不是独立真实市场权益。

|会话|FactorScore受影响权益|Ridge受影响权益|
|---|---:|---:|
|2023-04-26|10.092914%|10.264645%|
|2023-04-27|10.021034%|10.206932%|
|2023-04-28|9.966936%|10.124426%|
|2023-05-04|9.884137%|10.106832%|

例如首日估值输入上升1%，在其他部分固定的局部敏感性中，两账户权益分别增加0.100929和0.102646个百分点；下降1%则反向。该量没有提供停牌期间真实价格变化。

这些数值均与原执行器旧mark **7.12** 一致，所以在既有路径上仅改变估值语义和证据资格，不改变金额。经济意义是“交易中断期间最后观察成本基准的账面延续”，不是停牌期间存在可变现报价，也不是当日公允市场价值。1%敏感性只是对估值输入的局部变化量，不是实际收益或另一个回测。5月5日恢复来源真实收盘6.76，不继续carry；对应两笔持仓账面值分别89,908和91,936元。

## 000540：缺失的是哪一层状态

原P9-01 `execution_bars()`把 suspended映射为交易状态，但只保留 `st_status∈{0,1}`。BigQuant **5月8日原码为 suspended=0、st_status=2、is_risk_warning=1**；其中2被适配层转换为NaN，现有执行器检查trade_status或is_st任一个缺失即抛错。本任务不修改这段未合并实现，也不把2在全市场范围映射为true。

发行人[2023-36退市风险警示公告](https://pdf.dfcfw.com/pdf/H2_AN202304291586106477_1.pdf)签署于4月28日，公开索引公告日为4月29日：**5月4日停牌一天；5月5日开市复牌并实施退市风险警示，简称变为*ST中天，日涨跌幅限制5%。** 使用4月29日上海日终作为保守公开时间上界，早于既有5月5日收盘信号及5月8日开盘执行。因此本证券该日的is_st=true可以独立于供应商枚举，作为 `SIGNAL_TIME_STATE_KNOWN` 的候选事实。

[5月10日02:46发布的异动公告转载](https://finance.sina.com.cn/roll/2023-05-10/doc-imytfksn8482215.shtml)明确提及5月5、8、9日连续三个交易日收盘偏离，支持 **EX_POST_TRADING_CONFIRMED**，不支持5月8日指令发出前已经知道全天状态。

|字段/问题|实际证据|等级或边界|
|---|---|---|
|当日是否成交|BigQuant与CSMAR有正成交量，异动公告列明该日交易|EX_POST_TRADING_CONFIRMED|
|ST/*ST身份|事前发行人公告；当日CSMAR Trdsta=3，与字典*ST一致|SIGNAL_TIME_STATE_KNOWN，仅is_st事实|
|开盘/收盘及完整OHLC|BigQuant和CSMAR四列均0.60；CSMAR Filling=0|EXECUTION_PRICE_AVAILABLE，历史价格输入候选|
|前一有效收盘|5月5日0.63，与当日pre_close一致|供应商观察值，非事前分发认证|
|量/价格限制|两供应商当日5,953,200股；下限0.60，上限0.66|不推导特定卖单可成交|
|5月8日前可知交易状态|5月5日复牌安排已公开，但缺5月8日开盘状态发布/接收证据|UNKNOWN，不生成trade_status许可|
|盘中临时停牌或中断|日线和公告不足以排除|UNKNOWN|
|该开盘卖单实际成交能力|无逐笔/盘口、队列及订单回执|ORDER_EXECUTABLE_UNVERIFIED|

CSMAR **5月5日Trdsta=1** 与事前公告实施*ST的时间冲突；该日价格和Filling字段可单独核对，但其状态不进入候选，不能借它覆盖发行人指定身份。CNRDS目标2023代码排序分片的000540目标记录为空，保持SOURCE_MISSING；通达信既有资产索引没有sz000540.day，不扩展为“世界上不存在该来源”。

以已核实*ST的5%规则、前收0.63及0.01价格刻度计算，跌停下限四舍五入为 **0.60**。调用现有 `PriceLimitSchedule.blocks(... side='sell', is_st=True, tolerance_bps=5)` 得到 **blocked=true**。这是现有保守日线执行规则会拒绝卖单的诊断，不是该账户已经产生了一笔拒单回执，也不因当日有成交量便允许卖出。未知限制条件不会得到默认为正常交易的许可。

## Sidecar最小消费合同

候选版本在 [candidate.json](../../../configs/research/p9-d01/candidate.json)，来源文档身份、摘录及观察胶囊在 [public-sources.json](public-sources.json)。所有候选精确限定证券、日期和字段，绑定原P8-02输入manifest字节身份及具体来源文档。manifest、原始值或文档身份漂移、重复/冲突候选、范围扩张均拒绝。11条候选不是11个获得统一认证的字段。

- `suspension_confirmed`：仅002408四个指定会话，发行人证据。
- `valuation_mark`：仅同四会话，独立模型假设；需要前序真实价格、来源字节及正既有持股数，不能替换raw_close或成交价。
- `is_st`：仅000540/2023-05-08，公开时间核验后可读取事前身份事实。
- `trading_observed`：仅该日，事后确认，不产生交易许可。
- `execution_price_available`：仅该日，表示真实历史价格输入存在，不是成交能力。

`load_sidecar()`只读取和验证，保留original_value；`fact(... purpose='SIGNAL_STATE', cutoff=...)`仅允许该is_st事实且核对时间，其他事前/执行用途拒绝。`carry()`返回独立估值对象，不接收或返回订单、成交、可交易bar。原始raw_close和供应商状态不覆写；UNKNOWN trade_status不转换成正常交易。

现有引擎的开盘与收盘缺价沿用旧mark，不要求正式停牌证据；收盘有观察值后恢复真实价格。session仅传递mark及mark_time，未表达“实际观察/模型假设”的独立资格。**本任务不需要修改执行器来交付证据；后续若消费此估值政策，最小接口是单独传递估值价格、来源日、证据和估值资格，并让研究评价显式选择允许的政策，不能把carry塞进raw_close。** 000540后续适配只允许消费定向is_st证据；其他状态的事前与回顾性研究合同必须明确，保持原有涨跌停拒单逻辑。

## 下一步与研究边界

P9-01原冻结配置要求逐日真实收盘；该条件在正式停牌期间不能靠数据补丁满足。后续独立研究必须显式选择carry版本及相应条件化估值口径，保留原BLOCKED研究。*ST定向身份可消除已定位的枚举丢失问题，但本次不授权完整研究立即恢复：仍需明确5月8日交易状态的消费时点合同（事前证据或显式事后历史日线研究语义），以及估值对象的最小接入。若要求逐笔实际可成交保证，还需开盘盘口/队列及订单证据；若沿用既有保守日线模型，本日应拒绝卖单，而非强制成交。

TODO：P9-01合并后，在独立PR中接入具名估值政策及该日状态候选；不改原研究产物，重新研究须新版本且保留涨跌停限制。盘中中断及实际卖单成交能力保持UNKNOWN，不能为了消除BLOCKED补写许可。

## 来源范围、版本与复现

基线为origin/main `83d9b3a8d8db7d1a6edbcbb912d24fc520d574f3`，PR #83核查时未合并，只读头提交 `c2a0326c25309628cabae3c521642b3f02634a41`。主线没有P9-01实现，未复制其提交。P8-03的供应商语义、时间可得性及准入边界沿用，不升级为全市场认证。

BigQuant仅读取2023年4、5月价格/状态四分片；CSMAR仅读目标2023首分片；CNRDS仅读2023代码排序首分片，遇到002408后即停止；工作簿压缩格式没有证券索引，因此只对这两个必要分片作有界前缀读取，二者可并发。没有重下载商业资产、生成临时全量Parquet或扫描整个仓库。CSMAR另两份年度导出只核查首行范围，不作为2023目标证据。具体哈希、字典和缺来源摘要见 [source-identities.json](source-identities.json)。原始公开文档及不可再生查询响应保存在本地证据目录，仓库仅保存必要摘录与哈希；商业目标行和完整账本不发布。

来源当前导出/取得时间不是历史首次分发时间：BigQuant2026导出、CSMAR/CNRDS/通达信历史分发UNKNOWN；issuer已公开条款与当日尚未出现的开盘/全天事实分开。全部证据均为本次2026-11-11回顾性核验。

```sh
python -I -B -X utf8 tools/research/verify_p9_market_evidence.py \
  --sidecar configs/research/p9-d01/candidate.json \
  --sources docs/validation/p9-d01/public-sources.json \
  --source-root '<EXISTING_P9_D01_PUBLIC_SOURCE_DIRECTORY>' \
  --manifest '<EXISTING_P8_02_INPUT_MANIFEST>' --stock '<EXISTING_STOCK_ZIP>' \
  --study '<EXISTING_P9_01_PRIVATE_OUTPUT>' \
  --frozen-result '<P9_01_C2A0326_RESULT_JSON>' \
  --csmar '<EXISTING_CSMAR_2023_DAILY_ZIP>' --cnrds '<EXISTING_CNSP_ZIP>' \
  --output '<NEW_VERIFICATION_DIRECTORY>'

python -I -B -X utf8 -c "import sys; sys.path.insert(0,'src'); import pytest; sys.exit(pytest.main(['-q','-p','no:cacheprovider','tests/research/test_p9_market_evidence.py']))"
```

实际执行结果见 [result.json](result.json) 与 [verification.json](verification.json)。最终定向测试 **41 passed / 1.49秒**，真实来源核验成功生成8条既有路径敏感性，现有规则卖单检查为blocked=true。测试覆盖17项要求的边界，并实际验证既有引擎在复牌后恢复观察价格、停牌时不产生虚拟成交；核验工具只读取保存路径，不训练、不执行账户。
