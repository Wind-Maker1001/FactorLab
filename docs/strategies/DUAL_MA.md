# 双均线基准策略

`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

本功能独立于 ModelFactory、多因子排名及训练流程。原有CLI维持合成验证与正式门禁。
独立的 [EXPLORATORY_REAL_DATA 入口](EXPLORATORY_REAL_DATA.md) 支持受限真实历史原价研究；
中国石油2018年已完成价格收益比较，含分红总收益仍不可用。正式资格缺口见
[数据阻塞报告](DUAL_MA_2018_DATA_STATUS.md)。

## 运行

在安装 FactorLab 的 Python 3.14 环境中，从项目根运行：

```powershell
python -m factorlab.strategies.dual_ma_cli --synthetic --config configs/dual_ma.json --output results/dual-ma-synthetic
python -m factorlab.strategies.dual_ma_cli --synthetic --symbol 601857.SH --start 2018-01-01 --end 2018-12-31 --short-window 5 --long-window 20 --initial-cash 100000 --max-weight 1 --board-lot 100 --commission-bps 3 --minimum-commission-cny 5 --slippage-bps 5 --transfer-fee-bps 0 --output results/dual-ma-custom
python -m factorlab.strategies.dual_ma_cli --help
```

输出目录必须不存在，避免覆盖既有证据。CLI 只执行内部生成的合成行情；工作日日历
也是测试日历，包含真实交易所可能休市的日期。`--synthetic` 不能与 `--bars` 混用。
CLI 参数覆盖 JSON 中同名值。频率、时点和做空字段是显式合同，当前只接受 daily、
next_open、false，其他值明确拒绝。费率可通过 CLI 或 API 的 FeeSchedule 配置；
API 还可向独立 target adapter 提供已有 PriceLimitSchedule。

```powershell
python -m factorlab.strategies.dual_ma_cli --bars private/601857_2018.csv --output results/dual-ma-real
```

此真实数据请求当前返回退出码 2 和既有 `FORMAL_ADMISSION_BLOCKED` 拒绝，
不生成收益结果。没有新增 exploratory、force 或自报认证绕过开关。

## API 与执行合同

```python
from factorlab.strategies import DualMAConfig, moving_average_signals, run_software_backtest

