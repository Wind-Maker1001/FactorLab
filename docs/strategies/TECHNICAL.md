# 日线技术策略研究（Phase 5A）

`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

独立 Python 策略接入及内置双均线适配示例见 [自定义策略接口](CUSTOM.md)。

## 入口与证据边界

在已有 FactorLab Python 环境、仓库根目录运行（Bash 示例）：

```bash
PYTHONPATH=src python -m factorlab.strategies.technical_cli bollinger --synthetic --config configs/bollinger.json --output local-p5a/bollinger
PYTHONPATH=src python -m factorlab.strategies.technical_cli rsi --synthetic --config configs/rsi.json --output local-p5a/rsi
PYTHONPATH=src python -m factorlab.strategies.technical_cli macd --synthetic --config configs/macd.json --output local-p5a/macd
PYTHONPATH=src python -m factorlab.strategies.technical_cli cta --synthetic --config configs/cta.json --output local-p5a/cta
```

输出目录不得已存在。配置可同时指定 symbol/start/end/initial_cash/max_weight/board_lot；
模式仅接受 daily / next_open / long-only。省略 `--synthetic` 调用已有独立准入门禁，
当前拒绝执行。合成行情与工作日日历不是真实交易数据或交易所日历，不构成收益证据。

```python
from factorlab.strategies.technical import RSIConfig, technical_signals
from factorlab.strategies.technical_research import run_technical_backtest

