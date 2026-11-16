# P7-R12 验证证据

**R12_ENGINEERING_ACCEPTED · NO LIVE MONEY**

工程验收记录与真实金融资格分开：本文目录中的人工样本只验证适配器，不能成为前瞻经济研究样本。金融状态保持 `FINANCIAL_PROTOCOL_PENDING_APPROVAL`、`ECONOMIC_ADVANTAGE_NOT_ESTABLISHED`。

## 复现

使用项目 Python 3.13 环境，在仓库根目录执行：

```bash
python -m pytest tests/test_grid_forward_evidence.py tests/test_grid_forward.py \
  tests/research/test_experiment_tracker.py -q \
  --junitxml=/path/to/new-targeted-tests.xml

PYTHONPATH=src python tools/reproduce_p7_r12.py \
  --output /path/to/new-r12-evidence \
  --commit d9b608d01bd307b95e07030104a7620aa36b8d84 \
  --tree 85ab9d008f34767aab09d191b53da1e5107b5c61

PYTHONPATH=src python -m factorlab.experiments.grid_forward_evidence \
  --protocol docs/validation/p7-r11/protocol.json \
  --evidence docs/validation/p7-r12/synthetic-shadow.json \
  --evaluated-at 2026-11-15T12:31:00Z

PYTHONPATH=src python -m factorlab.experiments.grid_forward_evidence \
  --protocol docs/validation/p7-r11/protocol.json \
  --evidence docs/validation/p7-m13/real/events.csv --m13 \
  --start 2026-11-15T12:26:00Z --end 2026-11-15T12:30:00Z \
  --max-quote-gap-seconds 30 --evaluated-at 2026-11-15T12:31:00Z
```

复现工具仅读取仓库小型证据，新建独立输出目录和临时 tracker，导出不可变 JSON 后移除临时索引。已有目录会失败而非覆盖。无需凭据或网络，不启动 Paper 账户或交易。指定 commit/tree 标记开发基线；`adapter_sha256` 另外绑定实际适配器源码，不将修改中的源码冒充该基线的干净提交。inputs.json 记录原始证据文件摘要。Python 环境版本变化会改变 run 外壳摘要，但不改变输入和评价结果。

## 证据含义

- `synthetic-shadow.json`：显式人工、冻结后的合法输入，1 个 Bar、1 个提案、3 个盘口；不触价且右删失。不是实际市场或交易所成交。
- `evidence/synthetic-result.json`：工程合法，金融 metrics=null；Bar 覆盖率为 0.5（60 秒 / 120 秒），提案报价覆盖率为 1，右删失比例为 1。
- `evidence/m13-result.json`：读取已发布 M13 的 1 个提案、10 个盘口。提案早于 R11 冻结，Bar 完整可知证明未发布；工程状态 INVALID_TIMELINE，样本 INSUFFICIENT_SAMPLE，工程指标与金融指标均 null。
- `evidence/*-experiment.json`、`*-run.json`：既有 ExperimentTracker 发布的独立 R12 身份。run 的 SUCCEEDED 指评价程序完成，不表示样本准入成功。qualification=UNKNOWN；不授予金融资格。复现工具本身不运行 pytest，因此该 run 的 test_status=NOT_RUN；定向测试结果单独由下述 JUnit 记录。
- 不复制原始 M13 CSV、行情、SQLite 或账户账本；输入文件路径和摘要见 `evidence/inputs.json`。

## 定向验证

Python 3.13 定向测试实际结果：**73 passed in 10.12s**；JUnit 记录在 `targeted-tests.xml`。只读 CLI 与证据复现工具均已实际执行成功。覆盖合法影子、历史冒充前瞻、未闭合 Bar、时间倒置、缺报价、重复/冲突/乱序、未来价格、观察首尾和内部缺口、重启重放、终止中断、右删失、触价冒充成交、缺失成交确认、完整 Demo 仍不证明外部真实性、来源摘要与摘要计数不一致、R11 身份/外壳/内容篡改、未批准时指标恒 null，以及新 R12 tracker 记录不可变。

金融激活缺口和既有 Paper/FIFO/资金费接线边界见 [研究契约](../../research/p7-r12-forward-evaluation.md)。
