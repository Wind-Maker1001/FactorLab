# P6-B02 Binance TradFi 研究数据集成

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 接口与范围

`BinanceResearch(source_id, snapshot, expected_data_version=...).read(...)` 返回现有 `QueryResult(data, metadata)`。运行示例与查询配置分别见 `examples/binance_tradfi_research.py`、`configs/binance_tradfi_research.json`。

桥接层复用两个原生 Reader 的 manifest、Parquet 与原始响应 SHA256 核验，不修改源数据，不建立第二套存储、采集、审计或模型工厂。它独立于 A 股证券校验和 `ResearchSpec`，不把永续日线映射成美股交易日。哈希只证明本地产物一致性，不证明金融真实性。

| 来源 | 研究映射 |
| --- | --- |
| BINANCE_USDM_PUBLIC_REST | bars → UTC 闭合行情；funding → 独立资金费率事件；mark → 仅快照 |
| BINANCE_EQUITY | quotes / prices → 原生报价、价格观测；bars / open_bars 严格隔离；symbols 保留规则观察；sessions / gaps 是来源级资料 |

现货 `BINANCE:EQUITY:NVDA` 与永续 `BINANCE:USDM:NVDAUSDT` 不同身份。永续保留交易所实际报告的 `TRADIFI_PERPETUAL`、USDT 报价与保证金资产，不推断 NVDA 基础股票认证，不将 USDT 兑换成 USD。现货使用来源声明的 USD 与 shares。

所有查询要求带时区的起止和 `observation_cutoff`；窗口为左闭右开。采集时间不得替代事件时间；REST Quote 事件时间仍为 NULL，必须显式使用 `time_basis='acquired_at'`。K 线重复版本按明确观察截止时间选择，冲突观察拒绝。`gaps` 没有逐行采集时间，使用 manifest 的采集时间说明该资料的可见边界，仅支持 acquired_at 查询，保留原生缺口起止。

价格、数量保持精确十进制字符串；不填零、不前向填充。原生字段仍保留，发生时间转换的 Equity 时间另存 `native_*` 列。Equity K 线研究事件时间采用原生开盘边界；原生 `E` 推送时间单独映射为 `server_event_time`，并保留在 `native_event_time`，不能把服务器推送时间或采集时间当作历史交易日日期。

## 真实读取与证据

状态：**P6_B02_ENGINEERING_ACCEPTED / P6_B02_REAL_DATA_PARTIAL**。前者指离线工程合同可用；后者指缺少可用于闭合现货 K 线研究的真实历史，不是金融正式准入。

两套真实产物均可访问，无需联网采集。运行中禁止 `socket.connect` 与 `socket.create_connection`。机器可读结果见 [real-evidence.json](real-evidence.json)，其中记录数据版本、所读时间覆盖、原始回执数量与各表 SHA256；不提交新增原始行情、私有目录或凭证。

| 数据 | 真实结果（UTC） |
| --- | --- |
| 永续 bars | 原 Snapshot 10 条闭合日线，范围 `[2026-10-31, 2026-11-10)`；查询 `[2026-11-01, 2026-11-03)` 正确返回 2 条，事件日期为 11 月 1、2 日 |
| 永续 funding | 30 个独立事件；`2026-10-31T00:00:00.001` 至 `2026-11-09T16:00:00.001`；实际观察类型均为 Regular |
| 永续 mark | 1 个快照，事件时间 `2026-11-10T09:23:59.844`；普通研究历史请求明确拒绝 |
| Equity quotes | 16 条，其中 REST 1 条事件时间 NULL；流报价事件 `10:16:01–10:16:24`；采集 `10:15:59.227253–10:16:27.335706` |
| Equity prices | 3 条；事件 `10:16:04.027–10:16:24.027`，采集 `10:16:10.289768–10:16:28.499350` |
| Equity open_bars | 1 条；开盘边界 `10:15:00`，采集 `10:16:04.144279`；保持未闭合，不进入 bars |
| Equity bars | **EQUITY_CLOSED_BAR_RESEARCH_NOT_AVAILABLE**，没有真实闭合历史可读 |

