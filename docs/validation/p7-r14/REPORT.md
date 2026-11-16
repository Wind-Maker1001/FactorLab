# P7-R14：动态 Grid 风险预算验证

`NO LIVE MONEY / FINANCIAL_NOT_ESTABLISHED`

## 冻结研究设计

基于 `7e49d4189a60b05840252f194b4086520b1f0817` 的原始几何 Grid 对照动态风险预算，另以独立校准样本生成固定低暴露 Grid。策略规则见 `configs/research/p7-r14-regime-risk-budget.json` 与 [研究说明](../../research/p7-r14-regime-risk-budget.md)。

每个价格数组只生成一次，各组共用其输入身份。MC-03 的生成器内部状态不传给策略。固定状态表不依据评价收益或 MC-05 结果调整。

现有退出机制只支持整仓退出；预算降至确认库存之下时复用其确认驱动生命周期，未确认或不可成交库存不被抹去。退出后不会自动重启；这一参与率损失纳入风险归因。

## 执行状态

预算模块及相关回归首轮 134 项中 133 项通过；新测试的限价成交夹具未考虑滑点，修正价格后，配对研究测试 11 项全部通过。原始账户与中性预算适配器的指标及 daily/trades/result/evaluation 导出完全一致。

小样本 20 个账户全部完成，耗时 34.136 秒。连续下跌压力下 A/B NAV 为 971.132015/994.622125；快速反转为 1006.664917/1000.859128，显示风险降低伴随上行参与损失。低波动压力两组均零成交，未筛除。正式研究规则不因这些结果修改，正式面板待执行。

## 计划复现入口

```sh
PYTHONPATH=src python -B tools/research/run_mc_regime_risk_budget.py \
  --snapshot '<CERTIFIED_HOURLY_SNAPSHOT>' --output '<NEW_SMOKE_OUTPUT>' --smoke
PYTHONPATH=src python -B tools/research/run_mc_regime_risk_budget.py \
  --snapshot '<CERTIFIED_HOURLY_SNAPSHOT>' --output '<NEW_STUDY_OUTPUT>'
```

快照身份与实际可知时间记录于研究 definition；不将采集时间晚于观察期的历史数据称为真实前瞻验证。正式研究仅作冻结离线合成场景对照。
