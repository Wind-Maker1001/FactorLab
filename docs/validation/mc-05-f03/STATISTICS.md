# MC-05-F03 统计收敛

**CONVERGENCE_NOT_ESTABLISHED / STATISTICALLY_UNSTABLE**

预先固定的判据要求完成全部计划档位。每个模型的三个种子分别接受评估，覆盖四个人群及 95% 和 99% 两个尾部。两次相邻扩档的 VaR、CVaR 变化绝对值不得超过 0.5 个百分点，最终档三种子极差不得超过 0.5 个百分点。最终档每尾名义质量和按权重计算的 Kish 有效路径数均须达到 20。VaR 和 CVaR 的逐项 95% 区间须有界且宽度不超过 2 个百分点。最终档损失及参与率的逐项 95% Wilson 区间宽度须不超过 20 个百分点。在 1,000 USDT 初始资金下，0.5 与 2 个百分点分别对应 5 与 20 USDT 的损失差。20 条名义尾部路径把单条路径在经验 CVaR 中的权重限制在 5%。

路径损失为 1 − 期末 NAV ÷ 初始资金。VaR 使用线性经验分位数，CVaR 对损失最大的样本按精确分数质量加权。VaR 区间采用二项分布秩，样本量不足时保留无界端点。CVaR 的保守区间利用总体 CVaR ≥ VaR，上界在仅依靠已观察损失时为 +∞。经验 bootstrap 区间刻画现有样本内的模拟不确定性，重采样次数不增加独立模拟路径，也不参与 CVaR 精度验收。这些逐项区间固定了校准参数和输入历史。三种子的点估计极差衡量模拟重复之间的差异。模型校准不确定性另需研究。

## 各模型的无条件损失

每行合并同一个价格模型的三个完整种子前缀。种子和条件组的指标列于下方。

| 模型 | 档位 | 有效账户 | VaR95 | CVaR95 | VaR99 | CVaR99 | 收敛状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| jump_down | 16 | 48 | 1.428% | 1.486% | 1.501% | 1.553% | CONVERGENCE_NOT_ESTABLISHED |
| jump_down | 32 | 96 | 1.442% | 1.754% | 1.608% | 2.653% | CONVERGENCE_NOT_ESTABLISHED |
| jump_down | 64 | 192 | 1.442% | 1.790% | 2.287% | 2.519% | CONVERGENCE_NOT_ESTABLISHED |
| regime_historical | 16 | 48 | 0.055% | 0.150% | 0.173% | 0.258% | CONVERGENCE_NOT_ESTABLISHED |
| regime_historical | 32 | 96 | 0.079% | 0.124% | 0.101% | 0.258% | CONVERGENCE_NOT_ESTABLISHED |
| regime_historical | 64 | 192 | 0.074% | 0.122% | 0.167% | 0.214% | CONVERGENCE_NOT_ESTABLISHED |
| moving_block_bootstrap | 16 | 48 | 0.112% | 0.194% | 0.210% | 0.220% | CONVERGENCE_NOT_ESTABLISHED |
| moving_block_bootstrap | 32 | 96 | 0.127% | 0.193% | 0.200% | 0.220% | CONVERGENCE_NOT_ESTABLISHED |
| moving_block_bootstrap | 64 | 192 | 0.121% | 0.237% | 0.216% | 0.477% | CONVERGENCE_NOT_ESTABLISHED |

未满足的判据包括 `CVAR_PRECISION_NOT_ESTABLISHED`、`INSUFFICIENT_TAIL_SUPPORT`、`RATE_PRECISION_NOT_ESTABLISHED`、`SEED_SPREAD_CRITERIA_NOT_MET`、`TIER_CHANGE_CRITERIA_NOT_MET`、`VAR_PRECISION_NOT_ESTABLISHED`。

## 实际参与与尾部样本

档位是每种子的计划分母。条件组的纳入条件是有成交、曾持仓、期末持仓。条件风险的概率分母只包括满足条件的账户，样本比例的分母包括该档全部账户。Kish 路径数衡量经验尾部权重集中度。未抽中上尾概率按各独立路径进入固定概率质量尾部计算。

| 模型 | 档位 | 种子 | 条件 | 实际/总体 | 占比 | 95% Wilson 区间 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| jump_down | 16 | 1729 | unconditional | 16/16 | 100.000% | 80.639% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 1729 | traded | 13/16 | 81.250% | 56.991% 至 93.408% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 1729 | ever_inventory | 13/16 | 81.250% | 56.991% 至 93.408% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 1729 | terminal_inventory | 12/16 | 75.000% | 50.502% 至 89.818% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | unconditional | 16/16 | 100.000% | 80.639% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | traded | 13/16 | 81.250% | 56.991% 至 93.408% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | ever_inventory | 13/16 | 81.250% | 56.991% 至 93.408% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | terminal_inventory | 12/16 | 75.000% | 50.502% 至 89.818% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | unconditional | 16/16 | 100.000% | 80.639% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | traded | 11/16 | 68.750% | 44.404% 至 85.835% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | ever_inventory | 11/16 | 68.750% | 44.404% 至 85.835% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | terminal_inventory | 11/16 | 68.750% | 44.404% 至 85.835% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | unconditional | 32/32 | 100.000% | 89.282% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | traded | 26/32 | 81.250% | 64.691% 至 91.110% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | ever_inventory | 26/32 | 81.250% | 64.691% 至 91.110% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | terminal_inventory | 24/32 | 75.000% | 57.893% 至 86.748% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | unconditional | 32/32 | 100.000% | 89.282% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | traded | 25/32 | 78.125% | 61.245% 至 88.976% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | ever_inventory | 25/32 | 78.125% | 61.245% 至 88.976% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | terminal_inventory | 23/32 | 71.875% | 54.625% 至 84.435% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | unconditional | 32/32 | 100.000% | 89.282% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | traded | 22/32 | 68.750% | 51.433% 至 82.047% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | ever_inventory | 22/32 | 68.750% | 51.433% 至 82.047% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | terminal_inventory | 22/32 | 68.750% | 51.433% 至 82.047% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | unconditional | 64/64 | 100.000% | 94.338% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | traded | 53/64 | 82.812% | 71.787% 至 90.122% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | ever_inventory | 53/64 | 82.812% | 71.787% 至 90.122% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | terminal_inventory | 49/64 | 76.562% | 64.867% 至 85.250% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | unconditional | 64/64 | 100.000% | 94.338% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | traded | 52/64 | 81.250% | 70.026% 至 88.935% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | ever_inventory | 52/64 | 81.250% | 70.026% 至 88.935% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | terminal_inventory | 48/64 | 75.000% | 63.184% 至 83.985% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | unconditional | 64/64 | 100.000% | 94.338% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | traded | 47/64 | 73.438% | 61.517% 至 82.704% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | ever_inventory | 47/64 | 73.438% | 61.517% 至 82.704% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | terminal_inventory | 44/64 | 68.750% | 56.608% 至 78.769% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | unconditional | 16/16 | 100.000% | 80.639% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | traded | 9/16 | 56.250% | 33.179% 至 76.901% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | ever_inventory | 9/16 | 56.250% | 33.179% 至 76.901% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | terminal_inventory | 8/16 | 50.000% | 28.000% 至 72.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | unconditional | 16/16 | 100.000% | 80.639% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | traded | 5/16 | 31.250% | 14.165% 至 55.596% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | ever_inventory | 5/16 | 31.250% | 14.165% 至 55.596% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | terminal_inventory | 4/16 | 25.000% | 10.182% 至 49.498% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | unconditional | 16/16 | 100.000% | 80.639% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | traded | 7/16 | 43.750% | 23.099% 至 66.821% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | ever_inventory | 7/16 | 43.750% | 23.099% 至 66.821% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | terminal_inventory | 6/16 | 37.500% | 18.481% 至 61.359% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | unconditional | 32/32 | 100.000% | 89.282% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | traded | 18/32 | 56.250% | 39.326% 至 71.835% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | ever_inventory | 18/32 | 56.250% | 39.326% 至 71.835% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | terminal_inventory | 15/32 | 46.875% | 30.869% 至 63.550% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | unconditional | 32/32 | 100.000% | 89.282% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | traded | 13/32 | 40.625% | 25.520% 至 57.740% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | ever_inventory | 13/32 | 40.625% | 25.520% 至 57.740% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | terminal_inventory | 11/32 | 34.375% | 20.410% 至 51.689% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | unconditional | 32/32 | 100.000% | 89.282% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | traded | 14/32 | 43.750% | 28.165% 至 60.674% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | ever_inventory | 14/32 | 43.750% | 28.165% 至 60.674% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | terminal_inventory | 11/32 | 34.375% | 20.410% 至 51.689% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | unconditional | 64/64 | 100.000% | 94.338% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | traded | 34/64 | 53.125% | 41.072% 至 64.824% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | ever_inventory | 34/64 | 53.125% | 41.072% 至 64.824% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | terminal_inventory | 27/64 | 42.188% | 30.870% 至 54.390% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | unconditional | 64/64 | 100.000% | 94.338% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | traded | 31/64 | 48.438% | 36.634% 至 60.418% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | ever_inventory | 31/64 | 48.438% | 36.634% 至 60.418% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | terminal_inventory | 23/64 | 35.938% | 25.288% 至 48.179% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | unconditional | 64/64 | 100.000% | 94.338% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | traded | 24/64 | 37.500% | 26.666% 至 49.750% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | ever_inventory | 24/64 | 37.500% | 26.666% 至 49.750% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | terminal_inventory | 19/64 | 29.688% | 19.905% 至 41.770% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | unconditional | 16/16 | 100.000% | 80.639% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | traded | 7/16 | 43.750% | 23.099% 至 66.821% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | ever_inventory | 7/16 | 43.750% | 23.099% 至 66.821% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | terminal_inventory | 6/16 | 37.500% | 18.481% 至 61.359% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | unconditional | 16/16 | 100.000% | 80.639% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | traded | 7/16 | 43.750% | 23.099% 至 66.821% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | ever_inventory | 7/16 | 43.750% | 23.099% 至 66.821% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | terminal_inventory | 7/16 | 43.750% | 23.099% 至 66.821% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | unconditional | 16/16 | 100.000% | 80.639% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | traded | 11/16 | 68.750% | 44.404% 至 85.835% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | ever_inventory | 11/16 | 68.750% | 44.404% 至 85.835% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | terminal_inventory | 10/16 | 62.500% | 38.641% 至 81.519% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | unconditional | 32/32 | 100.000% | 89.282% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | traded | 14/32 | 43.750% | 28.165% 至 60.674% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | ever_inventory | 14/32 | 43.750% | 28.165% 至 60.674% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | terminal_inventory | 13/32 | 40.625% | 25.520% 至 57.740% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | unconditional | 32/32 | 100.000% | 89.282% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | traded | 13/32 | 40.625% | 25.520% 至 57.740% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | ever_inventory | 13/32 | 40.625% | 25.520% 至 57.740% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | terminal_inventory | 13/32 | 40.625% | 25.520% 至 57.740% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | unconditional | 32/32 | 100.000% | 89.282% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | traded | 20/32 | 62.500% | 45.254% 至 77.066% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | ever_inventory | 20/32 | 62.500% | 45.254% 至 77.066% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | terminal_inventory | 18/32 | 56.250% | 39.326% 至 71.835% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | unconditional | 64/64 | 100.000% | 94.338% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | traded | 31/64 | 48.438% | 36.634% 至 60.418% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | ever_inventory | 31/64 | 48.438% | 36.634% 至 60.418% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | terminal_inventory | 29/64 | 45.312% | 33.729% 至 57.426% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | unconditional | 64/64 | 100.000% | 94.338% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | traded | 30/64 | 46.875% | 35.176% 至 58.928% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | ever_inventory | 30/64 | 46.875% | 35.176% 至 58.928% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | terminal_inventory | 29/64 | 45.312% | 33.729% 至 57.426% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | unconditional | 64/64 | 100.000% | 94.338% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | traded | 34/64 | 53.125% | 41.072% 至 64.824% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | ever_inventory | 34/64 | 53.125% | 41.072% 至 64.824% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | terminal_inventory | 31/64 | 48.438% | 36.634% 至 60.418% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | unconditional | 48/48 | 100.000% | 92.590% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | traded | 37/48 | 77.083% | 63.461% 至 86.692% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | ever_inventory | 37/48 | 77.083% | 63.461% 至 86.692% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | terminal_inventory | 35/48 | 72.917% | 59.003% 至 83.434% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | unconditional | 96/96 | 100.000% | 96.152% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | traded | 73/96 | 76.042% | 66.608% 至 83.472% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | ever_inventory | 73/96 | 76.042% | 66.608% 至 83.472% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | terminal_inventory | 69/96 | 71.875% | 62.174% 至 79.893% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | unconditional | 192/192 | 100.000% | 98.038% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | traded | 152/192 | 79.167% | 72.878% 至 84.311% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | ever_inventory | 152/192 | 79.167% | 72.878% 至 84.311% | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | terminal_inventory | 141/192 | 73.438% | 66.775% 至 79.181% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | unconditional | 48/48 | 100.000% | 92.590% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | traded | 21/48 | 43.750% | 30.701% 至 57.725% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | ever_inventory | 21/48 | 43.750% | 30.701% 至 57.725% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | terminal_inventory | 18/48 | 37.500% | 25.215% 至 51.637% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | unconditional | 96/96 | 100.000% | 96.152% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | traded | 45/96 | 46.875% | 37.206% 至 56.784% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | ever_inventory | 45/96 | 46.875% | 37.206% 至 56.784% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | terminal_inventory | 37/96 | 38.542% | 29.426% 至 48.539% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | unconditional | 192/192 | 100.000% | 98.038% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | traded | 89/192 | 46.354% | 39.441% 至 53.410% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | ever_inventory | 89/192 | 46.354% | 39.441% 至 53.410% | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | terminal_inventory | 69/192 | 35.938% | 29.488% 至 42.939% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | unconditional | 48/48 | 100.000% | 92.590% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | traded | 25/48 | 52.083% | 38.329% 至 65.529% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | ever_inventory | 25/48 | 52.083% | 38.329% 至 65.529% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | terminal_inventory | 23/48 | 47.917% | 34.471% 至 61.671% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | unconditional | 96/96 | 100.000% | 96.152% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | traded | 47/96 | 48.958% | 39.193% 至 58.804% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | ever_inventory | 47/96 | 48.958% | 39.193% 至 58.804% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | terminal_inventory | 44/96 | 45.833% | 36.219% 至 55.768% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | unconditional | 192/192 | 100.000% | 98.038% 至 100.000% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | traded | 95/192 | 49.479% | 42.487% 至 56.492% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | ever_inventory | 95/192 | 49.479% | 42.487% 至 56.492% | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | terminal_inventory | 89/192 | 46.354% | 39.441% 至 53.410% | INSUFFICIENT_TAIL_SUPPORT |

