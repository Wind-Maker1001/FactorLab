# P7-R04B-E01：独立历史样本研究入口与 FIFO 经济归因

**P7_R04B_ENGINEERING_ONLY — ASSUMPTION_BOUND_PAPER_RESEARCH**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 研究准入与本轮结果

基线 `ab0b307a076bb08eaebfabe1121c0e10b6184fb8`。目标仍为官方 Binance USD-M NVDAUSDT `[2026-05-14, 2026-08-12)` UTC 的 2,160 根小时线。窗口未参与参数研发或收益筛选的来源声明已记录在 [admission.json](../validation/p7-r04b-e01/admission.json)，独立性仍定性为回溯未触碰候选，而不是严格前瞻样本外。

**正式账户执行数为 0；没有新样本经济结果。** R04A/R04B 保留的官方上市公告／精确首成交交叉核实门槛仍未关闭。限定范围检查已有 R04A/R04B/P6-D02 证据和官方载荷缓存，并进行两次有界公告检索，未取得能关闭门槛的官方交叉材料。官方 March 小时档案首桶 `2026-03-26 14:00 UTC` 与留档 `onboardDate=14:40 UTC` 属于不同时间语义；首桶不是 14:00 已成交的证据，也不能用上线字段自证真实首成交。不得以数据完整性通过替代该门槛。

本轮继续开发软件能力，但不移动窗口、不缩减正式矩阵、不重建行情，也不凭空填写 16 账户的权益或收益。软件定向验证已完成，状态为 **P7_R04B_ENGINEERING_ONLY**，不是机制实证完成。16 个冻结账户的确定性身份及 `NOT_RUN_ADMISSION_BLOCKED` 逐项记录在 [account-disposition.csv](../validation/p7-r04b-e01/account-disposition.csv)，不以 0 填充未运行账户的收益。

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
