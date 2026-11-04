# 中国石油 601857.SH：2018 年真实回测阻塞报告

检查日期：2026-11-04（Asia/Shanghai）。

**REAL_2018_BACKTEST=BLOCKED / NOT_RUN**

`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

当前没有发现可供本任务使用的完整2018年原始日线与至少20个首日前预热观测。
策略软件及合成测试已实现，合成结果不代表中国石油2018年收益。

## 本地检查的实际范围

只读核对了 FactorLab 仓库、现有财务证据目录、已整理本地行情存储、既有五年派生
窗口清单及两份 CSMAR 原始日个股行情 ZIP。现有五年窗口清单起点为2021年；
没有把该旧清单当作本次完整数据认证。

本次对两份原始 ZIP 内11个 xlsx 工作簿的原生 B列 `Trddt` 做流式逐日期单元格扫描：

|原始文件标识|SHA256|工作簿|日期单元格|观测日期范围|2018日期|
|---|---|---:|---:|---|---:|
|CSMAR日个股导出231033555|45676a0c928e5bb47e5a7c68b71d14afe2192928c326ebcf4ae8dfcc7d38299d|5|4,495,295|2019-01-01至2022-12-30|0|
|CSMAR日个股导出231200343|786e64016acdc58e5086fa446095888b50748afa1fed31d3bd2c0bb9886cdcc5|6|5,407,716|2023-01-02至2026-10-20|0|

共9,903,011个日期单元格，未解析为ISO日期的单元格数为0。原生列名实际核对为Trddt。
这证明所扫描原始日行情中没有2018日期；这些范围是供应商观测值，不是交易日历认证。
未发布供应商原始行、价格或受限文件。该检查不证明所有磁盘、所有供应商均无2018数据。
第一版 shared-string 检查发现日期实际为 inlineStr，不能用于覆盖结论；其诊断保留，
上述结论使用后续原生日期列扫描，不以第一版空共享字符串域推断缺失。

本地证据包保留两次扫描脚本、原始日志、逐成员哈希与日期范围。
没有完整2018行情，因此本轮没有对2018 OHLCV、真实日历、停牌、异常价格、
复权与分红一致性给出 PASS，也没有生成真实交易或真实收益指标。

## 尚缺资料与可复现导入方案

1. 获取有合法使用权的 `601857.SH` 原始未复权日线，覆盖2018全年及至少20个
   首日前有效观测；建议从2017-11-01开始，实际按交易日历验证预热量。
   保存供应商原始下载文件、获取时间、版本/修订、原始字段和 SHA256，禁止覆盖旧文件。
2. 单独取得连续复权收盘与复权算法/锚点/公司行为来源；分别保留 raw OHLC 和 adj OHLC，
   不能仅重命名列。取得期间分红、送转、拆股的除权日、登记日、支付日、比例与税务假设。
3. 取得真实交易所日历、逐日停牌和ST状态、上市身份/代码历史。单标的任务不意味着
   既有身份、来源、时点及正式资格要求自动豁免。
4. 在独立私有目录将输入规范为指南所述字段及 `calendar.csv(date)`；金额CNY，
   成交量为股。不要用业务工作日日历替代真实交易所日历，不凭空补缺失行或状态。
   可先只运行以下 Schema/信号诊断，保存诊断及原始文件哈希：

```python
from pathlib import Path
import hashlib
import pandas as pd
from factorlab.strategies import DualMAConfig, moving_average_signals

source = Path("private/601857_2017-11_2018-12.csv")
calendar_source = Path("private/calendar.csv")
bars = pd.read_csv(source, dtype={"symbol": str}, float_precision="round_trip")
bars["date"] = pd.to_datetime(bars["date"], errors="raise")
calendar = pd.DatetimeIndex(pd.to_datetime(pd.read_csv(calendar_source)["date"], errors="raise"))
for path in (source, calendar_source):
    with path.open("rb") as handle:
        print(path.name, hashlib.file_digest(handle, "sha256").hexdigest())
signals = moving_average_signals(bars, calendar, DualMAConfig())
print(signals.shape)  # 仅Schema/信号诊断，无收益、无准入授权
```

5. 用现有数据规范和独立证据协议完成六项准入与执行资格。当前 migration/security
   门禁无条件拒绝，且公司行为执行账本尚未实现：即便补齐原始行情，也不能据此
   声称真实总收益可运行。不添加自行认证、关闭门禁或探索性绕过入口。
6. 后续只有经独立批准的准入协议与经过验证的公司行为账本俱备，才能增加真实报告
   路径；必须重新绑定输入、代码、参数及环境，运行同区间策略与买入持有基准。

已有 CLI 真实请求当前如实失败；软件演示命令及完整输出定义见
[双均线指南](DUAL_MA.md)。本任务不下载替代年份，不伪造2018行情，不认证PIT/HFQ，
不修改任何金融、安全、身份或历史保护规则。
