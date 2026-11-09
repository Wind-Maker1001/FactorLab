# P4-02 配置驱动的可复现研究工作流

SYNTHETIC / SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

基线：`e27d2f893a9f432ea67f80a69f4e9300a9796a13`。

## 使用

在仓库根目录、项目既有 Python 环境中运行：

```bash
export PYTHONPATH=src
python -m tools.research.run_pipeline \
  --config configs/research/p4-02-synthetic.json --output <fresh-output>
python -m tools.research.run_pipeline --verify --output <completed-output>
```

`<fresh-output>` 必须不存在，包括空目录也不接受。实验 ID 及其四模型派生 ID 在该输出的 Tracker 中不可覆盖；另一独立研究使用新的输出目录和实验 ID。失败目录保留诊断，不复用失败 ID/目录。

首版受限 JSON 必须显式提供示例全部字段，不接受重复键、未知字段、NaN/Infinity、隐式布尔数值或任意 Python/YAML 执行。支持固定版本合成 fixture 的证券/日历/种子/数据版本身份，Registry V2 中显式版本因子，四个固定参数模型、研究区间、拆分、标签、风险窗口、预算、持仓上限、再平衡、成本和执行模式。四模型每个只有一组候选，不执行超参数搜索；方向由 Train-only selector 冻结。`version: "1"` 表示模型适配合同版本，实际数值库及 Python 版本另由不可变记录固定。

`minimum_variance` 不使用分数作为收益；`score` 只使用非负横截面百分位偏好。mean_variance 拒绝，因为没有合法的预期收益定义。batch 不接受换手上限；incremental_feedback 从真实执行持仓反馈计算换手，首次从现金进入预算必须可行。沿用 next-open、Train/Validation/Test 隔离、purge/embargo 和历史风险窗口约束。配置不能注入未来价格字段、时间对齐函数或新的模型代码。

REAL_CANDIDATE 在数据读取前即拒绝，不接受工程预检替代正式准入，也不调用其他并行任务的新接口。

## 产物与验证

`normalized-config.json`、`identity.json` 固定配置哈希、合成输入、因子定义引用、运行器/生成器源文件、Git 和 Python/package 身份。各模型折、筛选、拟合审计、目标及决策收据、实际交易/持仓/净值、拒单、目标偏离、OOS 和风险评价由既有 ResearchPipeline 写出。`comparison.csv` 仅作描述性比较，不排序推荐 Test 胜者。

`SUMMARY.zh-CN.md` 从 `summary.json` 的实际评价/执行结果生成，明确分离预测、组合收益、成本、统计显著性和合成限制。`reproduction.json` 提供新输出复现命令。`resources.json` 记录本进程 CPU 和墙钟时间。既有分块构建保留自身资源收据。

四个既有不可变实验之外增加一条 workflow 不可变记录，将完整配置及其哈希、合成输入身份和四个 run ID 绑定到所有保留产物。`receipt.json` 保存保留文件哈希；verify 再通过 Tracker 验证权威记录、配置/数据绑定和执行产物，并重新生成中文摘要核对。SQLite 索引和可再生 dataset 分块不是保留证据集合；不额外保存模型或逐日预测矩阵。

哈希是本地产物完整性检查，不是第三方可信证明。Tracker 的执行产物路径是绝对路径，验证原目录；移动归档需保持其原路径，不能声称任意重定位后仍可直接验证。重新执行应使用新目录。

## 验证记录

完整四模型执行命令（Git Bash；已有 Python 3.14 环境）：

```bash
PYTHONPATH=src PYTHONIOENCODING=utf-8 \
  D:/Environments/factorlab-py314/Scripts/python.exe \
  -m tools.research.run_pipeline \
  --config configs/research/p4-02-synthetic.json \
  --output D:/Code/FactorLab-archives/p4-02-synthetic-20261109-acceptance
```

固定配置 SHA-256：`6ae5ea2ea6f08c411e5af33c0de4f89924425fd090ef247f52367e4d796bdf44`。全部数据、因子定义、代码和环境身份保存在 `identity.json`；模型适配合同为 v1，实际 NumPy 2.3.4、SciPy 1.16.3、scikit-learn 1.7.2、LightGBM 4.6.0、XGBoost 2.1.4、Python 3.14.0。每个模型均为 3 折、1,552 行 OOS 预测。RankIC：FactorScore 0.320465、Ridge 0.323998、LightGBM 0.308024、XGBoost 0.311366。

minimum_variance 的四个独立模拟账本相同，最终净值约 0.986201635，合成净收益 −1.3798%，成本 866.32 元、123 笔成交、2 次拒单。该目标只使用协方差，因此不能从相同组合推断预测相同。原始评价指标及状态保留于 `summary.json`，中文样例为 `SUMMARY.zh-CN.md`。不进行 Test 胜者选择，不宣布真实 Alpha。

全量运行墙钟 28.405 秒、进程 CPU 27.969 秒、最终 RSS 375,586,816 字节（约 358.2 MiB，不是峰值或模型独占内存）。首次启动因 Python 3.14 环境缺少既有分块构建依赖 psutil，在训练前退出；按 `requirements/scalable-compute.txt` 安装 psutil 7.2.2 后使用新目录完成一次四模型全量研究，没有重复全量训练。

定向测试命令：

```bash
PYTHONPATH=src \
FACTORLAB_P402_ACCEPTANCE=D:/Code/FactorLab-archives/p4-02-synthetic-20261109-acceptance \
D:/Environments/factorlab-py314/Scripts/python.exe -m pytest -q \
  tests/research/test_p4_02_pipeline_cli.py \
  tests/research/test_pipeline_feedback.py \
  tests/research/test_execution_feedback.py \
  tests/research/test_experiment_tracker.py \
  tests/research/test_research_pipeline.py \
  -k 'not four_model_chain and not cash_positions_nav and not tracker_query_compare and not future_information_does'
```

排除的四个既有用例使用另一次四模型训练 fixture；本次通过一次真实 CLI 全量验收及其产物校验覆盖完整运行，避免等价重复。新增验收测试从已保存特征只重拟合 Ridge，核对预测身份、四项预测指标、目标和净值，再复用相同分数对照 batch 与 incremental_feedback。正常未提供验收目录的单元测试运行会跳过两个保存产物用例。

首次定向测试有两个测试预期异常类型错误：Registry 正确拒绝非法因子/版本并抛出 ConfigurationError，测试最初只接受 ValueError，已按原合同修正。后续一次复现测试因 CSV 的 signal_date 未解析为日期而失败，目标已相同；修正测试读取类型。最终定向测试 **93 passed、4 deselected，15.72 秒**，没有跳过用例；JUnit 结果见 `tests.xml`。非法配置/因子/模型参数、数据版本、时间/未来信息、预算/持仓/换手、REAL_CANDIDATE、已有输出拒绝、文件缺失和篡改负例均通过。配置/输出哈希与 Tracker 绑定、中文摘要一致性、固定种子的预测及执行复现和反馈模式对照均通过。

版本库只保留摘要、规范化配置、资源、哈希清单和 10 份原始不可变 Tracker JSON 记录（4 个模型和 1 个 workflow 各一份定义/运行），不提交可再生行情、dataset 分块、完整模型或大型预测矩阵。`receipt.json` 的路径相对于原始完整输出；此文档目录是精简证据归档，verify 应针对上述完整输出运行。记录中的基线 commit/tree 及 dirty 指纹对应执行时工作区，新增运行器/生成器的源文件哈希另行固定，不把基线 commit 误称为已包含新增代码的提交。
