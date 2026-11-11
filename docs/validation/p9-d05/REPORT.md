# P9-D05 000540 历史状态与 LightGBM 第一折

2026-11-12 · **P9_D05_PARTIAL**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

冻结 LightGBM 第 0 折连续验收了 **45/243 个来源会话**，较 D04 新增 2023 年 5 月 15 至 18 日四个会话。45 包含 3 月 13 日初始化会话，账户有 44 条收盘账本记录。完整折窗口为 `[2023-03-13, 2024-03-13)`，账本验收到 2023 年 5 月 18 日收盘。次日 000540.SZ 停牌，账户仍持有 133,700 股，缺少可用于该账户的估值合同，完整第一折因此仍未形成。

## 5 月 15 日的状态依据

中天金融集团股份有限公司 2023-36 号公告确认，证券 000540 于 5 月 4 日停牌一天，5 月 5 日开市复牌并被实施退市风险警示，简称变为 *ST 中天，适用 5% 日涨跌幅限制。深交所 5 月 4 日公告独立确认风险警示从 5 月 5 日生效。复用 D03 的 PDF 原件，其 SHA-256 为 `55f64543cc636130a1befa013868b1a4c37f7f82511bab1642f90cd54cc2e456`。 [发行人 2023-36](https://static.cninfo.com.cn/finalpage/2023-04-29/1216703562.PDF) · [深交所公告](https://www.szse.cn/disclosure/margin/business/t20230504_600265.html)

5 月 13 日公开的 2023-47 号公告继续以同一证券代码及 *ST 简称识别股票，并披露连续低于 1 元的退市风险。以公告日上海日终作为公开时间上界，该证据在 5 月 15 日开盘前可用。5 月 15 日的 2023-49 和 5 月 17 日的 2023-51 提供后续佐证，按各自公开日处理。5 月 19 日的停牌是交易状态的独立边界，上市简称在摘牌公告中仍为 *ST 中天。 [发行人 2023-47](https://static.cninfo.com.cn/finalpage/2023-05-13/1216809911.PDF) · [发行人 2023-49](https://static.cninfo.com.cn/finalpage/2023-05-15/1216810622.PDF) · [发行人 2023-51](https://static.cninfo.com.cn/finalpage/2023-05-17/1216831409.PDF) · [发行人 2023-56](https://static.cninfo.com.cn/finalpage/2023-05-19/1216852113.PDF)

Sidecar 的新增消费窗口为 **000540.SZ 在 `[2023-05-15, 2023-05-19)` 的四个来源会话**，只供冻结回放的 `EXECUTION_STATE` 使用。每个键绑定同一份 4 月 29 日公告，并以该日 09:30 为消费截点。5 月 13 日披露佐证持续身份。5 月 19 日及之后的披露只解释停止边界和后续权益。

5 月 15 日原始行仍为 `st_status=2`、`suspended=0`、`is_risk_warning=1`。适配器原先给出 `is_st=null`，Sidecar 以发行人证据补足该字段。BigQuant 历史字典版本仍为 `UNKNOWN`。停牌状态和原价继续取自冻结来源，订单仍由原执行器按历史 5% ST 涨跌幅、T+1 和其他拒单规则处理。实跑中 5 月 15 日的卖单结果是 `sell_price_limit`，000540.SZ 在四个新增会话的成交笔数为零。 [状态证据及字节身份](state-evidence.json)

## 冻结计划与账户连续性

从合并 [#105](https://github.com/Wind-Maker1001/FactorLab/pull/105)、[#106](https://github.com/Wind-Maker1001/FactorLab/pull/106) 和 [#107](https://github.com/Wind-Maker1001/FactorLab/pull/107) 的主线 `9af38d53cc35296e8e1e0c99e99d12da90d40bb7` 建立工作树。回放沿用 D04 的条件研究入口、共享逐会话执行、费用及账本审计，冻结预测、目标、调仓决策和配置均与原收据的字节哈希一致。使用冻结 128 只证券和原折日历，按计划在信号收盘提交下一会话目标。

D04 保存了累计账本及收据。FIFO 批次、现金与上一收盘估值由从原折起点重放的同一账户恢复。到 5 月 12 日时，对交易、持仓、NAV、拒单、目标对照和估值资格六张私有原表逐字段精确比较。D04 的已提交收据固定了运行 JSON，六张 Parquet 的身份由这次完整内容比较建立。41 个既有会话沿用 D04 的成果计数。

每个新增会话先核对实际消费的价格与状态，再接受收盘结果。四个会话的新增持仓估值均取自有成交量的真实原价收盘。此前获准的停牌估值仍按具名政策单独核验。共享审计将成交、费用、现金、股数及权益逐项勾稽，并按冻结费率独立复算每笔费用。验收只保存已通过的账户结果，5 月 19 日的成交、现金、持仓和 NAV 均不进入累计账本。 [执行收据](execution-results.json)

本结果属于原价交易成本与条件估值。PR #106 的独立 P10 价格资格门禁继续适用其消费者。PR #105 的现金分红覆盖适用于已验证的 FactorScore 和 Ridge 账户，LightGBM 的组合总收益仍为 `TOTAL_RETURN_UNAVAILABLE`。

## 5 月 19 日的实际阻断

5 月 18 日收盘的 133,700 股仍在账户内，原价估值为 0.40 元/股。2023-56 号公告确认该收盘为触及交易类强制退市前的最后交易收盘，5 月 19 日开市起停牌，并明确无退市整理期。冻结来源当日 `suspended=1`、`raw_close=null`，既有有限停牌估值合同没有该日资格。执行停在 `UNQUALIFIED_HELD_CLOSE: 2023-05-19/000540.SZ`。 [停牌公告](https://static.cninfo.com.cn/finalpage/2023-05-19/1216852113.PDF)

深交所 6 月 15 日作出终止上市决定，6 月 30 日摘牌。7 月的股份确权公告给出退市板块代码 400174。发行人随后公告自 8 月 30 日挂牌当日起暂停转让。继续记账需要适用于冻结账户的停牌期估值依据，以及从 A 股退出登记到后续股份确权、处置或最终结算的账户权益凭证和处理规则。 [终止上市决定公告](https://static.cninfo.com.cn/finalpage/2023-06-16/1217074647.PDF) · [摘牌公告](https://static.cninfo.com.cn/finalpage/2023-06-29/1217155324.PDF) · [股份确权公告](https://static.cninfo.com.cn/finalpage/2023-07-07/1217260200.PDF) · [挂牌当日暂停转让公告](https://pdf.dfcfw.com/pdf/H2_AN202308281595939419_1.pdf)

鹏华 5 月 20 日披露对特定非 ETF 基金自 5 月 18 日起采用 0.11 元/股的估值。该价格有独立的公开时点与基金适用范围，5 月 19 日冻结账户仍缺少可消费的估值。 [鹏华估值公告](https://paper.cnstock.com/html/2023-05/20/content_1769923.htm) · [剩余阻断](remaining-blockers.json)

## 复现与用量

最终回放单 Python 进程，研究步骤耗时 1.15 秒，含解释器启动的命令耗时 3.07 秒，峰值工作集约 233 MiB。定向测试 **118 项通过**，pytest 计时 5.25 秒，独立测试进程峰值约 195 MiB。回放产物、公告原件和测试临时文件合计约 2.48 MiB，包含首次拒绝的装载尝试。来源读取仅投影原第一折的日历、原价、量和状态字段，28 个所需分片与冻结收据匹配。公开产物保存证据和聚合收据，私有账本留在本地。 [验证收据](verification.json)

```sh
python -I -B -X utf8 tools/research/run_p9_d05.py \
  --original '<P8_02_PRIVATE_OUTPUT>' --study '<P9_01_PRIVATE_OUTPUT>' \
  --d03 '<P9_D03_FINAL_RUN>' --d04 '<P9_D04_FINAL_RUN>' \
  --evidence-root '<P9_D01_PUBLIC_DOCUMENT_DIRECTORY>' \
  --bigquant-root '<EXISTING_BIGQUANT_ARCHIVE_DIRECTORY>' --output '<NEW_PRIVATE_DIRECTORY>'
```

```sh
python -I -B -X utf8 -c "import sys; sys.path[:0] = ['src', '.']; import pytest; sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/research/test_p9_d05.py', 'tests/research/test_p9_d04.py', 'tests/research/test_p9_market_evidence.py', 'tests/research/test_p9_02_recovery.py', 'tests/research/test_p10_consumer_admission.py']))"
```
