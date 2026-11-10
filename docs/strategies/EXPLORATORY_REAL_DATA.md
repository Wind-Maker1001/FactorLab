# 真实历史探索性价格收益研究

`EXPLORATORY_REAL_DATA / NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

该入口仅进行回顾性单证券价格研究，不授予正式金融准入、Forward生产或实盘连接资格。
原 `dual_ma_cli --bars` 与正式研究消费者保持原门禁；探索产物不能作为资格包传回正式消费者。

## 首个实验与复现

配置 [exploratory_real_601857_2018.json](../../configs/exploratory_real_601857_2018.json) 显式选择
`CSMAR_PREPARED`，固定既有 `CSMAR_LOCAL_RESEARCH_CANDIDATE_V1` manifest 的SHA256、
2017年预热、2018年研究、10万元初始现金和费用。必须拥有相同的合法本地prepared输入。
manifest固定的是2026年准备版本，不是2017年的历史vintage。

```sh
python -I -B -X utf8 tools/research/run_exploratory_real.py --config configs/exploratory_real_601857_2018.json --source-root '<CSMAR_PREPARED_V4>' --output '<NEW_PRIVATE_OUTPUT>'
```

也可使用已安装的对应版本：`python -m factorlab.strategies.exploratory_real`，参数相同。
输出目录必须不存在。重新执行时将`--config`指向上次保存的`config.json`并使用新输出目录。
输入manifest或被消费行情、日历、事件、来源引用的内容漂移会被拒绝；不自动接受新哈希。
新数据版本应创建新配置与新实验，而不是覆盖旧配置来解释旧结果。

## 价格收益而非总收益

MA5/MA20使用原始收盘价的完整窗口均线，成交与估值分别使用原始开盘价和收盘价。
供应商`adj_close`不进入信号；不使用当前后复权因子，也不假定其历史可知。
这意味着除息价格下降可能改变均线交叉、净值与风险指标，不能解释为含分红表现。

事件候选保存在结果manifest，覆盖状态照录源manifest，不消费不完整事件账本。
已知2018年除息日为6月21日与9月21日；2017年预热也有两次现金候选。
`total_return.status=UNAVAILABLE`且`value=null`，不以0替代无法核算的分红、税或送转收益。
TODO：在完整事件覆盖及受控执行计划就绪后，经既有typed dividend plan接口新增独立总收益实验；不改变本次价格收益口径。

## 执行假设与数据限制

- 初始空仓；预热期不下单，MA只响应研究期收盘发生的交叉，最早下一可成交开盘执行。
- 买入持有为研究开始前已知的固定指令，首次研究开盘开始尝试；两腿使用相同日历、初始资金、整手、仓位上限和费用。期末不强制平仓。
- 未成交目标按原执行器逐日重试，直到成交或被新目标替换。保留拒单记录；不插值、不补成交，不把未知状态当成可交易。
- 输入缺所提供日历中的行、非有限OHLC、未知交易状态或预热不足直接拒绝，不输出虚构收益。
- CSMAR日历是证券观察会话，不是独立官方交易所日历；无法发现供应商行情与观察日历同时漏掉的会话。已保存D05/D01跨源调查支持该特定窗口487个观察的覆盖，但没有因此补成官方认证。
- CSMAR价格CNY/股、量为股、额为CNY，沿用既有输入合同；本策略不需要换手率或中证500成员资格。
- 历史首次分发、修订链及独立状态资格未知；2026获取时间不倒填历史known_at。这里的因果性是所给回顾性序列内的决策/执行顺序，不是来源PIT认证。
- 涨跌停沿用昨日原始收盘近似，除息参考价未事件调整；日线不能证明盘口排队、实际容量或券商成交。
- 默认佣金3bps、最低5元、滑点5bps、转让费0；2018卖出印花税10bps。滑点单列现金成本，不再修改原始成交价。这是研究假设而非历史券商费用认证。

## 输出

私有输出含保存配置、实际消费的raw信号输入、日历、信号、两腿成交/拒单和逐日股份、可卖股份、现金、市值、权益、净值；`metrics.json`使用原双均线统计口径，`*_evaluation.json`复用ResearchEvaluation。
原始输入及事件来源、源manifest、代码提交/tree、包内Python内容身份、运行环境、费率与各产物身份写入manifest；ExperimentTracker以`REAL_CANDIDATE`保存比较，不能升级为正式资格。

原价、事件原文、完整账本与输入快照应保留在私有目录。公共[实验报告](../validation/p5-r01/REPORT.md)只发布范围、派生指标、交易摘要和来源内容身份。
年化使用252会话，无风险年利率0；最大回撤包含初始净值1。期末未平仓保留估值，未完成回合不计胜率。
