# P7-R13 前瞻观察验证

**NO LIVE MONEY · UNVERIFIED_RECEIPT · ECONOMIC_ADVANTAGE_NOT_ESTABLISHED**

## 方法

观察源为 Demo GET-only Grid Preview，使用不变的 P7-M13 factory。每个窗口的实验事件先于网络访问持久化；窗口各 600 秒、600 GET、5 MiB、20 提案，轮询延迟五秒，最大报价间隔 30 秒。原始 Bar/盘口保留在 events.csv；规范化证据绑定原事件规范化内容摘要及原实验声明。请求总数、报价数、报价提案配对数和独立提案数分别计数。

采集状态：尚未启动真实窗口。受影响回归与 R13 集 **166 passed（61.57 秒）**，记录为 `targeted-tests.xml`；随后离线 CLI 发布与缺失数量补充后的 R13 集 **20 passed（2.59 秒）**，记录为 `r13-final-tests.xml`。

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

独立审计先核对 separately retained trusted digest，再通过规范化命令重放。摘要证明内容一致性，不证明外部真实性。R12 的时间、Bar 可知性、账户/策略绑定、报价首尾与提案内部间隔门禁原样执行；不为放行缩短窗口、删除重复或隐去中断。缺失数量不填零，R12 结构拒绝保留为 R12_MALFORMED_INPUT。

原始流限额小于总预算的三分之一，余量用于采集 summary、规范化窗口和共享汇总；适配器写入前检查每窗口总保留大小。Preview/feed SQLite 是本地执行状态，不发布。签名账户响应及凭据不进入研究证据。
