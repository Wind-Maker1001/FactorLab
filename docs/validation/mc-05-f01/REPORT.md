# MC-05-F01：随机 Grid 账户风险收敛补证

NO LIVE MONEY。FINANCIAL_NOT_ESTABLISHED。

## 冻结设计与执行进度

正式运行尚未启动。独立配置：`../../../configs/mc05_f01_grid_tail.json`。模型顺序固定为 GBM、moving_block_bootstrap、regime_historical、jump_down；每模型按16、32、64推进，种子1729、2718、31415，batch=8、168小时、999次统计重采样、900秒正式研究预算、安全系数1.2。不根据收益改变顺序或种子。

`planning.json` 的两个真实 WAL+FULL 校准账户不进入随机概率分布：平价4.4034秒、下降4.7588秒，校准总耗时9.1707秒。按较慢吞吐及1.2倍安全系数，48账户首组预计274.106秒，96账户两档预计548.212秒，900秒可以准入。正式预算单独计时，包括正式研究中的确定性校准、生成、账户、统计与落盘；预冻结规划的9.1707秒另列，不隐藏在正式预算内。

## 方法

复用原 Grid Paper/FIFO/NAV 和 MC-01 WAL+FULL、MC-02尾部定义及不确定性方法、MC-03生成器。逐账户删除临时数据库，不保存全部随机张量；保留每模型最差NAV路径用于复现。16→32→64只追加嵌套前缀中的新账户。

准入单位为一个模型的全部预定种子新增账户。某模型扩档被拒绝不阻断后续模型首档的独立准入。完整组完成后才发布正式种子统计；逐路径CSV保留已执行账户，部分组不发布正式跨种子结论。资金利用率定义为持仓市值/初始现金，并非包含未成交挂单冻结额的现金占用率。

16或32路径不能证明95% CVaR收敛；实际尾部有效质量、重复/退化诊断和区间必须随结果报告。全体、成交、曾有库存、期末库存四类分开。零成交NAV不变是未参与，不是成功；合成场景概率不是市场发生概率。

## 可复现命令

在项目根目录使用所需依赖环境，Windows Git Bash：

```bash
PYTHONPATH=src python tools/research/plan_mc05_f01.py \
  --definition configs/mc05_f01_grid_tail.json --output planning-replay.json
PYTHONPATH=src python tools/research/run_mc_grid_tail_convergence.py \
  --snapshot D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6 \
  --definition configs/mc05_f01_grid_tail.json --output mc05-f01-replay
```

原始 MC-05 配置及证据保持冻结，不将其零随机样本改写为本轮结果。
