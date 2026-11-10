# 离线配对交易研究

`SYNTHETIC / SOFTWARE_ONLY`

`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

## 复现

在 Python 3.13 或 3.14 环境安装可选统计依赖：

```bash
python -m pip install -e '.[pairs,dev]'
python -m factorlab.strategies.pairs_cli \
  --config configs/research/p5-b02-pairs-synthetic.json --output outputs/pairs-positive
python -m factorlab.strategies.pairs_cli \
  --config configs/research/p5-b02-pairs-counterexample.json --output outputs/pairs-negative
python -m pytest tests/research/test_pairs_statistics.py tests/research/test_pairs_account.py tests/research/test_pairs_trading.py -q
```

输出目录必须是新目录。重放时，将 `--config` 指向第一次输出的 `config.json`，
并指定另一个新输出目录。随机种子、借券假设、费用、候选限制、验证网格和切分协议均绑定在配置中。

合成正例为共享随机游走加 AR(1) 价差；反例为共享价格趋势加独立随机游走残差。
反例故意具有很高的价格相关性，但相关性不构成入选条件。

## 模型与时间合同

`fit_pair(prices, SelectionConfig(train_end=...))` 接受以交易日为索引的双列价格。
列顺序定义 `y = intercept + beta * x`。`select_pairs` 对证券身份排序后，
仅考察预先限制数量的组合，在训练窗口内检查完整率、交集/并集重叠率、样本长度、
正对冲比例、分半估计稳定性、Engle–Granger 协整和残差 ADF。
统计拒绝会保留相关系数及已经计算的检验统计量，而不只记录入选者。
检验 p 值为名义值，不作多重检验校正；不得将筛选所得 p 值解释为样本外盈利概率。

* 原始价格模型：`spread = Py - intercept - beta * Px`，beta 的单位为 X 股/Y 股。
  它对应固定证券单位的价格关系，适合本轮无公司行为的合成序列。
* 对数价格模型：`spread = log(Py) - intercept - beta * log(Px)`，beta 是价格弹性。
  入场时换算 `X股数/Y股数 = beta * Py_open/Px_open`；不是直接按 beta 配股。
  这是局部美元中性近似，不承诺对数价差与固定股数持仓的损益完全一致。
* 两种模型均先取整 Y 股数，再取整 X 股数；实际数量见成交和持仓账本。
  数量在本次持有期间固定，不进行隐含再平衡。

每折使用现有 `ExpandingWalkForwardSplitter`。训练标签覆盖到
`signal + max_holding_sessions + 2`，沿用实际持有期边界审计及 purge/embargo。
训练期选对并估计参数，以训练 Engle–Granger p 值排序选择一个交易对；
验证期按预先给定的入场阈值网格，以扣费后期末权益最大为目标，平局选较低阈值。
不使用测试期结果选对、选择阈值或修改参数。

测试期冻结回归参数。Z-Score 使用当前收盘价差和**此前**完整滚动窗口的
均值、样本标准差（ddof=1），不把当前价差加入其归一化窗口。
滚动协整诊断只读取截至当前收盘的数据，不将诊断中的回归估计写回持仓模型。
对冲比例的跨折变化及训练分半变化保留在 `folds.json` 和 `selections.csv`。

收盘决策只在下一交易日开盘执行；不以同一收盘价生成并成交信号。
多价差为买 Y/卖空 X，空价差反之；回到退出带或穿越均值即退出。
最大持有时间按入场开盘至计划退出开盘的 session 间隔计算。
亏损、保证金、总敞口或协整失效可提前触发退出。
退出执行当天不重新入场；若开仓后立即出现风险，只能发出下一开盘退出指令。
窗口结束退出在倒数第二个收盘预先安排，最后一个开盘执行。

验证折独立注资、从空仓开始，不将其损益计入样本外净值。
样本外折不重叠，下一折使用上一折结算后的真实权益，不重新补足初始本金。
折间空仓资金不计利息；不持仓穿越 fold 边界。未平仓负债不能被重置到下一折。

## 双腿账户与失败处理

此适配器是独立的软件多空模拟，不改变 A 股仅做多执行合同。
两个 `PositionBook` 分别表达实际持有与借入证券的正数量；借入簿对应负债，
不是额外持有资产。每种可卖空证券必须显式提供 `BorrowTerms(max_shares, annual_rate)`。
缺失借券条件或数量超过限额时拒绝卖空，不自动生成借券资格。

以当前市价计值：

```text
权益 = 现金 + 多头市值 - 空头负债
受限卖空所得 = 尚未归还借券所对应的原始卖出所得
保证金 = margin_rate × 当前空头负债
可用现金 = 现金 - 受限卖空所得 - 保证金
净敞口 = 多头市值 - 空头负债
总敞口 = 多头市值 + 空头负债
杠杆 = 总敞口 / 权益
权益 = 初始资金 + 多头损益 + 空头损益 - 累计交易费 - 累计借券费
```

卖空所得计入现金但全部受限，不能作为新增购买力。
保证金是现金的额外预留，不是从权益重复扣除的费用。
回补按实际回补数量释放相应卖空所得；平多先于回补，回补不凭空生成现金。
资金不足以回补时保留负债并记录失败。
手续费通过现有 `FeeSchedule` 计算。合成配置只假设佣金，零滑点、零最低佣金；
这不是任何真实市场的完整收费表。
借券费按上一收盘空头市值 × 年化费率 × 实际自然日/365 计提，包含周末；
现金不计息，无担保物复用、融资买入或外部信用额度。

开仓先提交卖空腿。首腿零成交或部分成交后不提交另一腿；
第二腿失败或部分成交则保留首腿及所有实际成交，不回写为原子成交。
当日记录残余敞口，下一开盘按实际库存发出清仓指令。
清仓也可部分成交或失败，未成交部分继续记账并在后续开盘尝试退出。
期末仍有单腿风险时，研究保留最后市值、负债和失败记录，
标记 `BLOCKED_UNRESOLVED_EXPOSURE` 并停止后续折，而非丢弃失败折。
`simulate_pair(..., fill_scenarios={日期字符串: {证券: 成交比例}})` 用于确定性故障注入。
该比例是每次提交可成交数量的上界假设，不是历史流动性证据。

## 输出与实际可交易性

* `selections.csv`、`folds.json`：训练选择依据、检验统计、模型和时间边界。
* `signals.csv`、`validation.csv`：因果价差/Z-Score、诊断、决策和验证目标。
* `trades.csv`、`positions.csv`、`failures.csv`：逐腿实际成交、带符号市值和失败记录。
* `nav.csv`：现金、受限所得、保证金、权益、费用、多空损益及敞口。
* `evaluation.json`：现有 `ResearchEvaluation` 的收益、Sharpe、最大回撤等评价。
  现金基准零收益；无交易、零波动时 Sharpe 为不可计算，不伪造为零。
* `metadata.json`、`config.json`、`tracker/`：假设、配置和现有 `ExperimentTracker` 记录。

`read_pair_panels` 可通过 DataHub 的 `daily_bars` 读取 `raw_open/raw_close`，
保留来源 receipt 和缺失日期，不进行前向或未来填充。
真实序列若含公司行为，必须先解决价格关系、证券数量及负债的经济一致性，
不能仅把复权价格作为实际成交价。

本轮借券上限和费率均为显式软件假设，没有可核实的历史券源、召回、资格或费率证据。
实际可交易性始终为 `UNKNOWN`。原始行情可读取不代表 PIT/HFQ 已认证；
软件多空盈亏不授予真实融券、杠杆交易或正式金融研究准入资格。

## 真实历史统计入口

[P5-R03 报告](../validation/p5-r03/REPORT.md) 使用预先固定的
`configs/research/p5-r03-real-pairs.json`。本入口只研究 CSMAR 可比收盘序列，
不将调整价交给收益账户。原始数据、原生重放分区和逐日诊断留在私有目录。

```bash
python -I -X utf8 tools/research/prepare_real_pairs.py \
  --config configs/research/p5-r03-real-pairs.json \
  --catalog <PRIVATE_CSMAR_CATALOG> --source-root <PRIVATE_CSMAR_ROOT> \
  --scratch <EXISTING_EMPTY_SCRATCH> --output <NEW_PRIVATE_CANDIDATE>
