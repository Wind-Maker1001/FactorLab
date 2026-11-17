# MC-05-F03

F03 以同模型、同种子的嵌套模拟路径研究 Grid Paper 尾部估计。研究参数和验收判据见 [事前方案](PROTOCOL.md)。执行、统计与金融结果分别见 [REPORT.md](REPORT.md)、[STATISTICS.md](STATISTICS.md) 和 [FINANCIAL.md](FINANCIAL.md)。

`run/paths.csv` 每行对应一个实际账户。`run/groups.json` 保存完整模型档位的种子检查点，`run/statistics.json` 给出逐档和逐条件分母、区间、分布和差值。路径价格用按序小端 float64 字节的 SHA-256 标识，`run/generation.json` 绑定各批生成器设置。`run/path-evidence.json` 保存跳跃与状态诊断，`run/tails.json` 保存各模型最差期末 NAV 的合成价格路径。`run/resources.json` 和 `run/decisions.json` 关联整组运行消耗与当时的准入决策。`run/audit.json` 绑定有效账户审计及最终状态。

## 复跑

从仓库根目录运行，使用 F02 经认证快照和新的输出目录。资源采样使用 `requirements/scalable-compute.txt` 中的 psutil。正式与预校准用同一环境和临时盘。报告阶段重算价格前缀并串行回放每个模型一个固定对照账户。

```powershell
$env:PYTHONPATH = 'src'
$snapshot = '<F02 certified snapshot directory>'
python tools/research/run_mc05_f03.py pilot --snapshot $snapshot --output local-mc05-f03-replay/pilot
python tools/research/run_mc05_f03.py formal --snapshot $snapshot --output local-mc05-f03-replay/formal --calibration local-mc05-f03-replay/pilot/calibration.json
python tools/research/run_mc05_f03.py report --snapshot $snapshot --output local-mc05-f03-replay/formal --calibration local-mc05-f03-replay/pilot/calibration.json
```

若正式运行中断，报告入口根据 `groups.json` 寻找完整档位，并只纳入这些档位内的账户。由进程捕获的异常及中断保存在 `failure.json`，正式工程状态为 `ENGINEERING_BLOCKED`。重新执行使用新目录。
