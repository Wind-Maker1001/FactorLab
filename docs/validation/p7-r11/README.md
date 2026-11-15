# P7-R11 工程验证

**NO LIVE MONEY · FINANCIAL_PROTOCOL_PENDING_APPROVAL · NOT_ESTABLISHED**

本目录只包含协议工程证据，未收集前瞻行情或交易所成交。空观察没有收益结果，`metrics` 为 `null`。

## 证据

- `protocol.json`：实际 `freeze` CLI 输出的 ExperimentTracker 权威 JSON envelope，包含协议版本、冻结时间、配置身份及 record SHA-256。金融配置保持待批准。
- `empty-evidence.json`：无新样本输入。
- `blocked.json`：实际 `evaluate` CLI 输出，绑定上述协议及空证据哈希。
- `tests.txt`：离线协议测试与现有 tracker 回归测试的真实 pytest 输出。测试中的日期与 receipt 是工程 fixture，不属于金融样本。

## 执行

源码检出尚未安装到当前 Python 环境，使用 `PYTHONPATH=src` 运行：

```sh
export PYTHONPATH=src
python -m pytest tests/test_grid_forward.py tests/research/test_experiment_tracker.py -q
python -m factorlab.experiments.grid_forward freeze \
  --store <new-private-store> --version engineering-v1 \
  --frozen-at <current-explicit-UTC-time>
python -m factorlab.experiments.grid_forward evaluate \
  --store <same-store> --identity <returned-id> \
  --evidence docs/validation/p7-r11/empty-evidence.json \
  --evaluated-at <time-at-or-after-freeze>
```

生成本目录时使用标准库 `tempfile.mkdtemp` 创建独立临时 tracker 目录，实际 UTC 时间由 `datetime.now(timezone.utc)` 取得，freeze 与 evaluate 使用同一时点；仅保存 CLI 输出，不复制 SQLite 或历史行情。复现既有身份时，将 `protocol.json` 复制到新 tracker 根目录的 `experiments/<id>.json` 后运行评价；评价读取权威 JSON，不依赖索引中的查询记录。保持相同评价时间可得到相同 JSON；使用新的冻结时间则按设计生成新身份。

测试覆盖：相同冻结身份不可覆盖、版本身份区分、篡改拒绝、已用历史/未来/未闭合/提前可知数据拒绝、缺失及重复 Bar 拒绝、影子触价与无交易所确认的 Demo 拒绝、对照时间/成本契约不可改写、固定输入结果复现，以及空样本明确不足。对照契约测试验证协议一致性，不是已运行金融对照账户或验证其经济收益。

首次 CLI 调用因该 Python 环境没有安装 `factorlab` 而失败（`ModuleNotFoundError`）；设置源码路径后重跑。不存在由失败调用产生的冻结记录或金融结果。

金融配置、有效外部可知性证据验证及执行/会计适配激活必须使用新的金融研究版本。本工程版本不能凭配置文件编辑变成收益评价；其待实现连接点在 `grid_forward.evaluate` 的 TODO 中。
