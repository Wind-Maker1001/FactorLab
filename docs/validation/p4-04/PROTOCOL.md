# P4-04 预声明实验协议

SYNTHETIC / SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

基线：`4b1baad1f661be5728cfb79ee9a4db5bc1b9f32f`。本协议在本次训练与评价前写入。

## 固定输入与训练预算

完整沿用 `configs/research/p4-02-synthetic.json` 的数据、四模型参数、种子、因子、分折、费用、风险窗口与交易限制；仅实验 ID 和输出位置不同。24 证券、270 合成会话、MOM20/REV1、三折 expanding walk-forward、next-open、80% 投资、15% 单股上限、40 日风险窗口、每五个预测日期调仓。不得按 Test 结果调整。

P4-02 原始完整输出位于 `D:/Code/FactorLab-archives/p4-02-synthetic-20261109-acceptance`，其持久化合同未保存完整预测。P3-12 同样只保留紧凑统计证据。现有证据不能完成相同预测下三目标的逐层配对比较，因此必要新增一次四模型 ResearchPipeline 运行；随后所有组合与随机控制复用该次内存预测，不再训练。不额外拟合标签打乱模型；该控制与五日滞后沿用 P3-12 已公布证据，不能作为本次相同样本检验。

## 组合与对照

四模型及三个既有 random_scores 种子 17、29、43，各比较 minimum_variance、equal、score，共 21 个账本。随机分数只由固定 RNG 与已有 OOS 键生成，不读取收益决定信号。所有臂具有相同 date/symbol/fold/label 键、完整连续执行日历、费用与约束。equal 使用当日全部有效 OOS 证券，而非全 24 证券常驻池或预测 top-k；不同日期有效证券数可变化。score 使用既有平均并列百分位秩偏好，不是预期收益率，也不按 top-k 选股。

主执行方式为 batch。另对四模型三目标复用相同预测运行 incremental_feedback，无换手上限，逐字段比较目标、成交、持仓、净值、拒单与目标偏离；仅新增反馈遥测不要求与 batch 相同。预测空档保留完整日历，持仓继续估值，无信号不提交新目标。

## 估计对象及检验家族

1. 预测家族：其余六个信号臂分别减 FactorScore 的每日 RankIC，共 6 个比较。重新索引到首末预测日间的完整合成交易日历，不压缩折间空档。
2. 经济家族：21 个组合中除 FactorScore/equal 基准外的 20 个臂，分别比较配对每日净收益；包括经济上可能重复的臂，仍全部纳入 Holm 家族，不按结果删选。

两个家族分别 Holm 校正，不声称跨家族联合 FWER。复用 P3-12 的 calendar_paired_inference、paired_inference 和 RobustnessOptions：双侧 95%、alpha=.05、Bartlett HAC lag=5、非循环 moving block=10、500 次 Bootstrap、seed=42、至少 30 日与 80% 覆盖。不足返回 INSUFFICIENT_COVERAGE，保留 null 推断值。1552 个证券日不当作独立时间样本。模型间排序、目标距离、实际偏离与 RankIC/收益排序关系仅描述，不新增事后显著性检验。

## 归因与证据

本地保留逐日期排序（含并列平均秩）、目标、决策、成交、收盘持仓、拒单、执行开盘目标偏离和净值；不保存模型权重或重复完整预测矩阵。正式结果保留预测/样本身份、每日模型对秩相关与排名距离、每日目标 L1 距离和非零支持变化、实际持仓差异、费用、换手与风险统计及文件哈希。

score 对 minimum_variance 的拆分：比较目标非零支持与权重 L1；实际开盘偏离沿用引擎 target_comparison，不以收盘漂移替代执行偏离；费用以实际账本差额报告。费用加回仅作固定成交终值会计分解，不冒充无费用重跑。净收益差减实际费用终值差后的余额标记 UNEXPLAINED，不将选择、配置、执行机制的共同作用虚构为可相加的因果贡献。整数手数舍入与零目标差额不自动算拒单；只有引擎明确记录的原因才作已知原因。

随机盈利、随机显著或模型不显著都不是软件失败。结论保持 NOT FINANCIAL EVIDENCE，不给出实盘建议。
