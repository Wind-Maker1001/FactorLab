# F02 v69 精简交接包

对应 [FactorLab 原 PR #240](https://github.com/Wind-Maker1001/FactorLab/pull/240) 的 `feat/v69-f02` 分支。

| 交付项 | 路径 |
|---|---|
| 原策略入口 | `v69/strategy.py` 中的 `Strategy`，导入路径 `v69.strategy:Strategy` |
| 依赖模块 | 实际继承为 v69 → v61 → v51 → `risk_research/parent.py` → `v50/strategy.py` → `v48_strategy.py` → Qlib。撮合和核验依赖也随包。 |
| 原始回测配置 | `original_configs/research_2023_2026/protocol.json` 为实验协议，`original_configs/research_2023_2026/run.py` 为归档入口，`beta_research/engine.py` 保留原撮合配置。 |
| 一份参考结果 | `fixture/v69_target_normal/`，含净值、目标权重、成交、归因、原引擎回执与同一账户的标普 500 指标。 |

原实验从 30 万美元空仓起算。计划窗口 2023-01-01 至 2026-11-23，实际交易日 2023-01-03 至 2026-11-23，共 977 日。按周首日开盘执行，使用上一完整交易日的行情，单边成本为 0.10%。正常情景保留八只标的。

该账户实际成交 371 笔，累计净收益 38.83%，最大回撤 −4.24%，相对标普 500 的年化 Alpha 为 5.24%。Qlib 原始回执中的 Alpha 和 Beta 以 QQQ 为基准。标普总回报指标另存于 `sp500_report.csv`。

## 运行

使用 Python 3.11 和 `requirements.txt`。策略导入使用 `from v69.strategy import Strategy`。验证回放时，从完整复现包解出原行情，再将对应目录传给下列入口。

```powershell
python replay_v69.py --market-dir "完整包解压目录/F02_v69/fixture/market" --verify
```

原文件由本机 Qlib 研究档案按字节归档。逐个文件指纹见 `source-manifest.json`。
