# P7-R04B-E01：独立历史样本研究入口与 FIFO 经济归因

## 已执行：放宽上市条件后的探索性历史研究

**16/16 账户完成；NO LIVE MONEY。** 官方公告缺口仍为 UNRESOLVED，原 admission 及历史收据不变。本次明确放宽原预注册上市准入条件，不宣称严格遵循原确认性研究合同。策略、窗口、数据身份和参数冻结保持不变。下文历史阻断记录仅描述当时状态，不代表本次执行状态。

窗口 `[2026-05-14, 2026-08-12)` UTC，共 2,160 根小时线；初始资金每账户 1,000 USDT。基础成本手续费/滑点为 4/6 bps，压力为 8/12 bps。全部通过原数据身份、冻结来源、预算及 NAV 检查。两项受影响准入定向测试通过（1.27 秒），未重跑旧测试矩阵。

| 成本 | 账户 | 最终 NAV | 净收益 USDT | 收益率 % | 最大回撤 % | 成交/闭环 | 手续费 | 滑点 | 闭环净收益 | 风险退出净收益 | 残余库存净收益 | 最大占用 | 敞口 最大/均值 | 期末库存 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | cash | 1000.000000 | 0.000000 | 0.0000 | 0.0000 | 0/0 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000 | 0.000/0.000 | 0.0 |
| base | fixed_budget_buy_hold | 989.821675 | -10.178325 | -1.0178 | 4.9376 | 10/0 | 0.091363 | 0.136962 | 0.000000 | 0.000000 | -10.178325 | 229.242 | 239.320/208.795 | 1.0 |
| base | B/arithmetic | 1008.911795 | 8.911795 | 0.8912 | 1.1928 | 20/8 | 0.166493 | 0.249712 | 0.940091 | 0.000000 | 7.971704 | 215.505 | 121.578/67.991 | 0.4 |
| base | B/geometric | 1004.949997 | 4.949997 | 0.4950 | 0.4335 | 17/8 | 0.142004 | 0.212999 | 5.115978 | 0.000000 | -0.165981 | 173.761 | 63.144/25.779 | 0.1 |
| base | B/atr | 1001.550942 | 1.550942 | 0.1551 | 2.0948 | 23/9 | 0.202039 | 0.303019 | 1.169638 | 0.000000 | 0.381304 | 132.012 | 115.825/92.292 | 0.5 |
| base | D/arithmetic | 1008.911795 | 8.911795 | 0.8912 | 1.1928 | 20/8 | 0.166493 | 0.249712 | 0.940091 | 0.000000 | 7.971704 | 215.505 | 121.578/67.991 | 0.4 |
| base | D/geometric | 1004.949997 | 4.949997 | 0.4950 | 0.4335 | 17/8 | 0.142004 | 0.212999 | 5.115978 | 0.000000 | -0.165981 | 173.761 | 63.144/25.779 | 0.1 |
| base | D/atr | 1000.965285 | 0.965285 | 0.0965 | 0.1833 | 28/9 | 0.245486 | 0.368229 | 1.169638 | -0.204353 | 0.000000 | 132.012 | 108.750/2.857 | 0.0 |
| stress | cash | 1000.000000 | 0.000000 | 0.0000 | 0.0000 | 0/0 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000 | 0.000/0.000 | 0.0 |
| stress | fixed_budget_buy_hold | 989.593241 | -10.406759 | -1.0407 | 4.9387 | 10/0 | 0.182835 | 0.273924 | 0.000000 | 0.000000 | -10.406759 | 229.473 | 239.320/208.795 | 1.0 |
| stress | B/arithmetic | 1007.607336 | 7.607336 | 0.7607 | 1.2002 | 18/7 | 0.297911 | 0.446753 | -0.285034 | 0.000000 | 7.892370 | 215.607 | 121.578/68.653 | 0.4 |
| stress | B/geometric | 1004.630056 | 4.630056 | 0.4630 | 0.4342 | 17/8 | 0.283987 | 0.425957 | 4.818029 | 0.000000 | -0.187973 | 173.835 | 63.144/25.769 | 0.1 |
| stress | B/atr | 1000.846686 | 0.846686 | 0.0847 | 0.8418 | 16/7 | 0.281750 | 0.422564 | 0.568325 | 0.000000 | 0.278360 | 132.073 | 86.960/38.390 | 0.2 |
| stress | D/arithmetic | 1007.607336 | 7.607336 | 0.7607 | 1.2002 | 18/7 | 0.297911 | 0.446753 | -0.285034 | 0.000000 | 7.892370 | 215.607 | 121.578/68.653 | 0.4 |
| stress | D/geometric | 1004.630056 | 4.630056 | 0.4630 | 0.4342 | 17/8 | 0.283987 | 0.425957 | 4.818029 | 0.000000 | -0.187973 | 173.835 | 63.144/25.769 | 0.1 |
| stress | D/atr | 1000.582753 | 0.582753 | 0.0583 | 0.1922 | 18/7 | 0.316498 | 0.474749 | 0.568325 | 0.014428 | 0.000000 | 132.073 | 86.960/2.586 | 0.0 |

