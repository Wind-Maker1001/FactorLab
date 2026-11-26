# v182 与 v179 策略复现包

先看 `结果对照.csv` 和 `策略说明与伪代码.md`。v182 是本轮编号最新版本，v179 是最后一组在三个开发窗口、两档交易费用下选出的版本。两个版本使用同一股票池和同一账户撮合逻辑。

## 从哪里看

| 项目 | 包内位置 |
|---|---|
| 最新策略入口 | `v182/strategy.py` 的 `Strategy` |
| 开发期入选策略入口 | `v179/strategy.py` 的 `Strategy` |
| 两版如何切换目标仓位 | `mix_research/policies.py` 的 `Blend.targets` |
| 原始策略依赖 | 根目录下的版本目录、研究模块及 `v48_strategy.py`，保留原导入路径 |
| 回测使用的配置 | `original_configs/` 保存原文件，`replay_config.json` 汇总单账户复现入口需要的参数 |
| 冻结行情和本地 Qlib 数据 | `fixtures/` 包含股票数据、Qlib provider 及相应标普总回报序列 |
| 成交及净值 | `accounts/版本/窗口/情景/` |
| 原报告、伪代码、标的与仓位控制 | `reports/`、`策略说明与伪代码.md` |
| 包内文件校验 | `SHA256SUMS.txt`，每一行记录 SHA-256 和相对路径 |

## 解压后复现

完整解压 ZIP，在包含 `replay.py` 的目录运行。原环境为 Python 3.11.9，主要第三方库版本见 `requirements.txt`。首次新建环境可以运行 `python -m pip install -r requirements.txt`。已经装有相同 Qlib 环境时可以直接复现，行情均从包内读取。

```text
python replay.py --list
python replay.py --version v182
python replay.py --version v179
python replay.py --version v182 --window target --case stress
python replay.py --version v179 --window twenty --case normal
```

每次在 `replays/` 下新建目录，从 30 万美元现金开始运行 Qlib，再与包内原始记录逐行比对日净值、逐笔成交、目标权重与金额归因，最后复核相对标普的指标。每个窗口独立起算。开发窗口使用的数据和 SNDK 排除设置与原始实验一致。可用组合以 `--list` 为准。

主窗口实际交易日期为 2023-01-03 至 2026-11-23。两版都有常规、三倍费用、剔除闪迪三种主窗口结果；二十年与 2001–2006 挑战窗口的原始账户归属于入选版 v179。原生 Qlib 使用 QQQ 生成内部记录，面向用户的 Alpha、Beta 和超额以 `sp500_metrics.json` 的标普500总回报为准。

`fills.csv` 的 quantity 和 price 采用复权行情对应的数量与价格。可用 quantity × price 核对 turnover。`targets.csv` 的 date 是下单日，signal_date 是读取的前一收盘日。`daily.parquet` 存日净值与实际仓位，`attribution.csv` 存各标的净损益。下单时 90% 总目标上限和价格变动后的每日实际持仓比例可有差异。

## 交付校验

本包 23 个原始账户均已从候选 ZIP 解压后独立复跑。逐日账户净值与原件的最大绝对差为 4.656612873e-10 美元。逐笔成交、每周目标、每只股票的损益归因以及标普口径指标均已核对。逐账户结果见 verification.json。