| 模型 | 档位 | 种子 | 条件 | 损失率 | 成交率 | 零成交率 | 曾持仓率 | 期末持仓率 | 平均利用率 | 账户峰值的均值 | 账户峰值的最大值 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| jump_down | 16 | 1729 | unconditional | 56.250% | 81.250% | 18.750% | 81.250% | 75.000% | 1.226% | 1.720% | 4.223% |
| jump_down | 16 | 1729 | traded | 69.231% | 100.000% | 0.000% | 100.000% | 92.308% | 1.509% | 2.117% | 4.223% |
| jump_down | 16 | 1729 | ever_inventory | 69.231% | 100.000% | 0.000% | 100.000% | 92.308% | 1.509% | 2.117% | 4.223% |
| jump_down | 16 | 1729 | terminal_inventory | 75.000% | 100.000% | 0.000% | 100.000% | 100.000% | 1.562% | 2.118% | 4.223% |
| jump_down | 16 | 2718 | unconditional | 56.250% | 81.250% | 18.750% | 81.250% | 75.000% | 1.105% | 1.642% | 2.932% |
| jump_down | 16 | 2718 | traded | 69.231% | 100.000% | 0.000% | 100.000% | 92.308% | 1.359% | 2.021% | 2.932% |
| jump_down | 16 | 2718 | ever_inventory | 69.231% | 100.000% | 0.000% | 100.000% | 92.308% | 1.359% | 2.021% | 2.932% |
| jump_down | 16 | 2718 | terminal_inventory | 75.000% | 100.000% | 0.000% | 100.000% | 100.000% | 1.445% | 2.014% | 2.932% |
| jump_down | 16 | 31415 | unconditional | 62.500% | 68.750% | 31.250% | 68.750% | 68.750% | 1.056% | 1.579% | 3.993% |
| jump_down | 16 | 31415 | traded | 90.909% | 100.000% | 0.000% | 100.000% | 100.000% | 1.535% | 2.297% | 3.993% |
| jump_down | 16 | 31415 | ever_inventory | 90.909% | 100.000% | 0.000% | 100.000% | 100.000% | 1.535% | 2.297% | 3.993% |
| jump_down | 16 | 31415 | terminal_inventory | 90.909% | 100.000% | 0.000% | 100.000% | 100.000% | 1.535% | 2.297% | 3.993% |
| jump_down | 32 | 1729 | unconditional | 59.375% | 81.250% | 18.750% | 81.250% | 75.000% | 1.159% | 1.708% | 4.223% |
| jump_down | 32 | 1729 | traded | 73.077% | 100.000% | 0.000% | 100.000% | 92.308% | 1.426% | 2.103% | 4.223% |
| jump_down | 32 | 1729 | ever_inventory | 73.077% | 100.000% | 0.000% | 100.000% | 92.308% | 1.426% | 2.103% | 4.223% |
| jump_down | 32 | 1729 | terminal_inventory | 79.167% | 100.000% | 0.000% | 100.000% | 100.000% | 1.467% | 2.103% | 4.223% |
| jump_down | 32 | 2718 | unconditional | 56.250% | 78.125% | 21.875% | 78.125% | 71.875% | 1.102% | 1.662% | 3.841% |
| jump_down | 32 | 2718 | traded | 72.000% | 100.000% | 0.000% | 100.000% | 92.000% | 1.411% | 2.128% | 3.841% |
| jump_down | 32 | 2718 | ever_inventory | 72.000% | 100.000% | 0.000% | 100.000% | 92.000% | 1.411% | 2.128% | 3.841% |
| jump_down | 32 | 2718 | terminal_inventory | 78.261% | 100.000% | 0.000% | 100.000% | 100.000% | 1.477% | 2.130% | 3.841% |
| jump_down | 32 | 31415 | unconditional | 59.375% | 68.750% | 31.250% | 68.750% | 68.750% | 1.073% | 1.568% | 3.993% |
| jump_down | 32 | 31415 | traded | 86.364% | 100.000% | 0.000% | 100.000% | 100.000% | 1.561% | 2.280% | 3.993% |
| jump_down | 32 | 31415 | ever_inventory | 86.364% | 100.000% | 0.000% | 100.000% | 100.000% | 1.561% | 2.280% | 3.993% |
| jump_down | 32 | 31415 | terminal_inventory | 86.364% | 100.000% | 0.000% | 100.000% | 100.000% | 1.561% | 2.280% | 3.993% |
| jump_down | 64 | 1729 | unconditional | 64.062% | 82.812% | 17.188% | 82.812% | 76.562% | 1.096% | 1.672% | 4.223% |
| jump_down | 64 | 1729 | traded | 77.358% | 100.000% | 0.000% | 100.000% | 92.453% | 1.323% | 2.019% | 4.223% |
| jump_down | 64 | 1729 | ever_inventory | 77.358% | 100.000% | 0.000% | 100.000% | 92.453% | 1.323% | 2.019% | 4.223% |
| jump_down | 64 | 1729 | terminal_inventory | 83.673% | 100.000% | 0.000% | 100.000% | 100.000% | 1.367% | 2.012% | 4.223% |
| jump_down | 64 | 2718 | unconditional | 56.250% | 81.250% | 18.750% | 81.250% | 75.000% | 1.157% | 1.740% | 3.971% |
| jump_down | 64 | 2718 | traded | 69.231% | 100.000% | 0.000% | 100.000% | 92.308% | 1.424% | 2.142% | 3.971% |
| jump_down | 64 | 2718 | ever_inventory | 69.231% | 100.000% | 0.000% | 100.000% | 92.308% | 1.424% | 2.142% | 3.971% |
| jump_down | 64 | 2718 | terminal_inventory | 75.000% | 100.000% | 0.000% | 100.000% | 100.000% | 1.487% | 2.145% | 3.971% |
| jump_down | 64 | 31415 | unconditional | 59.375% | 73.438% | 26.562% | 73.438% | 68.750% | 1.063% | 1.613% | 3.993% |
| jump_down | 64 | 31415 | traded | 80.851% | 100.000% | 0.000% | 100.000% | 93.617% | 1.447% | 2.196% | 3.993% |
| jump_down | 64 | 31415 | ever_inventory | 80.851% | 100.000% | 0.000% | 100.000% | 93.617% | 1.447% | 2.196% | 3.993% |
| jump_down | 64 | 31415 | terminal_inventory | 86.364% | 100.000% | 0.000% | 100.000% | 100.000% | 1.503% | 2.203% | 3.993% |
| regime_historical | 16 | 1729 | unconditional | 18.750% | 56.250% | 43.750% | 56.250% | 50.000% | 0.898% | 1.391% | 3.897% |
| regime_historical | 16 | 1729 | traded | 33.333% | 100.000% | 0.000% | 100.000% | 88.889% | 1.597% | 2.473% | 3.897% |
| regime_historical | 16 | 1729 | ever_inventory | 33.333% | 100.000% | 0.000% | 100.000% | 88.889% | 1.597% | 2.473% | 3.897% |
| regime_historical | 16 | 1729 | terminal_inventory | 37.500% | 100.000% | 0.000% | 100.000% | 100.000% | 1.763% | 2.518% | 3.897% |
| regime_historical | 16 | 2718 | unconditional | 18.750% | 31.250% | 68.750% | 31.250% | 25.000% | 0.374% | 0.648% | 2.177% |
| regime_historical | 16 | 2718 | traded | 60.000% | 100.000% | 0.000% | 100.000% | 80.000% | 1.197% | 2.073% | 2.177% |
| regime_historical | 16 | 2718 | ever_inventory | 60.000% | 100.000% | 0.000% | 100.000% | 80.000% | 1.197% | 2.073% | 2.177% |
| regime_historical | 16 | 2718 | terminal_inventory | 75.000% | 100.000% | 0.000% | 100.000% | 100.000% | 1.450% | 2.067% | 2.177% |
| regime_historical | 16 | 31415 | unconditional | 12.500% | 43.750% | 56.250% | 43.750% | 37.500% | 0.607% | 0.904% | 2.129% |
| regime_historical | 16 | 31415 | traded | 28.571% | 100.000% | 0.000% | 100.000% | 85.714% | 1.387% | 2.067% | 2.129% |
| regime_historical | 16 | 31415 | ever_inventory | 28.571% | 100.000% | 0.000% | 100.000% | 85.714% | 1.387% | 2.067% | 2.129% |
| regime_historical | 16 | 31415 | terminal_inventory | 33.333% | 100.000% | 0.000% | 100.000% | 100.000% | 1.520% | 2.062% | 2.129% |
| regime_historical | 32 | 1729 | unconditional | 18.750% | 56.250% | 43.750% | 56.250% | 46.875% | 0.906% | 1.395% | 3.974% |
| regime_historical | 32 | 1729 | traded | 33.333% | 100.000% | 0.000% | 100.000% | 83.333% | 1.611% | 2.481% | 3.974% |
| regime_historical | 32 | 1729 | ever_inventory | 33.333% | 100.000% | 0.000% | 100.000% | 83.333% | 1.611% | 2.481% | 3.974% |
| regime_historical | 32 | 1729 | terminal_inventory | 40.000% | 100.000% | 0.000% | 100.000% | 100.000% | 1.793% | 2.556% | 3.974% |
| regime_historical | 32 | 2718 | unconditional | 15.625% | 40.625% | 59.375% | 40.625% | 34.375% | 0.542% | 0.894% | 3.865% |
| regime_historical | 32 | 2718 | traded | 38.462% | 100.000% | 0.000% | 100.000% | 84.615% | 1.335% | 2.200% | 3.865% |
| regime_historical | 32 | 2718 | ever_inventory | 38.462% | 100.000% | 0.000% | 100.000% | 84.615% | 1.335% | 2.200% | 3.865% |
| regime_historical | 32 | 2718 | terminal_inventory | 45.455% | 100.000% | 0.000% | 100.000% | 100.000% | 1.455% | 2.219% | 3.865% |
| regime_historical | 32 | 31415 | unconditional | 9.375% | 43.750% | 56.250% | 43.750% | 34.375% | 0.727% | 1.135% | 5.671% |
| regime_historical | 32 | 31415 | traded | 21.429% | 100.000% | 0.000% | 100.000% | 78.571% | 1.663% | 2.593% | 5.671% |
| regime_historical | 32 | 31415 | ever_inventory | 21.429% | 100.000% | 0.000% | 100.000% | 78.571% | 1.663% | 2.593% | 5.671% |
| regime_historical | 32 | 31415 | terminal_inventory | 27.273% | 100.000% | 0.000% | 100.000% | 100.000% | 1.938% | 2.728% | 5.671% |
| regime_historical | 64 | 1729 | unconditional | 21.875% | 53.125% | 46.875% | 53.125% | 42.188% | 0.865% | 1.302% | 3.993% |
| regime_historical | 64 | 1729 | traded | 41.176% | 100.000% | 0.000% | 100.000% | 79.412% | 1.628% | 2.452% | 3.993% |
| regime_historical | 64 | 1729 | ever_inventory | 41.176% | 100.000% | 0.000% | 100.000% | 79.412% | 1.628% | 2.452% | 3.993% |
| regime_historical | 64 | 1729 | terminal_inventory | 51.852% | 100.000% | 0.000% | 100.000% | 100.000% | 1.863% | 2.543% | 3.993% |
| regime_historical | 64 | 2718 | unconditional | 17.188% | 48.438% | 51.562% | 48.438% | 35.938% | 0.615% | 1.059% | 3.965% |
| regime_historical | 64 | 2718 | traded | 35.484% | 100.000% | 0.000% | 100.000% | 74.194% | 1.269% | 2.187% | 3.965% |
| regime_historical | 64 | 2718 | ever_inventory | 35.484% | 100.000% | 0.000% | 100.000% | 74.194% | 1.269% | 2.187% | 3.965% |
| regime_historical | 64 | 2718 | terminal_inventory | 47.826% | 100.000% | 0.000% | 100.000% | 100.000% | 1.470% | 2.220% | 3.965% |
| regime_historical | 64 | 31415 | unconditional | 9.375% | 37.500% | 62.500% | 37.500% | 29.688% | 0.548% | 0.919% | 5.671% |
| regime_historical | 64 | 31415 | traded | 25.000% | 100.000% | 0.000% | 100.000% | 79.167% | 1.461% | 2.450% | 5.671% |
| regime_historical | 64 | 31415 | ever_inventory | 25.000% | 100.000% | 0.000% | 100.000% | 79.167% | 1.461% | 2.450% | 5.671% |
| regime_historical | 64 | 31415 | terminal_inventory | 31.579% | 100.000% | 0.000% | 100.000% | 100.000% | 1.695% | 2.542% | 5.671% |
| moving_block_bootstrap | 16 | 1729 | unconditional | 25.000% | 43.750% | 56.250% | 43.750% | 37.500% | 0.540% | 0.894% | 2.105% |
| moving_block_bootstrap | 16 | 1729 | traded | 57.143% | 100.000% | 0.000% | 100.000% | 85.714% | 1.234% | 2.044% | 2.105% |
| moving_block_bootstrap | 16 | 1729 | ever_inventory | 57.143% | 100.000% | 0.000% | 100.000% | 85.714% | 1.234% | 2.044% | 2.105% |
| moving_block_bootstrap | 16 | 1729 | terminal_inventory | 66.667% | 100.000% | 0.000% | 100.000% | 100.000% | 1.368% | 2.034% | 2.087% |
| moving_block_bootstrap | 16 | 2718 | unconditional | 25.000% | 43.750% | 56.250% | 43.750% | 43.750% | 0.899% | 1.229% | 5.446% |
| moving_block_bootstrap | 16 | 2718 | traded | 57.143% | 100.000% | 0.000% | 100.000% | 100.000% | 2.054% | 2.808% | 5.446% |
| moving_block_bootstrap | 16 | 2718 | ever_inventory | 57.143% | 100.000% | 0.000% | 100.000% | 100.000% | 2.054% | 2.808% | 5.446% |
| moving_block_bootstrap | 16 | 2718 | terminal_inventory | 57.143% | 100.000% | 0.000% | 100.000% | 100.000% | 2.054% | 2.808% | 5.446% |
| moving_block_bootstrap | 16 | 31415 | unconditional | 31.250% | 68.750% | 31.250% | 68.750% | 62.500% | 0.993% | 1.413% | 2.098% |
| moving_block_bootstrap | 16 | 31415 | traded | 45.455% | 100.000% | 0.000% | 100.000% | 90.909% | 1.445% | 2.055% | 2.098% |
| moving_block_bootstrap | 16 | 31415 | ever_inventory | 45.455% | 100.000% | 0.000% | 100.000% | 90.909% | 1.445% | 2.055% | 2.098% |
| moving_block_bootstrap | 16 | 31415 | terminal_inventory | 50.000% | 100.000% | 0.000% | 100.000% | 100.000% | 1.543% | 2.050% | 2.087% |
| moving_block_bootstrap | 32 | 1729 | unconditional | 21.875% | 43.750% | 56.250% | 43.750% | 40.625% | 0.697% | 1.063% | 5.565% |
| moving_block_bootstrap | 32 | 1729 | traded | 50.000% | 100.000% | 0.000% | 100.000% | 92.857% | 1.594% | 2.429% | 5.565% |
| moving_block_bootstrap | 32 | 1729 | ever_inventory | 50.000% | 100.000% | 0.000% | 100.000% | 92.857% | 1.594% | 2.429% | 5.565% |
| moving_block_bootstrap | 32 | 1729 | terminal_inventory | 53.846% | 100.000% | 0.000% | 100.000% | 100.000% | 1.684% | 2.454% | 5.565% |
| moving_block_bootstrap | 32 | 2718 | unconditional | 25.000% | 40.625% | 59.375% | 40.625% | 40.625% | 0.768% | 1.054% | 5.446% |
| moving_block_bootstrap | 32 | 2718 | traded | 61.538% | 100.000% | 0.000% | 100.000% | 100.000% | 1.891% | 2.593% | 5.446% |
| moving_block_bootstrap | 32 | 2718 | ever_inventory | 61.538% | 100.000% | 0.000% | 100.000% | 100.000% | 1.891% | 2.593% | 5.446% |
| moving_block_bootstrap | 32 | 2718 | terminal_inventory | 61.538% | 100.000% | 0.000% | 100.000% | 100.000% | 1.891% | 2.593% | 5.446% |
| moving_block_bootstrap | 32 | 31415 | unconditional | 28.125% | 62.500% | 37.500% | 62.500% | 56.250% | 1.034% | 1.346% | 4.029% |
| moving_block_bootstrap | 32 | 31415 | traded | 45.000% | 100.000% | 0.000% | 100.000% | 90.000% | 1.654% | 2.154% | 4.029% |
| moving_block_bootstrap | 32 | 31415 | ever_inventory | 45.000% | 100.000% | 0.000% | 100.000% | 90.000% | 1.654% | 2.154% | 4.029% |
| moving_block_bootstrap | 32 | 31415 | terminal_inventory | 50.000% | 100.000% | 0.000% | 100.000% | 100.000% | 1.760% | 2.160% | 4.029% |
| moving_block_bootstrap | 64 | 1729 | unconditional | 25.000% | 48.438% | 51.562% | 48.438% | 45.312% | 0.838% | 1.222% | 5.565% |
| moving_block_bootstrap | 64 | 1729 | traded | 51.613% | 100.000% | 0.000% | 100.000% | 93.548% | 1.730% | 2.523% | 5.565% |
| moving_block_bootstrap | 64 | 1729 | ever_inventory | 51.613% | 100.000% | 0.000% | 100.000% | 93.548% | 1.730% | 2.523% | 5.565% |
| moving_block_bootstrap | 64 | 1729 | terminal_inventory | 55.172% | 100.000% | 0.000% | 100.000% | 100.000% | 1.799% | 2.552% | 5.565% |
| moving_block_bootstrap | 64 | 2718 | unconditional | 26.562% | 46.875% | 53.125% | 46.875% | 45.312% | 0.823% | 1.152% | 5.446% |
| moving_block_bootstrap | 64 | 2718 | traded | 56.667% | 100.000% | 0.000% | 100.000% | 96.667% | 1.755% | 2.458% | 5.446% |
| moving_block_bootstrap | 64 | 2718 | ever_inventory | 56.667% | 100.000% | 0.000% | 100.000% | 96.667% | 1.755% | 2.458% | 5.446% |
| moving_block_bootstrap | 64 | 2718 | terminal_inventory | 58.621% | 100.000% | 0.000% | 100.000% | 100.000% | 1.792% | 2.471% | 5.446% |
| moving_block_bootstrap | 64 | 31415 | unconditional | 18.750% | 53.125% | 46.875% | 53.125% | 48.438% | 0.924% | 1.213% | 4.029% |
| moving_block_bootstrap | 64 | 31415 | traded | 35.294% | 100.000% | 0.000% | 100.000% | 91.176% | 1.739% | 2.283% | 4.029% |
| moving_block_bootstrap | 64 | 31415 | ever_inventory | 35.294% | 100.000% | 0.000% | 100.000% | 91.176% | 1.739% | 2.283% | 4.029% |
| moving_block_bootstrap | 64 | 31415 | terminal_inventory | 38.710% | 100.000% | 0.000% | 100.000% | 100.000% | 1.829% | 2.301% | 4.029% |
| jump_down | 16 | 同模型三种子 | unconditional | 58.333% | 77.083% | 22.917% | 77.083% | 72.917% | 1.129% | 1.647% | 4.223% |
| jump_down | 16 | 同模型三种子 | traded | 75.676% | 100.000% | 0.000% | 100.000% | 94.595% | 1.464% | 2.137% | 4.223% |
| jump_down | 16 | 同模型三种子 | ever_inventory | 75.676% | 100.000% | 0.000% | 100.000% | 94.595% | 1.464% | 2.137% | 4.223% |
| jump_down | 16 | 同模型三种子 | terminal_inventory | 80.000% | 100.000% | 0.000% | 100.000% | 100.000% | 1.513% | 2.139% | 4.223% |
| jump_down | 32 | 同模型三种子 | unconditional | 58.333% | 76.042% | 23.958% | 76.042% | 71.875% | 1.111% | 1.646% | 4.223% |
| jump_down | 32 | 同模型三种子 | traded | 76.712% | 100.000% | 0.000% | 100.000% | 94.521% | 1.461% | 2.165% | 4.223% |
| jump_down | 32 | 同模型三种子 | ever_inventory | 76.712% | 100.000% | 0.000% | 100.000% | 94.521% | 1.461% | 2.165% | 4.223% |
| jump_down | 32 | 同模型三种子 | terminal_inventory | 81.159% | 100.000% | 0.000% | 100.000% | 100.000% | 1.500% | 2.168% | 4.223% |
| jump_down | 64 | 同模型三种子 | unconditional | 59.896% | 79.167% | 20.833% | 79.167% | 73.438% | 1.105% | 1.675% | 4.223% |
| jump_down | 64 | 同模型三种子 | traded | 75.658% | 100.000% | 0.000% | 100.000% | 92.763% | 1.396% | 2.116% | 4.223% |
| jump_down | 64 | 同模型三种子 | ever_inventory | 75.658% | 100.000% | 0.000% | 100.000% | 92.763% | 1.396% | 2.116% | 4.223% |
| jump_down | 64 | 同模型三种子 | terminal_inventory | 81.560% | 100.000% | 0.000% | 100.000% | 100.000% | 1.450% | 2.117% | 4.223% |
| regime_historical | 16 | 同模型三种子 | unconditional | 16.667% | 43.750% | 56.250% | 43.750% | 37.500% | 0.626% | 0.981% | 3.897% |
| regime_historical | 16 | 同模型三种子 | traded | 38.095% | 100.000% | 0.000% | 100.000% | 85.714% | 1.432% | 2.242% | 3.897% |
| regime_historical | 16 | 同模型三种子 | ever_inventory | 38.095% | 100.000% | 0.000% | 100.000% | 85.714% | 1.432% | 2.242% | 3.897% |
| regime_historical | 16 | 同模型三种子 | terminal_inventory | 44.444% | 100.000% | 0.000% | 100.000% | 100.000% | 1.613% | 2.266% | 3.897% |
| regime_historical | 32 | 同模型三种子 | unconditional | 14.583% | 46.875% | 53.125% | 46.875% | 38.542% | 0.725% | 1.141% | 5.671% |
| regime_historical | 32 | 同模型三种子 | traded | 31.111% | 100.000% | 0.000% | 100.000% | 82.222% | 1.547% | 2.435% | 5.671% |
| regime_historical | 32 | 同模型三种子 | ever_inventory | 31.111% | 100.000% | 0.000% | 100.000% | 82.222% | 1.547% | 2.435% | 5.671% |
| regime_historical | 32 | 同模型三种子 | terminal_inventory | 37.838% | 100.000% | 0.000% | 100.000% | 100.000% | 1.736% | 2.507% | 5.671% |
| regime_historical | 64 | 同模型三种子 | unconditional | 16.146% | 46.354% | 53.646% | 46.354% | 35.938% | 0.676% | 1.093% | 5.671% |
| regime_historical | 64 | 同模型三种子 | traded | 34.831% | 100.000% | 0.000% | 100.000% | 77.528% | 1.458% | 2.359% | 5.671% |
| regime_historical | 64 | 同模型三种子 | ever_inventory | 34.831% | 100.000% | 0.000% | 100.000% | 77.528% | 1.458% | 2.359% | 5.671% |
| regime_historical | 64 | 同模型三种子 | terminal_inventory | 44.928% | 100.000% | 0.000% | 100.000% | 100.000% | 1.686% | 2.435% | 5.671% |
| moving_block_bootstrap | 16 | 同模型三种子 | unconditional | 27.083% | 52.083% | 47.917% | 52.083% | 47.917% | 0.811% | 1.178% | 5.446% |
| moving_block_bootstrap | 16 | 同模型三种子 | traded | 52.000% | 100.000% | 0.000% | 100.000% | 92.000% | 1.556% | 2.263% | 5.446% |
| moving_block_bootstrap | 16 | 同模型三种子 | ever_inventory | 52.000% | 100.000% | 0.000% | 100.000% | 92.000% | 1.556% | 2.263% | 5.446% |
| moving_block_bootstrap | 16 | 同模型三种子 | terminal_inventory | 56.522% | 100.000% | 0.000% | 100.000% | 100.000% | 1.653% | 2.277% | 5.446% |
| moving_block_bootstrap | 32 | 同模型三种子 | unconditional | 25.000% | 48.958% | 51.042% | 48.958% | 45.833% | 0.833% | 1.154% | 5.565% |
| moving_block_bootstrap | 32 | 同模型三种子 | traded | 51.064% | 100.000% | 0.000% | 100.000% | 93.617% | 1.702% | 2.357% | 5.565% |
| moving_block_bootstrap | 32 | 同模型三种子 | ever_inventory | 51.064% | 100.000% | 0.000% | 100.000% | 93.617% | 1.702% | 2.357% | 5.565% |
| moving_block_bootstrap | 32 | 同模型三种子 | terminal_inventory | 54.545% | 100.000% | 0.000% | 100.000% | 100.000% | 1.776% | 2.375% | 5.565% |
| moving_block_bootstrap | 64 | 同模型三种子 | unconditional | 23.438% | 49.479% | 50.521% | 49.479% | 46.354% | 0.862% | 1.196% | 5.565% |
| moving_block_bootstrap | 64 | 同模型三种子 | traded | 47.368% | 100.000% | 0.000% | 100.000% | 93.684% | 1.741% | 2.417% | 5.565% |
| moving_block_bootstrap | 64 | 同模型三种子 | ever_inventory | 47.368% | 100.000% | 0.000% | 100.000% | 93.684% | 1.741% | 2.417% | 5.565% |
| moving_block_bootstrap | 64 | 同模型三种子 | terminal_inventory | 50.562% | 100.000% | 0.000% | 100.000% | 100.000% | 1.807% | 2.438% | 5.565% |