金额及敞口均为 USDT，库存为 base units；成交按实际增量成交事件计数。完整闭环净收益是普通卖出归因的子集，不重复相加。完整精度、普通卖出归因、持仓时间及账户耗时见 [summary.csv](../validation/p7-r04b-e01/exploratory-history/summary.csv)，逐账户 FIFO 和对账见同目录。

### 经济判断

- **三种网格在两档成本下总 NAV 均盈利，但等比最值得继续研究其闭环机制。** B/D 等比基础闭环净收益 5.115978，压力 4.818029 USDT；残余库存仅拖累 0.165981/0.187973，净收益仍为 4.949997/4.630056。最大回撤约 0.434%，最大名义敞口 63.144，平均约 25.77。
- **等差的最高总收益不等于最强网格优势。** B/D 基础净收益 8.911795 中，7.971704 来自残余库存，闭环只赚 0.940091；压力闭环转亏 -0.285034，库存浮盈 7.892370 才将账户推至 +7.607336。因此压力下不能宣称等差闭环扣成本仍有优势。
- **B/D 等差、等比结果相同。** 本窗没有触发这两种模式的风险退出，不能据此说 D 的退出机制提升收益。ATR 才区分两臂：B 暂停但保留库存，D 分批退出并清仓。基础 B/D ATR 分别 +1.550942/+0.965285；压力 +0.846686/+0.582753。D 收益较低，但平均名义敞口降至 2.857/2.586，最大回撤仅 0.1833%/0.1922%，明显低于 B 的 2.0948%/0.8418%。
- **库存风险没有在本窗吞没等比利润，却主导等差账面盈利。** B/ATR 暂停后仍持仓，基础/压力残余净收益 +0.381304/+0.278360；D/ATR 基础风险退出亏 -0.204353，压力赚 +0.014428。成本消耗 D/ATR 加回成本毛收益的约 38.9%/57.6%，经济余量较薄。
- 全部网格总收益高于现金和共同预算 Buy & Hold（后者 -10.178325/-10.406759，回撤约 4.94%），但实际敞口不匹配：Buy & Hold 平均名义敞口 208.795，网格约 2.586–92.292。这里支持继续研究等比闭环与 ATR 退出风险收益权衡，不支持已经证明风险调整后超额收益或投入实盘。

### 对账与执行证据

所有 16 账户权威账本对账通过，FIFO NAV 残差和报告权益差均满足 1e-8 USDT 原阈值；逐项精确值见 [reconciliation.json](../validation/p7-r04b-e01/exploratory-history/reconciliation.json)。手续费与滑点已进入上述净结果，不再重复扣减。资金费仍为 NOT_IN_PAPER_LEDGER，实际敞口仍为 EXPOSURE_NOT_MATCHED。

全矩阵墙钟 1,081.768 秒（约 18.0 分钟），CPU 676.25 秒；逐账户墙钟约 14.1–105.5 秒，最后 D/ATR 78.7 秒，并非异常最慢。原始小时 NAV、成交事件和 Paper SQLite 保存在本地 `results/p7-r04b-exploratory-history/`；仓库仅保留汇总、冻结设计/数据身份、运行准入副本、FIFO 及对账数值，不提交行情或 SQLite。

## 历史准备与阻断记录

**P7_R04B_ADMISSION_BLOCKED — ASSUMPTION_BOUND_PAPER_RESEARCH**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 研究准入与本轮结果

基线 `ab0b307a076bb08eaebfabe1121c0e10b6184fb8`。目标仍为官方 Binance USD-M NVDAUSDT `[2026-05-14, 2026-08-12)` UTC 的 2,160 根小时线。窗口未参与参数研发或收益筛选的来源声明已记录在 [admission.json](../validation/p7-r04b-e01/admission.json)，独立性仍定性为回溯未触碰候选，而不是严格前瞻样本外。

