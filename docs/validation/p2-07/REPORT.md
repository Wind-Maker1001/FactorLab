# P2-07 Walk-Forward 动态因子选择

`P207_ENGINEERING_ACCEPTED`，仅软件工程验收。

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

基线 fetch 后为 `11ec87d2dfe677ea1b8ff03b4e268f2a4eb6b7a6`，
Tree `bc5e1186917fd18d20c8e60e82ae4fd3aa7e286c`。分支
`feat/p2-07-walk-forward-selection`，worktree 位于
`D:/Code/FactorLab-worktrees/p2-07-walk-forward-selection`。
最终 Commit/Tree 由 Git/PR head 确定，交付时另写本地
`local-validation/delivery.json`，避免提交内容自引用哈希。仅创建 Draft PR，不自动合并。

## 实现与接口

- `src/factorlab/validation/factor_selection.py`：冻结的 `FactorSelectionConfig`、
  `TrainFactorSelector(snapshot, factor_refs, config)` 和 `FactorSelectionResult`。
  每折只接收 Splitter 已 purge、完成标签成熟度过滤的 Train，包含尚无目标值的形成行；
  RankIC 仅使用有限成对观测。复用现有 `_PreparedFactors`、`_daily_ic`、`_summarise_ic`。
- `src/factorlab/validation/walk_forward.py`：可选 `factor_selector=None`，默认固定模式。
  用浅副本绑定每折特征与权重，不改写 Runner 配置，不跨折缓存选择。
  原模型工厂、构造参数、引用合同、超参数选择和异常不变；不修改四模型算法。
- `src/factorlab/exploratory/factor_discovery/redundancy.py`：新增默认关闭的
  `summary_only`，复用逐日 Pearson 计算，省去逐日归档；原调用和数学不变。
- `validation/__init__.py` 导出接口；新增测试
  `tests/research/test_walk_forward_factor_selection.py` 和合成演示
  `examples/research/walk_forward_factor_selection_demo.py`。

```python
from factorlab.validation import FactorSelectionConfig, TrainFactorSelector, WalkForwardRunner

selector = TrainFactorSelector(snapshot, tuple(pinned_refs), FactorSelectionConfig(
    k=10, minimum_selected=1, shortfall_policy="use_available",
    minimum_coverage=0.8, maximum_missing_ratio=0.2, maximum_nonfinite_ratio=0,
    minimum_observations=100, minimum_cross_section=20, minimum_dates=20,
    minimum_abs_rank_ic=0.01, minimum_direction_consistency=0.5,
    minimum_pair_coverage=0.8, redundancy_threshold=0.95,
    orient_by_train_ic=True,
))
runner = WalkForwardRunner(splitter, model_name="ridge",
    feature_names=selector.feature_names, factor_selector=selector,
    ridge_alphas=(0.1, 1.0), model_directory=output / "models")
result = runner.run(dataset, calendar=calendar)
```

`top_k` 仍为原预测评估的组合参数；选因子数量是 `FactorSelectionConfig.k`。
质量排序依次为绝对 RankIC 均值、方向一致率、覆盖率、FactorRef 身份。
稳定性保存 RankIC 标准差、RankICIR 与方向一致率；标准差为零时 RankICIR 为未定义诊断值，
不把它当成零或额外的通过证据。最低日期数和方向一致率是显式过滤条件。

相同定义哈希和递归别名先去重且核对 Train 数值；冲突明确拒绝。
其余合格因子用逐日横截面的平均绝对 Pearson 相关性判冗余。
证据不足为 `UNDETERMINED`，保守拒绝排序较低者。全窗口数值重复也单独标记。
先确定独立合格池再取 Top-K，不足 K 时明确使用实际数量，或设置 `shortfall_policy="error"`。
不足最低数量时抛出 `FactorSelectionError`，含逐因子原因及可保存的 `quality/redundancy` 表。
缺失值保持缺失；显式允许的非有限值在模型入口转为缺失，沿用各模型既有缺失处理。