config = DualMAConfig(short_window=5, long_window=20)
signals = moving_average_signals(bars, calendar, config)
result = run_software_backtest(bars, calendar, config)  # SOFTWARE_ONLY calculation
```

低层计算与原有研究引擎一样不授予数据资格，不得由此推导真实金融证据或正式准入。
真实消费者仍须满足已有 RAW、ADJUSTMENT、STATUS、PIT、SOURCE_BINDING、FORMAL
及 execution admission；当前独立信任根无条件拒绝，不能用此函数代替准入。

输入为单证券、严格递增、不重复、无时区的每日日期。`calendar` 必须显式包含预热及
研究交易日，每日一行，不补齐或删除未知行情。至少需要 long_window 个研究首日前
观测和两个研究日。字段为 `date,symbol,adj_close,raw_open,raw_high,raw_low,raw_close,
volume,trade_status,is_st`。价格须有限且为正，OHLC 关系有效，成交量非负，状态明确
为布尔值或0/1。停牌日也必须提供可审计的价格观测与估值，不能凭空填充缺失行。
均线使用所提供日历上的全部每日 adj_close 完整窗口观测；停牌观测若提供结转价，
也计入窗口。这是明确的软件输入假设，不是停牌数据或复权认证。

金叉严格为前一日 short<=long 且当日 short>long；死叉严格为前一日 short>=long
且当日 short<long。当前相等时保持；前一日相等允许穿越。
初始空仓，不承接预热期订单；首个研究日收盘的真实交叉允许次日执行，
首日已经多头排列不创造买入。无信号时 target_weight=NaN，清仓为0，建仓为配置的
max_weight。正目标仅在空仓时建仓，不逐日追涨补仓，不重复买卖。

`backtest/target.py` 调用原 AShareExecutionBacktester 的订单执行方法及 PositionBook，
复用 T+1、整手、现金、状态、停牌、日期费用表和开盘涨跌停拒单。
不构造 Top-K 分数，也不改变原 Top-K/Fast/Execution 的接口或语义。
失败订单最早从信号后一个日历交易日开盘重试，直到成交或被后续收盘目标替换。
若最后一天产生新交叉，保留信号，不在区间外成交。期末未平仓按 raw_close 估值。

每次买入使用 max_weight 比例的可用现金预算，费用也计入该预算，数量向下取整。
持仓上涨后实际权重可漂移；max_weight 是入场目标，不是持续风险再平衡。
成交价为 raw_open，滑点是另列现金成本；不再把滑点加到价格重复扣除。
沿用原引擎的浮点人民币成本，不做逐笔分币舍入。默认转让费假设为0，
2018卖出印花税按既有表为10bps；全部费率仅为软件研究假设，不是历史券商费率认证。
涨跌停使用昨日原始收盘近似，不能还原盘口、排队、流动性或新股无涨跌停期。

信号价格与成交价格分列。低层收益计算拒绝非空公司行为账本及变化的 adj_close/raw_close
比例；现金分红、税收、送转、拆股执行账本尚未实现。比例恒定或空账本也不能证明
真实公司行为不存在。需要公司行为的真实收益结论保持阻断。

## 产物与统计口径

* `signals.csv`：每日收盘、短长均线、buy/sell/hold、0/正权重/NaN。
* `strategy_trades.csv`、`benchmark_trades.csv`：日期、股数、真实开盘、各项成本及成交后现金。
* `*_rejections.csv`：每次尝试及停牌、涨跌停、T+1、现金等拒单原因。
* `strategy_nav.csv`、`benchmark_nav.csv`：含空仓日的每日股份、可卖股份、现金、市值、权益、净值。
* `*_round_trips.csv`：已完成买卖回合及扣除成本后的盈亏。
* `metrics.json`：收益、年化收益、波动率、夏普、回撤、订单数、回合胜率、费用及滑点。
* `input_bars.csv`、`calendar.csv`：本次生成输入；`nav.png`、`report.md`：比较图与假设。
* `manifest.json`：参数、费率、源码文件 SHA256、真实运行代码位置、可用的 Git 身份、
  Python/平台/依赖版本、产物哈希和持续阻断的资格状态。安装包 Git 身份为 null。

买入持有基准在区间第一个开盘执行预先已知的建仓指令，沿用同一资金、目标上限、
规则及费用，失败同样重试。比较区间从第一个研究开盘持续到最后一个研究收盘，
不因策略首次买入晚而缩短基准区间。

年化收益 = (期末权益/初始资金)^(252/N)-1，N 为实际输入研究会话数，包括首日开盘
到收盘的收益。`--periods-per-year` 可更改252。年化波动率为日收益样本标准差(ddof=1)
乘 sqrt(periods_per_year)。夏普使用既有 risk.py 的日超额均值/样本标准差年化，
无风险年利率默认0，可用 `--risk-free-rate` 配置。最大回撤包含初始净值1及入场成本。
交易数是成交订单数；胜率是已平仓买卖回合扣除买卖费用及滑点后净利润>0的比例，
未平仓不计入；无已平仓回合为 null，不伪装为0或100%。其他未定义统计也写为 null。

## 手工 Golden 与回归

新增 Golden 是测试函数中的人工常量，没有重生成或修改既有 Golden 文件。
短长窗口2/3，预热收盘10/10/10，研究收盘11/12/12/10/9/9，所有开盘10：
第一个研究收盘金叉，次日买900股；第四个研究收盘死叉，次日卖900股。
初始10000，买入佣金5、滑点4.5，卖出佣金5、印花税9、滑点4.5，期末现金9972，
净收益-0.28%。交易日股份依次为0/900/900/900/0/0，账本逐日复算。

```powershell
python -m pytest tests/research/test_dual_ma.py -q --basetemp=local-dual-ma/tmp --junitxml=local-dual-ma/tests.xml
python tools/ci/integration_checks.py --suite core --output local-dual-ma/regression
python tools/ci/integration_checks.py --suite financial-gates --output local-dual-ma/regression
python tools/ci/integration_checks.py --suite legacy --output local-dual-ma/full
```

新增49项覆盖穿越/相等/趋势/震荡、预热、无效和缺失、逐笔时点、重复订单、
整手和现金、T+1、停牌和涨跌停失败重试/反向取消、费用、对账、未来扰动、
确定性、期末未平仓、公司行为拒绝、CLI真实请求拒绝及报告哈希。
完整历史回归中的已知缺失控制对象、旧身份和证据问题须单独报告；不能用增量通过
替换完整回归结论。原控制、冻结基线、历史证据、Golden 和金融门禁均不在本次修改范围。

实际软件验收、历史失败及未完成范围见[兼容性验证报告](DUAL_MA_VALIDATION.md)。
