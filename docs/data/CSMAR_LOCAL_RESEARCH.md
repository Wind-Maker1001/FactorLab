# 本地 CSMAR 候选数据与公司行为账本

审查基线：origin/main `da5ed287d328d5a65b1da94393695b75b372652a`。
PR #10 冻结基线：`2935e8a553412b4fb77f3777bc3ae0922f8b29c5`，Draft、未合并。

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED。

## 现有能力审查

|问题|main 已有能力|缺口与本次复用/扩展|
|---|---|---|
|1. 如何发现原始文件|`csmar.reference` 要求显式 ZIP basename、成员、工作表计数和 SHA；操作者传入 source root|新增只读目录 catalog 自动定位成员；仍要求显式根目录|
|2. 如何解析 ZIP/XLSX|`native.source_zip/workbook/rows` 校验路径、文件类型/链接、哈希、CRC、扩展限额、XML、公式、物理行坐标；临时仅保存一个工作簿|完全复用，未新增 XLSX 解析器|
|3. 完整行情字段映射|`TRDNEW_Dalyr` 的 RAW OHLCV/amount；`IDX_Idxtrd` 明确限定 000905 / 中证500，成交量万股和金额万元转标准单位|新桥接读取已审字典中的 `Adjprcwd`，保留其含再投资可比价含义和候选状态|
|4. 仅原生提取的表|`TRD_AdjustFactor`、`TRDNEW_Co`、`TRDNEW_Cptl`、`TRDNEW_Capchg`、`IDX_Chgsmp`、`IDX_Smprat`；都有已审字典与主键|本次保留 native/lineage；Cptl 转统一 CorporateAction 的 PROPOSED 候选|
|5. Parquet 输出|`binding.write_candidate` 已有 raw/native/lineage；`publish_local_market_data` 和 `ParquetBarCache` 已有不可变代/缓存合同|新数据集只是原重放之上的分区编排，不重新实现缓存|
|6. 研究如何读取|LocalParquetProvider 验证 manifest、字节、schema、source_version；DatasetBuilder 使用统一 provider，经正式消费边界验证资格；cache 保留源身份|本候选不冒充 LocalParquetProvider 的合格包；信号诊断显式读取候选 Parquet|
|7. 统一事件模型|CorporateAction、EventCoverage、SourceSnapshot、版本/知识时点选择、不可变 store 已存在；有本地已审、公告、Sina 因子来源|Cptl 先映射候选，缺 record_date/reference 等不得成为执行事件|
|8. 因子能关联事件吗|`build_hfq` 可按完整事件、独立 anchor、每信号知识时点生成因果 affine/ratio，并校验事件来源与覆盖；原生因子表仅校验数值|尚无 CSMAR 因子→分配事件的认证连接；本轮只出对照诊断，不假定所有因子变化均为现金分红|
|9. 为什么不能直接供 PR10|需 raw 成交价、连续信号价、状态、预热和日历；PR10 拒绝变动复权比例/非空行为账本；真实入口仍拒绝准入|新行情可供有范围限制的信号研究；不改变 PR10 的收益执行拒绝|
|10. 最小新增接口|旧入口需要手工填写每成员配置|catalog_source/references、prepare、独立小 CLI、通用 cash_dividend_ledger；隔离集成检查 PR10|

字典哈希是内容版本，不等于供应商获取时间、历史 vintage、许可、身份或独立认证。
已映射表与未审表必须分开：目录清单中 UNKNOWN 不代表没有数据。未选范围的全市场主键重复和冲突保持 NOT_CHECKED；选中范围由原 replay 主键检查和跨分片重复检查拒绝。

## 使用

使用仓库支持的 Python 3.14 或 3.13 及锁定依赖。从源码运行时设置 `PYTHONPATH=src`。
原始目录只读；scratch 必须已有且满足 native 的同盘空间约束；output 必须是新目录。

```powershell
python -m factorlab.data.csmar.local_cli list `
  --source-root 'E:\CSMAR Raw' --scratch 'D:\Code\FactorLab-worktrees\my-csmar-scratch' `
  --output 'E:\FactorLabData\candidates\my-catalog-v1' `
  --scan-family TRDNEW_Dalyr --scan-family TRDNEW_Cptl `
  --scan-family TRDNEW_Capchg --scan-family TRDNEW_Co --scan-family TRD_AdjustFactor

python -m factorlab.data.csmar.local_cli prepare `
  --catalog 'E:\FactorLabData\candidates\my-catalog-v1\catalog.json' `
  --source-root 'E:\CSMAR Raw' --scratch 'D:\Code\FactorLab-worktrees\my-csmar-scratch' `
  --output 'E:\FactorLabData\candidates\my-601857-v1' `
  --symbol 601857.SH --warmup-start 2017-01-01 --start 2018-01-01 --end 2018-12-31
```