方向为 Train RankIC 均值的符号，四模型统一输入 `direction_i * X_i`。
动态 FactorScore 必须显式指定 `dynamic_factor_score_weighting`：
`equal` 对已定向特征使用 `1 / selected_count`；`configured_subset` 保留入选特征的配置权重，
遵守原 `normalize_by_absolute_weight`。后者对原始 X 的有效系数是 `weight_i * direction_i`，
已有负权重可能抵消定向；要完全保持配置方向，应将 `orient_by_train_ic=False`。
固定模式仍要求完整权重，不改变默认规则；动态模式也不补权重或替换原模型异常。

## 审计与无泄漏证据

`WalkForwardResult.factor_selection/factor_quality/factor_redundancy` 保存每折候选引用、
定义哈希、顺序、质量、拒绝原因、相关性摘要、规则版本、参数、方向与身份凭据。
既有 `diagnostic_audit` 绑定实际选中特征的训练输入、三阶段预测和保存模型哈希。
指定 `model_directory` 时，每个模型旁保存 `.selection.json`，绑定方向与实际模型特征，
质量和冗余表须随结果保存；演示已写出这些表，不保存逐因子逐日明细。

存在 V2 sidecar 时核对其校验和、实际数据内容、snapshot 和候选 pins；普通研究表生成内容加 pins
的研究身份，不宣称来源已验证。完整数据集身份会随 Test 数据变更；独立 `selection_hash`
仅绑定 Train 内容、规则和选择证据，因此验证泄漏使用后者。

27 项新增测试覆盖：逐折变化与短缺策略；Train 标签扰动改变对应折选择；Validation/Test
标签扰动不改变选择；Test 特征扰动不改变训练输入和已训练系数；purge、embargo、实际标签
退出日和不完整标签过滤；缺失、常量、非有限、证据不足、重复定义、别名、数值重复、高相关
与身份数值冲突；四模型实际 fit 列名、持久化特征、重载预测精确匹配、方向 sidecar；
四模型的输入行序与候选顺序可复现；自定义语义列名；质量失败与身份错误显式拒绝。
Validation 可以改变模型超参数选择，不允许改变特征选择。早期 Test 数据未来进入扩展 Train
是正常 Walk-Forward 行为，扰动测试按对应折界定隔离范围。

固定模式额外装载 main 原版 `walk_forward.py`，在同一合成面板、14 因子、四模型与参数下，
精确比较预测、Validation 候选、系数、重要性、三阶段指标及剔除路径后的折摘要，全部一致。
源码 SHA-256 见 [manifest.json](manifest.json)。

## 三折合成对照

预先固定原 14 因子、16 个有限网格 AST 定义、1 个相同定义 ID、1 个依赖别名，共 32 候选。
24 只合成股票、300 个生成会话，经原 DatasetBuilder 形成研究样本并验证 sidecar。
原模型与超参数候选一致。A 固定 14 因子等权；B 从 32 因子中逐折选 10 并按 Train 定向。
因此对照同时包含选择与方向规则的变化，不用于拆解两者的因果贡献。

??????? **5630 ?**??????? **3162 ?**?dataset identity
`f1003c7c515ee56108fb24c7d87c8da6910f240066ffbce35d203819c4d44f8b`?

| ?? | A ??14 RankIC | A ????? | B ?? RankIC | B ????? | A ? | B ? |
|---|---:|---:|---:|---:|---:|---:|
| factor_score | -0.007964 | 0.034615 | 0.338767 | 0.027643 | 27.65 | 54.39 |
| ridge | 0.408082 | 0.039870 | 0.421406 | 0.033453 | 29.99 | 60.58 |
| lightgbm | 0.414940 | 0.023345 | 0.420558 | 0.022853 | 28.67 | 57.03 |
| xgboost | 0.395081 | 0.030904 | 0.386617 | 0.039439 | 28.71 | 34.53 |

???????? OOS ?? RankIC ???????ddof=1???? RankIC ? OOS ??????

| ? | ?? Train | ?? Validation | ?? Test | ?????????10?? |
|---|---|---|---|---|
| 0 | 2023-04-03?2023-06-22 | 2023-07-03?2023-07-25 | 2023-08-03?2023-10-02 | REV5, PRICE_MA20, MOM20, MA20_MA60, MOM60, REV1, TURN5, VOL20, VOLUME_RATIO, AMIHU20 |
| 1 | 2023-04-03?2023-08-24 | 2023-09-04?2023-09-22 | 2023-10-03?2023-12-01 | REV5, PRICE_MA20, MA20_MA60, MOM20, MOM60, REV1, TURN5, TURN20, VOLUME_RATIO, synthetic_015 |
| 2 | 2023-04-03?2023-10-25 | 2023-11-03?2023-11-23 | 2023-12-04?2024-02-02 | REV5, PRICE_MA20, MA20_MA60, MOM20, MOM60, REV1, TURN20, TURN5, VOL20, AMIHU20 |

