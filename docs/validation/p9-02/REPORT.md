# P9-02：Sidecar接入与样本外执行恢复

2026-11-11 · **P9_02_BLOCKED**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 为什么没有进入完整三折研究

两个已知缺口的接入实现已经完成：002408.SZ 的四个停牌会话可以通过现有 Sidecar 核准旧 mark；000540.SZ 在2023-05-08的定向 `is_st` 事实可以安全消费，真实历史最小复现中的卖单被既有跌停规则拒绝。但 Stage A 随即暴露了第三个估值缺口：**000540.SZ 在2023-05-04有正持仓、raw_close=null，且现有 Sidecar 没有该证券日的估值候选。**

这是原 LightGBM 目标路径实际会遇到的日期：2023-04-25信号要求10%仓位，下一会话4月26日买入；下一固定调仓信号为5月5日，在5月8日尝试清仓。因此不能通过换买入日、提前退出或删除该股票避开5月4日。发行人公告提到5月4日停牌，并不自动给现有 Sidecar 增加估值权限、来源观察身份或政策范围。

研究严格停在 Stage A。**四模型、两个股票参考和现金参考的三折资金账户全部 NOT_RUN；没有完整折的净收益、年化收益、波动率、Sharpe、最大回撤或Calmar。** [账户状态](account-status.csv)逐一列出21个方案/折次组合。空的[绩效表](fold-metrics.csv)保留列头，没有把未运行账户或未知收益写成0。Stage A是隔离的执行诊断，不是完整模型组合，也不参与模型收益比较。

## 冻结合同与复用边界

启动时最新 `origin/main` 为 `937ed07`。PR #83、#84分别于2026-11-10 17:35:36 UTC、18:07:27 UTC合并，合并提交为 `a16bfa35143f43be794bba51273b35cf37cb7100`、`6c953aae0c9c81301c2a7f7e9254a1ee8a64fd8c`。

[独立配置](../../../configs/research/p9-02/recovery.json)引用原P9-01合同，只新增具名估值政策、状态消费语义及cutoff。仍是B FactorScore、B Ridge、B LightGBM、B XGBoost；20日动量、固定哈希排序等权及零利息现金参考；三个Walk-Forward Test折；每折独立100万元；Top10、每只10%；每5个来源交易会话调仓；收盘决策、下一会话开盘执行；long-only、T+1、整手、共享现金、停牌、ST及保守涨跌停规则；原始价格净交易成本收益。

费用仍为固定佣金3bps、最低5元、滑点5bps、卖出印花税10bps、转让费0。没有修改费用引擎，也没有采用历史费率切换。执行器、session、TopK、模型工厂及交易账本均未修改或重建。原P8/P9冻结报告及P9-D02、P9-C01任务文件保持原样。

实现复用 `run_oos_execution.execute()`，仅新增可选估值资格回调；共享 `audit_ledger()` 接收独立核准估值表，默认仍要求真实观察收盘。估值表不回写执行行情。完整折只有在资格验证、逐日现金/股数/权益/费用核验通过后才进入既有 `ResearchEvaluation` 与 `ExperimentTracker`。本次门槛没有通过，不对未知估值的窗口生成ResearchEvaluation结果。

## 保存模型和预测的身份核对

本地原P9-01的12份拟合状态及候选预测全部存在。没有加载模型重新拟合，也没有重新生成预测。已对原输入manifest、来源分片身份、保存模型SHA256、逐模型/折训练选择摘要、拟合源摘要、Fold候选键及预测选择身份核对。

- 原P8-02预测SHA256：`19b0fab33b8cb247ba7b34bd0721c8119757464f16822ac95f111bae16f5243c`。
- 复用P9-01候选预测SHA256：`c8f18f07fde5521c340b9ad3c4364469a855481fc5f0c4b12d93a6ab20e6ad3f`。
- 不依赖未来标签完整性的候选特征摘要：`b3e0ff2abb472d67fafd1bcf0d30593e9260d8f43095dfa60d9ae9e9789a5764`，94,300证券日。
- 各模型三折候选预测分别20,347、15,326、13,355行；每模型49,028行，共196,112行。