| 模型 | 档位 | 种子 | 条件 | 尾部 | 名义质量 | 加权路径数 | Kish 路径数 | 不同加权损失值 | 最大权重 | 未抽中上尾概率 | VaR | 95% VaR 区间 | CVaR | 95% 经验 bootstrap 范围 | 95% 保守 CVaR 区间 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| jump_down | 16 | 1729 | unconditional | 95 | 0.80 | 1 | 1.00 | 1 | 100.000% | 44.013% | 0.819% | 0.665% 至 +∞ | 1.087% | 0.665% 至 1.087% | 0.665% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 1729 | unconditional | 99 | 0.16 | 1 | 1.00 | 1 | 100.000% | 85.146% | 1.033% | 0.730% 至 +∞ | 1.087% | 0.665% 至 1.087% | 0.730% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 1729 | traded | 95 | 0.65 | 1 | 1.00 | 1 | 100.000% | 51.334% | 0.873% | 0.665% 至 +∞ | 1.087% | 0.665% 至 1.087% | 0.665% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 1729 | traded | 99 | 0.13 | 1 | 1.00 | 1 | 100.000% | 87.752% | 1.044% | 0.730% 至 +∞ | 1.087% | 0.665% 至 1.087% | 0.730% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 1729 | ever_inventory | 95 | 0.65 | 1 | 1.00 | 1 | 100.000% | 51.334% | 0.873% | 0.665% 至 +∞ | 1.087% | 0.665% 至 1.087% | 0.665% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 1729 | ever_inventory | 99 | 0.13 | 1 | 1.00 | 1 | 100.000% | 87.752% | 1.044% | 0.730% 至 +∞ | 1.087% | 0.665% 至 1.087% | 0.730% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 1729 | terminal_inventory | 95 | 0.60 | 1 | 1.00 | 1 | 100.000% | 54.036% | 0.890% | 0.665% 至 +∞ | 1.087% | 0.665% 至 1.087% | 0.665% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 1729 | terminal_inventory | 99 | 0.12 | 1 | 1.00 | 1 | 100.000% | 88.638% | 1.047% | 0.730% 至 +∞ | 1.087% | 0.665% 至 1.087% | 0.730% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | unconditional | 95 | 0.80 | 1 | 1.00 | 1 | 100.000% | 44.013% | 1.457% | 0.584% 至 +∞ | 1.553% | 0.584% 至 1.553% | 0.584% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | unconditional | 99 | 0.16 | 1 | 1.00 | 1 | 100.000% | 85.146% | 1.534% | 1.425% 至 +∞ | 1.553% | 0.584% 至 1.553% | 1.425% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | traded | 95 | 0.65 | 1 | 1.00 | 1 | 100.000% | 51.334% | 1.476% | 0.652% 至 +∞ | 1.553% | 0.584% 至 1.553% | 0.652% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | traded | 99 | 0.13 | 1 | 1.00 | 1 | 100.000% | 87.752% | 1.538% | 1.425% 至 +∞ | 1.553% | 0.584% 至 1.553% | 1.425% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | ever_inventory | 95 | 0.65 | 1 | 1.00 | 1 | 100.000% | 51.334% | 1.476% | 0.652% 至 +∞ | 1.553% | 0.584% 至 1.553% | 0.652% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | ever_inventory | 99 | 0.13 | 1 | 1.00 | 1 | 100.000% | 87.752% | 1.538% | 1.425% 至 +∞ | 1.553% | 0.584% 至 1.553% | 1.425% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | terminal_inventory | 95 | 0.60 | 1 | 1.00 | 1 | 100.000% | 54.036% | 1.482% | 0.652% 至 +∞ | 1.553% | 0.584% 至 1.553% | 0.652% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 2718 | terminal_inventory | 99 | 0.12 | 1 | 1.00 | 1 | 100.000% | 88.638% | 1.539% | 1.425% 至 +∞ | 1.553% | 0.584% 至 1.553% | 1.425% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | unconditional | 95 | 0.80 | 1 | 1.00 | 1 | 100.000% | 44.013% | 1.433% | 0.931% 至 +∞ | 1.441% | 0.931% 至 1.441% | 0.931% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | unconditional | 99 | 0.16 | 1 | 1.00 | 1 | 100.000% | 85.146% | 1.440% | 1.430% 至 +∞ | 1.441% | 0.931% 至 1.441% | 1.430% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | traded | 95 | 0.55 | 1 | 1.00 | 1 | 100.000% | 56.880% | 1.436% | 0.945% 至 +∞ | 1.441% | 0.931% 至 1.441% | 0.945% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | traded | 99 | 0.11 | 1 | 1.00 | 1 | 100.000% | 89.534% | 1.440% | 1.430% 至 +∞ | 1.441% | 0.931% 至 1.441% | 1.430% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | ever_inventory | 95 | 0.55 | 1 | 1.00 | 1 | 100.000% | 56.880% | 1.436% | 0.945% 至 +∞ | 1.441% | 0.931% 至 1.441% | 0.945% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | ever_inventory | 99 | 0.11 | 1 | 1.00 | 1 | 100.000% | 89.534% | 1.440% | 1.430% 至 +∞ | 1.441% | 0.931% 至 1.441% | 1.430% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | terminal_inventory | 95 | 0.55 | 1 | 1.00 | 1 | 100.000% | 56.880% | 1.436% | 0.945% 至 +∞ | 1.441% | 0.931% 至 1.441% | 0.945% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 31415 | terminal_inventory | 99 | 0.11 | 1 | 1.00 | 1 | 100.000% | 89.534% | 1.440% | 1.430% 至 +∞ | 1.441% | 0.931% 至 1.441% | 1.430% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | unconditional | 95 | 1.60 | 2 | 1.88 | 2 | 62.500% | 19.371% | 1.455% | 1.087% 至 +∞ | 1.532% | 1.168% 至 1.543% | 1.087% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | unconditional | 99 | 0.32 | 1 | 1.00 | 1 | 100.000% | 72.498% | 1.534% | 1.406% 至 +∞ | 1.543% | 1.394% 至 1.543% | 1.406% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | traded | 95 | 1.30 | 2 | 1.55 | 2 | 76.923% | 26.352% | 1.487% | 1.087% 至 +∞ | 1.536% | 1.323% 至 1.543% | 1.087% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | traded | 99 | 0.26 | 1 | 1.00 | 1 | 100.000% | 77.004% | 1.536% | 1.406% 至 +∞ | 1.543% | 1.394% 至 1.543% | 1.406% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | ever_inventory | 95 | 1.30 | 2 | 1.55 | 2 | 76.923% | 26.352% | 1.487% | 1.087% 至 +∞ | 1.536% | 1.323% 至 1.543% | 1.087% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | ever_inventory | 99 | 0.26 | 1 | 1.00 | 1 | 100.000% | 77.004% | 1.536% | 1.406% 至 +∞ | 1.543% | 1.394% 至 1.543% | 1.406% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | terminal_inventory | 95 | 1.20 | 2 | 1.38 | 2 | 83.333% | 29.199% | 1.498% | 1.087% 至 +∞ | 1.538% | 1.343% 至 1.543% | 1.087% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 1729 | terminal_inventory | 99 | 0.24 | 1 | 1.00 | 1 | 100.000% | 78.568% | 1.536% | 1.514% 至 +∞ | 1.543% | 1.394% 至 1.543% | 1.514% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | unconditional | 95 | 1.60 | 2 | 1.88 | 2 | 62.500% | 19.371% | 1.482% | 0.693% 至 +∞ | 2.241% | 1.140% 至 2.653% | 0.693% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | unconditional | 99 | 0.32 | 1 | 1.00 | 1 | 100.000% | 72.498% | 2.312% | 1.425% 至 +∞ | 2.653% | 1.408% 至 2.653% | 1.425% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | traded | 95 | 1.25 | 2 | 1.47 | 2 | 80.000% | 27.739% | 1.528% | 0.693% 至 +∞ | 2.433% | 1.265% 至 2.653% | 0.693% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | traded | 99 | 0.25 | 1 | 1.00 | 1 | 100.000% | 77.782% | 2.389% | 1.425% 至 +∞ | 2.653% | 1.408% 至 2.653% | 1.425% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | ever_inventory | 95 | 1.25 | 2 | 1.47 | 2 | 80.000% | 27.739% | 1.528% | 0.693% 至 +∞ | 2.433% | 1.265% 至 2.653% | 0.693% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | ever_inventory | 99 | 0.25 | 1 | 1.00 | 1 | 100.000% | 77.782% | 2.389% | 1.425% 至 +∞ | 2.653% | 1.408% 至 2.653% | 1.425% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | terminal_inventory | 95 | 1.15 | 2 | 1.29 | 2 | 86.957% | 30.736% | 1.540% | 0.693% 至 +∞ | 2.510% | 1.315% 至 2.653% | 0.693% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 2718 | terminal_inventory | 99 | 0.23 | 1 | 1.00 | 1 | 100.000% | 79.361% | 2.411% | 1.553% 至 +∞ | 2.653% | 1.408% 至 2.653% | 1.553% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | unconditional | 95 | 1.60 | 2 | 1.88 | 2 | 62.500% | 19.371% | 1.435% | 0.931% 至 +∞ | 1.443% | 0.945% 至 1.444% | 0.931% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | unconditional | 99 | 0.32 | 1 | 1.00 | 1 | 100.000% | 72.498% | 1.443% | 1.430% 至 +∞ | 1.444% | 0.945% 至 1.444% | 1.430% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | traded | 95 | 1.10 | 2 | 1.20 | 2 | 90.909% | 32.353% | 1.441% | 0.945% 至 +∞ | 1.444% | 0.944% 至 1.444% | 0.945% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | traded | 99 | 0.22 | 1 | 1.00 | 1 | 100.000% | 80.163% | 1.444% | 1.441% 至 +∞ | 1.444% | 0.945% 至 1.444% | 1.441% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | ever_inventory | 95 | 1.10 | 2 | 1.20 | 2 | 90.909% | 32.353% | 1.441% | 0.945% 至 +∞ | 1.444% | 0.944% 至 1.444% | 0.945% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | ever_inventory | 99 | 0.22 | 1 | 1.00 | 1 | 100.000% | 80.163% | 1.444% | 1.441% 至 +∞ | 1.444% | 0.945% 至 1.444% | 1.441% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | terminal_inventory | 95 | 1.10 | 2 | 1.20 | 2 | 90.909% | 32.353% | 1.441% | 0.945% 至 +∞ | 1.444% | 0.944% 至 1.444% | 0.945% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 31415 | terminal_inventory | 99 | 0.22 | 1 | 1.00 | 1 | 100.000% | 80.163% | 1.444% | 1.441% 至 +∞ | 1.444% | 0.945% 至 1.444% | 1.441% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | unconditional | 95 | 3.20 | 4 | 3.37 | 4 | 31.250% | 3.752% | 1.498% | 0.902% 至 +∞ | 2.030% | 1.302% 至 2.367% | 0.902% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | unconditional | 99 | 0.64 | 1 | 1.00 | 1 | 100.000% | 52.560% | 2.313% | 1.514% 至 +∞ | 2.373% | 1.514% 至 2.373% | 1.514% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | traded | 95 | 2.65 | 3 | 2.90 | 3 | 37.736% | 6.597% | 1.526% | 1.087% 至 +∞ | 2.134% | 1.364% 至 2.373% | 1.087% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | traded | 99 | 0.53 | 1 | 1.00 | 1 | 100.000% | 58.704% | 2.324% | 1.543% 至 +∞ | 2.373% | 1.514% 至 2.373% | 1.543% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | ever_inventory | 95 | 2.65 | 3 | 2.90 | 3 | 37.736% | 6.597% | 1.526% | 1.087% 至 +∞ | 2.134% | 1.364% 至 2.373% | 1.087% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | ever_inventory | 99 | 0.53 | 1 | 1.00 | 1 | 100.000% | 58.704% | 2.324% | 1.543% 至 +∞ | 2.373% | 1.514% 至 2.373% | 1.543% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | terminal_inventory | 95 | 2.45 | 3 | 2.73 | 3 | 40.816% | 8.099% | 1.531% | 1.087% 至 +∞ | 2.182% | 1.388% 至 2.373% | 1.087% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 1729 | terminal_inventory | 99 | 0.49 | 1 | 1.00 | 1 | 100.000% | 61.112% | 2.328% | 1.543% 至 +∞ | 2.373% | 1.514% 至 2.373% | 1.543% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | unconditional | 95 | 3.20 | 4 | 3.37 | 4 | 31.250% | 3.752% | 1.424% | 1.170% 至 +∞ | 1.856% | 1.364% 至 2.585% | 1.170% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | unconditional | 99 | 0.64 | 1 | 1.00 | 1 | 100.000% | 52.560% | 1.960% | 1.425% 至 +∞ | 2.653% | 1.425% 至 2.653% | 1.425% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | traded | 95 | 2.60 | 3 | 2.86 | 3 | 38.462% | 6.944% | 1.435% | 1.405% 至 +∞ | 1.952% | 1.415% 至 2.653% | 1.405% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | traded | 99 | 0.52 | 1 | 1.00 | 1 | 100.000% | 59.297% | 2.092% | 1.448% 至 +∞ | 2.653% | 1.425% 至 2.653% | 1.448% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | ever_inventory | 95 | 2.60 | 3 | 2.86 | 3 | 38.462% | 6.944% | 1.435% | 1.405% 至 +∞ | 1.952% | 1.415% 至 2.653% | 1.405% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | ever_inventory | 99 | 0.52 | 1 | 1.00 | 1 | 100.000% | 59.297% | 2.092% | 1.448% 至 +∞ | 2.653% | 1.425% 至 2.653% | 1.448% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | terminal_inventory | 95 | 2.40 | 3 | 2.67 | 3 | 41.667% | 8.526% | 1.440% | 1.405% 至 +∞ | 1.994% | 1.415% 至 2.653% | 1.405% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 2718 | terminal_inventory | 99 | 0.48 | 1 | 1.00 | 1 | 100.000% | 61.729% | 2.136% | 1.448% 至 +∞ | 2.653% | 1.425% 至 2.653% | 1.448% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | unconditional | 95 | 3.20 | 4 | 3.37 | 4 | 31.250% | 3.752% | 1.440% | 1.248% 至 +∞ | 1.465% | 1.355% 至 1.509% | 1.248% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | unconditional | 99 | 0.64 | 1 | 1.00 | 1 | 100.000% | 52.560% | 1.470% | 1.441% 至 +∞ | 1.513% | 1.441% 至 1.513% | 1.441% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | traded | 95 | 2.35 | 3 | 2.60 | 3 | 42.553% | 8.974% | 1.442% | 1.291% 至 +∞ | 1.473% | 1.408% 至 1.513% | 1.291% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | traded | 99 | 0.47 | 1 | 1.00 | 1 | 100.000% | 62.353% | 1.481% | 1.442% 至 +∞ | 1.513% | 1.441% 至 1.513% | 1.442% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | ever_inventory | 95 | 2.35 | 3 | 2.60 | 3 | 42.553% | 8.974% | 1.442% | 1.291% 至 +∞ | 1.473% | 1.408% 至 1.513% | 1.291% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | ever_inventory | 99 | 0.47 | 1 | 1.00 | 1 | 100.000% | 62.353% | 1.481% | 1.442% 至 +∞ | 1.513% | 1.441% 至 1.513% | 1.442% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | terminal_inventory | 95 | 2.20 | 3 | 2.37 | 3 | 45.455% | 10.467% | 1.442% | 1.423% 至 +∞ | 1.475% | 1.421% 至 1.513% | 1.423% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 31415 | terminal_inventory | 99 | 0.44 | 1 | 1.00 | 1 | 100.000% | 64.261% | 1.484% | 1.442% 至 +∞ | 1.513% | 1.441% 至 1.513% | 1.442% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | unconditional | 95 | 0.80 | 1 | 1.00 | 1 | 100.000% | 44.013% | 0.123% | 0.000% 至 +∞ | 0.258% | 0.060% 至 0.258% | 0.000% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | unconditional | 99 | 0.16 | 1 | 1.00 | 1 | 100.000% | 85.146% | 0.231% | 0.077% 至 +∞ | 0.258% | 0.060% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | traded | 95 | 0.45 | 1 | 1.00 | 1 | 100.000% | 63.025% | 0.186% | 0.063% 至 +∞ | 0.258% | 0.059% 至 0.258% | 0.063% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | traded | 99 | 0.09 | 1 | 1.00 | 1 | 100.000% | 91.352% | 0.243% | 0.077% 至 +∞ | 0.258% | 0.059% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | ever_inventory | 95 | 0.45 | 1 | 1.00 | 1 | 100.000% | 63.025% | 0.186% | 0.063% 至 +∞ | 0.258% | 0.059% 至 0.258% | 0.063% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | ever_inventory | 99 | 0.09 | 1 | 1.00 | 1 | 100.000% | 91.352% | 0.243% | 0.077% 至 +∞ | 0.258% | 0.059% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | terminal_inventory | 95 | 0.40 | 1 | 1.00 | 1 | 100.000% | 66.342% | 0.195% | 0.063% 至 +∞ | 0.258% | -0.007% 至 0.258% | 0.063% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 1729 | terminal_inventory | 99 | 0.08 | 1 | 1.00 | 1 | 100.000% | 92.274% | 0.245% | 0.077% 至 +∞ | 0.258% | -0.007% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | unconditional | 95 | 0.80 | 1 | 1.00 | 1 | 100.000% | 44.013% | 0.024% | 0.000% 至 +∞ | 0.037% | 0.000% 至 0.037% | 0.000% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | unconditional | 99 | 0.16 | 1 | 1.00 | 1 | 100.000% | 85.146% | 0.034% | 0.020% 至 +∞ | 0.037% | 0.000% 至 0.037% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | traded | 95 | 0.25 | 1 | 1.00 | 1 | 100.000% | 77.378% | 0.034% | 0.020% 至 +∞ | 0.037% | 0.003% 至 0.037% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | traded | 99 | 0.05 | 1 | 1.00 | 1 | 100.000% | 95.099% | 0.036% | 0.020% 至 +∞ | 0.037% | 0.003% 至 0.037% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | ever_inventory | 95 | 0.25 | 1 | 1.00 | 1 | 100.000% | 77.378% | 0.034% | 0.020% 至 +∞ | 0.037% | 0.003% 至 0.037% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | ever_inventory | 99 | 0.05 | 1 | 1.00 | 1 | 100.000% | 95.099% | 0.036% | 0.020% 至 +∞ | 0.037% | 0.003% 至 0.037% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | terminal_inventory | 95 | 0.20 | 1 | 1.00 | 1 | 100.000% | 81.451% | 0.034% | 0.020% 至 +∞ | 0.037% | 0.003% 至 0.037% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 2718 | terminal_inventory | 99 | 0.04 | 1 | 1.00 | 1 | 100.000% | 96.060% | 0.037% | 0.020% 至 +∞ | 0.037% | 0.003% 至 0.037% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | unconditional | 95 | 0.80 | 1 | 1.00 | 1 | 100.000% | 44.013% | 0.033% | 0.000% 至 +∞ | 0.041% | 0.000% 至 0.041% | 0.000% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | unconditional | 99 | 0.16 | 1 | 1.00 | 1 | 100.000% | 85.146% | 0.040% | 0.031% 至 +∞ | 0.041% | 0.000% 至 0.041% | 0.031% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | traded | 95 | 0.35 | 1 | 1.00 | 1 | 100.000% | 69.834% | 0.038% | -0.018% 至 +∞ | 0.041% | -0.018% 至 0.041% | -0.018% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | traded | 99 | 0.07 | 1 | 1.00 | 1 | 100.000% | 93.207% | 0.041% | 0.031% 至 +∞ | 0.041% | -0.018% 至 0.041% | 0.031% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | ever_inventory | 95 | 0.35 | 1 | 1.00 | 1 | 100.000% | 69.834% | 0.038% | -0.018% 至 +∞ | 0.041% | -0.018% 至 0.041% | -0.018% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | ever_inventory | 99 | 0.07 | 1 | 1.00 | 1 | 100.000% | 93.207% | 0.041% | 0.031% 至 +∞ | 0.041% | -0.018% 至 0.041% | 0.031% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | terminal_inventory | 95 | 0.30 | 1 | 1.00 | 1 | 100.000% | 73.509% | 0.039% | -0.018% 至 +∞ | 0.041% | -0.018% 至 0.041% | -0.018% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 31415 | terminal_inventory | 99 | 0.06 | 1 | 1.00 | 1 | 100.000% | 94.148% | 0.041% | 0.031% 至 +∞ | 0.041% | -0.018% 至 0.041% | 0.031% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | unconditional | 95 | 1.60 | 2 | 1.88 | 2 | 62.500% | 19.371% | 0.089% | 0.063% 至 +∞ | 0.196% | 0.072% 至 0.258% | 0.063% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | unconditional | 99 | 0.32 | 1 | 1.00 | 1 | 100.000% | 72.498% | 0.207% | 0.085% 至 +∞ | 0.258% | 0.077% 至 0.258% | 0.085% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | traded | 95 | 0.90 | 1 | 1.00 | 1 | 100.000% | 39.721% | 0.117% | 0.077% 至 +∞ | 0.258% | 0.077% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | traded | 99 | 0.18 | 1 | 1.00 | 1 | 100.000% | 83.451% | 0.230% | 0.092% 至 +∞ | 0.258% | 0.077% 至 0.258% | 0.092% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | ever_inventory | 95 | 0.90 | 1 | 1.00 | 1 | 100.000% | 39.721% | 0.117% | 0.077% 至 +∞ | 0.258% | 0.077% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | ever_inventory | 99 | 0.18 | 1 | 1.00 | 1 | 100.000% | 83.451% | 0.230% | 0.092% 至 +∞ | 0.258% | 0.077% 至 0.258% | 0.092% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | terminal_inventory | 95 | 0.75 | 1 | 1.00 | 1 | 100.000% | 46.329% | 0.142% | 0.077% 至 +∞ | 0.258% | 0.077% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 1729 | terminal_inventory | 99 | 0.15 | 1 | 1.00 | 1 | 100.000% | 86.006% | 0.235% | 0.092% 至 +∞ | 0.258% | 0.077% 至 0.258% | 0.092% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | unconditional | 95 | 1.60 | 2 | 1.88 | 2 | 62.500% | 19.371% | 0.028% | 0.003% 至 +∞ | 0.071% | 0.008% 至 0.092% | 0.003% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | unconditional | 99 | 0.32 | 1 | 1.00 | 1 | 100.000% | 72.498% | 0.075% | 0.020% 至 +∞ | 0.092% | 0.008% 至 0.092% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | traded | 95 | 0.65 | 1 | 1.00 | 1 | 100.000% | 51.334% | 0.059% | 0.020% 至 +∞ | 0.092% | 0.008% 至 0.092% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | traded | 99 | 0.13 | 1 | 1.00 | 1 | 100.000% | 87.752% | 0.085% | 0.037% 至 +∞ | 0.092% | 0.008% 至 0.092% | 0.037% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | ever_inventory | 95 | 0.65 | 1 | 1.00 | 1 | 100.000% | 51.334% | 0.059% | 0.020% 至 +∞ | 0.092% | 0.008% 至 0.092% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | ever_inventory | 99 | 0.13 | 1 | 1.00 | 1 | 100.000% | 87.752% | 0.085% | 0.037% 至 +∞ | 0.092% | 0.008% 至 0.092% | 0.037% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | terminal_inventory | 95 | 0.55 | 1 | 1.00 | 1 | 100.000% | 56.880% | 0.064% | 0.020% 至 +∞ | 0.092% | 0.008% 至 0.092% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 2718 | terminal_inventory | 99 | 0.11 | 1 | 1.00 | 1 | 100.000% | 89.534% | 0.086% | 0.037% 至 +∞ | 0.092% | 0.008% 至 0.092% | 0.037% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | unconditional | 95 | 1.60 | 2 | 1.88 | 2 | 62.500% | 19.371% | 0.036% | 0.000% 至 +∞ | 0.069% | 0.000% 至 0.085% | 0.000% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | unconditional | 99 | 0.32 | 1 | 1.00 | 1 | 100.000% | 72.498% | 0.072% | 0.031% 至 +∞ | 0.085% | 0.000% 至 0.085% | 0.031% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | traded | 95 | 0.70 | 1 | 1.00 | 1 | 100.000% | 48.767% | 0.057% | -0.018% 至 +∞ | 0.085% | -0.018% 至 0.085% | -0.018% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | traded | 99 | 0.14 | 1 | 1.00 | 1 | 100.000% | 86.875% | 0.080% | 0.041% 至 +∞ | 0.085% | -0.018% 至 0.085% | 0.041% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | ever_inventory | 95 | 0.70 | 1 | 1.00 | 1 | 100.000% | 48.767% | 0.057% | -0.018% 至 +∞ | 0.085% | -0.018% 至 0.085% | -0.018% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | ever_inventory | 99 | 0.14 | 1 | 1.00 | 1 | 100.000% | 86.875% | 0.080% | 0.041% 至 +∞ | 0.085% | -0.018% 至 0.085% | 0.041% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | terminal_inventory | 95 | 0.55 | 1 | 1.00 | 1 | 100.000% | 56.880% | 0.063% | 0.031% 至 +∞ | 0.085% | 0.028% 至 0.085% | 0.031% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 31415 | terminal_inventory | 99 | 0.11 | 1 | 1.00 | 1 | 100.000% | 89.534% | 0.081% | 0.041% 至 +∞ | 0.085% | 0.028% 至 0.085% | 0.041% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | unconditional | 95 | 3.20 | 4 | 3.37 | 4 | 31.250% | 3.752% | 0.097% | 0.071% 至 +∞ | 0.191% | 0.095% 至 0.252% | 0.071% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | unconditional | 99 | 0.64 | 1 | 1.00 | 1 | 100.000% | 52.560% | 0.201% | 0.098% 至 +∞ | 0.258% | 0.098% 至 0.258% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | traded | 95 | 1.70 | 2 | 1.94 | 2 | 58.824% | 17.482% | 0.167% | 0.085% 至 +∞ | 0.220% | 0.096% 至 0.258% | 0.085% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | traded | 99 | 0.34 | 1 | 1.00 | 1 | 100.000% | 71.055% | 0.228% | 0.167% 至 +∞ | 0.258% | 0.098% 至 0.258% | 0.167% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | ever_inventory | 95 | 1.70 | 2 | 1.94 | 2 | 58.824% | 17.482% | 0.167% | 0.085% 至 +∞ | 0.220% | 0.096% 至 0.258% | 0.085% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | ever_inventory | 99 | 0.34 | 1 | 1.00 | 1 | 100.000% | 71.055% | 0.228% | 0.167% 至 +∞ | 0.258% | 0.098% 至 0.258% | 0.167% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | terminal_inventory | 95 | 1.35 | 2 | 1.62 | 2 | 74.074% | 25.034% | 0.167% | 0.092% 至 +∞ | 0.234% | 0.098% 至 0.258% | 0.092% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 1729 | terminal_inventory | 99 | 0.27 | 1 | 1.00 | 1 | 100.000% | 76.234% | 0.234% | 0.167% 至 +∞ | 0.258% | 0.098% 至 0.258% | 0.167% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | unconditional | 95 | 3.20 | 4 | 3.37 | 4 | 31.250% | 3.752% | 0.043% | 0.018% 至 +∞ | 0.068% | 0.036% 至 0.090% | 0.018% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | unconditional | 99 | 0.64 | 1 | 1.00 | 1 | 100.000% | 52.560% | 0.078% | 0.044% 至 +∞ | 0.092% | 0.044% 至 0.092% | 0.044% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | traded | 95 | 1.55 | 2 | 1.84 | 2 | 64.516% | 20.391% | 0.059% | 0.037% 至 +∞ | 0.084% | 0.044% 至 0.092% | 0.037% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | traded | 99 | 0.31 | 1 | 1.00 | 1 | 100.000% | 73.230% | 0.085% | 0.047% 至 +∞ | 0.092% | 0.044% 至 0.092% | 0.047% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | ever_inventory | 95 | 1.55 | 2 | 1.84 | 2 | 64.516% | 20.391% | 0.059% | 0.037% 至 +∞ | 0.084% | 0.044% 至 0.092% | 0.037% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | ever_inventory | 99 | 0.31 | 1 | 1.00 | 1 | 100.000% | 73.230% | 0.085% | 0.047% 至 +∞ | 0.092% | 0.044% 至 0.092% | 0.047% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | terminal_inventory | 95 | 1.15 | 2 | 1.29 | 2 | 86.957% | 30.736% | 0.068% | 0.037% 至 +∞ | 0.089% | 0.043% 至 0.092% | 0.037% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 2718 | terminal_inventory | 99 | 0.23 | 1 | 1.00 | 1 | 100.000% | 79.361% | 0.087% | 0.070% 至 +∞ | 0.092% | 0.044% 至 0.092% | 0.070% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | unconditional | 95 | 3.20 | 4 | 3.37 | 4 | 31.250% | 3.752% | 0.040% | 0.000% 至 +∞ | 0.070% | 0.032% 至 0.085% | 0.000% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | unconditional | 99 | 0.64 | 1 | 1.00 | 1 | 100.000% | 52.560% | 0.083% | 0.041% 至 +∞ | 0.085% | 0.041% 至 0.085% | 0.041% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | traded | 95 | 1.20 | 2 | 1.38 | 2 | 83.333% | 29.199% | 0.076% | 0.031% 至 +∞ | 0.085% | 0.041% 至 0.085% | 0.031% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | traded | 99 | 0.24 | 1 | 1.00 | 1 | 100.000% | 78.568% | 0.084% | 0.082% 至 +∞ | 0.085% | 0.041% 至 0.085% | 0.082% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | ever_inventory | 95 | 1.20 | 2 | 1.38 | 2 | 83.333% | 29.199% | 0.076% | 0.031% 至 +∞ | 0.085% | 0.041% 至 0.085% | 0.031% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | ever_inventory | 99 | 0.24 | 1 | 1.00 | 1 | 100.000% | 78.568% | 0.084% | 0.082% 至 +∞ | 0.085% | 0.041% 至 0.085% | 0.082% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | terminal_inventory | 95 | 0.95 | 1 | 1.00 | 1 | 100.000% | 37.735% | 0.082% | 0.041% 至 +∞ | 0.085% | 0.041% 至 0.085% | 0.041% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 31415 | terminal_inventory | 99 | 0.19 | 1 | 1.00 | 1 | 100.000% | 82.617% | 0.085% | 0.082% 至 +∞ | 0.085% | 0.041% 至 0.085% | 0.082% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | unconditional | 95 | 0.80 | 1 | 1.00 | 1 | 100.000% | 44.013% | 0.138% | 0.033% 至 +∞ | 0.199% | 0.033% 至 0.199% | 0.033% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | unconditional | 99 | 0.16 | 1 | 1.00 | 1 | 100.000% | 85.146% | 0.187% | 0.118% 至 +∞ | 0.199% | 0.033% 至 0.199% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | traded | 95 | 0.35 | 1 | 1.00 | 1 | 100.000% | 69.834% | 0.174% | 0.076% 至 +∞ | 0.199% | 0.076% 至 0.199% | 0.076% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | traded | 99 | 0.07 | 1 | 1.00 | 1 | 100.000% | 93.207% | 0.194% | 0.118% 至 +∞ | 0.199% | 0.076% 至 0.199% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | ever_inventory | 95 | 0.35 | 1 | 1.00 | 1 | 100.000% | 69.834% | 0.174% | 0.076% 至 +∞ | 0.199% | 0.076% 至 0.199% | 0.076% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | ever_inventory | 99 | 0.07 | 1 | 1.00 | 1 | 100.000% | 93.207% | 0.194% | 0.118% 至 +∞ | 0.199% | 0.076% 至 0.199% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | terminal_inventory | 95 | 0.30 | 1 | 1.00 | 1 | 100.000% | 73.509% | 0.178% | 0.076% 至 +∞ | 0.199% | 0.076% 至 0.199% | 0.076% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 1729 | terminal_inventory | 99 | 0.06 | 1 | 1.00 | 1 | 100.000% | 94.148% | 0.195% | 0.118% 至 +∞ | 0.199% | 0.076% 至 0.199% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | unconditional | 95 | 0.80 | 1 | 1.00 | 1 | 100.000% | 44.013% | 0.106% | 0.040% 至 +∞ | 0.220% | 0.040% 至 0.220% | 0.040% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | unconditional | 99 | 0.16 | 1 | 1.00 | 1 | 100.000% | 85.146% | 0.197% | 0.068% 至 +∞ | 0.220% | 0.040% 至 0.220% | 0.068% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | traded | 95 | 0.35 | 1 | 1.00 | 1 | 100.000% | 69.834% | 0.174% | 0.046% 至 +∞ | 0.220% | 0.046% 至 0.220% | 0.046% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | traded | 99 | 0.07 | 1 | 1.00 | 1 | 100.000% | 93.207% | 0.211% | 0.068% 至 +∞ | 0.220% | 0.046% 至 0.220% | 0.068% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | ever_inventory | 95 | 0.35 | 1 | 1.00 | 1 | 100.000% | 69.834% | 0.174% | 0.046% 至 +∞ | 0.220% | 0.046% 至 0.220% | 0.046% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | ever_inventory | 99 | 0.07 | 1 | 1.00 | 1 | 100.000% | 93.207% | 0.211% | 0.068% 至 +∞ | 0.220% | 0.046% 至 0.220% | 0.068% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | terminal_inventory | 95 | 0.35 | 1 | 1.00 | 1 | 100.000% | 69.834% | 0.174% | 0.046% 至 +∞ | 0.220% | 0.046% 至 0.220% | 0.046% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 2718 | terminal_inventory | 99 | 0.07 | 1 | 1.00 | 1 | 100.000% | 93.207% | 0.211% | 0.068% 至 +∞ | 0.220% | 0.046% 至 0.220% | 0.068% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | unconditional | 95 | 0.80 | 1 | 1.00 | 1 | 100.000% | 44.013% | 0.098% | 0.018% 至 +∞ | 0.100% | 0.018% 至 0.100% | 0.018% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | unconditional | 99 | 0.16 | 1 | 1.00 | 1 | 100.000% | 85.146% | 0.100% | 0.098% 至 +∞ | 0.100% | 0.018% 至 0.100% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | traded | 95 | 0.55 | 1 | 1.00 | 1 | 100.000% | 56.880% | 0.099% | 0.053% 至 +∞ | 0.100% | 0.018% 至 0.100% | 0.053% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | traded | 99 | 0.11 | 1 | 1.00 | 1 | 100.000% | 89.534% | 0.100% | 0.098% 至 +∞ | 0.100% | 0.018% 至 0.100% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | ever_inventory | 95 | 0.55 | 1 | 1.00 | 1 | 100.000% | 56.880% | 0.099% | 0.053% 至 +∞ | 0.100% | 0.018% 至 0.100% | 0.053% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | ever_inventory | 99 | 0.11 | 1 | 1.00 | 1 | 100.000% | 89.534% | 0.100% | 0.098% 至 +∞ | 0.100% | 0.018% 至 0.100% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | terminal_inventory | 95 | 0.50 | 1 | 1.00 | 1 | 100.000% | 59.874% | 0.099% | 0.053% 至 +∞ | 0.100% | 0.018% 至 0.100% | 0.053% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 31415 | terminal_inventory | 99 | 0.10 | 1 | 1.00 | 1 | 100.000% | 90.438% | 0.100% | 0.098% 至 +∞ | 0.100% | 0.018% 至 0.100% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | unconditional | 95 | 1.60 | 2 | 1.88 | 2 | 62.500% | 19.371% | 0.134% | 0.091% 至 +∞ | 0.182% | 0.108% 至 0.199% | 0.091% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | unconditional | 99 | 0.32 | 1 | 1.00 | 1 | 100.000% | 72.498% | 0.184% | 0.118% 至 +∞ | 0.199% | 0.118% 至 0.199% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | traded | 95 | 0.70 | 1 | 1.00 | 1 | 100.000% | 48.767% | 0.169% | 0.118% 至 +∞ | 0.199% | 0.118% 至 0.199% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | traded | 99 | 0.14 | 1 | 1.00 | 1 | 100.000% | 86.875% | 0.193% | 0.153% 至 +∞ | 0.199% | 0.118% 至 0.199% | 0.153% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | ever_inventory | 95 | 0.70 | 1 | 1.00 | 1 | 100.000% | 48.767% | 0.169% | 0.118% 至 +∞ | 0.199% | 0.118% 至 0.199% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | ever_inventory | 99 | 0.14 | 1 | 1.00 | 1 | 100.000% | 86.875% | 0.193% | 0.153% 至 +∞ | 0.199% | 0.118% 至 0.199% | 0.153% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | terminal_inventory | 95 | 0.65 | 1 | 1.00 | 1 | 100.000% | 51.334% | 0.171% | 0.118% 至 +∞ | 0.199% | 0.118% 至 0.199% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 1729 | terminal_inventory | 99 | 0.13 | 1 | 1.00 | 1 | 100.000% | 87.752% | 0.193% | 0.153% 至 +∞ | 0.199% | 0.118% 至 0.199% | 0.153% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | unconditional | 95 | 1.60 | 2 | 1.88 | 2 | 62.500% | 19.371% | 0.130% | 0.068% 至 +∞ | 0.211% | 0.064% 至 0.220% | 0.068% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | unconditional | 99 | 0.32 | 1 | 1.00 | 1 | 100.000% | 72.498% | 0.213% | 0.074% 至 +∞ | 0.220% | 0.071% 至 0.220% | 0.074% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | traded | 95 | 0.65 | 1 | 1.00 | 1 | 100.000% | 51.334% | 0.207% | 0.074% 至 +∞ | 0.220% | 0.074% 至 0.220% | 0.074% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | traded | 99 | 0.13 | 1 | 1.00 | 1 | 100.000% | 87.752% | 0.217% | 0.198% 至 +∞ | 0.220% | 0.074% 至 0.220% | 0.198% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | ever_inventory | 95 | 0.65 | 1 | 1.00 | 1 | 100.000% | 51.334% | 0.207% | 0.074% 至 +∞ | 0.220% | 0.074% 至 0.220% | 0.074% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | ever_inventory | 99 | 0.13 | 1 | 1.00 | 1 | 100.000% | 87.752% | 0.217% | 0.198% 至 +∞ | 0.220% | 0.074% 至 0.220% | 0.198% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | terminal_inventory | 95 | 0.65 | 1 | 1.00 | 1 | 100.000% | 51.334% | 0.207% | 0.074% 至 +∞ | 0.220% | 0.074% 至 0.220% | 0.074% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 2718 | terminal_inventory | 99 | 0.13 | 1 | 1.00 | 1 | 100.000% | 87.752% | 0.217% | 0.198% 至 +∞ | 0.220% | 0.074% 至 0.220% | 0.198% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | unconditional | 95 | 1.60 | 2 | 1.88 | 2 | 62.500% | 19.371% | 0.104% | 0.054% 至 +∞ | 0.158% | 0.082% 至 0.187% | 0.054% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | unconditional | 99 | 0.32 | 1 | 1.00 | 1 | 100.000% | 72.498% | 0.163% | 0.100% 至 +∞ | 0.187% | 0.098% 至 0.187% | 0.100% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | traded | 95 | 1.00 | 1 | 1.00 | 1 | 100.000% | 35.849% | 0.113% | 0.098% 至 +∞ | 0.187% | 0.098% 至 0.187% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | traded | 99 | 0.20 | 1 | 1.00 | 1 | 100.000% | 81.791% | 0.172% | 0.109% 至 +∞ | 0.187% | 0.098% 至 0.187% | 0.109% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | ever_inventory | 95 | 1.00 | 1 | 1.00 | 1 | 100.000% | 35.849% | 0.113% | 0.098% 至 +∞ | 0.187% | 0.098% 至 0.187% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | ever_inventory | 99 | 0.20 | 1 | 1.00 | 1 | 100.000% | 81.791% | 0.172% | 0.109% 至 +∞ | 0.187% | 0.098% 至 0.187% | 0.109% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | terminal_inventory | 95 | 0.90 | 1 | 1.00 | 1 | 100.000% | 39.721% | 0.120% | 0.098% 至 +∞ | 0.187% | 0.098% 至 0.187% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 31415 | terminal_inventory | 99 | 0.18 | 1 | 1.00 | 1 | 100.000% | 83.451% | 0.174% | 0.109% 至 +∞ | 0.187% | 0.098% 至 0.187% | 0.109% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | unconditional | 95 | 3.20 | 4 | 3.37 | 4 | 31.250% | 3.752% | 0.121% | 0.106% 至 +∞ | 0.156% | 0.118% 至 0.196% | 0.106% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | unconditional | 99 | 0.64 | 1 | 1.00 | 1 | 100.000% | 52.560% | 0.170% | 0.121% 至 +∞ | 0.199% | 0.121% 至 0.199% | 0.121% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | traded | 95 | 1.55 | 2 | 1.84 | 2 | 64.516% | 20.391% | 0.137% | 0.118% 至 +∞ | 0.182% | 0.120% 至 0.199% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | traded | 99 | 0.31 | 1 | 1.00 | 1 | 100.000% | 73.230% | 0.185% | 0.122% 至 +∞ | 0.199% | 0.121% 至 0.199% | 0.122% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | ever_inventory | 95 | 1.55 | 2 | 1.84 | 2 | 64.516% | 20.391% | 0.137% | 0.118% 至 +∞ | 0.182% | 0.120% 至 0.199% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | ever_inventory | 99 | 0.31 | 1 | 1.00 | 1 | 100.000% | 73.230% | 0.185% | 0.122% 至 +∞ | 0.199% | 0.121% 至 0.199% | 0.122% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | terminal_inventory | 95 | 1.45 | 2 | 1.75 | 2 | 68.966% | 22.594% | 0.140% | 0.118% 至 +∞ | 0.184% | 0.120% 至 0.199% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 1729 | terminal_inventory | 99 | 0.29 | 1 | 1.00 | 1 | 100.000% | 74.717% | 0.186% | 0.122% 至 +∞ | 0.199% | 0.121% 至 0.199% | 0.122% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | unconditional | 95 | 3.20 | 4 | 3.37 | 4 | 31.250% | 3.752% | 0.196% | 0.068% 至 +∞ | 0.372% | 0.119% 至 0.683% | 0.068% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | unconditional | 99 | 0.64 | 1 | 1.00 | 1 | 100.000% | 52.560% | 0.402% | 0.198% 至 +∞ | 0.714% | 0.198% 至 0.714% | 0.198% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | traded | 95 | 1.50 | 2 | 1.80 | 2 | 66.667% | 21.464% | 0.218% | 0.188% 至 +∞ | 0.549% | 0.195% 至 0.714% | 0.188% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | traded | 99 | 0.30 | 1 | 1.00 | 1 | 100.000% | 73.970% | 0.571% | 0.216% 至 +∞ | 0.714% | 0.198% 至 0.714% | 0.216% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | ever_inventory | 95 | 1.50 | 2 | 1.80 | 2 | 66.667% | 21.464% | 0.218% | 0.188% 至 +∞ | 0.549% | 0.195% 至 0.714% | 0.188% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | ever_inventory | 99 | 0.30 | 1 | 1.00 | 1 | 100.000% | 73.970% | 0.571% | 0.216% 至 +∞ | 0.714% | 0.198% 至 0.714% | 0.216% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | terminal_inventory | 95 | 1.45 | 2 | 1.75 | 2 | 68.966% | 22.594% | 0.218% | 0.188% 至 +∞ | 0.560% | 0.188% 至 0.714% | 0.188% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 2718 | terminal_inventory | 99 | 0.29 | 1 | 1.00 | 1 | 100.000% | 74.717% | 0.575% | 0.216% 至 +∞ | 0.714% | 0.198% 至 0.714% | 0.216% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | unconditional | 95 | 3.20 | 4 | 3.37 | 4 | 31.250% | 3.752% | 0.100% | 0.053% 至 +∞ | 0.140% | 0.086% 至 0.184% | 0.053% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | unconditional | 99 | 0.64 | 1 | 1.00 | 1 | 100.000% | 52.560% | 0.152% | 0.100% 至 +∞ | 0.187% | 0.100% 至 0.187% | 0.100% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | traded | 95 | 1.70 | 2 | 1.94 | 2 | 58.824% | 17.482% | 0.117% | 0.086% 至 +∞ | 0.164% | 0.099% 至 0.187% | 0.086% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | traded | 99 | 0.34 | 1 | 1.00 | 1 | 100.000% | 71.055% | 0.169% | 0.109% 至 +∞ | 0.187% | 0.100% 至 0.187% | 0.109% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | ever_inventory | 95 | 1.70 | 2 | 1.94 | 2 | 58.824% | 17.482% | 0.117% | 0.086% 至 +∞ | 0.164% | 0.099% 至 0.187% | 0.086% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | ever_inventory | 99 | 0.34 | 1 | 1.00 | 1 | 100.000% | 71.055% | 0.169% | 0.109% 至 +∞ | 0.187% | 0.100% 至 0.187% | 0.109% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | terminal_inventory | 95 | 1.55 | 2 | 1.84 | 2 | 64.516% | 20.391% | 0.120% | 0.098% 至 +∞ | 0.168% | 0.100% 至 0.187% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 31415 | terminal_inventory | 99 | 0.31 | 1 | 1.00 | 1 | 100.000% | 73.230% | 0.171% | 0.109% 至 +∞ | 0.187% | 0.100% 至 0.187% | 0.109% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | unconditional | 95 | 2.40 | 3 | 2.67 | 3 | 41.667% | 8.526% | 1.428% | 0.931% 至 +∞ | 1.486% | 1.145% 至 1.553% | 0.931% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | unconditional | 99 | 0.48 | 1 | 1.00 | 1 | 100.000% | 61.729% | 1.501% | 1.430% 至 +∞ | 1.553% | 1.425% 至 1.553% | 1.430% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | traded | 95 | 1.85 | 2 | 1.99 | 2 | 54.054% | 14.989% | 1.433% | 0.945% 至 +∞ | 1.502% | 1.204% 至 1.553% | 0.945% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | traded | 99 | 0.37 | 1 | 1.00 | 1 | 100.000% | 68.945% | 1.513% | 1.430% 至 +∞ | 1.553% | 1.425% 至 1.553% | 1.430% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | ever_inventory | 95 | 1.85 | 2 | 1.99 | 2 | 54.054% | 14.989% | 1.433% | 0.945% 至 +∞ | 1.502% | 1.204% 至 1.553% | 0.945% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | ever_inventory | 99 | 0.37 | 1 | 1.00 | 1 | 100.000% | 68.945% | 1.513% | 1.430% 至 +∞ | 1.553% | 1.425% 至 1.553% | 1.430% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | terminal_inventory | 95 | 1.75 | 2 | 1.96 | 2 | 57.143% | 16.608% | 1.434% | 0.945% 至 +∞ | 1.505% | 1.223% 至 1.553% | 0.945% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 16 | 同模型三种子 | terminal_inventory | 99 | 0.35 | 1 | 1.00 | 1 | 100.000% | 70.345% | 1.515% | 1.430% 至 +∞ | 1.553% | 1.425% 至 1.553% | 1.430% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | unconditional | 95 | 4.80 | 5 | 4.97 | 5 | 20.833% | 0.727% | 1.442% | 1.406% 至 2.653% | 1.754% | 1.443% 至 2.468% | 1.406% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | unconditional | 99 | 0.96 | 1 | 1.00 | 1 | 100.000% | 38.105% | 1.608% | 1.514% 至 +∞ | 2.653% | 1.514% 至 2.653% | 1.514% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | traded | 95 | 3.65 | 4 | 3.89 | 4 | 27.397% | 2.365% | 1.472% | 1.408% 至 2.653% | 1.845% | 1.458% 至 2.457% | 1.408% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | traded | 99 | 0.73 | 1 | 1.00 | 1 | 100.000% | 48.014% | 1.861% | 1.514% 至 +∞ | 2.653% | 1.514% 至 2.653% | 1.514% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | ever_inventory | 95 | 3.65 | 4 | 3.89 | 4 | 27.397% | 2.365% | 1.472% | 1.408% 至 2.653% | 1.845% | 1.458% 至 2.457% | 1.408% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | ever_inventory | 99 | 0.73 | 1 | 1.00 | 1 | 100.000% | 48.014% | 1.861% | 1.514% 至 +∞ | 2.653% | 1.514% 至 2.653% | 1.514% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | terminal_inventory | 95 | 3.45 | 4 | 3.72 | 4 | 28.986% | 2.904% | 1.486% | 1.425% 至 +∞ | 1.864% | 1.459% 至 2.517% | 1.425% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 32 | 同模型三种子 | terminal_inventory | 99 | 0.69 | 1 | 1.00 | 1 | 100.000% | 49.984% | 1.905% | 1.514% 至 +∞ | 2.653% | 1.514% 至 2.653% | 1.514% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | unconditional | 95 | 9.60 | 10 | 9.85 | 10 | 10.417% | 0.005% | 1.442% | 1.406% 至 1.553% | 1.790% | 1.481% 至 2.189% | 1.406% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | unconditional | 99 | 1.92 | 2 | 2.00 | 2 | 52.083% | 14.520% | 2.287% | 1.514% 至 +∞ | 2.519% | 1.548% 至 2.653% | 1.514% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | traded | 95 | 7.60 | 8 | 7.85 | 8 | 13.158% | 0.041% | 1.446% | 1.423% 至 2.278% | 1.881% | 1.495% 至 2.342% | 1.423% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | traded | 99 | 1.52 | 2 | 1.82 | 2 | 65.789% | 21.704% | 2.325% | 1.543% 至 +∞ | 2.557% | 1.550% 至 2.653% | 1.543% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | ever_inventory | 95 | 7.60 | 8 | 7.85 | 8 | 13.158% | 0.041% | 1.446% | 1.423% 至 2.278% | 1.881% | 1.495% 至 2.342% | 1.423% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | ever_inventory | 99 | 1.52 | 2 | 1.82 | 2 | 65.789% | 21.704% | 2.325% | 1.543% 至 +∞ | 2.557% | 1.550% 至 2.653% | 1.543% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | terminal_inventory | 95 | 7.05 | 8 | 7.10 | 8 | 14.184% | 0.072% | 1.448% | 1.425% 至 2.373% | 1.915% | 1.488% 至 2.383% | 1.425% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| jump_down | 64 | 同模型三种子 | terminal_inventory | 99 | 1.41 | 2 | 1.70 | 2 | 70.922% | 24.242% | 2.335% | 1.543% 至 +∞ | 2.572% | 1.542% 至 2.653% | 1.543% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | unconditional | 95 | 2.40 | 3 | 2.67 | 3 | 41.667% | 8.526% | 0.055% | 0.020% 至 +∞ | 0.150% | 0.036% 至 0.258% | 0.020% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | unconditional | 99 | 0.48 | 1 | 1.00 | 1 | 100.000% | 61.729% | 0.173% | 0.063% 至 +∞ | 0.258% | 0.041% 至 0.258% | 0.063% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | traded | 95 | 1.05 | 2 | 1.10 | 2 | 95.238% | 34.056% | 0.077% | 0.041% 至 +∞ | 0.249% | 0.041% 至 0.258% | 0.041% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | traded | 99 | 0.21 | 1 | 1.00 | 1 | 100.000% | 80.973% | 0.222% | 0.077% 至 +∞ | 0.258% | 0.041% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | ever_inventory | 95 | 1.05 | 2 | 1.10 | 2 | 95.238% | 34.056% | 0.077% | 0.041% 至 +∞ | 0.249% | 0.041% 至 0.258% | 0.041% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | ever_inventory | 99 | 0.21 | 1 | 1.00 | 1 | 100.000% | 80.973% | 0.222% | 0.077% 至 +∞ | 0.258% | 0.041% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | terminal_inventory | 95 | 0.90 | 1 | 1.00 | 1 | 100.000% | 39.721% | 0.105% | 0.041% 至 +∞ | 0.258% | 0.041% 至 0.258% | 0.041% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 16 | 同模型三种子 | terminal_inventory | 99 | 0.18 | 1 | 1.00 | 1 | 100.000% | 83.451% | 0.227% | 0.077% 至 +∞ | 0.258% | 0.041% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | unconditional | 95 | 4.80 | 5 | 4.97 | 5 | 20.833% | 0.727% | 0.079% | 0.037% 至 0.258% | 0.124% | 0.065% 至 0.196% | 0.037% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | unconditional | 99 | 0.96 | 1 | 1.00 | 1 | 100.000% | 38.105% | 0.101% | 0.085% 至 +∞ | 0.258% | 0.085% 至 0.258% | 0.085% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | traded | 95 | 2.25 | 3 | 2.45 | 3 | 44.444% | 9.944% | 0.090% | 0.077% 至 +∞ | 0.166% | 0.084% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | traded | 99 | 0.45 | 1 | 1.00 | 1 | 100.000% | 63.619% | 0.185% | 0.092% 至 +∞ | 0.258% | 0.085% 至 0.258% | 0.092% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | ever_inventory | 95 | 2.25 | 3 | 2.45 | 3 | 44.444% | 9.944% | 0.090% | 0.077% 至 +∞ | 0.166% | 0.084% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | ever_inventory | 99 | 0.45 | 1 | 1.00 | 1 | 100.000% | 63.619% | 0.185% | 0.092% 至 +∞ | 0.258% | 0.085% 至 0.258% | 0.092% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | terminal_inventory | 95 | 1.85 | 2 | 1.99 | 2 | 54.054% | 14.989% | 0.092% | 0.077% 至 +∞ | 0.182% | 0.085% 至 0.258% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 32 | 同模型三种子 | terminal_inventory | 99 | 0.37 | 1 | 1.00 | 1 | 100.000% | 68.945% | 0.198% | 0.092% 至 +∞ | 0.258% | 0.085% 至 0.258% | 0.092% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | unconditional | 95 | 9.60 | 10 | 9.85 | 10 | 10.417% | 0.005% | 0.074% | 0.047% 至 0.098% | 0.122% | 0.077% 至 0.167% | 0.047% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | unconditional | 99 | 1.92 | 2 | 2.00 | 2 | 52.083% | 14.520% | 0.167% | 0.092% 至 +∞ | 0.214% | 0.095% 至 0.258% | 0.092% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | traded | 95 | 4.45 | 5 | 4.71 | 5 | 22.472% | 1.041% | 0.092% | 0.077% 至 0.258% | 0.164% | 0.091% 至 0.228% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | traded | 99 | 0.89 | 1 | 1.00 | 1 | 100.000% | 40.882% | 0.178% | 0.098% 至 +∞ | 0.258% | 0.098% 至 0.258% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | ever_inventory | 95 | 4.45 | 5 | 4.71 | 5 | 22.472% | 1.041% | 0.092% | 0.077% 至 0.258% | 0.164% | 0.091% 至 0.228% | 0.077% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | ever_inventory | 99 | 0.89 | 1 | 1.00 | 1 | 100.000% | 40.882% | 0.178% | 0.098% 至 +∞ | 0.258% | 0.098% 至 0.258% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | terminal_inventory | 95 | 3.45 | 4 | 3.72 | 4 | 28.986% | 2.904% | 0.096% | 0.085% 至 +∞ | 0.184% | 0.091% 至 0.246% | 0.085% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| regime_historical | 64 | 同模型三种子 | terminal_inventory | 99 | 0.69 | 1 | 1.00 | 1 | 100.000% | 49.984% | 0.196% | 0.098% 至 +∞ | 0.258% | 0.098% 至 0.258% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | unconditional | 95 | 2.40 | 3 | 2.67 | 3 | 41.667% | 8.526% | 0.112% | 0.068% 至 +∞ | 0.194% | 0.089% 至 0.220% | 0.068% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | unconditional | 99 | 0.48 | 1 | 1.00 | 1 | 100.000% | 61.729% | 0.210% | 0.118% 至 +∞ | 0.220% | 0.100% 至 0.220% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | traded | 95 | 1.25 | 2 | 1.47 | 2 | 80.000% | 27.739% | 0.183% | 0.098% 至 +∞ | 0.215% | 0.100% 至 0.220% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | traded | 99 | 0.25 | 1 | 1.00 | 1 | 100.000% | 77.782% | 0.215% | 0.118% 至 +∞ | 0.220% | 0.100% 至 0.220% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | ever_inventory | 95 | 1.25 | 2 | 1.47 | 2 | 80.000% | 27.739% | 0.183% | 0.098% 至 +∞ | 0.215% | 0.100% 至 0.220% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | ever_inventory | 99 | 0.25 | 1 | 1.00 | 1 | 100.000% | 77.782% | 0.215% | 0.118% 至 +∞ | 0.220% | 0.100% 至 0.220% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | terminal_inventory | 95 | 1.15 | 2 | 1.29 | 2 | 86.957% | 30.736% | 0.191% | 0.098% 至 +∞ | 0.217% | 0.100% 至 0.220% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 16 | 同模型三种子 | terminal_inventory | 99 | 0.23 | 1 | 1.00 | 1 | 100.000% | 79.361% | 0.215% | 0.199% 至 +∞ | 0.220% | 0.100% 至 0.220% | 0.199% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | unconditional | 95 | 4.80 | 5 | 4.97 | 5 | 20.833% | 0.727% | 0.127% | 0.098% 至 0.220% | 0.193% | 0.130% 至 0.212% | 0.098% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | unconditional | 99 | 0.96 | 1 | 1.00 | 1 | 100.000% | 38.105% | 0.200% | 0.187% 至 +∞ | 0.220% | 0.187% 至 0.220% | 0.187% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | traded | 95 | 2.35 | 3 | 2.60 | 3 | 42.553% | 8.974% | 0.195% | 0.118% 至 +∞ | 0.207% | 0.152% 至 0.220% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | traded | 99 | 0.47 | 1 | 1.00 | 1 | 100.000% | 62.353% | 0.210% | 0.198% 至 +∞ | 0.220% | 0.187% 至 0.220% | 0.198% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | ever_inventory | 95 | 2.35 | 3 | 2.60 | 3 | 42.553% | 8.974% | 0.195% | 0.118% 至 +∞ | 0.207% | 0.152% 至 0.220% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | ever_inventory | 99 | 0.47 | 1 | 1.00 | 1 | 100.000% | 62.353% | 0.210% | 0.198% 至 +∞ | 0.220% | 0.187% 至 0.220% | 0.198% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | terminal_inventory | 95 | 2.20 | 3 | 2.37 | 3 | 45.455% | 10.467% | 0.196% | 0.118% 至 +∞ | 0.208% | 0.152% 至 0.220% | 0.118% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 32 | 同模型三种子 | terminal_inventory | 99 | 0.44 | 1 | 1.00 | 1 | 100.000% | 64.261% | 0.211% | 0.198% 至 +∞ | 0.220% | 0.187% 至 0.220% | 0.198% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | unconditional | 95 | 9.60 | 10 | 9.85 | 10 | 10.417% | 0.005% | 0.121% | 0.100% 至 0.199% | 0.237% | 0.146% 至 0.395% | 0.100% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | unconditional | 99 | 1.92 | 2 | 2.00 | 2 | 52.083% | 14.520% | 0.216% | 0.188% 至 +∞ | 0.477% | 0.198% 至 0.714% | 0.188% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | traded | 95 | 4.75 | 5 | 4.95 | 5 | 21.053% | 0.765% | 0.191% | 0.122% 至 0.714% | 0.315% | 0.176% 至 0.532% | 0.122% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | traded | 99 | 0.95 | 1 | 1.00 | 1 | 100.000% | 38.490% | 0.249% | 0.199% 至 +∞ | 0.714% | 0.199% 至 0.714% | 0.199% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | ever_inventory | 95 | 4.75 | 5 | 4.95 | 5 | 21.053% | 0.765% | 0.191% | 0.122% 至 0.714% | 0.315% | 0.176% 至 0.532% | 0.122% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | ever_inventory | 99 | 0.95 | 1 | 1.00 | 1 | 100.000% | 38.490% | 0.249% | 0.199% 至 +∞ | 0.714% | 0.199% 至 0.714% | 0.199% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | terminal_inventory | 95 | 4.45 | 5 | 4.71 | 5 | 22.472% | 1.041% | 0.194% | 0.122% 至 0.714% | 0.323% | 0.175% 至 0.553% | 0.122% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |
| moving_block_bootstrap | 64 | 同模型三种子 | terminal_inventory | 99 | 0.89 | 1 | 1.00 | 1 | 100.000% | 40.882% | 0.279% | 0.199% 至 +∞ | 0.714% | 0.199% 至 0.714% | 0.199% 至 +∞ | INSUFFICIENT_TAIL_SUPPORT |

