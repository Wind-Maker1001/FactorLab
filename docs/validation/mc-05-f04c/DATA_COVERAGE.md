# MC-05-F04C 行情覆盖与准入

`MINUTE_DATA_READY`。正式窗口为 UTC `[2026-08-05 00:00, 2026-08-12 00:00)`。分钟、小时和日线分别覆盖 10,080、168 和 7 根已闭合成交 K 线。前一根已闭合小时线的收盘价 216.54000 USDT 用作共同网格锚点。这根线从 8 月 4 日 23:00 开始，在研究窗口起点可供策略使用，计入初始化。

| 行情 | 本次使用范围（UTC，左闭右开） | 来源和身份 |
| --- | --- | --- |
| 1h 成交线 | 2026-05-14 至 2026-08-12，共 2,160 根。正式窗口 168 根，另读 1 根前置线 | 项目 R04B 已认证的 `BINANCE_USDM_PUBLIC_DATA` 快照，generation `bde00b2f948842e2b8c2a00465a9f8c6`。 |
| 1m 成交线 | 2026-08-05 至 2026-08-12，共 10,080 根 | 币安官方 USD-M Public Data，每个 UTC 日一个压缩对象及其官方校验文件。 |
| 1d 成交线 | 2026-08-05 至 2026-08-12，共 7 根 | 同一官方归档的独立日线对象，用于核对小时线聚合。 |

项目 P6-D02 曾取得 `[2026-08-12, 2026-11-10)` 的 REST 小时和日线，以及 `[2026-11-09, 2026-11-10)` 的 REST 分钟样本。原取证目录在本次环境中已不存在。对应的已记录范围与本轮档案窗口相邻，因此没有同一时间桶的 REST 和归档修订对照。小时来源选择了实际可用的 R04B 数据，研究窗口固定在该资产结束前的最后七个 UTC 日。 [P6-D02 历史覆盖](../p6-d02/REPORT.md) · [R04B 小时来源认证](../../research/p7-r04b-historical-data.md)

## 桶边界与聚合

每根成交线保留 Binance 原生开盘和闭合毫秒，开盘与 UTC 固定周期对齐，原生闭合时间等于下一周期起点前 1 毫秒。回放在下一周期起点使用该根线。分钟和日线闭合时间须早于各自的抓取时间。解析依原生顺序验证每个预期桶，范围内缺失、重复、错序及未闭合均阻断准入。

| 对账 | 精确一致 | 查验字段 |
| --- | ---: | --- |
| 60 根分钟线与每根小时线 | 168 / 168 | 开盘取首值，最高取最大值，最低取最小值，收盘取末值。按十进制精度求和后核对 base volume、quote volume、成交笔数及两种 taker-buy volume。 |
| 24 根小时线与每根独立日线 | 7 / 7 | 相同的 OHLCV、笔数和 taker-buy 规则。 |
| 正式窗口和前置小时线与八月原始归档 | 169 / 169 | 先按 R04B 固定摘要校验原始 ZIP，再逐桶对照 Parquet 的原生时间及上述全部成交字段。 |

价格为每 base unit 的 USDT 合约报价。成交量以 base units 计量，quote volume 以 USDT 计量，count 为交易所成交笔数。合约身份为 `BINANCE:USDM:NVDAUSDT`，留档 exchangeInfo 的合约类型为 `TRADIFI_PERPETUAL`，标的类型为 `EQUITY`，报价和保证金资产均为 USDT。规则快照观察于 2026 年 11 月 10 日 UTC，PRICE_FILTER tickSize 为 0.01000，LOT_SIZE 的 minQty 和 stepSize 均为 0.01，MIN_NOTIONAL 为 5。交易规则证据的时间界限是这次快照，八月的 Paper 配置来自 F04A 固定研究参数。 [R04B 固定快照 manifest](../p7-r04b/snapshot-manifest.json) · [币安 Public Data 字段说明](https://github.com/binance/binance-public-data#klines)

## 采集和可复现身份

补采前对 8 月 11 日的 1m、1d ZIP 各做一次 HEAD，均返回 200。预定范围为单一合约的七个分钟 ZIP、七个日线 ZIP及各自的 CHECKSUM，共 28 次 GET。实际 GET 用时 15.102 秒，共收到 337,031 字节。下载时间为 2026-11-18 10:29:02.639118 至 10:29:17.115300 UTC，离线准入用时 0.844 秒。运行直接读取压缩对象和原有 Parquet。 [8 月 11 日分钟对象](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1m/NVDAUSDT-1m-2026-08-11.zip) · [8 月 11 日日线对象](https://data.binance.vision/data/futures/um/daily/klines/NVDAUSDT/1d/NVDAUSDT-1d-2026-08-11.zip)

R04B 小时快照在 2026-11-13 17:12:50.891167 UTC 取得完毕，固定 data version 为 `352d960fad24e50f1423ff173d838a994a29603d2629652f46c7edc76e5e2a83`，Parquet SHA-256 为 `16aa66258880efdace966880f59691a29cb74f4f87428240a5ef247ad044a2ab`。本轮重放的官方八月小时 ZIP SHA-256 为 `198d588ff3f55eccde27d1f0c4a267765fc6e17b92ceed24e7d906adf8706056`。留档 exchangeInfo SHA-256 为 `bb6cc37b014e203eefccfdedf8a1258ebe3de52ac878d46c26aa40a58c642d10`。逐个分钟及日线对象的 URL、SHA-256、取得时点、长度和服务器修改时间在运行审计中记录。 [本次运行审计](AUDIT.json) · [R04B 固定快照 manifest](../p7-r04b/snapshot-manifest.json)

从仓库根目录运行以下 PowerShell 命令可以重新取得固定的 28 个对象。已有压缩对象及凭据时，从离线回放命令开始。持有原 R04B 快照但其 raw 路径已迁移时，可通过 `--hourly-archive-root` 指向含 `NVDAUSDT-1h-2026-08.zip` 的目录。回放校验重新下载的字节与原审计的 SHA-256，再校验币安的 CHECKSUM。来源发生修订时，固定证据回放会拒绝变更的对象。

```powershell
$f04cAudit = Get-Content -Raw -Encoding UTF8 docs/validation/mc-05-f04c/AUDIT.json | ConvertFrom-Json
$f04cArchive = Join-Path (Get-Location) 'results/mc05-f04c-reacquired'
New-Item -ItemType Directory -Path $f04cArchive -ErrorAction Stop | Out-Null
foreach ($object in $f04cAudit.data.archive_requests) {
    $destination = Join-Path $f04cArchive $object.name
    curl.exe --fail --silent --show-error --connect-timeout 8 --max-time 20 --max-filesize 2097152 --output $destination $object.url
    if ($LASTEXITCODE -ne 0) { throw "Archive download failed for $($object.name)" }
}
$f04cReceipt = ConvertTo-Json -InputObject @($f04cAudit.data.archive_requests) -Depth 8
[IO.File]::WriteAllText((Join-Path $f04cArchive 'requests.json'), $f04cReceipt, [Text.UTF8Encoding]::new($false))

$env:PYTHONPATH = 'src'
$f04cHourly = 'D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6'
python tools/research/run_mc05_f04c.py --hourly-snapshot $f04cHourly --archive-root $f04cArchive --output results/mc05-f04c-repeat
```
