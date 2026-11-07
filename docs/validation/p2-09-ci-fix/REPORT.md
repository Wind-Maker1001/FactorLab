# PR #17 CI 依赖集成修复

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

修复范围：恢复受保护的 `pyproject.toml`、`requirements/py314-core.txt` 原始 Git Blob；
psutil 7.2.2 移入独立 `requirements/scalable-compute.txt`。
增量及 Legacy 测试作业显式安装可选依赖；核心 wheel/sdist 作业继续验证核心依赖安装。
分块计算和基准脚本按需加载监测库，缺依赖明确报错，不跳过 RSS/磁盘守卫。
核心导入、CLI 帮助和已提交分区读取无需 psutil。

原始 Blob SHA-256（与未修改的 `BASELINE_4157.json` 一致）：

- pyproject.toml：`eba6fb3c43b27d1e8ea61f95298852c0e31807f36fcdf7e0aa216270c0641621`
- requirements/py314-core.txt：`abcc740d6003a078364f3619098f6c1a96443c9d8b93b592c1bbbea3ea8d1dd9`

## 复用的 CI 证据

[原运行 42393821431](https://github.com/Wind-Maker1001/FactorLab/actions/runs/42393821431)：
3.13、3.14 的 CURRENT_SOFTWARE_VALIDATION_V2 工件均为 **5364 PASS**；
原始 4157 软件节点全部保留，历史 **50 FAIL / 136 ERROR** 无漂移，当前软件无失败。
Independent V2 的 preservation_drift 只有上述两个 Blob；executed_source_drift 为空。
本轮直接复用原工件核对 outcomes/节点集合，不改写旧 receipt，不把旧执行结果当作新 HEAD 全量执行。

3.13 作业取消注释明确为 `The job has exceeded the maximum execution time of 40m0s`。
16:56:49Z 启动，17:36:52Z 结束；测试步骤和工件上传均成功，超时发生在收尾阶段。
增量作业上限从 40 分钟增加到 60 分钟，不重试测试或改变失败传播。

Native lock diagnosis 原证据：main/current raw exit 0、历史 input raw exit 1，
三组诊断均有效；它保留历史 input 控制失败，因此作业仍为 FAIL。
Legacy 的 186 项历史失败/错误继续保留；本轮不修历史归档，也不把整条 CI 宣称为全绿。

## 必要本地验证

既有 Python 3.13 环境，**12 PASS，exit 0，12.36 秒**：

```powershell
$env:PYTHONPATH = 'src;.'
& D:/Environments/factorlab-py313/Scripts/python.exe -m pytest tests/research/test_scalable_optional_dependency.py 'tests/research/test_partitioned_dataset.py::test_dense_dataset_exact_equivalence[2-5-13-2]' tests/research/test_partitioned_dataset.py::test_failure_never_publishes -q
```

覆盖核心无依赖导入、CLI 帮助/安装提示、缺依赖拒绝计算、不创建未提交目录、
不依赖监测库的分区读取，以及正常守卫下的精确一致性和资源/输入/计算失败不发布。
新增 5 个可选依赖用例，不改原测试节点或比较器。

未重建环境，未运行本地全量矩阵，未重跑 L2/L3。
原 `docs/validation/p2-09/` 所有性能、数据身份及哈希证据逐字节保留。
原计算、标签、预处理、原子提交和金融门禁算法不变。
最新提交的 Actions/Independent V2 结论以 PR #17 Checks 为准；
新 CI 完成前不以复用工件冒充新提交通过，最终结果另存本地交付收据。
