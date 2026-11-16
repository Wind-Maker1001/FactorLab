# P7-R13 前瞻观察验证

**NO LIVE MONEY · UNVERIFIED_RECEIPT · ECONOMIC_ADVANTAGE_NOT_ESTABLISHED**

## 方法

观察源为 Demo GET-only Grid Preview，使用不变的 P7-M13 factory。每个窗口的实验事件先于网络访问持久化；窗口各 600 秒、600 GET、5 MiB、20 提案，轮询延迟五秒，最大报价间隔 30 秒。原始 Bar/盘口保留在 events.csv；规范化证据绑定原事件规范化内容摘要及原实验声明。请求总数、报价数、报价提案配对数和独立提案数分别计数。

第一窗口已完成：553 GET、29 个盘口、9 个 live Bar、1 个独立提案、26 个前瞻配对，零触价、1 个右删失；POST/DELETE 均为零。绝对期限由既有采集器记录成晚于冻结截止的 CAPACITY_LIMIT，R12 原样拒绝为 TERMINAL_OBSERVATION_INTERRUPTION、INVALID_TERMINATION_TIME。原记录不裁剪或重标。第二窗口也已完成：554 GET、29 个盘口、1 个独立提案、24 个前瞻配对，同样因 TERMINAL_OBSERVATION_INTERRUPTION、INVALID_TERMINATION_TIME 被拒绝。两窗口均使用采集源码提交 `59a54ccce228f5e0e4a6ed66f3330f69da26aff9`；运行中对磁盘源码的后续修正不改变已加载的采集进程或历史证据。

| 计数口径 | A | B | 合计 |
|---|---:|---:|---:|
| 独立窗口 | 1 | 1 | 2 |
| GET | 553 | 554 | 1107 |
| 捕获盘口 | 29 | 29 | 58 |
| 独立提案 | 1 | 1 | 2 |
| 描述性前瞻报价配对 | 26 | 24 | 50 |
| R12 合格报价 / 配对 | 0 / 0 | 0 / 0 | 0 / 0 |
| midpoint touch / opposite-book crossing | 0 / 0 | 0 / 0 | 0 / 0 |
| 右删失提案 | 1 | 1 | 2 |
| POST / DELETE | 0 / 0 | 0 / 0 | 0 / 0 |
| 保留证据字节（含分摊前完整共享 summary） | 254956 | 253277 | — |

A 冻结窗口为 `2026-11-16T13:28:55.435416Z` 至 `13:38:55.435416Z`，记录结束为 `13:38:55.790138Z`；B 为 `13:39:11.239329Z` 至 `13:49:11.239329Z`，记录结束为 `13:49:12.150735Z`。两者未续期或重启。原策略定义摘要为 `fbf38aaaba653e366e2b032f32fc98201e556e1760c204341216be6215c14774`。

两个 BUY LIMIT 分别为 155.14000、155.81000，数量均 0.04。首个前瞻 bid/ask 距离分别为 -103.974/-108.391 bps、-86.531/-106.045 bps；最小绝对 midpoint 距离分别 107.322、97.234 bps。未观察到触价或 crossing，因此触价延迟和 crossing 数量覆盖均不可估计；右删失为 2/2。盘口距离不是成交价或收益。

真实窗口均为 `INVALID_EVIDENCE` / `INSUFFICIENT_SAMPLE`，不授予 `VALID_ENGINEERING_OBSERVATION`。后者仅由人工定向测试验证合法路径。真实性仍 `UNVERIFIED_RECEIPT`，经济结论 `ECONOMIC_ADVANTAGE_NOT_ESTABLISHED`；R12 工程与金融 metrics 均 null。没有追加窗口来替换被拒绝证据。未来采集的 deadline 类型/有效结束时间修正通过 **46 passed（14.31 秒）**，见 `deadline-tests.xml`，不用于重写这两个窗口。受影响回归与 R13 集 **166 passed（61.57 秒）**，记录为 `targeted-tests.xml`；随后离线 CLI 发布与缺失数量补充后的 R13 集 **20 passed（2.59 秒）**，记录为 `r13-final-tests.xml`。

## 复现

在仓库根目录运行，所有输出使用新目录；原始窗口不可重启或覆盖。

```bash
PYTHONPATH=src python -m pytest -q \
  tests/integrations/test_forward_observation.py \
  tests/integrations/test_r13_forward_observation.py \
  tests/integrations/test_demo_market_feed.py \
  tests/integrations/test_demo_grid_supervisor.py \
  tests/integrations/test_binance_demo_grid_bridge.py \
  tests/test_grid_forward_evidence.py tests/test_grid_forward_observation.py \
  --junitxml=/path/to/new-targeted-tests.xml

PYTHONPATH=src python -m factorlab.integrations.binance_futures_trading.forward_observation \
  --output /path/to/new-window-a --experiment-id p7-r13-new-window-a \
  --version "$(git rev-parse HEAD)" --seconds 600 --window-seconds 600 \
  --sample-seconds 5 --max-quote-gap-seconds 30 \
  --max-requests 600 --max-proposals 20 --max-bytes 5242880

# 第二窗口使用另一个全新目录和实验 ID；各自重新冻结 UTC 起止时间。
PYTHONPATH=src python -m factorlab.experiments.grid_forward_observation \
  --events /path/to/window-a/events.csv /path/to/window-b/events.csv \
  --protocol docs/validation/p7-r11/protocol.json \
  --evaluated-at '<UTC timestamp after both frozen deadlines>' \
  --output /path/to/new-normalized-evidence
```

本次离线评价时间为 `2026-11-16T13:49:21Z`。重放已发布证据（输出目录必须全新）：

```bash
PYTHONPATH=src python -m factorlab.experiments.grid_forward_observation \
  --events docs/validation/p7-r13/window-a/events.csv docs/validation/p7-r13/window-b/events.csv \
  --protocol docs/validation/p7-r11/protocol.json \
  --evaluated-at 2026-11-16T13:49:21Z --output /path/to/new-audit
```

A/B 原始事件内容摘要分别为 `ba28f39ffe07b66e4ee4c76e12ae10c712a777caf4ccd06dfe4bd55d3266ecdf`、`b21c88dde1d30c78a3ba2c1799b3cc478d46ae246360211581b18dc23d52dd46`；规范化证据摘要分别为 `5744bbc6a38964e6b4d0c22d61626479d45058211e7ed354dda61f920de17ee9`、`1d588737f42c4fd639b3864b740c1a0d86d59abdf8cbbaeb0934ce4b78f0103c`。

独立审计先核对 separately retained trusted digest，再通过规范化命令重放。摘要证明内容一致性，不证明外部真实性。R12 的时间、Bar 可知性、账户/策略绑定、报价首尾与提案内部间隔门禁原样执行；不为放行缩短窗口、删除重复或隐去中断。缺失数量不填零，R12 结构拒绝保留为 R12_MALFORMED_INPUT。

原始流限额小于总预算的三分之一，余量用于采集 summary、规范化窗口和共享汇总；适配器写入前检查每窗口总保留大小。Preview/feed SQLite 是本地执行状态，不发布。签名账户响应及凭据不进入研究证据。