?????????????????????????????
??????? tracemalloc ????????B ???????????????
??????????????main ????????374.23 ????? deep memory 4.46 MiB?
Python ?????? 47.39 MiB?OS ???????? RSS 402.41 MiB?
OS ??????????????????????????????????????????
?????????A/B ?????????????????????????????

完整入选顺序和符号见 [selections.json](selections.json)，全部因子的入选频次见
[selection-frequency.csv](selection-frequency.csv)，质量与淘汰原因见
[factor-quality.csv](factor-quality.csv)，相关性摘要见 [factor-redundancy.csv](factor-redundancy.csv)。
[model-proof.json](model-proof.json) 保存 24 个已训练模型的实际特征、模型哈希、训练输入身份和
重载预测精确一致证据；[predictions.sample.csv](predictions.sample.csv) 仅保留小型预测样本。

以上只证明软件流程与分析方法。合成收益、构造的数据机制、单一种子和短样本不能说明真实市场优劣，
不构成金融研究认证或可交易性证据。

## 测试、复现与清理

使用现有 `D:/Environments/factorlab-py313/Scripts/python.exe`，未创建环境。
最终新增测试 27 PASS，退出码 0；直接相关回归 523 PASS / 2 FAIL，退出码 1；
补充 Splitter/因子发现测试 40 PASS / 1 FAIL，退出码 1。
最终三个验证范围共 590 PASS / 3 既有历史依赖失败，没有新增回归失败。
详细次数、耗时、退出码、命令及日志校验和见 [validation.json](validation.json)。

三个失败的范围与依据：

- `test_committed_public_package_receipt_expressions_and_aggregate_schema[synthetic/private]`：
  `docs/research/evidence/factor-discovery-{synthetic,private}-v2/receipt.json` 不在基线 Git tree，读取即失败。
- `test_old_factor_golden_configs_and_historical_artifacts_unchanged`：引用对象
  `48ea4dd8c1876c1515f3172c9bb2e812f20ebe82` 不存在，`git diff` 退出 128。

未改动这些测试、Oracle、Golden、V2 比较器、金融门禁或原模型算法。
首次新增测试因 basetemp 父目录缺失出现 4 个 setup ERROR，创建目录后通过；
演示开发时修正模块启动方式和写出后 sidecar identity 读取，不修改身份验证逻辑。
未执行双 Python 本地全量、Legacy 50 FAIL / 136 ERROR 矩阵、B4/B5 冻结或模型工厂独立复审。
Draft PR 的仓库自动 CI 独立运行，其状态以 GitHub 为准。

从仓库根目录 PowerShell 复现：

```powershell
$python = 'D:/Environments/factorlab-py313/Scripts/python.exe'
New-Item -ItemType Directory -Force local-validation | Out-Null
& $python -B -m pytest tests/research/test_walk_forward_factor_selection.py -q -p no:cacheprovider --basetemp=local-validation/repro-tests
$env:PYTHONPATH = 'src'
& $python -B -m examples.research.walk_forward_factor_selection_demo --output local-validation/repro-demo
```

可选 `--reference-runner <从上述 main SHA 提取的原版 walk_forward.py>` 重做固定模式精确对照。
完整相关测试命令保存在 `validation.json`，不需重做历史失败矩阵。

本轮临时目录均在指定 worktree 根下；保留小型不可再生运行收据、原始日志/XML 与交付身份，
删除测试 basetemp、完整合成面板/特征文件、完整预测和可重建模型，不保留重复环境或逐日因子档案。
后续可增加显式研究 CLI/YAML 和外部固定的可信 dataset identity 输入；大候选集相关性计算仍为
日期数乘候选数平方，先做容量测量再扩容。PIT、HFQ 和正式准入需要独立证据与流程。
