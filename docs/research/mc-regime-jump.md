# MC-03：状态区块与 Jump Stress

**NO LIVE MONEY。合成风险研究，不是价格预测、策略准入或真实市场概率验证。**

## 状态条件历史区块模型 v1

入口：`factorlab.research.regime_jump.RegimeBootstrap`。输入为等间隔、严格递增的同步简单收益矩阵 `r[t,a] > -1`，具备逐行观察结束时间、`available_at`、来源、数据版本、资产名称和校准截止时间。所有数据必须在校准截止前已观察且可取得；缺失、未来和不规则样本直接拒绝，不插值、不截取未来行。

设参考资产对数收益 `x[t]=log(1+r[t,reference])`。候选区块从 `s` 开始，特征只使用 `[s-L,s)` 的收益：`v[s]=std(x[s-L:s], ddof=1)`，`m[s]=sum(x[s-L:s])`。`L>=2`。波动率阈值 `q` 是历史前缀所有完整 L 步窗口标准差的指定分位数，趋势边界固定为 0。四类状态固定为 `low_down, low_up, high_down, high_up`；`v>q` 为 high，`m>=0` 为 up。参考资产必须显式指定；状态共同约束所有资产，而不是逐资产独立抽样。

`threshold_as_of` 是**观察时间上的前缀切分边界**，不是数据取得时间。阈值仅使用该边界以内的数据；条件区块从后缀开始，特征不得包含区块内收益。阈值的实际可知时间为前缀 `available_at` 的最大值，完整模型在校准 `as_of` 冻结。快照晚于原始观察取得时，不声称历史区块当时已经获得该快照或已经实施此分类。

每一类区块池包含后缀内所有具有完整 B 步后续收益的起点（默认 B=24，L=24）。历史状态转移计数来自所有合法 `(z[s], z[s+B])` 对，步长为 **B 行而非一小时**；重叠对不被当作独立统计观察。行归一化得到 P。初始状态默认服从条件区块池频数，也可显式指定。对于每一条路径：抽初始状态，均匀抽取该状态区块，按原顺序复制完整的联合多资产收益，再按 P 切换；最后区块按剩余时长截断。价格满足 `S[t+1,a]=S[t,a]*(1+r*[t,a])`，实现以累计对数收益计算。

区块内部联合收益、顺序与同时关系被保留；跨区块连续性和更长记忆**不保证保留**。状态持续是区块边界的生成假设，不意味着区块内的每个滚动特征都保持同一标签。默认每状态至少 20 个条件起点和 20 个转移对；这些是工程支持下限，不是独立有效样本量或统计可靠性证明。不加伪计数、不将空池替换为全样本。`CalibrationError` 表示历史支持不足。

压力模式可显式替换 P 和初始分布，仍须有真实支持的条件区块池。仅转移支持不足时可保留条件 Bootstrap 压力模式；区块池本身不足时不能伪造该状态数据，研究入口记录拒绝原因并保留独立 Jump Stress。

## 参数化 Jump Stress v1

入口：`JumpStress` / `JumpConfig`。每步：

`log(S[t+1,a]/S[t,a]) = d[a] + sigma[a]*Z[t,a] + I[t]*j[a]`。

背景 Z 为跨时间、跨资产独立标准正态；I 为每步独立 Bernoulli(p)，同一时刻所有资产共用 I；有符号跳幅 j 为显式逐资产常数（点质量分布）。每步至多一次事件，dt=1。背景 d 是对数漂移，不是 GBM 算术漂移；没有跳跃补偿项。资产背景并非历史相关模型，共同跳跃的同步关系仅由共享 I 和载荷定义。输入参数在 `as_of` 冻结；输入来源即使引用历史样本，也不赋予 p 或 j 市场估计地位。

有限对照使用历史对数均值/样本标准差作为背景，并比较 p=0、p=1/168 且 j=log(0.7)、p=1/168 且 j=log(1.3)。这分别是每步假设的无跳跃、下跌30%、上涨30%，不是真实发生概率。多步可发生多次事件。

## 复现、资源与收据

`PathPlan` 限制路径数、步数、批量与批次工作区预算。生成器以有界批次 yield `(first_path_id, prices)`，不保留全样本索引或全量路径。每路径 RNG 为 `PCG64(SeedSequence([seed, global_path_id]))`；新模型同版本、同 NumPy 和参数下不受批量布局影响。原 GBM/MBB 仍使用原 Runner 随机流与批次语义，不要求与新模型逐路径相等。价格出界产生明确错误，不裁剪、不填零、不重试。

`receipt(plan)` 返回名称、版本、所有有效参数、状态/转移计数、时间边界、来源与包含收益和 PIT 信息的样本哈希、seed、RNG、NumPy 版本、批次计划、数值与概率解释。收据不嵌入历史收益矩阵；历史来源须保留在原数据仓库。Jump 收据识别所引用来源与假设，不冒充历史收益校准。

## Grid 对照

新增研究入口复用原 `historical_input`、`evaluate_path`、`distribution`、`stress_paths`。Grid 适配仅支持原单资产 NVDAUSDT 小时 close 序列，与 R10 冻结参数、1000 USDT 初始权益、4/6 bps 费用/滑点和 0.10 **合成数量容量**一致。没有生成 high/low/volume；不是盘口或真实成交。资金费为 NOT_SIMULATED。

历史估计状态模型、高波动持续（高状态行 high 总概率 0.98）、下降趋势持续（每行 down 总概率 0.98）单列，三组状态模型保持相同的初始池频数分布，仅改变转移假设；与 GBM/MBB 和三个 Jump 组分别归约。每个随机组单独计算 loss=`1-terminal_NAV/1000`、线性 VaR、分数边界 CVaR 与逐小时最大回撤；六类原确定性压力单列，不参与任何随机概率分布。库存、残余损失、成交与费用来自原 Paper 账户。零成交不等于安全或退出完成。

每组最多 64 条、336 步；只导出每路径汇总和每组一条最差终值 close 路径。每条账户归约后删除临时目录与 SQLite。ExperimentTracker 存储紧凑定义与执行收据。

```sh
PYTHONPATH=src python tools/research/run_mc_regime_jump.py \
  --snapshot D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6 \
  --as-of 2026-11-14T00:00:00Z --threshold-as-of 2026-06-28T00:00:00Z \
  --start 2026-11-15T00:00:00Z --paths 16 --steps 168 --batch-size 8 --seed 710 \
  --output results/mc-03
PYTHONPATH=src python -m pytest tests/research/test_regime_jump.py tests/research/test_mc_regime_jump.py tests/research/test_monte_carlo.py tests/research/test_grid_monte_carlo.py -q
```

实际执行、数据身份和风险对照见 [MC-03 REPORT](../validation/mc-03/REPORT.md)。