以上 Equity 时间均在 2026-11-10。永续使用版本 `59f806b0bb04df2e4f12de805779cdab1e029d71629c446b2c43111c729cb49c`，manifest 采集时间 `2026-11-10T09:24:03.954728Z`，核验 6 份原始回执。Equity 使用 P6-B03 版本 `d6873933696da1241cbe58838e0d25166fd3bf550fde5a9462362809c9e8c849`，核验 34 份原始回执。所有查询观察截止为 `2026-11-11T00:00:00Z`，不宣称上述历史行情在事件当时已被本地采集或可得。

真实数据只观察到 Regular 资金费率、未闭合 Equity K 线与有限的报价窗口。Special 资金费率、Equity 闭合 K 线、修订截止选择、篡改、错误单位与缺失字段拒绝均由合成 fixture 验证，不作为真实金融实证。

## 复现与定向测试

从仓库根目录运行，先将 `src` 加入 Python 导入路径或按项目惯例安装本包：

```bash
PYTHONPATH=src python examples/binance_tradfi_research.py \
  --equity-snapshot "$EQUITY_SNAPSHOT" \
  --output evidence.json
```

`EQUITY_SNAPSHOT` 指向上述固定版本的本地 P6-B03 Snapshot。未提供路径或产物不存在时结果为 `NOT_RUN / SNAPSHOT_NOT_AVAILABLE`，不会重新采集。永续使用配置中的仓库相对 generation 路径，拒绝 `latest.json` 指针。数据版本不匹配、哈希异常会直接失败，而不是被当作缺失数据跳过。

最小消费方式：

```python
from factorlab.integrations.binance_research import BinanceResearch

source = BinanceResearch(
    'BINANCE_USDM_PUBLIC_REST',
    'docs/validation/p6-b01/nvda/runs/real',
    expected_data_version='59f806b0bb04df2e4f12de805779cdab1e029d71629c446b2c43111c729cb49c',
)
result = source.read(
    'bars', instrument_id='BINANCE:USDM:NVDAUSDT',
    start='2026-11-01T00:00:00Z', end='2026-11-03T00:00:00Z',
    observation_cutoff='2026-11-11T00:00:00Z',
)
```

首轮新增桥接、原生 Reader、DataHub 与跨市场相关测试：99 passed；跨市场因环境缺少 `exchange_calendars` 产生 3 failed、27 errors。随后在专用测试环境安装仓库固定的 `requirements/cross-market.txt`，补验并增加 A 股合同回归：**165 passed，10.90 秒**，结果见 [targeted-tests.xml](targeted-tests.xml)。最后在区分 Equity K 线开盘事件与服务器推送时间后，重验桥接层 **20 passed，3.00 秒**，见 [bridge-final-tests.xml](bridge-final-tests.xml)。这些数字有覆盖重叠，不能相加为独立用例数。

直接运行的模块包括 `test_binance_research.py`、`test_binance_equity.py`、`test_binance_collection.py`、`test_datahub.py`、跨市场 `test_contracts.py`，以及 A 股 `test_schema_phase1.py`、`test_schema_numeric_integrity.py`、`test_schema_ohlc_relationship.py`。未扩张 A 股证券格式、跨市场市场集合或正式准入语义。

真实重放环境为 Python 3.14.0、pandas 2.3.3、PyArrow 22.0.0；补验跨市场与最终桥接测试使用 Python 3.12.7、exchange-calendars 4.13.2。两者均运行同一任务源码，不构成重复全量矩阵。

最终有界真实读取耗时 **0.309 秒**，`tracemalloc` Python 分配峰值 **4,635,479 字节**。这是本次小 Snapshot 重放的 Python 分配量，不是整个进程 RSS 或吞吐量基准；未运行无关压力测试。SHA256 检查按查询执行，耗时已经包含其成本。运行配置只使用现有 pandas/PyArrow 与标准库，桥接不新增依赖。

## 后续研究边界

P6-B04 可直接消费返回的 DataFrame 与 SourceMetadata，但必须自行处理估值、持仓、费用、保证金和强平。资金费率事件不是已发生持仓扣费，Mark Price 不是历史估值序列。历史可得性一律保留 `HISTORICAL_AVAILABILITY_UNVERIFIED`；观察截止仅证明本地产物在该采集观察范围内可见，不等同于历史交易时点可知性。PIT、复权与正式金融准入均未提升。
