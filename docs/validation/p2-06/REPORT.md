# P2-06 实施与验收

`P206_ENGINEERING_ACCEPTED` / `FINANCIAL_ADMISSION_BLOCKED`

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

启动 fetch 后，origin/main 为 `82db7be8598d12d1967063a12fbd88929dea59fb`，
Tree `fab6d3c30caa9111f2b5c0ba46b3dd01633df522`，与任务给定值一致。
分支 `codex/p2-06-dataset-registry-v2` 从该 main 建立，独立 worktree 位于
`D:/Code/FactorLab-worktrees/p2-06-dataset-registry-v2`。
最终 Commit/Tree/PR 和远端 CI 状态由 PR head 及本地 `local-validation/delivery.json` 记录，
避免在提交内容中自引用该提交的哈希。

## 实施和 API

- `src/factorlab/dataset/builder.py`：可选关键字参数 `snapshot`、`factor_refs`、
  `data_context`、`data_version`、`calendar_id`；`factor_names` 可由有序引用推导，
  若同时提供则必须精确一致。默认调用继续使用原计算路径。
- V2 调用原 `snapshot.compute()`，共用原 PIT、状态过滤、标签、市场特征和横截面预处理；
  保留历史预热和缺失日，不填充。拒绝重复/不匹配引用、受保护字段和输入字段冲突，
  包括预处理即将生成的 `raw_<factor>` 字段。
- `src/factorlab/dataset/identity.py`：独立 JSON sidecar、内容/文件哈希及读回核对。
  `DatasetBuilder.write_artifacts()` 在 V2 下返回 sidecar 路径，旧模式仍返回 None；
  `read_artifacts(..., calendar=..., expected_identity_hash=..., expected_input_hashes=...)`
  验证后返回标准 DataFrame。V2 读回必须有 sidecar。
- `examples/research/dataset_registry_v2_demo.py`：有限发现生成器 → 注册 → 冻结 →
  DatasetBuilder → 写入并验证读回 → 原四模型 → 原 WalkForwardRunner。
- `tests/research/test_dataset_registry_v2.py`：67 个新增用例；本目录保存小型验证收据、
  sidecar 样例和预测样本。未改原因子公式、模型、工厂、Walk-Forward、Oracle、Golden 或金融门禁。

使用方式（`bars` 等为标准输入，包含完整预热日历）：

```python
from factorlab.dataset import DatasetBuildOptions, DatasetBuilder
from factorlab.factors.registry_v2 import FactorDataContext, FactorRegistryV2

snapshot = FactorRegistryV2.with_builtins().freeze()
builder = DatasetBuilder(
    DatasetBuildOptions(),
    snapshot=snapshot,
    factor_refs=[snapshot.get("MOM20", "1").ref],
    data_context=FactorDataContext("caller-declared-source", "UNVERIFIED"),
    data_version="explicit-input-release-v1",
    calendar_id="explicit-calendar-release-v1",
)
dataset = builder.build(bars, benchmark, membership, status, listings, calendar)
sidecar = builder.write_artifacts(dataset, dataset_path, features_dir)
verified = builder.read_artifacts(dataset_path, features_dir, calendar=calendar)
```

sidecar 记录全快照哈希、有序引用/定义哈希、解析后的依赖定义（含价格基准及可得时间）、
调用方来源与版本、五类输入内容哈希、日历 ID/会话哈希、研究区间、预处理配置、
完整数据集及原始/预处理特征文件 SHA-256。NaN 位模式在内容哈希中统一；列顺序和类型参与身份，
输入行/索引顺序不参与身份。文件哈希覆盖实际 Parquet 字节。
每个文件原子替换，sidecar 最后写入；多文件替换不承诺事务或并发发布，读方验证可发现不完整组合。
外部保存的 `expected_identity_hash` 可绑定已有研究运行；checksum 本身不是数字签名，
不防止所有文件与未锚定 sidecar 同时被恶意重写。声明来源、软件定义一致性均不等于金融数据认证。

## 验收结果和复现

使用已有 Windows Python 3.13.9 环境，未创建虚拟环境。NumPy 2.3.4、pandas 2.3.3、
LightGBM 4.6.0、XGBoost 2.1.4。

| 运行 | 实际结果 | 退出码 |
|---|---|---|
| 首轮新增集成测试 | 50 PASS / 4 FAIL | 1 |
| 修复 NaN 哈希及三处测试预期后 | 53 PASS / 1 FAIL | 1 |
| 定位并修复演示 audit 表字段引用后，单独模型测试 | 1 PASS | 0 |
| 针对性验收及已有回归，10 个测试文件 | 206 PASS | 0 |
| 最后补充 raw 特征字段碰撞防护后的边界复验 | 66 PASS / 1 deselected | 0 |
| 最终独立四模型演示 | 成功 | 0 |

最终覆盖 **207 个不同测试节点**（有重叠，不把复验累加成独立测试数量）。
第一轮失败包含真实的 NaN 内容哈希缺陷；另外三项为测试未考虑末尾 NaT、
缺成交日被过滤、缺状态日被保守排除。第二轮剩余失败为演示错误地从 fold_summary
读取仅存在于 diagnostic_audit 的 feature_names；均已修复。没有运行历史 Legacy 全矩阵。