用现有 `signal_candidates()` 从冻结来源重新计算候选特征，核对摘要，不使用未来标签筛选目标。原共同Test预测在11个模型/折组合逐值相同；FactorScore fold 2只有1个值存在 **2.7755575615628914e-17** 的浮点差异，沿用原重建比较的 `rtol=1e-10, atol=1e-12` 通过。这里比较的是已保存候选预测与原P8-02预测，不是原报告所述runner重建输出与原预测的比较，不将本次核对声称为全部逐位相等。逐折摘要与模型状态SHA256见 [inference-verification.json](inference-verification.json)。候选预测字节保持原样。

## Stage A：002408四个停牌会话

隔离最小复现使用4月24日信号、4月25日开盘买入；随后提交零目标，让现有执行器在4月26日遇到停牌拒单，并继续持有至5月5日。它用于验证估值、拒单及复牌行为，不替代冻结的完整组合目标日程。

`MarketEvidence.carry()`逐一核准2023-04-26、04-27、04-28、05-04，要求证券日匹配、原始收盘缺失、供应商停牌、正式停牌证据、前序真实观察及正既有持仓。执行器自然沿用的7.12与核准价格完全一致，持仓数量在carry期间不变，四会话没有成交。5月5日恢复实际观察收盘6.76。

政策为 **LAST_OBSERVED_RAW_CLOSE_CARRY_ON_CONFIRMED_SUSPENSION**，来源日期2023-04-25，资格为 **MODEL_VALUATION_ASSUMPTION**。原始raw_close在四会话仍为null；7.12没有进入raw_open、成交价或可交易许可。缺少Sidecar、来源身份漂移或mark不一致时阻断。

[估值资格](stage-a-valuation-assumptions.csv)保留date、symbol、shares、observed_raw_close、valuation_price、valuation_source_date、valuation_policy、valuation_status、evidence_identity、affected_portfolio_weight和valuation_assumption_used。隔离诊断持有14,000股，核准账面值99,680元；不是原FactorScore/Ridge组合持股数量。

这条隔离诊断的现金、累计成交股数、费用组成、每日权益守恒及核准持仓估值全部通过。所有使用carry的完整绩效出口都会标记 **CONDITIONAL_ON_CONFIRMED_SUSPENSION_CARRY**；本次没有完整绩效。7.12是条件化账面估值，不是四个会话的真实收盘价认证。

## Stage A：000540定向ST与保守卖单拒绝

只对 `000540.SZ / 2023-05-08` 调用 `fact(... purpose='SIGNAL_STATE', cutoff='2023-05-05T15:00:00+08:00')`。Sidecar验证事前发行人风险警示公告时间早于cutoff后，将核准 `is_st=true` 放入独立执行输入副本。原 `st_status=2`、`suspended=0`、`is_risk_warning=1`均核对并保留，没有全市场枚举2映射。`trading_observed`及`execution_price_available`事实没有转化为成交许可。

BigQuant `suspended=0`仍只表示 **RETROSPECTIVE_VENDOR_DAILY_STATE_NOT_PREOPEN_KNOWLEDGE_OR_FILL_PERMISSION**。它是回顾性日线研究输入候选，不宣称盘前可得、全天无中断或特定订单可成交；UNKNOWN不自动变成正常状态。

隔离复现沿用原LightGBM对本证券的4月25日买入信号与5月5日退出信号。5月8日前收0.63、开盘0.60、*ST 5%及0.01刻度下，原 `PriceLimitSchedule` 拒绝138,800股卖单，原因 `sell_price_limit`。当天没有成交、没有新增费用，现金在5月5日至5月8日保持 **899,984.0512元**，持仓继续存在。唯一买入的费用79.9488元，分别佣金29.9808、滑点49.968、印花税0。逐笔费用已通过原固定 `FeeSchedule.calculate()`重新核对。

[拒单现金摘要](stage-a-rejection.json)只提供必要派生收据；完整交易/持仓账本留在私有输出。5月9日虽有日线观察，既有信号日程没有新目标，拒单不导致逐日自行重试。合成定向测试另外验证：失败退出持仓持续保留，只有后续预先提交的新目标才触发下一次卖出尝试，不因随后一天可交易便修改日程。