`list` 无 scan-family 时只盘点文件/成员哈希；加 scan-family 则逐物理行计算该表记录数、代码集合、日期范围和 metadata。没有 profile 的表保持 UNKNOWN。大型扫描按来源保存 checkpoint；用 `--source-name` 可明确限定文件，避免重复扫描无关档案。来源优先级固定为显式 CSMAR_ONLY；重叠主键拒绝，不能静默拼其他供应商。没有新增顶层 `factorlab data` 命令。

`prepare` 通过已有 reference/replay 再次校验实际原始 ZIP、成员、字典、表计数和选中记录；生成 market、corporate_actions、calendar、provenance、diagnostics 和 manifest。manifest 绑定文件字节、代码提交/实现文件、运行时和输入 catalog。生成中失败会留下无最终 manifest 的目录，作为失败证据；重跑必须用新目录。

`raw_*` 为原始 CNY/股价格，volume 为股，amount 为 CNY。`adj_close` 来自已审字典的 Adjprcwd（含现金红利再投资的可比收盘），不是把 raw_close 改名，也不声称 HFQ 已认证。供应商节假日填充行不进入 observed_sessions；停牌占位的缺失 RAW 不填充。Trdsta 的 ST 系列与 Filling/volume 分别决定候选状态；不支持的 PT 状态拒绝。calendar 只是该证券原生观测集合，不能宣称交易所完整日历。

Cptl 的 CA 金额字典为每股 CNY；非 CA 事件保留 OTHER 和原始分配类型，执行拒绝。Capchg 是股本存量变动记录，不能由存量差推测投资者获赠股份。公司表仅作为当前导出的身份观测，不能证明历史身份。登记日不得用除息日代替。外部公告只能作分字段补证，必须保存官方 URL、下载时点、原件/抽取结果哈希和与原始行的明确关联；贡献者生成哈希只是完整性观测。

## 可复用执行组件

`factorlab.backtest.corporate_actions.cash_dividend_ledger` 消费任意策略执行器的每日收盘持仓、原价 mark 和净交易现金流，以及显式完整事件覆盖。调用方负责提供真实执行结果和经过独立验证的覆盖；此低层账本本身不能批准金融消费。

登记日收盘确定权益；除息日形成应收，支付日将应收转现金，卖出后权益仍保留、除息日买入不获前日权益。账本输出税前 cash/equity 与单独税后现金。未知税率时 net 保持 None/UNKNOWN，不能把税前值当税后；显式税率只是合成假设，尚未实现按投资者/持有期追缴的税务子账。送转、配股、拆股等沿用统一事件合同表达，但当前执行器明确拒绝。必须使用 RAW mark，传入 ADJUSTED 会拒绝。合同不替代实际成交现金流的审核。

人工算例：100股×100元，初始10000元全额买入，登记日100股；每股分红2元，除息后原价98元。支付前资产=9800市值+200应收，支付后=9800市值+200现金，总额始终10000。若显式假设10%扣税，支付税后现金180；未知税务则 net 未知。不能再叠加复权价格计算的200元收益。

## 隔离双均线验证与边界

在 PR10 冻结提交之上的独立集成工作树应用本分支，运行 `tools/ci/csmar_signal_check.py --candidate <new generation> --output <new diagnostic directory>`。它验证候选文件哈希、PR10 输入 Schema、MA5/MA20 人工公式、未来扰动不改历史信号和下一观测开盘日期；不调用收益执行器。

A：选定真实输入读取/转换。B：选定观测范围信号研究。C：正式历史收益。三者独立；A/B PASS 不能产生 C PASS。完整时点、复权锚点/算法、事件覆盖、身份、日历、税务与现有正式/执行资格仍必须各自验证。真实收益保留 BLOCKED / NOT_RUN。

完整原始数据、私有 Parquet、原始行、绝对路径清单和完整执行日志保留本地；公开 PR 只放实现、合成测试、说明及非敏感聚合状态。旧 Golden、冻结控制和历史失败不得刷新。
