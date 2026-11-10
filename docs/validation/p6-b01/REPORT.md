# P6-B01：Binance TradFi 只读行情与实采完整性报告

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 实际采集

2026-11-10 通过 Binance 官方 USD-M 公共 REST、Windows PowerShell transport 采集；不是 fixture。历史截止时刻为 `2026-11-10T09:23:58.762058Z`，最后响应采集时间为 `2026-11-10T09:24:03.954728Z`。各请求的独立采集时间、URL、参数和原始字节摘要保存在 manifest 与 request receipts 中。

| 内容 | 实际结果 |
| --- | --- |
| 合约 | `BINANCE:USDM:NVDAUSDT`，USDT 报价与保证金币种 |
| exchangeInfo 身份 | `contractType=TRADIFI_PERPETUAL`、`status=TRADING`、`underlyingType=EQUITY` |
| UTC 请求区间 | `[2026-10-31T00:00:00Z, 2026-11-10T00:00:00Z)` |
| 已闭合日 K | 应有 10 根，取得 10 根，重复与缺口均为 0 |
| 历史资金费率 | 30 条；源字段 `rateType=Regular`；保留负费率和实际零费率 |
| 标记价 | `181.94164469` USDT |
| 指数价 | `181.85610703` USDT |
| 标记/指数快照源时间 | `2026-11-10T09:23:59.844Z`；是采集时快照，不冒充历史区间末估值 |
| 原始请求回执 | 6 条，独立 reader 已校验并重放 |
| 数据版本 | `59f806b0bb04df2e4f12de805779cdab1e029d71629c446b2c43111c729cb49c` |

原始 `exchangeInfo` 的拼写实际为 **TRADIFI_PERPETUAL**，不是 TRADFI_PERPETUAL。适配器显式接受这两个类型值并保留源原值，另以 `EQUITY` 筛选；不根据 ticker 后缀判断。此次归档发现 197 个活跃的 EQUITY 类 TradFi 永续候选，包括 TSLAUSDT、INTCUSDT、METAUSDT、NVDAUSDT，完整枚举见 [discovery.json](discovery.json)，绑定同一次原始响应摘要。

`EQUITY` 类别也可能包括 ETF/杠杆产品，不能据此声称整个发现列表都是普通股票。当前 NVDAUSDT 示例保留供应商 `baseAsset=NVDA`，但不自动推导证券交易所、ISIN 或现货身份；`underlying_status=UNVERIFIED`。需要连接具体底层股票时，必须显式提供股票映射与匹配摘要的官方证据字节，不允许剥离 USDT 后缀生成身份。

资金费率完整性仅声明“实际返回的结算事件”，不以当前间隔反推历史应有结算次数。此次区间没有返回 Special；Special、未知类型缺字段以及同时间戳 Regular/Special 跨分页边界由离线 fixture 覆盖。不会补造缺失结算，也不会把资金费率当作价格收益。

## 安装与使用

使用项目支持的 Python 3.13/3.14 和正常安装环境；无额外网络库依赖。从源码运行时可设置 `PYTHONPATH=src`。

```bash
# 显式启用网络；Windows 若需要系统网络栈，可添加 --transport powershell。
python -m factorlab.integrations.binance discover \
  --allow-network --output results/binance-discovery

# 新输出目录，历史区间右端不含。
python -m factorlab.integrations.binance collect \
  --allow-network --transport powershell \
  --symbol NVDAUSDT --start 2026-10-31 --end 2026-11-10 --interval 1d \
  --output results/binance-nvda

# 同一开始日期，扩展结束日期；重读边界、补先前日 K 缺口。
python -m factorlab.integrations.binance update \
  --allow-network --transport powershell \
  --symbol NVDAUSDT --start 2026-10-31 --end 2026-11-11 --interval 1d \
  --output results/binance-nvda

# 无网络、无客户端构造；重放本仓库已归档的真实示例。
python -m factorlab.integrations.binance replay \
  --output docs/validation/p6-b01/nvda --table bars
python -m factorlab.integrations.binance replay \
  --output docs/validation/p6-b01/nvda --table funding
python -m factorlab.integrations.binance replay \
  --output docs/validation/p6-b01/nvda --table mark
```

实采 generation 位于 `nvda/runs/real`。独立请求回执使用完整 UUID 文件名，历史请求缓存索引仍使用完整 URL SHA256；原始响应仍以完整 SHA256 命名。此次仅移动归档并缩短回执名称，manifest 内的相对原始响应路径与全部内容未变，因此数据版本保持不变。

重放已归档数据可复现本次字节版本；重新请求交易所会产生新采集时间与数据版本，不要求实时快照与旧版相同。CLI 不接受交易、撤单、划转、提现操作；无密钥、签名或账户接口。

### Python 读取合同