## 相邻档与种子之间的差异

相邻档差值按同一模型、同一种子计算，较大档位包含较小档位的路径。条件组新增样本数仅统计新增前缀中符合条件的账户。下面的差值和极差是点估计，单位为百分点。

| 模型 | 种子 | 条件 | 档位 | 原样本+新增 | ΔVaR95 | ΔCVaR95 | ΔVaR99 | ΔCVaR99 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| jump_down | 1729 | unconditional | 16 → 32 | 16 + 16 | 0.636 pp | 0.446 pp | 0.501 pp | 0.457 pp |
| jump_down | 1729 | traded | 16 → 32 | 13 + 13 | 0.614 pp | 0.450 pp | 0.492 pp | 0.457 pp |
| jump_down | 1729 | ever_inventory | 16 → 32 | 13 + 13 | 0.614 pp | 0.450 pp | 0.492 pp | 0.457 pp |
| jump_down | 1729 | terminal_inventory | 16 → 32 | 12 + 12 | 0.607 pp | 0.452 pp | 0.489 pp | 0.457 pp |
| jump_down | 2718 | unconditional | 16 → 32 | 16 + 16 | 0.026 pp | 0.688 pp | 0.778 pp | 1.100 pp |
| jump_down | 2718 | traded | 16 → 32 | 13 + 12 | 0.051 pp | 0.880 pp | 0.852 pp | 1.100 pp |
| jump_down | 2718 | ever_inventory | 16 → 32 | 13 + 12 | 0.051 pp | 0.880 pp | 0.852 pp | 1.100 pp |
| jump_down | 2718 | terminal_inventory | 16 → 32 | 12 + 11 | 0.058 pp | 0.957 pp | 0.872 pp | 1.100 pp |
| jump_down | 31415 | unconditional | 16 → 32 | 16 + 16 | 0.002 pp | 0.002 pp | 0.004 pp | 0.003 pp |
| jump_down | 31415 | traded | 16 → 32 | 11 + 11 | 0.005 pp | 0.003 pp | 0.003 pp | 0.003 pp |
| jump_down | 31415 | ever_inventory | 16 → 32 | 11 + 11 | 0.005 pp | 0.003 pp | 0.003 pp | 0.003 pp |
| jump_down | 31415 | terminal_inventory | 16 → 32 | 11 + 11 | 0.005 pp | 0.003 pp | 0.003 pp | 0.003 pp |
| jump_down | 1729 | unconditional | 32 → 64 | 32 + 32 | 0.043 pp | 0.498 pp | 0.779 pp | 0.830 pp |
| jump_down | 1729 | traded | 32 → 64 | 26 + 27 | 0.039 pp | 0.597 pp | 0.788 pp | 0.830 pp |
| jump_down | 1729 | ever_inventory | 32 → 64 | 26 + 27 | 0.039 pp | 0.597 pp | 0.788 pp | 0.830 pp |
| jump_down | 1729 | terminal_inventory | 32 → 64 | 24 + 25 | 0.034 pp | 0.644 pp | 0.791 pp | 0.830 pp |
| jump_down | 2718 | unconditional | 32 → 64 | 32 + 32 | -0.059 pp | -0.385 pp | -0.352 pp | 0.000 pp |
| jump_down | 2718 | traded | 32 → 64 | 25 + 27 | -0.093 pp | -0.481 pp | -0.297 pp | 0.000 pp |
| jump_down | 2718 | ever_inventory | 32 → 64 | 25 + 27 | -0.093 pp | -0.481 pp | -0.297 pp | 0.000 pp |
| jump_down | 2718 | terminal_inventory | 32 → 64 | 23 + 25 | -0.101 pp | -0.516 pp | -0.275 pp | -0.000 pp |
| jump_down | 31415 | unconditional | 32 → 64 | 32 + 32 | 0.004 pp | 0.022 pp | 0.026 pp | 0.069 pp |
| jump_down | 31415 | traded | 32 → 64 | 22 + 25 | 0.001 pp | 0.029 pp | 0.038 pp | 0.069 pp |
| jump_down | 31415 | ever_inventory | 32 → 64 | 22 + 25 | 0.001 pp | 0.029 pp | 0.038 pp | 0.069 pp |
| jump_down | 31415 | terminal_inventory | 32 → 64 | 22 + 22 | 0.001 pp | 0.031 pp | 0.040 pp | 0.069 pp |
| regime_historical | 1729 | unconditional | 16 → 32 | 16 + 16 | -0.034 pp | -0.062 pp | -0.024 pp | 0.000 pp |
| regime_historical | 1729 | traded | 16 → 32 | 9 + 9 | -0.068 pp | 0.000 pp | -0.014 pp | 0.000 pp |
| regime_historical | 1729 | ever_inventory | 16 → 32 | 9 + 9 | -0.068 pp | 0.000 pp | -0.014 pp | 0.000 pp |
| regime_historical | 1729 | terminal_inventory | 16 → 32 | 8 + 7 | -0.053 pp | 0.000 pp | -0.011 pp | 0.000 pp |
| regime_historical | 2718 | unconditional | 16 → 32 | 16 + 16 | 0.003 pp | 0.034 pp | 0.040 pp | 0.055 pp |
| regime_historical | 2718 | traded | 16 → 32 | 5 + 8 | 0.025 pp | 0.055 pp | 0.049 pp | 0.055 pp |
| regime_historical | 2718 | ever_inventory | 16 → 32 | 5 + 8 | 0.025 pp | 0.055 pp | 0.049 pp | 0.055 pp |
| regime_historical | 2718 | terminal_inventory | 16 → 32 | 4 + 7 | 0.030 pp | 0.055 pp | 0.050 pp | 0.055 pp |
| regime_historical | 31415 | unconditional | 16 → 32 | 16 + 16 | 0.002 pp | 0.027 pp | 0.032 pp | 0.044 pp |
| regime_historical | 31415 | traded | 16 → 32 | 7 + 7 | 0.019 pp | 0.044 pp | 0.039 pp | 0.044 pp |
| regime_historical | 31415 | ever_inventory | 16 → 32 | 7 + 7 | 0.019 pp | 0.044 pp | 0.039 pp | 0.044 pp |
| regime_historical | 31415 | terminal_inventory | 16 → 32 | 6 + 5 | 0.025 pp | 0.044 pp | 0.040 pp | 0.044 pp |
| regime_historical | 1729 | unconditional | 32 → 64 | 32 + 32 | 0.008 pp | -0.005 pp | -0.006 pp | 0.000 pp |
| regime_historical | 1729 | traded | 32 → 64 | 18 + 16 | 0.050 pp | -0.037 pp | -0.002 pp | 0.000 pp |
| regime_historical | 1729 | ever_inventory | 32 → 64 | 18 + 16 | 0.050 pp | -0.037 pp | -0.002 pp | 0.000 pp |
| regime_historical | 1729 | terminal_inventory | 32 → 64 | 15 + 12 | 0.025 pp | -0.024 pp | -0.000 pp | 0.000 pp |
| regime_historical | 2718 | unconditional | 32 → 64 | 32 + 32 | 0.015 pp | -0.003 pp | 0.003 pp | 0.000 pp |
| regime_historical | 2718 | traded | 32 → 64 | 13 + 18 | -0.000 pp | -0.008 pp | 0.000 pp | 0.000 pp |
| regime_historical | 2718 | ever_inventory | 32 → 64 | 13 + 18 | -0.000 pp | -0.008 pp | 0.000 pp | 0.000 pp |
| regime_historical | 2718 | terminal_inventory | 32 → 64 | 11 + 12 | 0.003 pp | -0.003 pp | 0.001 pp | 0.000 pp |
| regime_historical | 31415 | unconditional | 32 → 64 | 32 + 32 | 0.004 pp | 0.001 pp | 0.011 pp | 0.000 pp |
| regime_historical | 31415 | traded | 32 → 64 | 14 + 10 | 0.020 pp | -0.001 pp | 0.005 pp | 0.000 pp |
| regime_historical | 31415 | ever_inventory | 32 → 64 | 14 + 10 | 0.020 pp | -0.001 pp | 0.005 pp | 0.000 pp |
| regime_historical | 31415 | terminal_inventory | 32 → 64 | 11 + 8 | 0.019 pp | 0.000 pp | 0.004 pp | 0.000 pp |
| moving_block_bootstrap | 1729 | unconditional | 16 → 32 | 16 + 16 | -0.004 pp | -0.017 pp | -0.002 pp | 0.000 pp |
| moving_block_bootstrap | 1729 | traded | 16 → 32 | 7 + 7 | -0.006 pp | 0.000 pp | -0.001 pp | 0.000 pp |
| moving_block_bootstrap | 1729 | ever_inventory | 16 → 32 | 7 + 7 | -0.006 pp | 0.000 pp | -0.001 pp | 0.000 pp |
| moving_block_bootstrap | 1729 | terminal_inventory | 16 → 32 | 6 + 7 | -0.007 pp | 0.000 pp | -0.001 pp | 0.000 pp |
| moving_block_bootstrap | 2718 | unconditional | 16 → 32 | 16 + 16 | 0.024 pp | -0.008 pp | 0.016 pp | 0.000 pp |
| moving_block_bootstrap | 2718 | traded | 16 → 32 | 7 + 6 | 0.032 pp | 0.000 pp | 0.006 pp | 0.000 pp |
| moving_block_bootstrap | 2718 | ever_inventory | 16 → 32 | 7 + 6 | 0.032 pp | 0.000 pp | 0.006 pp | 0.000 pp |
| moving_block_bootstrap | 2718 | terminal_inventory | 16 → 32 | 7 + 6 | 0.032 pp | 0.000 pp | 0.006 pp | 0.000 pp |
| moving_block_bootstrap | 31415 | unconditional | 16 → 32 | 16 + 16 | 0.006 pp | 0.057 pp | 0.063 pp | 0.087 pp |
| moving_block_bootstrap | 31415 | traded | 16 → 32 | 11 + 9 | 0.014 pp | 0.087 pp | 0.072 pp | 0.087 pp |
| moving_block_bootstrap | 31415 | ever_inventory | 16 → 32 | 11 + 9 | 0.014 pp | 0.087 pp | 0.072 pp | 0.087 pp |
| moving_block_bootstrap | 31415 | terminal_inventory | 16 → 32 | 10 + 8 | 0.021 pp | 0.087 pp | 0.074 pp | 0.087 pp |
| moving_block_bootstrap | 1729 | unconditional | 32 → 64 | 32 + 32 | -0.013 pp | -0.026 pp | -0.015 pp | 0.000 pp |
| moving_block_bootstrap | 1729 | traded | 32 → 64 | 14 + 17 | -0.032 pp | -0.016 pp | -0.008 pp | 0.000 pp |
| moving_block_bootstrap | 1729 | ever_inventory | 32 → 64 | 14 + 17 | -0.032 pp | -0.016 pp | -0.008 pp | 0.000 pp |
| moving_block_bootstrap | 1729 | terminal_inventory | 32 → 64 | 13 + 16 | -0.031 pp | -0.014 pp | -0.007 pp | 0.000 pp |
| moving_block_bootstrap | 2718 | unconditional | 32 → 64 | 32 + 32 | 0.067 pp | 0.160 pp | 0.190 pp | 0.494 pp |
| moving_block_bootstrap | 2718 | traded | 32 → 64 | 13 + 17 | 0.011 pp | 0.329 pp | 0.354 pp | 0.494 pp |
| moving_block_bootstrap | 2718 | ever_inventory | 32 → 64 | 13 + 17 | 0.011 pp | 0.329 pp | 0.354 pp | 0.494 pp |
| moving_block_bootstrap | 2718 | terminal_inventory | 32 → 64 | 13 + 16 | 0.012 pp | 0.341 pp | 0.358 pp | 0.494 pp |
| moving_block_bootstrap | 31415 | unconditional | 32 → 64 | 32 + 32 | -0.004 pp | -0.018 pp | -0.010 pp | 0.000 pp |
| moving_block_bootstrap | 31415 | traded | 32 → 64 | 20 + 14 | 0.004 pp | -0.023 pp | -0.003 pp | 0.000 pp |
| moving_block_bootstrap | 31415 | ever_inventory | 32 → 64 | 20 + 14 | 0.004 pp | -0.023 pp | -0.003 pp | 0.000 pp |
| moving_block_bootstrap | 31415 | terminal_inventory | 32 → 64 | 18 + 13 | -0.000 pp | -0.020 pp | -0.003 pp | 0.000 pp |

