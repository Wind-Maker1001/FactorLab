# MC-05-F04A 执行与验收

**ENGINEERING_ACCEPTED · PAIRED_SAMPLE_VALID · TTL_SENSITIVITY_ESTABLISHED**

**STATISTICAL_CONVERGENCE_NOT_ESTABLISHED · FINANCIAL_NOT_ESTABLISHED · NO LIVE MONEY**

按[事前方案](PROTOCOL.md)，在每个模型的三个种子中各取 F03 路径 0–15。对 144 条不同价格路径分别运行 48、100、168 小时 TTL，共完成 432 个正式 Paper 账户。每组的三个账户拥有独立订单和账务，使用同一小时价格序列。100 小时组全部重新执行。

## 资源与完整性

| 阶段 | 并行账户 | 额外串行对照 | 墙钟秒 | 全进程树 CPU 秒 | 采样 RSS 峰值 MiB | 采样临时文件峰值 MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 预校准 | 27 | 9 | 18.69 | 38.38 | 1,632.83 | 17.19 |
| 正式 | 432 | — | 113.58 | 297.73 | 1,648.50 | 17.86 |

正式使用最多 8 个 spawn workers。整批安全后预计墙钟 171.60 秒，另留 15 秒写出时间，正式预算为 900 秒。采样包括活跃子进程。账户收集后清理可再生文件，发布表逐账户保留配对身份、价格指纹、成交、费用和账务结果。正式报告及账户表在写入审计文件前合计 381,666 字节。

- 全部 144 条配对路径的价格指纹通过 F03 对照，三个 TTL 组之间一致。
- 144 个 100 小时基准账户逐项匹配 F03，每账户比较 61 个冻结字段。
- 432 个账户的 FIFO 对账、导出 NAV 和库存残差的最大绝对值均为 0。预校准的 9 个串行对照与正式并行结果逐项一致。
- 定向回归覆盖到期前、到期小时、取消确认后、滑点下限价不达标、拒绝提交和风险退出、资源拒绝、worker 失败与错配、尾部路径计数、配对判据及冻结证据，共 33 项通过。F01、F02、F03 冻结证据保留原样。

[AUDIT.json](AUDIT.json)记录实际执行源提交、冻结快照、执行参数、资源、判据和账户表摘要。正式执行源提交为 [38df28a](https://github.com/Wind-Maker1001/FactorLab/commit/38df28a447567f7383574500c672aaf5f0b0cee6)。Windows 检出时，研究 worktree 的合成示例文件 src/factorlab/demo.py 仅有换行符差异，审计因此记录工作树为 dirty。该文件不在本研究提交中。报告发布时用相同的账户表重新渲染，将方向判据导出为原生 JSON 布尔值，并纠正尾部质量恰为整数时的加权路径计数显示。

## 结果解释

48→100 小时在 Regime historical 与 Moving-block bootstrap 各新增 9/48 条成交路径，均通过事前判据，Holm 校正后 p=0.0234375。Jump-down 新增 6/48 条，校正后 p=0.125。100→168 小时三模型分别新增 4、2、3 条，从前到后均未出现有成交变为零成交。两档都成交的路径首笔成交均发生在同一小时。每个新增成交账户在较短 TTL 时都有到期后买单触价，仍有部分到期后触价路径在更长档没有成交。

三种模型的平均持仓市值利用率与期末平均库存均随 TTL 上升。Jump-down 从 48 到 100 小时，最差期末 NAV 从 985.6972 降到 984.4674 USDT，跳前库存的标记冲击合计从 −155.0926 扩大到 −176.7249 USDT。100→168 小时保持同一个最差 NAV。这个对照支持分别追踪跳前持仓与跳价后新成交。独立样本仅 48 条/模型，经验 99% CVaR 由该组最差路径决定。详见[成交参与](PARTICIPATION.md)和[金融分析](FINANCIAL.md)。

下一轮可事先冻结同样的三组对照和主要指标，增加独立价格路径来复核 100→168 小时参与率及持仓变化，并使条件尾部样本达到预先确定的支持量。小时内触价和容量问题应另用更细粒度行情研究。

## 复跑

从仓库根目录使用同一冻结快照，安装仓库所需研究依赖及 requirements/scalable-compute.txt 中的 psutil。预校准与正式阶段使用同一环境和相邻的新输出目录。

~~~powershell
$env:PYTHONPATH = 'src'
$snapshot = '<F03 certified source snapshot directory>'
python tools/research/run_mc05_f04a.py pilot --snapshot $snapshot --output local-mc05-f04a/pilot
python tools/research/run_mc05_f04a.py formal --snapshot $snapshot --output local-mc05-f04a/formal --calibration local-mc05-f04a/pilot/calibration.json
~~~

[预校准收据](pilot/calibration.json)和[正式账户表](paths.csv)对应上述入口。