```python
from factorlab.integrations.binance import Reader

reader = Reader("docs/validation/p6-b01/nvda")
bars = reader.read("bars")       # QueryResult：data + SourceMetadata
funding = reader.read("funding")
mark = reader.read("mark")
reader.verify_raw()              # 校验原始响应与映射证据
```

每个目录只绑定一个合约与周期。`latest.json` 指向不可变 generation；可直接给 Reader 传 generation 路径固定版本。`manifest.json` 绑定合约身份、请求范围、来源、采集完成时间、历史截止时间、schema/data version、原始回执、Parquet 摘要与完整性报告。原始响应保存在 `raw/*.bin`，规范化表为 `bars.parquet`、`funding.parquet`、`mark.parquet`。

表中的事件时间均为 aware UTC；价格、数量、费率保留交易所十进制字符串，避免二进制浮点改变原值，使用计算时由消费者显式选择 Decimal 或数值转换。trade_count 为整数。资金结算键为 `(funding_time, rate_type)`；类型缺字段是 UNKNOWN，不补 Regular。标记/指数是当前快照，last_funding_rate 不是历史结算表的替代品。

日期参数解释为 UTC 零点；带时间的参数必须声明时区，且请求边界需对齐固定周期。日线是 UTC 桶，不复用美股/中国现货 session；不使用上海日末的证据日期规则。以固定历史截止时刻排除未闭合 K，上市前区间不计缺口；上市所在的非完整首桶不计为完整历史 K。

失败时不发布新的 latest；成功的历史分页及回执留在 pending 目录，原参数重跑会重用已完成分页。增量更新保持 start，允许扩展 end，重新获取重叠边界及先前缺口；冲突记录报错，不覆盖旧值，不默默选一个版本。资金费率没有已证明的历史发布水位，因此每次更新重查其历史范围，合并去重，捕获延迟发布的 Regular/Special，不把先前空响应当作永久缺失证明。已有合约暂停/退市但仍被 exchangeInfo 返回时，也允许历史补采；若源身份已消失则明确拒绝，不能从旧快照伪造当前身份。对旧 K 线的非边界事后修订，应使用新的输出目录重新 collect。

### 显式底层映射

`collect/update` 可同时传 `--underlying-mapping mapping.json --underlying-evidence official-document.bin`。JSON 合同为：

```json
{
  "symbol": "NVDAUSDT",
  "security_id": "<explicit underlying security identity>",
  "name": "<underlying issuer/security name>",
  "evidence_url": "https://www.binance.com/<official evidence locator>",
  "evidence_sha256": "<sha256 of the supplied official document bytes>"
}
```

适配器归档并绑定这份证据；映射是调用者显式提供的声明，不由 ticker 生成，也不构成历史 PIT 认证。Reader 可校验字节绑定，不将摘要存在当作证据内容的真实性认证。

## 离线验证

```bash
python -m pytest -q tests/research/test_binance_client.py \
  tests/research/test_binance_collection.py
```

Python 3.13 定向离线测试 **38 passed**，结果见 [targeted-tests.xml](targeted-tests.xml)。测试通过注入 HTTP fixture、时钟和退避函数执行，无外部网络。覆盖分页/去重冲突、UTC/未闭合 K/范围和缺口、Regular/Special、mark/index、429/Retry-After、超时/截断/断线、恢复与增量、证据绑定与 Parquet 重放、CLI 只读拒绝。真实 smoke 仅通过显式 `--allow-network` 命令运行，不作为默认 CI 依赖。

### Windows 嵌套 Git checkout 回归

```bash
python -m pytest -q tests/research/test_binance_client.py \
  tests/research/test_binance_collection.py \
  tests/research/test_binance_windows_checkout.py \
  tests/research/test_d07_git_receipt.py
```

2026-11-10 Windows 本地定向结果：

| Python | 结果 | 耗时 | JUnit |
| --- | --- | --- | --- |
| 3.13.9 | 46 passed | 50.30 秒 | [windows-path-tests-py313.xml](windows-path-tests-py313.xml) |
| 3.14.0 | 46 passed | 53.67 秒 | [windows-path-tests-py314.xml](windows-path-tests-py314.xml) |

每组包括上述 38 项离线测试、1 项实采归档 checkout/重放回归及 7 项 D07 Git 回执测试（含 `test_nonancestor_rejected`）。新增回归在 140 字符长的嵌套仓库根路径中，显式设置 `core.longpaths=false`，fetch 当前提交并通过 sparse checkout 实际检出完整 P6-B01 归档；检出路径均短于 260 字符，原回执布局在同一根路径下会超过此限制。随后校验全部 6 条原始响应回执及请求回执与 manifest 的一致性，离线重放 10 根 K 线、30 条资金费率和 1 条标记/指数快照。D07 测试另外执行完整候选仓库 checkout 和历史提交切换。

既有 A 股 Provider、DataHub 路由、现货日历、研究配置与准入门禁不因本采集器获得放宽。