config = RSIConfig(window=14, exit_rule="midline")
signals = technical_signals(bars, calendar, config)
result = run_technical_backtest(bars, calendar, config, source_kind="REAL_CANDIDATE")
```

低层 API 是 SOFTWARE_ONLY 计算，不是金融准入 API。默认 REAL_CANDIDATE 表明输入未获认证，
只有明确生成的合成输入才应声明 SYNTHETIC。source_kind 不授予执行权限。
输入字段、完整日历、停牌估值及原始 OHLC 合同复用 [双均线合同](DUAL_MA.md)。
缺失输入不插值、不删除；独立 Wilder/EMA 指标函数允许 NaN 并重新预热，完整回测则拒绝缺失价格。
变化复权比例和非空公司行为账本被拒绝，不能作真实总收益解释。

## 信号合同

所有信号仅用当前及过去观察。研究从空仓意图开始，不承接预热期信号。
交叉采用前值含等号、当前值严格穿越。退出优先于买入；无动作 target_weight=NaN，
退出为0。状态是策略意图而非成交推定；失败入场期间保留该意图，退出会取消待成交入场。

| 策略 | 指标与入场 | 退出 |
|---|---|---|
| Bollinger mean_reversion | 完整窗口 SMA，标准差 ddof=0；收盘从下轨以下重新上穿下轨 | 收盘 >= 中轨 |
| Bollinger breakout | 同一轨道；收盘上穿上轨 | 收盘 < 中轨 |
| RSI | Wilder RSI 从 <= oversold 上穿阈值 | 从 >= overbought 下穿阈值；midline 模式改为下穿50 |
| MACD | DIF=快EMA-慢EMA；DEA=DIF的EMA；Histogram=2*(DIF-DEA)；DIF上穿DEA | DIF下穿DEA |
| CTA | 收盘 > 前 entry_window 个 high 最大值且 > trend_window SMA | 收盘 < 前 exit_window 个 low 最小值，或 < 趋势均线，或触发既存 ATR 止损 |

布林带额外保留上下轨向外突破及向内回归布尔列。RSI 阈值需满足
0 < oversold < 50 < overbought < 100。MACD zero_filter 打开后仅接受 DIF>0 的金叉，
且 DIF<0 时退出；不会因后来上穿零轴而补造一次金叉。

Wilder 使用 n 个连续变化的算术均值初始化，此后 alpha=1/n；RSI 零损失为100、
零收益为0、两者皆零为50。缺失价格同时使当前和下一次差分失效，需要新的 n 个连续变化。
EMA 使用 n 个连续观察的 SMA 初始化，此后 alpha=2/(n+1)；DEA 只能在 DIF 可用后开始初始化。
预热会话数：Bollinger=window，RSI=window+1，MACD=slow+signal_window-1，
CTA=max(entry_window,exit_window,atr_window+1,trend_window)。
要求这些会话都在首个研究日前，确保首个研究日交叉有有效前值；不足立即拒绝。

### CTA 风险预算及止损近似

Donchian 的 high/low 窗口先 shift(1)，排除当前突破观察。高低价按当前 adj_close/raw_close
比例转到信号单位；执行仍使用原始价格。ATR 的 TR=max(high-low,abs(high-prev_close),abs(low-prev_close))，
首个无前收盘的 TR 不计，使用 Wilder 均值。

入场权重=min(max_weight,risk_fraction*signal_close/(atr_multiple*ATR))。
这是信号时点、入场资金的名义风险预算，不是每日再平衡或损失保证；预算包含执行费用，
整手向下取整。默认风险预算可能不足一手，执行器记录 cash_constraint 而不擅自增加风险。

止损初值=入场意图收盘-atr_multiple*ATR；后续每个收盘只上移，不下移。
先比较当日收盘与此前已知止损，再更新当日止损供后续收盘使用。
止损跟踪的是入场意图和后续行情，不是假设的成交成本；买入被拒绝时仍会跟踪并可取消入场。
即使日内 low 穿越止损，只要收盘未触发也不成交。收盘确认退出最早下一个研究交易日
raw_open 执行；跳空、停牌和跌停可能扩大亏损、推迟成交。没有盘中止损、盘口队列或风险硬上限保证。

## 执行、评价、复现

四种策略和买入持有基准共用 run_target_positions、AShareExecutionBacktester、PositionBook
及 FeeSchedule，不修改这些接口。正目标只在空仓时入场，持仓不每日调仓；失败订单逐日重试，
被后续目标替换；期末不强制平仓。基准在首个研究开盘使用预先已知的相同 max_weight 资金指令。
原有双均线、Top-K、PortfolioOptimizer、run_target_weights、模型训练均保持原语义。

输出 signals.csv、双方 nav/trades/rejections.csv、evaluation.json 和既有不可变 tracker。
净值账本含持仓、现金、市值；evaluation 使用 ResearchEvaluation 的收益、波动率、回撤、
交易成本等口径。换手为当日单边成交额/前收盘权益，首日分母为初始资金；不与优化器权重变化混同。
拒单单独按次及原因统计。费用及滑点沿用既有研究假设，并非券商费率认证。

Tracker 绑定配置、输入 frame_hash、费用、代码 commit/tree/脏状态、环境、评价和输出路径；
输入为可复算的既有 synthetic_bars 生成器，不另存重复输入账本。参数预先指定，不做事后选优。
ResearchPipeline 现有入口要求模型、因子构建器及六类输入；技术信号不伪装成模型/Top-K分数接入它。
DataHub 保持来源显式、能力检查和知识截止语义，真实数据适配仍须满足已有正式准入合同。
单证券入场无需求解多资产优化问题，故不额外调用 PortfolioOptimizer。Walk-Forward 未被替换或放宽。

定向验收命令：

```bash
mkdir -p local-p5a
PYTHONPATH=src python -m pytest tests/research/test_technical_strategies.py tests/research/test_dual_ma.py tests/research/test_qualification_admission_boundaries.py -q --basetemp=local-p5a/tmp --junitxml=local-p5a/tests.xml
```

新增测试采用手算常量而不改既有 Golden，覆盖初始化、交叉、缺失、参数、前缀一致性、未来扰动、
震荡/单边行情、确定性、现金持仓对账、拒单及 CLI 金融阻断。
2026-11-10 在已有 Python 3.14 环境执行上述三份测试：130 passed（33.89秒）。
这是软件工程验收；未赋予 PIT、HFQ、真实交易或正式金融资格。

## Phase 5B 接口依赖（未实施）

* 动态网格：现有日线目标接口不保存多个挂单、撤单及部分成交状态。需要订单意图/生命周期和
  独立事件执行适配器；先确定事件源的时间精度及 OHLC 路径歧义规则，不能假设最有利触价顺序。
  费用可复用 FeeSchedule，现金/库存约束须逐事件生效；完成订单账本后再适配共同净值评价和 Tracker。
* 配对交易：已有 DataHub、Walk-Forward 可提供显式来源和训练/样本外边界，但还需冻结训练期选对、
  对冲比率/检验身份、滚动更新规则、同步双腿及失败处理合同。当前 run_target_weights 仅接受非负且总和<=1的
  权重，不能表达空腿；不得放宽它。需要明确的借券/融资/保证金及双腿账户执行边界。
  无真实借券合同的 A 股场景只能是软件假设研究，不能给出可执行多空结论。
* PortfolioOptimizer 现有预算和风险输入约束保持原样；未实现网格订单、协整检验或新优化器。
  Phase 5C 不在本轮开发范围，未用日线数据构造高频收益或占位事件接口。
