# P10-G02：冻结 CSI500 回放的原价门禁

**P10_G02_PARTIAL**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 实际消费者与合同

`tools/research/run_evidence_consumption.py:run` 是本次唯一接入消费者。既有调用链：`load_bound_evidence` 核对 sidecar、manifest、冻结结果及证据分片 → `checked` 核对保存预测/配置/目标身份 → `load_source` 投影指定证券和窗口 → `execution_bars` 状态适配 → `AShareExecutionBacktester.run_target_weights` → `value_positions` / `audit_valuation_ledger`。

新增 `replay_source_result` / `qualified_replay_prices` 位于来源加载后、价格诊断和任何输出目录/目标/成交写入前。它绑定 `BigQuant/cn_stock_real_bar1d` 和 manifest 原始字节 SHA256；预期版本独立取自已经验证的 sidecar `source_manifest_sha256`。来源价格字节另由既有分片 receipt 与 manifest 双重核对。不新增命令行绕过开关。

原字段 `open/high/low/close` 分别映射到 `raw_open/raw_high/raw_low/raw_close`。名称映射仅记录原字段身份，不构成原价证明。每个字段要求 P10-G01 `EXECUTION` / `CNY/share` 合同、精确版本及证券日期覆盖、OBSERVED_RAW、NONE 调整政策、RESEARCH_QUALIFIED 和证据；由既有 `require_fields` 实现。其余基准 ADJUSTED_CANDIDATE、SUPPLIER_FILLED、MODEL_VALUATION_ASSUMPTION、UNKNOWN 不授予成交用途。

**本次没有任何真实字段新增限定资格。** 当前适配器只有字节身份，没有版本绑定的字段单位、原价/填充政策、覆盖及证据合同，所以真实来源对象无条件产生 `CONTRACT_MISSING_OR_UNKNOWN`，不会进入价格诊断、成交或估值。缺合同是预期安全拒绝；不得为兼容旧回放取消检查。已有 CSMAR 601857 控制案例维持原证券/版本/范围，不能用于此来源。

当前为回顾回放；证据状态 cutoff 仍属于既有定向状态事实，不代表全部价格具备信号时点资格。门禁接受显式历史 cutoff 时，未知 available_at 继续拒绝。将来接入合格来源还需明确历史可得认证状态，不得以回顾资格冒充 PIT。

## 身份边界

证据继承自 `docs/validation/dq-03/`、`docs/validation/p3-d06/membership-identity-status.md` 和 `docs/validation/tri-source/semantics/identity-membership.csv`。

- 300114.SZ 最后旧代码成员观察到 2025-02-14；302132.SZ 代码变更自 2025-02-17 开市有效。法律主体和股份连续性有公告支持，但稳定供应商 ID、历史查询命名空间及价格实际身份合同仍 UNKNOWN；成员观察不能证明最后/首次实际交易。消费者拒绝旧码在生效日之后、新码在生效日之前的行，不映射或回填任何行。283 个旧码成员缺键原样保留。
- 600068.SH 2021-09-13 退市，换股基准 2021-09-10；601868.SH 2021-09-28 上市。1:4.4337 是不同证券权益承接，不是 1:1 重命名。消费者拒绝退市后的旧证券行情及上市前的新证券行情；不提供持仓转换功能。九个 2021-09-13—09-27 成员日的实施版本、登记、现金选择权、零碎股、实际股份入账和结算边界未闭合，持仓承接保持阻断。

日期合法只排除明确冲突，不赋予证券或价格资格；两个代码不能作为全局别名。当前冻结第一折窗口不覆盖这两次事件，因此身份案例为定向拒绝探针，不宣称实际冻结持仓已穿越事件。

## 兼容与未闭合证据

合成合格对象经过消费者门禁后 DataFrame 精确不变；同时运行已有价格适配器及证据消费回归，覆盖原价标签、执行及独立估值的既有边界。合成探针不属于真实 CSI500 认证。

真实冻结输入仍被新门禁拒绝，未进行新的模型计算、目标或账本生成。旧预测、因子定义、标签成熟期、目标和保存账本均不改写。真实同资格样本集合、因子、标签、目标、费用、持仓、NAV、拒单及估值状态的全链比较 **未闭合**：此来源没有可进入比较的合格对象。不能以删样本、更换供应商或缩日期制造一致性。

TODO：获得精确来源版本字段合同及来源证券身份合同后，接入合格对象并完成冻结回放全链兼容验证；增加权益股份及结算证据测试。当前不授予 ENGINEERING_ACCEPTED。

## 验证

定向命令：

```text
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m pytest tests/research/test_p10_consumer_admission.py tests/research/test_price_consumption.py tests/research/test_evidence_consumption_study.py -q -p no:cacheprovider
```

仅运行测试和既有公开证据读取，不读取商业行情、不扫描三源、不训练、不复制冻结行情。新增测试覆盖合格输入不变、缺来源合同、错误版本、调整/填充/模型价、未知历史可得时间以及四个无效身份日期。换股结算和真实同资格冻结回放仍为上述 TODO。
