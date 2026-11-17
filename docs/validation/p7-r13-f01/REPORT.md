# P7-R13-F01：真实前瞻窗口与 R12 准入复验

**NO LIVE MONEY · UNVERIFIED_RECEIPT · ECONOMIC_ADVANTAGE_NOT_ESTABLISHED**

使用原有 M15/R13 GET-only Preview 采集器与 R12 合同，历史 R13 两个拒绝窗口保持不变。新实验身份为 `p7-r13-f01-valid-forward-window-a`，采集源码为 `bc9000d`。正式窗口与 Demo 写入实验分开安排。

冻结配置为 600 秒、600 GET、20 提案、5 MiB、5 秒轮询延迟、30 秒最大报价间隔；轮内串行 GET 的额外最小间隔为 0。策略沿用 P7-M13 factory 默认定义，不修改 Demo 策略运行参数。实验事件在首次网络请求前冻结 UTC 起止、策略定义摘要、来源和终止规则。发生中断立即停止，不恢复、不续期、不根据中途结果修改配置。

冻结窗口为 `2026-11-16T14:54:55.681556Z` 至 `15:04:55.681556Z`，但真实采集于 `14:56:11.925134Z` 记录 `R13_QUOTE_GAP` 中断，随后记录 `DATA_INTERRUPTION`，没有续期或重启。两个盘口本地接收时间相隔 31.121846 秒，超过冻结的 30 秒门限；HTTP 请求本身没有报告错误，不能将此描述为已证实的网络断连。窗口没有运行到正常期限，因此正常到期仅有定向测试证据，不能据此窗口宣称真实到期验收通过。

实际为 62 GET、0 POST、0 DELETE、2 个盘口、1 个独立 BUY LIMIT 提案（159.08000，数量 0.04）、0 次触价及 crossing，1 个右删失（100%）。第二个盘口的市场时间及本地接收时间均早于提案捕获时间，不能作为合格前瞻报价配对。所有失败事实保留，不将窗口换身份重新计数。

首次离线评价在冻结截止之前执行，原有 R12 抛出 `ValueError: invalid evaluation window`，未输出规范化目录。正式评价使用实际 UTC `2026-11-16T15:06:06.824383Z`，晚于冻结截止，原有 CLI 正常完成。R12 结果为 **INVALID_EVIDENCE / INSUFFICIENT_SAMPLE**，实际合格窗口、合格报价和合格前瞻配对均为 **0**；工程及经济 metrics 均为 null。完整拒绝码为 `DECLARED_OBSERVATION_GAP`、`QUOTE_GAP`、`TERMINAL_OBSERVATION_INTERRUPTION`、`MISSING_FORWARD_QUOTE`（提案 `preview-g:preview-g-00000002`）。没有将真实中断改写为正常到期或 CAPACITY_LIMIT。

原始事件有两个 Bar receipt，其中一个为初始化历史 Bar；规范化证据包含一个完成的 live Bar：开盘 `14:55:00Z`、收盘 `14:56:00Z`、接收 `14:56:11.322177Z`、可知 `14:56:11.878402Z`、决策 `14:56:11.886868Z`。缺失的是提案捕获后的前瞻报价及冻结窗口剩余观察；两个已捕获盘口的 bid/ask 数量均有记录。规范化 gaps 原样记录 `R13_QUOTE_GAP` 与 `EARLY_WINDOW_END`，R12 diagnostics 为 1 quote gap、2 declared interruptions。原始事件、采集 summary、规范化窗口与汇总共 57195 字节，小于 5 MiB。

策略定义摘要为 `fbf38aaaba653e366e2b032f32fc98201e556e1760c204341216be6215c14774`。原始事件规范内容摘要为 `393cd4f90a38e4357222eb30decb2d450636b1d6ecba77a200cea930eac39d0d`，R12 证据摘要为 `36874cc7e451ca6a94e87be0f799b146d38732df41ae4231600e6439ef0f7633`。沿用现有来源身份检查，未合并或重标历史窗口。报价触价不是交易所成交，本次确认成交数为 0，不授予金融准入，不得据此推断策略盈利。

定向回归 **181 passed（52.46 秒）**，见 `targeted-tests.xml`，覆盖现有期限、晚到记录、真实中断、缺失窗口结束、来源独立性、重复记录、报价完整性、Bar 可知时间、右删失、历史原始证据保留和 R12 准入路径。

## 独立审计

以下命令只读已发布原始事件，输出目录必须全新。已在独立目录执行成功，重放获得相同证据摘要、计数及四个拒绝码；不发布第二份重复原始数据。R12 完整评价、diagnostics 与描述性观察位于 `normalized/window-1.json`，多窗口口径汇总位于 `normalized/summary.json`。摘要用于内容一致性审计，不升级外部回执真实性。

```bash
PYTHONPATH=src python -m factorlab.experiments.grid_forward_observation \
  --events docs/validation/p7-r13-f01/window-a/events.csv \
  --protocol docs/validation/p7-r11/protocol.json \
  --evaluated-at 2026-11-16T15:06:06.824383Z \
  --output /path/to/new-independent-audit
```

交付仅含原始事件、采集 summary、规范化证据、报告及定向测试结果；本地 SQLite 执行状态不作为研究证据发布。采集源码完整 HEAD 为 `bc9000d42735f917fcb0559648118af9a363cf93`；交付提交 HEAD 由 Draft PR 分支标识，可运行 `git rev-parse HEAD` 获取。

## 测试复现

```bash
PYTHONPATH=src python -m pytest -q \
  tests/integrations/test_forward_observation.py \
  tests/integrations/test_r13_forward_observation.py \
  tests/integrations/test_demo_market_feed.py \
  tests/integrations/test_demo_grid_supervisor.py \
  tests/integrations/test_binance_demo_grid_bridge.py \
  tests/test_grid_forward_evidence.py tests/test_grid_forward_observation.py \
  --junitxml=/path/to/new-targeted-tests.xml
```
