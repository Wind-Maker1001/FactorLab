# MC-05-F04C-02 真实行情非零成交与多粒度复验

已观察到真实历史价格触发的非零 Paper 成交。分钟报价观察改变了配对账户的成交轨迹，具体库存和 NAV 差异逐窗报告。研究从先前冻结的 F04C 执行规则出发，使用独立历史窗口与小时触价专项案例两组样本。

## 独立窗口结果

| 计数 | 全部预定独立窗口 |
| --- | --- |
| 事前预定窗口 | 3 |
| 行情准入窗口 | 3 |
| A 有效订单路径有真实极值触价窗口 | 2 |
| 产生 Paper 成交窗口 | 2 |
| A/B 成交轨迹不同窗口 | 2 |
| 期末 NAV 不同窗口 | 2 |

每个独立窗口都保留，窗口选择未参考最终 NAV、回撤或执行差异。窗口数量不足以估计可靠的市场成交发生概率、VaR 或 CVaR。专项案例采用 `CONDITIONALLY_SELECTED_EXECUTION_CASE` 标记，只验证触价与账户路径。

## 逐窗结果

| 窗口 | 样本类型 | A 成交 | B 成交 | A 数量 | B 数量 | A NAV | B NAV | NAV B−A |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| I1 | INDEPENDENT_HISTORICAL_WINDOW | 2 | 2 | 0.2 | 0.2 | 0.99896498519904 | 0.9985535741004 | -0.00041141109864 |
| I2 | INDEPENDENT_HISTORICAL_WINDOW | 1 | 1 | 0.1 | 0.1 | 0.99985444582824 | 0.99986645783112 | 0.00001201200288 |
| I3 | INDEPENDENT_HISTORICAL_WINDOW | 0 | 0 | 0 | 0 | 1 | 1 | 0 |

分钟观察增加报价检查机会，同时可能更早持仓、消耗小时合成容量并改变后续反向单。额外手续费与滑点由实际确认成交计算，不能以成交率较高代替风险与净值评价。逐窗成本、库存、回撤、现金与 FIFO 见 [FINANCIAL.md](FINANCIAL.md)；首处分歧和真实订单时序见 [EXECUTION_CASES.md](EXECUTION_CASES.md)。

本轮没有出现“更多最终成交笔数”的窗口：前两个独立窗口的成交数量相同，但成交时间和价格不同。I1 的分钟组更早承担库存，期末权益较小时组下降；I2 的分钟组更早成交且期末权益略高。因此本轮确认了账户路径对报价粒度敏感，未得到分钟观察必然增加最终成交量或改善净值的结论。

窗口 C1 [2026-06-05T00:00:00+00:00, 2026-06-12T00:00:00+00:00) 拒绝准入：incomplete bounded acquisition receipts; no retry or replacement window: NVDAUSDT-1m-2026-06-07.zip, NVDAUSDT-1m-2026-06-07.zip.CHECKSUM, NVDAUSDT-1d-2026-06-07.zip, NVDAUSDT-1d-2026-06-07.zip.CHECKSUM, NVDAUSDT-1m-2026-06-08.zip, NVDAUSDT-1m-2026-06-08.zip.CHECKSUM, NVDAUSDT-1d-2026-06-08.zip, NVDAUSDT-1d-2026-06-08.zip.CHECKSUM, NVDAUSDT-1m-2026-06-09.zip, NVDAUSDT-1m-2026-06-09.zip.CHECKSUM, NVDAUSDT-1d-2026-06-09.zip, NVDAUSDT-1d-2026-06-09.zip.CHECKSUM, NVDAUSDT-1m-2026-06-10.zip, NVDAUSDT-1m-2026-06-10.zip.CHECKSUM, NVDAUSDT-1d-2026-06-10.zip, NVDAUSDT-1d-2026-06-10.zip.CHECKSUM, NVDAUSDT-1m-2026-06-11.zip, NVDAUSDT-1m-2026-06-11.zip.CHECKSUM, NVDAUSDT-1d-2026-06-11.zip, NVDAUSDT-1d-2026-06-11.zip.CHECKSUM。保留 MARKET_DATA_BLOCKED，未替换候选。

