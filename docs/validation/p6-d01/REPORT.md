# P6-D01：多证券真实历史研究输入

2026-11-10 · **P6_D01_RESEARCH_INPUT_READY**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 可交付的实际输入

固定沿用P5-R03的12组、24只证券，不替换证券。既有私有P5-R03 Parquet除`Adjprcwd → adj_close`外，实际还保留了独立的原始OHLC、量、交易状态与ST代理；本次复用原始列，未重新扫描13百万行原生工作簿，也未将再投资价格用作成交价。

2015-01-05—2018-12-28共**975个观察会话、23,400个证券日**。2015/2016/2017各244会话，2018为243会话。**22,955个证券日合格，445个证券日拒绝**，无重复键、无所给观察日历中的缺失证券键。445行均为`trade_status=0`、volume=0、四列raw OHLC为空；拒绝理由为`SUSPENDED_OR_VENDOR_FILL_NO_CLOSE_MARK`。正常交易行无非有限/非正价格、OHLC关系异常、非法状态或异常成交量。全部原始观察的ST代理为0，不等于独立认证历史ST身份。

**13只证券四年全段RESEARCH_INPUT_READY**：600028.SH、600585.SH、601186.SH、601288.SH、601318.SH、601398.SH、601601.SH、601628.SH、601688.SH、601857.SH、601898.SH、601939.SH、601988.SH。另外11只为PARTIAL，而非整只丢弃；无整只BLOCKED。READY表示至少12只完整覆盖本观察窗口，不能理解为24只全部完整。

|PARTIAL证券|合格证券日|拒绝证券日|合格连续区间数|
|---|---:|---:|---:|
|000401.SZ|857|118|7|
|000709.SZ|936|39|2|
|600019.SH|883|92|4|
|600030.SH|971|4|1|
|600036.SH|970|5|2|
|600837.SH|974|1|2|
|600999.SH|969|6|2|
|601088.SH|911|64|2|
|601166.SH|970|5|2|
|601336.SH|950|25|2|
|601390.SH|889|86|4|

逐证券精确日期、拒绝统计与**43个连续合格区间**见[quality.json](quality.json)。完整年度可用证券数量依次为18、21、21、21；[year-coverage.csv](year-coverage.csv)保留全部96个证券年度，不按收益挑选。不能将分离区间首尾拼接为连续回测，也不能越过缺价停牌日持仓。上市/退市只保留首末观察候选，未将窗口边缘写成真实上市或退市日期。

## 来源与字段

[manifest.json](manifest.json)绑定唯一消费Parquet、上游manifest、源ZIP及三个原生成员的SHA256、原生parser/profile身份、字段映射版本与当前实现文件身份。重新读取原始407,652,983字节ZIP并核验其哈希，未下载数据。文件身份只证明字节一致，不证明历史PIT。

|来源|实际使用|
|---|---|
|CSMAR|唯一行情/状态来源；既有P5-R03规范化快照。原始ZIP含TRDNEW_Dalyr.xlsx、Dalyr2.xlsx、Dalyr3.xlsx，来源与成员SHA见manifest|
|BigQuant|参考P3-D06/P5-D01已保存资产/质量调查；本次不混合数值，不借用其2021—2026状态和上市快照补2015—2018身份|
|通达信|既有.day资产索引存在，但未建立本窗口24证券的独立字段与来源绑定；不覆盖CSMAR缺值|
|CNRDS|既有财报及事件相关资产索引不能替代原始OHLC/交易状态，不进入成交输入|

无本轮跨源数值一致性认证。既有资产证据见[P3-D06 assets](../p3-d06/assets.md)、[身份边界](../p3-d06/membership-identity-status.md)及[P5-D01](../p5-d01/REPORT.md)。

- `date/symbol`：无时区日日期、带交易所代码，稳定唯一键。
- `raw_open/high/low/close`：原生Opnprc/Hiprc/Loprc/Clsprc，CNY/股；volume为Dnshrtrd，股。沿用已审字典和原生十进制转换，无新缩放/舍入；Parquet数值为既有float64表示。
- `trade_status=1`：既有`volume>0 AND Filling=0`代理；`Filling=2`假日行已在上游排除，非交易填充保留观察但拒绝执行区间。is_st沿用Trdsta已有映射，当前版本不提供独立的状态认证。
- `adj_close`：含红利再投资可比收盘，**不消费、不暴露给研究执行查询**。无复权算法/锚点认证。
- 观察会话为24证券日期并集，不是正式交易所日历；不能发现所有证券与日历同时漏掉的会话。

## P6-R01消费合同

配置为[configs/research/p6-d01-multisecurity.json](../../../configs/research/p6-d01-multisecurity.json)。输入由三部分构成：固定配置、研究manifest/quality、唯一外部规范化Parquet。完整行情不复制到公开库，也不为各证券/年度生成重复Parquet。

