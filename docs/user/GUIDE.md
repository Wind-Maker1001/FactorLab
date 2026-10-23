# FactorLab 使用与研究边界

FactorLab 是离线因子研究、四模型比较、统计诊断与日线回测的软件候选。
当前版本全程 **NO LIVE MONEY / PIT_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**。
软件通过、合成数据、真实数据可获得、正式金融准入和可交易性是不同结论。

## Windows 11 安装

使用标准 CPython 3.14（64位、带GIL）；3.13是兼容环境。先在源码目录创建新环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements/py314-win64.lock.txt
.\.venv\Scripts\python -m pip install .
.\.venv\Scripts\python -m pip check
.\.venv\Scripts\factorlab --help
.\.venv\Scripts\factorlab demo --output results/synthetic-first-run
```

已有目录不能作为新的 Demo 输出目录，防止把旧文件误计入本轮证据。
也可以从 wheel 安装：`python -m pip install factorlab-0.1.0.dev0-py3-none-any.whl`。
无需 Qbot 源目录、交易 SDK、数据库、GUI 或原项目 PYTHONPATH。

## Linux 安装

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m pip check
.venv/bin/factorlab demo --output results/synthetic-first-run
```

源码中的依赖上下界与独立 CI 候选同时约束安装；Windows 锁文件记录本轮实际环境。
Linux CI 必须在实际运行后才能宣称通过，生成 workflow 本身不代表执行成功。
LightGBM 的 Linux wheel 可能需要系统 OpenMP 运行库；不要从不明来源复制 DLL。

## 快速开始和四模型流程

`factorlab demo` 在本地用固定种子生成行情、基准、历史股票池与证券状态，计算14基础因子，
使用 DatasetBuilder 生成下一开盘进入、五交易日持有的超额收益标签，然后执行
FactorScore、Ridge、LightGBM、XGBoost 的 Expanding Walk-Forward、两级回测和报告。
Train 拟合、Validation 选择、Test 评价保持隔离，保留 Purge/Embargo 与标签成熟约束。

结果目录包括输入 Parquet、原始因子、预处理 Dataset、逐模型预测与折叠模型、
Fast/Execution 权益与交易、IC/RankIC、图表、Markdown 报告和哈希清单。
全部标记 `SYNTHETIC / SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE`。
这些模拟证券及行情不代表真实历史股票池，不能据此评价 A 股 Alpha 或收益预期。

## 用户自有数据 Schema

行情以 `(date, symbol)` 为唯一键，日期为无时区的交易日，代码为 `000001.SZ` 或
`600000.SH`。规范日线有18列：

```text
date symbol open high low close volume amount turnover adj_factor
raw_open raw_high raw_low raw_close adj_open adj_high adj_low adj_close
```

成交量为股、金额为人民币元、换手率为小数。原始与复权价格必须分列，不能只把列名
改为 adj_* 就宣称后复权来源已验证。Parquet/CSV/JSON 合同保持原有语义。

股票池为 `date,symbol,is_member`，每个研究交易日要求完整成员证据；证券状态为
`date,symbol,trade_status,is_st`；上市信息为 `symbol,listing_date`。交易日历显式提供，
不得补入自然日或用今天的成分股回填历史。可选 AKShare/BaoStock 适配器通过
`pip install '.[suppliers]'` 安装。本任务没有下载真实行情，也不要求商业数据。

本地文件并不自动取得正式资格。正式消费者仍要求 RAW、ADJUSTMENT、STATUS、PIT、
SOURCE_BINDING、FORMAL 六项资格及输入逐行绑定。本次独立身份迁移另有无条件拒绝门禁，
独立信任根验收未完成前无法批准正式研究或真实 Forward。

## 自定义因子接口

研究扩展可继承 `factorlab.factors.base.Factor`，声明 `name`、`lookback`、
`required_columns`，实现返回与输入索引一致的 `compute(bars)`。例如：

```python
from factorlab.factors.base import Factor, grouped_shift

class CloseLagReturn(Factor):
    name = "CUSTOM_RETURN2"
    lookback = 2

    def compute(self, bars):
        ordered = self.validate(bars)
        values = ordered.adj_close / grouped_shift(ordered.adj_close, ordered.symbol, 2) - 1
        return values.reindex(bars.index)
```

先独立调用并验证缺失、停牌、常数输入、未来数据扰动和窗口边界。新候选不自动进入
14项正式基础注册表；若需要研究注册，提交独立代码审查与定义合同，不能改写旧 Golden。
自动发现模块仅运行受约束 AST，原有39个成功候选表达式合同保留，无 eval/exec 通路。

## 统计、回测与解释

IC 是横截面 Pearson 相关，RankIC 是 Spearman 相关。逐日配对 RankIC 差、HAC 标准误、
Moving Block Bootstrap 和 Holm 多重比较校正沿用现有定义。样本不足、常数输入和缺日
按原合同保留不可用状态；不调低阈值制造显著性，不把合成回测的显著性当金融证据。

Fast Backtest 是研究近似；Execution Backtest 加入下一开盘、A股T+1、整手、停牌、
涨跌停、现金与费用约束。默认费率来自 `FeeSchedule.a_share_default`，Demo 滑点为0。
日线成交近似不能重现订单队列、真实流动性或盘口冲击；报告必须同时说明这些局限。
收益、回撤、换手与费用应结合信号时点、样本资格和交易近似解释。

市场结构保留32项合同：14项为合成实现，18项仍为 CONTRACT_ONLY。
5/15/30分钟聚合和 STATUS 适配器不提供金融认证。

## 配置、路径、身份与版本升级

从源码项目运行时，相对配置路径以项目根为基准。仓库外使用绝对配置路径时，
未提供显式根目录则以配置文件所在目录解析数据/输出相对路径；不会搜索旧 Qbot 目录。
安装包的 Git 身份为 null，资源/源码摘要只描述当前软件，不签发金融资格。
Git 绑定的 model_readiness 源码验收需要已提交源码；wheel 中明确为 SOURCE_CHECKOUT_REQUIRED。

旧 `qbot.research` 的模型 pickle 不可直接当新版本模型加载；类名和源码身份已经变化。
保留旧环境与原始收据，在新版本重新训练并生成如实标记的新清单。已有文件格式中的
部分 qbot-* 协议名为了兼容仍保留，它们不是 Legacy 交易依赖，也不能替代迁移授权。

Python 3.9 仅作固定旧源行为基线。Python 3.14 使用 NumPy2/Pandas2.3/SciPy1.16 等
经过实际验证的版本，XGBoost 保留2.1.4以保持数值行为。不得用重生成 Golden 掩盖差异。

## 已知限制与贡献

本候选尚有独立信任根、上游文档许可证范围、历史私有产物与跨平台远端 CI 的发布阻塞。
真实数据来源不完整时继续保留 UNKNOWN/null/原始“-”，不能从当前事实回填历史证据。
贡献前创建隔离环境，运行独立测试脚本，提交小范围变更并记录源码/依赖身份。
不要提交供应商原件、真实派生证券明细、账户配置、环境密钥、旧签发收据替代版本。
安全问题按 SECURITY.md 在受控渠道处理；禁止将真实凭据或可重构的受限数据写进公开 issue。