## 工程与复现

复用 `ResearchExecution`、既有官方归档准入、Grid、Paper、FIFO 和 NAV 审计。新增研究代码只调度预定窗口、筛查初始有效网格、整理价格与订单证据并生成报告。两组都每小时决策；B 在后续已完成分钟报价上撮合，反向单留到小时阶段。每小时容量保持 0.10，TTL 保持 100，资金费为 `NOT_SIMULATED`。

同输入逐窗重复执行及冻结 F04C 对照：

| 窗口 | 首次结果 SHA-256 | 重复结果 SHA-256 |
| --- | --- | --- |
| I1 | 90a1ef4ef9f3c424d3dc861419ad5e87d9a790bca3e2549752730a0a40a24965 | 90a1ef4ef9f3c424d3dc861419ad5e87d9a790bca3e2549752730a0a40a24965 |
| I2 | cc537c297ceb236561f242d304df985808e687cc1d8b290b25749e9d74455a70 | cc537c297ceb236561f242d304df985808e687cc1d8b290b25749e9d74455a70 |
| I3 | f8bec75e757eebdcae32f6f33ea2929949388fc43ffc85ab12d384f4582002c0 | f8bec75e757eebdcae32f6f33ea2929949388fc43ffc85ab12d384f4582002c0 |

```json
{
  "actual": "9570d0f0b83e2f11bb96a052c1c0205561860bb12e1928e81ca54843b638adef",
  "expected": "9570d0f0b83e2f11bb96a052c1c0205561860bb12e1928e81ca54843b638adef"
}
```

定向验证：

```json
{
  "accounting_tolerance": "0.00000001",
  "deterministic_replay": "PASS",
  "frozen_f04c": "PASS",
  "targeted_tests": {
    "existing_and_window_suite": {
      "files": [
        "tests/research/test_mc05_f04c_02.py",
        "tests/research/test_mc05_f04c_execution.py",
        "tests/research/test_mc_grid_tail_f04c_data.py",
        "tests/research/test_grid_inventory_risk.py"
      ],
      "passed": 41,
      "seconds": 160.83
    },
    "nonzero_case_test": {
      "passed": 1,
      "seconds": 2.3,
      "test": "test_nonzero_case_reconstructs_native_wallet_and_excludes_terminal_order_touches"
    },
    "passed": 42,
    "status": "PASS"
  }
}
```

资源实测：

```json
{
  "account_bytes": 1685122,
  "cpu_seconds": 3.75,
  "formal_accounts": 6,
  "formal_replay_cpu_seconds": 5.15625,
  "formal_replay_wall_seconds": 7.939525699941441,
  "frozen_regression_accounts": 2,
  "repeat_accounts": 6,
  "repeat_replay_cpu_seconds": 4.234375,
  "repeat_replay_wall_seconds": 6.983842699963134,
  "wall_seconds": 5.360003000008874
}
```

归档采集统计：

```json
{
  "acquisition_timing": "initial acquisition completed; per-object receipts retained; original perf_counter interval was interrupted before final report",
  "downloaded_bytes": 0,
  "first_fetched_at": "2026-11-19T10:46:49.048715+00:00",
  "get_requests": 0,
  "last_fetched_at": "2026-11-19T10:47:31.612568+00:00",
  "mode": "OFFLINE_REUSE",
  "original_failed_request_details": "not retained before evidence interruption; incomplete C1 receipts rejected without retry",
  "original_head_request_count": 112,
  "rejected_windows": {
    "C1": "incomplete bounded acquisition receipts; no retry or replacement window: NVDAUSDT-1m-2026-06-07.zip, NVDAUSDT-1m-2026-06-07.zip.CHECKSUM, NVDAUSDT-1d-2026-06-07.zip, NVDAUSDT-1d-2026-06-07.zip.CHECKSUM, NVDAUSDT-1m-2026-06-08.zip, NVDAUSDT-1m-2026-06-08.zip.CHECKSUM, NVDAUSDT-1d-2026-06-08.zip, NVDAUSDT-1d-2026-06-08.zip.CHECKSUM, NVDAUSDT-1m-2026-06-09.zip, NVDAUSDT-1m-2026-06-09.zip.CHECKSUM, NVDAUSDT-1d-2026-06-09.zip, NVDAUSDT-1d-2026-06-09.zip.CHECKSUM, NVDAUSDT-1m-2026-06-10.zip, NVDAUSDT-1m-2026-06-10.zip.CHECKSUM, NVDAUSDT-1d-2026-06-10.zip, NVDAUSDT-1d-2026-06-10.zip.CHECKSUM, NVDAUSDT-1m-2026-06-11.zip, NVDAUSDT-1m-2026-06-11.zip.CHECKSUM, NVDAUSDT-1d-2026-06-11.zip, NVDAUSDT-1d-2026-06-11.zip.CHECKSUM"
  },
  "retained_bytes": 1145509,
  "retained_objects": 92,
  "retained_successful_get_receipts": 92
}
```