python -I -X utf8 tools/research/run_real_pairs.py \
  --config configs/research/p5-r03-real-pairs.json \
  --candidate <PRIVATE_CANDIDATE> --output <NEW_PRIVATE_RESULT>
python -I -X utf8 tools/research/verify_real_pairs.py \
  --candidate <PRIVATE_CANDIDATE> --result <PRIVATE_RESULT> \
  --output <NEW_PRIVATE_REPLAY> --cutoff 2017-08-01
```

`read_pair_panels(..., close_field="adj_close")` 可显式选择统计信号价；
默认仍为 `raw_close`。`run_pair_research(..., statistics_only=True)` 返回
`StatisticalResult`，不调用交易、借券、费用或投资组合评价模块。
普通软件模拟路径不变。验证期仅报告固定阈值的诊断，不优化收益或选择阈值。

恢复事件是独立阈值下的不重叠价差观察：收盘 Z 超过入场阈值后，未来最多
10 个观测 session 内首次回到退出带或穿越零为恢复。Z 的归一化只用过去窗口，
回归参数每折冻结；恢复时间按信号收盘起算，不代表持仓时间或可实现利润。
缺失滚动窗口和折末不足观察期分别标记删失；删失不计入完整观察恢复比例。
拒绝对照也保留相同定义的诊断，但永不成为可交易配对。

价格单位和 `Adjprcwd` 的含再投资可比价定义来自现有已审 CSMAR 字典与原生重放；
这不提供历史复权 vintage、稳定证券身份或公司行为负债账本认证。
`HISTORICAL_TRADABILITY=UNKNOWN`、`BORROW_ASSUMPTIONS=SYNTHETIC`。
公司行为及空头负债语义未解决时，收益、回撤、Sharpe、成交、费用和借券敏感性
均记录为不可计算，不用零值代替。