```python
from factorlab.data.hub import DataHub
from factorlab.data.research_input import ResearchInputAdapter

adapter = ResearchInputAdapter(research_root, existing_p5_r03_root, pinned_manifest_sha256)
hub = DataHub({'research': adapter})
query = dict(symbol='601857.SH', start='2015-01-05', end='2018-12-28')
bars = hub.read('research', 'daily_bars', **query).data
calendar = hub.read('research', 'observed_sessions', **query).data
```

`research_root`含本次manifest/quality/config；`existing_p5_r03_root`含已固定的上游manifest与bars.parquet。消费者必须固定本次manifest的SHA256，且查询范围完整落在该证券一个合格interval内；跨度包括拒绝或缺失会话时直接拒绝。返回QueryResult/SourceMetadata，原有DataHub无需改造。返回字段只有date、symbol、四列raw、volume、trade_status、is_st；信号明确使用raw_close，高低价指标也用raw。按证券送入现有`run_target_positions`；本任务不建立多证券共享资金账户。

`knowledge_as_of`查询拒绝；正式PIT、HFQ或准入开关为真时拒绝。输入仍属于REAL_CANDIDATE回顾性输入，不是正式资格包。当前target执行器要求停牌日也有有限正收盘mark，所以缺价行必须切断区间，而不是默认昨收估值或补造成交。

复现准备（已有依赖Python3.13环境，所有路径显式传入）：

```sh
python -I -B -X utf8 tools/research/prepare_multisecurity_input.py --config configs/research/p6-d01-multisecurity.json --source-root '<EXISTING_P5_R03_ROOT>' --raw-root '<CSMAR_RAW_ROOT>' --output '<NEW_PRIVATE_OUTPUT>'
python -I -B -X utf8 tools/research/verify_multisecurity_input.py --root '<RESEARCH_ROOT>' --source-root '<EXISTING_P5_R03_ROOT>' --manifest-sha256 '<PINNED_RESEARCH_MANIFEST_SHA256>' --output '<NEW_VERIFICATION_JSON>'
```

准备重放使用保存的config.json；时间和运行耗时会变化，行情身份、质量结果与固定配置必须相同。准备输出只有小型JSON，不复制行情。

## 实测成本与验证

唯一消费行情为**844,219字节（约824.4 KiB）**，原始ZIP为407,652,983字节；本次没有新增Parquet副本。最终准备耗时**0.450秒**，含源ZIP内容身份检查及24证券质量分类。完整消费验证耗时**1.126秒**，Windows实测进程峰值工作集**153,939,968字节（约146.8 MiB）**；该峰值对应含准备结果重放的验证进程，不冒充原生工作簿提取峰值。完整原生提取成本属于上游已保存P5-R03执行。

最终**46项定向测试通过，28.84秒**，覆盖必需字段逐项缺失、原始/调整价格隔离、非法OHLC/非有限值、重复键、缺失证券、停牌区间阻断、消费哈希漂移、PIT查询拒绝及原有正式准入边界。[verification-final.json](verification-final.json)记录现有执行器实际消费全部43区间、22,955行，空仓目标不产生交易；这验证输入接口，不计算或筛选策略收益。[reproduction.json](reproduction.json)记录从保存配置重放得到相同质量结果与行情身份。manifest时间/耗时本来就非确定，不要求其整体哈希在重新准备后相同。

最终公开与私有研究manifest SHA256均为`800d8413ee7d2b0adc90adde6b38f5b290f12cd08189515afecf49268bde876a`。消费快照SHA256为`795d567e6ae3af6d96c7ab454f2dae4ca6f07f3cb481a934327a1b3c10d90611`。基线`88652d8a1f2b37a0140586c49497a235950510b7`；manifest同时绑定准备时实际实现文件SHA256，避免仅用未包含新增文件的基线提交冒充处理代码身份。

## 金融证据边界

A（可读取）覆盖24只；B（足够字段的限定回顾性执行输入）覆盖上述43个区间；C（正式历史PIT、复权及金融准入）**无证券获得**。当前供应商版本、历史首次可得时间、修订链、历史稳定证券身份与成分资格均UNKNOWN，整理日期不倒填known_at。经济关联名单及长期存续样本不代表无生存偏差的全市场股票池。

发现1个相邻原始收盘绝对跳变超过20%的候选；它只是需调查的公司行为/重组线索，不是经审公司行为，不据此改价或隔离此前观察。公司行为完整覆盖、登记日、送配股/换股权益及空头补偿未闭合，故仅允许原始价格收益研究，不能解释为含分红总收益或可执行配对套利。历史借券、召回、保证金、真实费率和容量仍UNKNOWN；日线与状态代理不证明涨跌停排队成交。除息参考价沿用现有执行器的近似限制，不提升资格。

TODO：在来源版本适用性、历史身份/修订链、官方日历及完整公司行为权益证据补齐后，通过既有准入和公司行为接口单独评估；不改变本数据集的限定回顾性身份。