每个正式窗口只有 A/B 两个账户；确定性复跑与旧窗口回归另列为验证账户，不增加正式样本或研究窗口。原始行情以压缩对象复用，不展开为重复 JSON 或 SQLite；SQLite 只用于小型 Paper 账户。

离线复现（从仓库根目录执行；参数目录分别指向 R04B 快照、本轮压缩归档、原 F04C 压缩归档和新的输出目录）：

```powershell
$env:PYTHONPATH = "src"
python tools/research/run_mc05_f04c_02.py --hourly-snapshot <R04B_SNAPSHOT> --archive-root <ARCHIVE_ROOT> --legacy-archive-root <F04C_ARCHIVE_ROOT> --accounts-root <NEW_ACCOUNT_ROOT> --output <NEW_REPORT_ROOT>
```

## 历史更细证据

```json
{
  "historical_spread_depth": "NOT_ADMITTED",
  "minute_trade_fields": "trade_count, taker_buy_volume, taker_buy_quote_volume",
  "probes": [
    {
      "checked_at": "2026-11-19T10:50:30.768490+00:00",
      "content_length": "1721245",
      "date": "2026-05-15",
      "last_modified": "Sat, 16 May 2026 06:59:09 GMT",
      "product": "aggTrades",
      "status": 200,
      "url": "https://data.binance.vision/data/futures/um/daily/aggTrades/NVDAUSDT/NVDAUSDT-aggTrades-2026-05-15.zip",
      "window": "I1"
    },
    {
      "checked_at": "2026-11-19T10:50:31.089328+00:00",
      "date": "2026-05-15",
      "product": "bookTicker",
      "status": 404,
      "url": "https://data.binance.vision/data/futures/um/daily/bookTicker/NVDAUSDT/NVDAUSDT-bookTicker-2026-05-15.zip",
      "window": "I1"
    },
    {
      "checked_at": "2026-11-19T10:50:31.253747+00:00",
      "content_length": "446576",
      "date": "2026-05-15",
      "last_modified": "Sat, 16 May 2026 07:17:12 GMT",
      "product": "bookDepth",
      "status": 200,
      "url": "https://data.binance.vision/data/futures/um/daily/bookDepth/NVDAUSDT/NVDAUSDT-bookDepth-2026-05-15.zip",
      "window": "I1"
    }
  ],
  "queue_position": "NOT_AVAILABLE",
  "trade_payload_downloaded": false
}
```

已有分钟线提供成交笔数和主动买入数量，能够说明该分钟内存在公开成交。对应日期的逐笔归档可用于进一步辨认价格顺序；历史买卖价、盘口深度、队列与订单确认共同决定实际订单是否成交。当前盘口不替代历史盘口。本轮结果验证的是冻结报价、滑点与容量下的 Paper 执行和账务，不构成一般收益结论。

研究状态：

- `ENGINEERING_ACCEPTED`
- `MARKET_DATA_BLOCKED`
- `WINDOW_SELECTION_PRE_REGISTERED`
- `PAPER_NONZERO_FILL_OBSERVED`
- `EXECUTION_GRANULARITY_DIFFERENCE_OBSERVED`
- `FIFO_NAV_RECONCILED`
- `REAL_FILL_NOT_VERIFIED`
- `FINANCIAL_NOT_ESTABLISHED`
- `NO LIVE MONEY`
