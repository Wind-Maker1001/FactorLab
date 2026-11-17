# MC-05-F01：随机 Grid 账户风险收敛补证

NO LIVE MONEY。FINANCIAL_NOT_ESTABLISHED。

## 冻结设计与执行进度

已实际完成192个随机GBM账户，三个预定种子全部完成16、32、64三档；复用前缀，实际账户数为3×64，而非三档相加。独立配置：`../../../configs/mc05_f01_grid_tail.json`。模型顺序固定为 GBM、moving_block_bootstrap、regime_historical、jump_down；每模型按16、32、64推进，种子1729、2718、31415，batch=8、168小时、999次统计重采样、900秒正式研究预算、安全系数1.2。不根据收益改变顺序或种子。

`planning.json` 的两个真实 WAL+FULL 校准账户不进入随机概率分布：平价4.4034秒、下降4.7588秒，校准总耗时9.1707秒。按较慢吞吐及1.2倍安全系数，48账户首组预计274.106秒，96账户两档预计548.212秒，900秒可以准入。正式预算单独计时，包括正式研究中的确定性校准、生成、账户、统计与落盘；预冻结规划的9.1707秒另列，不隐藏在正式预算内。

## 实际账户与资源结果

正式研究墙钟770.6335秒，CPU249.6875秒，账户执行768.3344秒，生成0.0513秒，统计0.5292秒；进程生命周期峰值RSS177,053,696字节。192个随机账户加6个确定性账户，共198个；900秒预算超额为0。预冻结规划另有2个确定性账户、9.1707秒，因此本任务账户执行合计200个，规划与正式研究计时合计779.8042秒（不包含测试）。`run/`证据705,492字节，无持久化Paper SQLite。

|模型|已完成每种子规模|下一未准入规模|判断时已用秒|预计新增秒|
|---|---|---|---:|---:|
|GBM|16、32、64|无（冻结上限64）|368.2565（准入64）|414.8130|
|moving_block_bootstrap|无|16|770.6279|224.2121|
|regime_historical|无|16|770.6287|224.2121|
|jump_down|无|16|770.6294|224.2121|

后三模型分别判断首组准入，未执行，不估计其风险。`completed_paths_per_seed`是所有模型共同完成规模的旧兼容字段，因此为0；实际按模型进度以 `completed_paths_per_seed_by_model` 为准，GBM为64，不能将旧字段误读为本轮零随机账户。

192个随机账户：125个成交且曾有库存（65.1042%），67个零成交（34.8958%），100个期末仍有库存（52.0833%），64个亏损（33.3333%）。最大回撤观察最大值0.3624%；路径平均持仓市值利用率均值1.0320%，全体峰值4.0632%，平均名义敞口均值10.3199 USDT，观察最大峰值40.6321 USDT。期末库存合计12.10（跨账户之和，不是单账户仓位）；费用合计1.369045 USDT，滑点合计2.052716 USDT。所有随机账户FIFO/NAV归因残差绝对值最大0，无风险退出事件。低敞口及零成交是解读小损失的必要背景，不是相对满仓持有的优势证据。

完整的各种子、各规模、四类条件的VaR95/CVaR95/VaR99/CVaR99、区间、种子SD、敞口与参与统计以及相邻规模差异见 [STATISTICS.md](STATISTICS.md)。`run/groups.json`还保留Wilson概率区间、回撤分位数区间、VaR无界端点、尾部权重及所有条件分布；逐账户费用、滑点、库存、退出与对账摘要见 `run/paths.csv`。

## 统计结论

**STATISTICALLY_UNSTABLE；CONVERGENCE_NOT_ESTABLISHED；FINANCIAL_NOT_ESTABLISHED。**

每种子16、32、64条路径的95%有效尾部质量分别只有0.8、1.6、3.2；99%分别0.16、0.32、0.64。条件样本更少。所有单种子95/99尾部均触发MC-02统计不稳定诊断；99% CVaR在三档均由单个最差账户决定。部分VaR分布无关上界无穷，不能把JSON中的null当零。种子1729在16档的CVaR95为0.00294604，95%经验bootstrap区间[0.00136738, 0.00294604]，VaR95没有有限分布无关上界。

相邻规模CVaR95绝对变化最大0.00184879（初始资金比例），详细差异逐种子逐条件列出；两档甚至三档完成并未建立收敛。经验bootstrap只能重采样已观察支持集，不能补足未见尾部。没有将三种子的嵌套样本或不同条件混为新增独立尾部，也未因某个风险估计较低而选择性展示。

## 方法

复用原 Grid Paper/FIFO/NAV 和 MC-01 WAL+FULL、MC-02尾部定义及不确定性方法、MC-03生成器。逐账户删除临时数据库，不保存全部随机张量；保留每模型最差NAV路径用于复现。16→32→64只追加嵌套前缀中的新账户。

准入单位为一个模型的全部预定种子新增账户。某模型扩档被拒绝不阻断后续模型首档的独立准入。完整组完成后才发布正式种子统计；逐路径CSV保留已执行账户，部分组不发布正式跨种子结论。资金利用率定义为持仓市值/初始现金，并非包含未成交挂单冻结额的现金占用率。

16或32路径不能证明95% CVaR收敛；实际尾部有效质量、重复/退化诊断和区间必须随结果报告。全体、成交、曾有库存、期末库存四类分开。零成交NAV不变是未参与，不是成功；合成场景概率不是市场发生概率。

## 验证

最终定向及相关回归145项通过，28.89秒。覆盖单模型完整组准入、不完整组中断不发布正式统计、拒绝扩档后其他模型仍可准入、资源不足保留完整结果、四模型嵌套前缀、校准时间边界、零成交分类、MC-02尾部不足诊断、FIFO/NAV、WAL事务和原始MC-05证据冻结。

实际证据检查确认192个唯一账户、9个完整种子/规模单元、每种子path_id为0至63且不重复，全部随机账户归因残差为0、正式耗时小于900秒，无持久化临时SQLite。原MC-05配置和证据路径相对07821df无改动。

```bash
PYTHONPATH=src python -m pytest tests/research/test_mc_grid_tail_convergence.py \
  tests/research/test_mc05_f01.py tests/research/test_mc_regime_jump.py \
  tests/research/test_mc_tail_reliability.py tests/research/test_regime_jump.py \
  tests/research/test_grid_monte_carlo.py tests/trading/test_paper_control.py -q -o addopts=''
```

## 可复现命令

在项目根目录使用所需依赖环境，Windows Git Bash：

```bash
PYTHONPATH=src python tools/research/plan_mc05_f01.py \
  --definition configs/mc05_f01_grid_tail.json --output planning-replay.json
PYTHONPATH=src python tools/research/run_mc_grid_tail_convergence.py \
  --snapshot D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6 \
  --definition configs/mc05_f01_grid_tail.json --output mc05-f01-replay
```

统计表可由下列命令重建：

```bash
python tools/research/report_mc05_f01.py \
  --run docs/validation/mc-05-f01/run --output mc05-f01-statistics-replay.md
```

正式执行代码收据为 `21792b7db69bdb616262335df8f3c27fc26016f7`，`git_dirty=false`；后续提交只增加证据、报告生成器及测试，不改变已执行实验实现或冻结配置。重跑账户样本可复现，资源准入规模取决于执行机器实测吞吐。

原始 MC-05 配置及证据保持冻结，不将其零随机样本改写为本轮结果。新增冻结证据测试通过Git checkout filters比较，兼容Windows CRLF，不改写原文件。