| 模型 | 档位 | 条件 | 各种子样本数 | VaR95 极差 | CVaR95 极差 | VaR99 极差 | CVaR99 极差 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| jump_down | 16 | unconditional | 1729=16, 2718=16, 31415=16 | 0.638 pp | 0.467 pp | 0.501 pp | 0.467 pp |
| jump_down | 16 | traded | 1729=13, 2718=13, 31415=11 | 0.603 pp | 0.467 pp | 0.494 pp | 0.467 pp |
| jump_down | 16 | ever_inventory | 1729=13, 2718=13, 31415=11 | 0.603 pp | 0.467 pp | 0.494 pp | 0.467 pp |
| jump_down | 16 | terminal_inventory | 1729=12, 2718=12, 31415=11 | 0.592 pp | 0.467 pp | 0.492 pp | 0.467 pp |
| jump_down | 32 | unconditional | 1729=32, 2718=32, 31415=32 | 0.047 pp | 0.798 pp | 0.869 pp | 1.209 pp |
| jump_down | 32 | traded | 1729=26, 2718=25, 31415=22 | 0.087 pp | 0.989 pp | 0.946 pp | 1.209 pp |
| jump_down | 32 | ever_inventory | 1729=26, 2718=25, 31415=22 | 0.087 pp | 0.989 pp | 0.946 pp | 1.209 pp |
| jump_down | 32 | terminal_inventory | 1729=24, 2718=23, 31415=22 | 0.100 pp | 1.066 pp | 0.968 pp | 1.209 pp |
| jump_down | 64 | unconditional | 1729=64, 2718=64, 31415=64 | 0.074 pp | 0.565 pp | 0.844 pp | 1.140 pp |
| jump_down | 64 | traded | 1729=53, 2718=52, 31415=47 | 0.091 pp | 0.660 pp | 0.842 pp | 1.140 pp |
| jump_down | 64 | ever_inventory | 1729=53, 2718=52, 31415=47 | 0.091 pp | 0.660 pp | 0.842 pp | 1.140 pp |
| jump_down | 64 | terminal_inventory | 1729=49, 2718=48, 31415=44 | 0.092 pp | 0.707 pp | 0.844 pp | 1.140 pp |
| regime_historical | 16 | unconditional | 1729=16, 2718=16, 31415=16 | 0.098 pp | 0.221 pp | 0.196 pp | 0.221 pp |
| regime_historical | 16 | traded | 1729=9, 2718=5, 31415=7 | 0.152 pp | 0.221 pp | 0.207 pp | 0.221 pp |
| regime_historical | 16 | ever_inventory | 1729=9, 2718=5, 31415=7 | 0.152 pp | 0.221 pp | 0.207 pp | 0.221 pp |
| regime_historical | 16 | terminal_inventory | 1729=8, 2718=4, 31415=6 | 0.160 pp | 0.221 pp | 0.209 pp | 0.221 pp |
| regime_historical | 32 | unconditional | 1729=32, 2718=32, 31415=32 | 0.061 pp | 0.127 pp | 0.135 pp | 0.173 pp |
| regime_historical | 32 | traded | 1729=18, 2718=13, 31415=14 | 0.061 pp | 0.173 pp | 0.150 pp | 0.173 pp |
| regime_historical | 32 | ever_inventory | 1729=18, 2718=13, 31415=14 | 0.061 pp | 0.173 pp | 0.150 pp | 0.173 pp |
| regime_historical | 32 | terminal_inventory | 1729=15, 2718=11, 31415=11 | 0.079 pp | 0.173 pp | 0.154 pp | 0.173 pp |
| regime_historical | 64 | unconditional | 1729=64, 2718=64, 31415=64 | 0.057 pp | 0.123 pp | 0.123 pp | 0.173 pp |
| regime_historical | 64 | traded | 1729=34, 2718=31, 31415=24 | 0.108 pp | 0.136 pp | 0.143 pp | 0.173 pp |
| regime_historical | 64 | ever_inventory | 1729=34, 2718=31, 31415=24 | 0.108 pp | 0.136 pp | 0.143 pp | 0.173 pp |
| regime_historical | 64 | terminal_inventory | 1729=27, 2718=23, 31415=19 | 0.099 pp | 0.149 pp | 0.150 pp | 0.173 pp |
| moving_block_bootstrap | 16 | unconditional | 1729=16, 2718=16, 31415=16 | 0.039 pp | 0.119 pp | 0.097 pp | 0.119 pp |
| moving_block_bootstrap | 16 | traded | 1729=7, 2718=7, 31415=11 | 0.075 pp | 0.119 pp | 0.110 pp | 0.119 pp |
| moving_block_bootstrap | 16 | ever_inventory | 1729=7, 2718=7, 31415=11 | 0.075 pp | 0.119 pp | 0.110 pp | 0.119 pp |
| moving_block_bootstrap | 16 | terminal_inventory | 1729=6, 2718=7, 31415=10 | 0.079 pp | 0.119 pp | 0.110 pp | 0.119 pp |
| moving_block_bootstrap | 32 | unconditional | 1729=32, 2718=32, 31415=32 | 0.030 pp | 0.054 pp | 0.050 pp | 0.032 pp |
| moving_block_bootstrap | 32 | traded | 1729=14, 2718=13, 31415=20 | 0.094 pp | 0.032 pp | 0.045 pp | 0.032 pp |
| moving_block_bootstrap | 32 | ever_inventory | 1729=14, 2718=13, 31415=20 | 0.094 pp | 0.032 pp | 0.045 pp | 0.032 pp |
| moving_block_bootstrap | 32 | terminal_inventory | 1729=13, 2718=13, 31415=18 | 0.086 pp | 0.032 pp | 0.043 pp | 0.032 pp |
| moving_block_bootstrap | 64 | unconditional | 1729=64, 2718=64, 31415=64 | 0.097 pp | 0.232 pp | 0.250 pp | 0.527 pp |
| moving_block_bootstrap | 64 | traded | 1729=31, 2718=30, 31415=34 | 0.101 pp | 0.385 pp | 0.402 pp | 0.527 pp |
| moving_block_bootstrap | 64 | ever_inventory | 1729=31, 2718=30, 31415=34 | 0.101 pp | 0.385 pp | 0.402 pp | 0.527 pp |
| moving_block_bootstrap | 64 | terminal_inventory | 1729=29, 2718=29, 31415=31 | 0.098 pp | 0.393 pp | 0.405 pp | 0.527 pp |

结构化统计还保留逐个单元的期末 NAV、收益、损失、最大回撤、资金利用率和库存盈亏分布，损失及参与率区间，以及各指标的种子标准差。