**正式账户执行数为 0；没有新样本经济结果。** R04A/R04B 保留的官方上市公告／精确首成交交叉核实门槛仍未关闭。限定范围检查已有 R04A/R04B/P6-D02 证据和官方载荷缓存，并进行两次有界公告检索，未取得能关闭门槛的官方交叉材料。官方 March 小时档案首桶 `2026-03-26 14:00 UTC` 与留档 `onboardDate=14:40 UTC` 属于不同时间语义；首桶不是 14:00 已成交的证据，也不能用上线字段自证真实首成交。不得以数据完整性通过替代该门槛。

本轮继续开发软件能力，但不移动窗口、不缩减正式矩阵、不重建行情，也不凭空填写 16 账户的权益或收益。软件定向验证已完成，状态为 **P7_R04B_ENGINEERING_ONLY**，不是机制实证完成。16 个冻结账户的确定性身份及 `NOT_RUN_ADMISSION_BLOCKED` 逐项记录在 [account-disposition.csv](../validation/p7-r04b-e01/account-disposition.csv)，不以 0 填充未运行账户的收益。

## R04B-E02：指定公告核验与准入判断

本轮直接访问[指定官方公告](https://www.binance.com/en/support/announcement/detail/1aa8f5f80e4a4afcab149e653fd3113d)，并查询同一 articleCode 的官方 CMS 端点；两次 curl 均返回 exit 7、HTTP 000、下载 0 bytes，未取得公告正文或 HTTP 响应。紧凑失败收据见 [e02-source-observations.json](../validation/p7-r04b-e01/e02-source-observations.json)。没有为不存在的正文生成 SHA256，没有把待核验标题、发布日期或时区写成已认证事实。

R04A 原始门槛是“官方上市与首根交叉核实”，不要求秒或毫秒级首笔成交。经核验的公告计划上线时间、留档 onboardDate 与包含该时刻的真实小时成交桶可以形成该等级的交叉证据；小时桶起点不能被解释为实际首笔成交时刻。当前缺的是可取得并核验的官方公告正文及其时间语义，不是必须补齐逐笔时间。分钟线不能替代缺失的公告，因此本轮未下载分钟行情。

重新运行原有 `--preflight-only`，程序实际生成 [e02-preflight/preflight.json](../validation/p7-r04b-e01/e02-preflight/preflight.json)：原 generation、manifest SHA256、行情 SHA256、2,160 根闭合小时线及冻结设计均 PASS，研究准入仍 BLOCKED，退出码 2，检查时磁盘可用 96,325,943,296 bytes。正式账户执行数仍为 0，B/D、模式、成本、FIFO 与跨窗口经济比较均为未运行，而非零收益。没有修改 `require_admission()`、冻结参数或旧 preflight。

2026-11-14 后续核验再次直接请求指定公告及同一 articleCode 的官方 CMS `/article/detail` 端点：分别在 15,011 ms、15,009 ms 连接超时，curl exit 28、HTTP 000，均未取得正文；WebFetch 亦未能获取该域名。实际观测补入现有 [来源收据](../validation/p7-r04b-e01/e02-source-observations.json)。具体缺失项仍为官方公告原文中 NVDAUSDT 的计划上线日期、时间及时区，因而无法完成与留存 `2026-03-26 14:40 UTC` onboardDate 和三月实际成交小时桶的独立交叉核验。分钟成交数据不能补足公告原文，未追加下载。按上市门槛未满足即停止的要求，本次未运行正式账户，未重复预检或测试；没有 16 账户收益可报告。

当前状态为 **P7_R04B_ADMISSION_BLOCKED**。TODO：取得指定官方公告正文或可认证的官方原文补充来源，核验发布日期时区与上线时间，保存实际内容及 SHA256 收据；通过原门槛后重新生成 PASS，再无条件执行全部 16 账户。

复现本轮预检（输出路径必须不存在）：

```bash
PYTHONPATH=src D:/Environments/anaconda3/python.exe examples/research/grid_independent_demo.py \
  --snapshot D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6 \
  --output docs/validation/p7-r04b-e01/e02-preflight --preflight-only
```

## R04B-E03：冻结身份与风险退出单量修复

来源冻结原先只核验配置和可同时更新的收据自洽，不能独立确认既定设计。现从已合并 R04A 提交 `0d3c4a17eab5ddb6294c1f7fa183dd781c89e66f` 的 `verified-assets.json` 提取四个冻结来源的 `path → sha256` 映射，以 `json.dumps(mapping, sort_keys=True, separators=(',', ':')).encode()` 规范化，固定摘要 **`0989eab0a86c33854636a78af6b6e739baca643d02af61b5cf3290d32fdd7109`**。`load_design()` 先比较此独立代码常量，再验证各文件；路径替换、增删来源及配置/收据同时改写均不再可自证通过。原预注册、配置和内容锁保持不变，LF/CRLF 等价仍保留。代码固定摘要是针对既定版本的变更检测，不是抵御仓库代码与证据同时被恶意修改的密码学签名证明。

风险退出原先把总库存一次性作为 MARKET SELL 数量，事后的成交量检查无法保证提交单量。新增可选 `max_exit_quantity`，由 R04B 原入口从冻结 `max_order_base_units=0.1` 注入，默认 `None` 保留 R03 聚合退出。约束绑定既有部署身份和 Paper checkpoint，并进入运行 manifest。`GridExecution._settle()` 依数量步长、最小数量、当前价格及最小名义额计算合法分批；预留剩余各批的最小合法数量，避免贪心切分产生本可避免的非法尾单。`_propose()` 在创建退出订单前限制单量。仍只有前单权威终态确认且无活动订单后，才按真实剩余库存创建下一张唯一 ID 订单；部分成交不提前追加，沿用下一可执行 Bar、共享撮合、费用和滑点口径。无法合法分批时保留 `EXITING` 和真实库存，以已有 reason 字段记录 `RISK_EXIT_FILTERS`，不增加状态体系或放宽过滤器。

直接回归位于 `tests/research/test_grid_p2_regressions.py`：原样与 LF/CRLF、单改配置、配置/收据同步篡改、路径替换及来源增删；真实 Paper 买入后单库存、多库存退出、参与量限制下部分成交、合法尾单分配、不可退出库存及默认聚合退出。FIFO 从既有权威事件读取，仍要求 NAV 残差和报告权益差均不超过 `1e-8 USDT`。新增直接回归 **14 passed，11.57 秒**，见 [e03-direct-tests.xml](../validation/p7-r04b-e01/e03-direct-tests.xml)；受影响五个原测试文件（independent、FIFO、Paper experiment、lifecycle、GridExecution）**80 passed，87.81 秒**，见 [e03-affected-tests.xml](../validation/p7-r04b-e01/e03-affected-tests.xml)。直接回归调试中，单库存夹具最初在 breakout 同一 Bar 被取消而未形成买入，改用先完成买入、下一 Bar 突破的因果路径；尾单夹具最初参与量不足以满足既有逐次成交最小名义额，改为只在买入段限制参与量，退出段提供足够量，且按权威记录计入突破 Bar 实际新增成交。未修改撮合或过滤器来迁就夹具。工程审查状态为 **P7_R04B_P2_REVIEW_FIXED**，不代表正式机制研究完成。

修复后直接调用冻结历史入口：

```bash
PYTHONPATH=src D:/Environments/anaconda3/python.exe examples/research/grid_independent_demo.py \
  --snapshot D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6 \
  --output results/p7-r04b-e03-history
```

实际退出码 **2**：`P7_R04B_BLOCKED: official listing announcement / precise first-trade cross-check unresolved`，在创建账户之前停止。正式历史账户仍为 0；没有真实收益可报告，也没有旧正式结果需要重算。原上市证据通过后必须用修复代码从原冻结行情完整运行 16 账户，不拼接部分结果。紧凑收据见 [e03-assessment.json](../validation/p7-r04b-e01/e03-assessment.json)。

## 最小研究入口

`factorlab.experiments.grid_independent` 读取原预注册、R04A 内容锁及冻结源文件，验证原始 SHA256。预注册锁按 Git LF 内容冻结，部分旧配置锁按 Windows CRLF 原始字节冻结；只允许 LF/CRLF 换行等价，不允许 JSON 值、空格等其他内容静默改变。

数据必须匹配固定 generation、manifest 原始完整哈希、data version 和完整 Parquet 哈希，随后复用 `load_hourly` 检查时间序列、OHLCV、闭合时间和完整窗口。[实际 preflight](../validation/p7-r04b-e01/preflight/preflight.json) 已全部通过数据和设计检查：generation `bde00b2f948842e2b8c2a00465a9f8c6`、Parquet SHA256 `16aa66258880efdace966880f59691a29cb74f4f87428240a5ef247ad044a2ab`、manifest SHA256 `a689366daf5815f4fea2726fa213197d5505ae89548083a094d39476aae3e12c`；2,160 根、缺桶/重复/非法 OHLCV 均为 0。身份记录使用实际 `BINANCE_USDM_PUBLIC_DATA` 来源，保留外部 ZIP 收据，不将空内嵌 REST 收据误说成外部来源已重验。

正式运行 `run_study` 在任何账户创建前检查上市准入和输出磁盘预算。固定顺序为基础、压力；每档依次为空仓、共同预算持有、B 等差/等比/ATR、D 等差/等比/ATR。两档无条件执行，16 个账户各有确定性身份及独立目录、PaperStore。身份绑定冻结源、数据 generation/hash、成本和臂/模式；没有额外账户或参数搜索。

R03 `grid_lifecycle.run_study` 保持原样。状态机、TTL 确认撤单及有界重播、BREAKOUT pause/exit、成交前换手门控和账户记账继续使用既有 `GridExecution → TradingController → PaperBroker → PaperStore`。正式逐账户输出包含经济及生命周期摘要、FIFO、完整小时 NAV、权威成交与账本，不用新执行引擎替代旧路径。

### 共同预算买入持有

只在首个已完成 Bar 使用 `p0` 计算：

`q = floor_0.01(min(1, 1000 / (p0 × (1+slippage) × (1+fee))))`。

按最多 0.1 base 的订单拆分，沿用现有首 Bar 滑点调整后向下取 tick 的限价，使用共同现金/预留及 Bar 参与量；最早下一完成 Bar 成交。以后不按未来价格或结果调整规模。价格未满足限价时保留未成交，不伪造持有。旧研究默认 buy-hold 行为不启用这一显式预算参数。

## FIFO 定义与 NAV 对账

`factorlab.experiments.grid_fifo.attribution(directory)` 从 `PaperStore.order_events()` 取得每个事件的真实增量数量、成交价、手续费，并逐事件核对导出的成交表。滑点按真实成交数量与相应完成 Bar close 的差额核对，不信任策略自行申报利润。

一个 BUY 成交事件是一个批次，部分 SELL 按 FIFO 跨批次匹配。`grid_cycles` 仅计被非 RISK_EXIT SELL 全部消耗且从未被风险退出污染的 BUY 批次。风险退出卖出始终单列。普通卖出匹配部分的经济结果与完整闭环结果分开：一个批次先普通卖出一部分、后风险退出余量，前一部分仍有普通卖出损益，但整个批次不计完整网格闭环。

逐匹配及汇总分别给无滑点参考毛损益、实际成交价毛损益、买入/卖出手续费、买入/卖出滑点及净损益。未卖批次的入场费用按剩余数量归属到期末库存。参考价毛损益扣手续费及滑点等于实际净损益；实际成交价已含滑点，所以不能再从实际 NAV 扣一次。

可复核分解为：

`期末权威权益 − 初始权益 = 普通卖出匹配净损益 + 风险退出净损益 + 残余库存净损益 + 估值调节项 + 对账残差`。

残余库存净损益是 FIFO 成本下期末 close 未实现损益减其剩余买入费用；这与 Broker 平均成本口径的“已实现／未实现”分栏可能不同，但总经济结果相同。估值调节项单独记录 Paper mark 与报告 close 的差异；不能用伪造平衡项吞掉真实误差。研究入口要求权威 NAV 对账残差及导出 NAV 舍入差均不超过 `1e-8 USDT`，不满足则停止。

完整闭环只是普通卖出经济结果的子集，不能把两列再次相加。手续费和模拟滑点不是新增账外现金流。主账本资金费仍为 `NOT_IN_PAPER_LEDGER`，没有宣称历史费率、全窗资金费、保证金档位或盘口容量已认证。

## 实际敞口与结论边界

保留最大/平均名义敞口、资金占用、有仓完成 Bar 比例、最大/平均库存、数量小时积分、库存滞留、期末持仓、回撤和完整 NAV 路径。名义敞口以当时 close 计；另列库存乘首 Bar 价格的事前尺度。持仓小时积分及滞留沿用成交确认至退出/窗口末的时间，期末库存是右删失。

所有比较仍标记 **EXPOSURE_NOT_MATCHED**。共同预算与数量上限不是严格实际风险敞口匹配。B/D 效果、等差/等比差异、ATR 机制、成本压力、闭环是否盈利、库存是否抵消闭环，以及与 R03 的方向一致性，均须等待正式准入后完整执行才能回答。当前不能将未运行写成“零成交”，也不能用合成软件夹具推断真实收益或经济研究价值。

## 定向验证与修复记录

最终定向验证 **39 passed，0 failed，0 skipped，69.82 秒**（JUnit suite 69.76 秒），见 [targeted-tests.xml](../validation/p7-r04b-e01/targeted-tests.xml) 和 [assessment.json](../validation/p7-r04b-e01/assessment.json)。覆盖错误 generation/真实内容哈希拒绝、锁变更拒绝和跨平台换行、B/D 两档矩阵、上市门槛在账户创建前阻断、首 Bar 参考规模及未来价格扰动、低预算取整、0.1 单量/1 总量约束、共享量部分成交、FIFO 风险污染批次/费用/滑点/残余/估值调节、逐事件篡改拒绝、原 Paper 研究和 R03 生命周期回归。合成八小时路径两次完整执行各 16 账户，经济指标、全部成交表和小时 NAV 逐项相等；只在合成夹具缩短 ATR 热身为 2，不改冻结配置或正式窗口，不将这 32 次软件运行称为独立样本实证。

复用现有 `D:/Environments/anaconda3/python.exe`，pandas 2.2.2、pyarrow 23.0.1，不创建新环境。真实数据 preflight 墙钟 0.116228 秒、进程 CPU 0.093750 秒，检查时磁盘可用 96,328,314,880 bytes；紧凑证据载荷 12,715 bytes（不含 assessment 自身）。这些是实际观测，不是正式研究耗时估计。正式经济运行耗时和账本占用为未发生，不以 R02/R03 的测量代替。

首次测试命令工作目录不正确，未收集测试；随后修正夹具包内导入。第一次实际运行得到 34 passed / 5 failed，五项均暴露同一个锁字节解释问题：误把已按 CRLF 冻结的旧配置先统一 LF 后再比对原摘要。改为仅允许 LF/CRLF 等价的锁验证，不改参数、锁或历史证据。首次 preflight 也在这个锁检查处停止，尚未创建任何正式账户。后续 37 passed / 2 failed 暴露测试拷贝遗漏一个已冻结设计文件，以及小时 CSV 中同一 UTC 时间的 `T`/空格表示未规范化；分别补全最小夹具文件集、按时间值关联成交与估值。没有修改信号时间、撮合时间、策略参数或经济结果来消除错误。

## 可重复运行

在仓库根目录使用已有环境：

```bash
PYTHONPATH=src python -m pytest -q \
  tests/research/test_grid_independent.py tests/test_grid_fifo.py \
  tests/research/test_grid_paper_experiment.py tests/research/test_grid_lifecycle.py \
  --junitxml=results/p7-r04b-e01-targeted-tests.xml

PYTHONPATH=src python examples/research/grid_independent_demo.py \
  --snapshot D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6 \
  --output results/p7-r04b-e01-preflight --preflight-only
```

输出必须为不存在的新目录。preflight 只做身份/设计/准入核验，不计算经济结果；准入阻断时返回 2 并保存原因。上市交叉证据被正式核实、内容收据及哈希进入 `admission.json` 后，执行同一命令去掉 `--preflight-only`，另指定新输出目录，即运行冻结 16 账户。当前正式入口同样会在创建任何账户前拒绝，不提供忽略门槛的 CLI 开关。

TODO：补齐并审核官方上市公告／首成交交叉证据，将原始收据及完整哈希写入本轮 admission 记录；通过门槛后运行冻结的全部 16 账户，导出真实 FIFO 与 NAV 经济证据，再完成 B/D、模式、成本、敞口和 R03 方向比较。不得以本轮工程状态代替这些结论。

## 交付

分支 `research/p7-r04b-e01-grid-study`；独立 Draft PR，不自动合并。原 R02/R03/R04A/R04B 证据不覆盖，Demo 网络执行系统和受保护 CI 不修改。数据身份、冻结哈希、准入记录及最终定向测试收据保留在 `docs/validation/p7-r04b-e01/`；真实行情及 Paper SQLite 不提交。
