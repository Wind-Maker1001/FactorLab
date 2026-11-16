# MC-05：动态 Grid 账户尾部风险收敛

NO LIVE MONEY。

研究入口 `tools/research/run_mc_grid_tail_convergence.py` 逐路径执行原 Paper 账户，以 MC-01 WAL + FULL 路径保存事务，由原 FIFO 和生命周期归因归约，再删除临时账户。价格生成器返回的固定权重组合指标不是本研究的账户风险。

## 冻结定义与随机流

`configs/mc05_grid_tail.json` 冻结来源身份、策略、资本、执行规则、费用、时间边界、三个种子、规模梯度和900秒资源预算。沿用 MC-03 单资产 NVDAUSDT；资金费未模拟。Jump 下跌情景的每小时1/168概率与30%跳幅是压力假设，不是市场估计。

原 GBM/MBB 保留 Runner 批次种子 `seed + batch_id` 和 batch=8 语义；状态与 Jump 保留 MC-03 每路径随机流。16、64、128使用嵌套前缀，规模间结果相关，不作为独立重复实验。状态模型重放生成前缀但不重跑已有账户。

先执行六类确定性压力，根据实测单账户耗时，以1.2成本系数预测完整四模型×三个种子的下一规模新增成本。预算只决定是否准入完整规模，不根据风险大小决定。执行中的账户不被强杀；实际预算超支单列。确定性路径不进入随机风险分布。

## 参与度、库存与敞口

互斥终态为零成交、成交后平仓、成交后仍有库存；曾有库存、触及库存上限、风险退出、退出未完成为可重叠标志。分别报告全样本、有成交、曾持仓、期末有库存分布，附各条件样本量和占总体比例。条件损失概率不能解释为总体损失概率。

实际平均/峰值绝对名义敞口及其相对NAV、初始资本比例来自逐小时账户导出。非零持仓小时与原 lifecycle 的数量加权 position_hours 分列。策略库存单位按 quantity_unit 换算为实际账户数量；配置 max_inventory=100、quantity_unit=0.01 对应实际上限1。现金基准为零风险暴露，不提供满仓持有优劣比较。

零成交 NAV=1000 表示没有参与；不能据此宣称策略有低尾部风险优势。即使路径发生大跌，越界暂停和未入场也可能使账户没有损失。

## 统计定义

损失为 `1-terminal_NAV/1000`，负值表示盈利。VaR使用线性分位，CVaR使用最差 `(1-confidence)*n` 条路径的分数尾部。直接复用 MC-02 的分位秩区间、路径经验Bootstrap和Wilson概率区间。95%/99%尾部均报告有效质量、最大单路径权重和诊断；少尾部、退化Bootstrap或无有限上界时为 STATISTICALLY_UNSTABLE。空条件组为 INSUFFICIENT_DATA。

种子范围和样本标准差是描述性模拟离散度，不是市场预测区间。增加路径减少条件模拟误差，不能消除校准样本偏差、模型假设和合成成交容量误差。未预设金融精度验收阈值，不以点估计接近宣布可靠收敛。

## 执行

```sh
PYTHONPATH=src python tools/research/run_mc_grid_tail_convergence.py \
  --snapshot D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6 \
  --definition configs/mc05_grid_tail.json --output docs/validation/mc-05/run
```

输出目录必须不存在。逐路径CSV是所有已执行随机账户；分组JSON按模型、种子、规模区分；六类确定性压力单列；只保存每模型必要最差价格证据，不保存全部价格或SQLite。工程对账、统计稳定性、数据充分性、金融结论是独立维度。