覆盖：旧 14 因子完整数据集与 V2 在同一完整日历下精确一致、AST 和依赖别名、
版本/哈希/重复/非法输入拒绝、预热/缺失日/乱序/证券隔离、未来变更不影响过去因子与预处理、
次日入场与末尾标签缺失、PIT 内部缺日拒绝、状态缺失保守排除、sidecar 缺失/篡改/定义/
版本/顺序/日历/输入身份冲突、三个文件篡改，以及原本地与 qualification 金融边界拒绝测试。

```powershell
New-Item -ItemType Directory -Force local-validation | Out-Null
$env:PYTHONPATH = 'src'
$py = 'D:/Environments/factorlab-py313/Scripts/python.exe'
& $py -B -m pytest tests/research/test_dataset_registry_v2.py tests/research/test_factor_registry_v2.py tests/research/test_factors_phase1.py tests/research/test_dataset_phase1.py tests/research/test_labels_phase1.py tests/research/test_walk_forward_runner_phase2.py tests/research/test_models_phase2.py tests/research/test_tree_models_phase3.py tests/research/test_local_admission_boundaries.py tests/research/test_qualification_admission_boundaries.py -q -p no:cacheprovider --basetemp=local-validation/pytest-acceptance --junitxml=local-validation/acceptance.xml
& $py -B -m pytest tests/research/test_dataset_registry_v2.py -k 'not four_models' -q -p no:cacheprovider --basetemp=local-validation/pytest-final-boundaries --junitxml=local-validation/final-boundaries.xml
& $py -B -m examples.research.dataset_registry_v2_demo --output local-validation/demo
```

第一条在最终源码上将收集 207 项；实际验收运行的 206 项发生在最后新增 raw_collision
参数用例之前，随后 66 项边界复验和最终独立演示验证最终源码。
实际计数、各轮退出码、JUnit/日志哈希及四个相关源码哈希见 [validation.json](validation.json)。

## 因子实际进入模型的证据

合成 8 只证券、280 个工作日（明确不是认证交易所日历），65 日预热后得到 1,710 行数据。
特征有序为 `MOM20@1`、发现生成器的 `FD_001@1`、固定依赖别名 `pinned_reversal@1`。
每个模型执行 2 折，产生 690 条 Test 预测；训练仅使用 Train，保留原 purge/embargo 和
训练/验证标签成熟边界检查。FactorScore 为原固定权重基线，不学习权重。

| 模型 | 合成 RankIC 均值 | 扰动 FD_001 后最大预测变化 |
|---|---:|---:|
| FactorScore | 0.229557 | 2.000000 |
| Ridge | 0.263355 | 0.027514 |
| LightGBM | 0.253384 | 0.005745 |
| XGBoost | 0.290888 | 0.003766 |

演示加载本轮保存的模型，对 64 行真实生成的特征将 FD_001 加 5，四模型预测均发生变化。
这是接线验证，不是金融有效性或样本外显著性结论；发现模板选取不读取 Test 标签。
[demo-manifest.json](demo-manifest.json) 保存模型/预测/训练源哈希及 IC；
[predictions.sample.csv](predictions.sample.csv) 保存每模型 8 行预测样本。
复现命令输出完整预测、日度与汇总指标、折分、训练审计、系数/重要性及模型文件。

[sidecar.example.json](sidecar.example.json) 为实际运行的完整 sidecar，读回核对通过，身份哈希：
`2c1b18260659a90dbdb428386b71eb26a6f25efb987e247ef8b6ea784d296168`。
sidecar 的模型输入文件哈希与 manifest 的模型/预测哈希共同绑定本次研究链路。

本机单次实测：含输入生成的构建 1.63 秒，全流程 33.96 秒（含 tracemalloc 开销）；
Python 跟踪峰值 22,936,801 字节，OS 进程生命周期峰值 RSS 358,588,416 字节，
数据集 deep memory 628,374 字节。OS 指标包含导入和原生模型分配；不是生产容量基准。

## 限制与下一阶段

本轮未新增 CLI/YAML V2 模式、持久化 registry 加载、通用依赖算子、自动择优搜索、
增量缓存或大规模性能调优；保留 V2 原有 250,000 行面板上限。
数据来源和日历版本由调用方声明，供应商 PIT、HFQ 及正式准入仍需独立金融证据。
sidecar 不会赋予现有 CLI、provider、forward 或金融门禁任何认证权限。

下一阶段最小建议：增加显式 V2 研究 CLI 配置，把经过现有准入流程的数据版本和外部固定的
dataset identity hash 传给本 API；沿用当前金融阻断，先完成一份来源合格的离线研究输入。
清理可重建的测试目录、模型/Parquet 和重复演示输出，仅保留小型收据、必要日志及关键样例；
未重新冻结 B3/B4，也未创建完整历史哈希归档。