### 新的估值阻塞

同一真实最小路径在 **2023-05-04** 持有138,800股000540，原raw_close缺失。执行器沿用4月28日旧mark **0.66**，但这只是未核准的引擎诊断值。Sidecar的carry仅允许002408四个指定证券日，对000540明确拒绝：

```text
UNQUALIFIED_HELD_CLOSE: 2023-05-04/000540.SZ:
MARKET DATA QUALIFICATION REQUIRED: candidate outside exact security/date/field scope
```

因此这个账户没有完整估值核验通过的状态，不能把引擎诊断净值、累计收益或0.66当作有效研究绩效。5月8日的拒单、现金和费用收据可以单独验证，不会追认5月4日的未知估值。[缺口清单](valuation-gaps.csv)保留具体日期、证券与空的原始收盘字段；没有为新缺口填充价格或扩展Sidecar。

**TODO：为000540.SZ / 2023-05-04独立取得和绑定停牌期间估值所需的最后真实观察、来源身份、停牌证据及精确候选范围；获得核准证据前，不进入Stage B/C。** 仅引用风险公告中的停牌安排不足以调用当前Sidecar的002408估值权限。

## 有界运行、验证与复现

实际入口共调用5次：前两次分别止于严格浮点逐值比较及未装载5月价格证据分片；第三次因最小复现错误地选择5月5日新买入，触发未授权扩展的该日ST状态阻断；第四次改为原4月25日信号后发现5月4日新估值缺口；第五次以同一原路径固化阻塞、拒单及派生证据。前四次失败输出保留在本地。没有Stage B/C账户执行、重新训练、预测写回、下载行情或参数搜索。

定向测试覆盖carry资格及完整账本、复牌恢复、无证据和错误mark阻断、定向ST cutoff与原码保留、跌停卖单拒绝/现金不变/持仓保留、不自动重试、新证券停牌缺价继续阻断、固定费率重新核对，以及Stage A失败禁止任何完整研究账户执行。既有P9市场证据、P9执行及共享组合合同一并纳入，最终 **80 passed / 3.58秒**，未运行全仓库测试矩阵。

公开结果由只读发布器生成；原数据、模型、预测和完整交易/持仓账本保留本地。代码身份、私有产物相对名称与SHA256见 [result.json](result.json)、[code-identity.json](code-identity.json)，公开派生文件身份见 [publication.json](publication.json)。

```sh
python -I -B -X utf8 -c "import sys; sys.path.insert(0,'src'); import pytest; sys.exit(pytest.main(['-q','-p','no:cacheprovider','tests/research/test_p9_02_recovery.py','tests/research/test_p9_market_evidence.py','tests/research/test_oos_execution.py','tests/research/test_shared_portfolio.py']))"

# 所有输入均指向既有私有资产；输出目录必须不存在。
python -I -B -X utf8 tools/research/run_oos_recovery.py \
  --study '<EXISTING_P9_01_PRIVATE_OUTPUT>' \
  --original '<EXISTING_P8_02_PRIVATE_OUTPUT>' \
  --evidence-root '<EXISTING_P9_D01_PUBLIC_SOURCE_DIRECTORY>' \
  --bigquant-root '<EXISTING_BIGQUANT_ARCHIVE_DIRECTORY>' \
  --membership-zip '<EXISTING_JOINQUANT_MEMBERSHIP_ZIP>' \
  --output '<NEW_PRIVATE_P9_02_DIRECTORY>'

# 只读取已保存证据，不训练、不执行账户；输出目录必须不存在。
python -I -B -X utf8 tools/research/publish_oos_recovery.py \
  --source '<EXISTING_P9_02_PRIVATE_OUTPUT>' \
  --output '<NEW_PUBLIC_DERIVED_DIRECTORY>'
```

原价口径不包含完整公司行为账务，仍为 **TOTAL_RETURN_UNAVAILABLE / null**。本轮交付的是有证据门槛的接入及可定位的研究阻断，不授予实盘许可，也不把条件化carry解释为独立市场价格认证。
