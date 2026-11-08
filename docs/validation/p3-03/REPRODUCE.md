# P3-03 复现

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

在任务分支仓库根目录运行，复用 Python 3.13 环境。测试通过 pytest 的 `pythonpath=src`
使用当前源代码；示例明确设置 `PYTHONPATH=src`，不依赖其他工作树的 editable 安装。

```bash
PY=D:/Environments/factorlab-py313/Scripts/python.exe
export PYTHONPATH=src OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2

"$PY" -m pytest tests/research/test_partitioned_walk_forward.py \
  tests/research/test_walk_forward_runner_phase2.py::test_runner_validates_columns_weights_and_finite_values \
  tests/research/test_walk_forward_runner_phase2.py::test_runner_rejects_target_or_audit_columns_as_features \
  tests/research/test_walk_forward_runner_phase2.py::test_custom_label_audit_columns_are_preserved_under_canonical_output_names -q

"$PY" -m pytest tests/research/test_partitioned_dataset.py \
  tests/research/test_validation_phase2.py \
  tests/research/test_walk_forward_runner_phase2.py \
  tests/research/test_walk_forward_factor_selection.py \
  tests/research/test_walk_forward_stage_diagnostics.py -q

# 使用全新输出目录；prepare 拒绝覆盖数据目录。
"$PY" -m examples.research.partitioned_walk_forward prepare \
  --data local-p303-reproduction/dataset --report local-p303-reproduction/report
"$PY" -m examples.research.partitioned_walk_forward dense \
  --data local-p303-reproduction/dataset --report local-p303-reproduction/report
"$PY" -m examples.research.partitioned_walk_forward window \
  --data local-p303-reproduction/dataset --report local-p303-reproduction/report
```

三个示例命令为三个独立进程；A/B 各运行一次且不与其他本任务测试并行。
每次比较 `prediction_sha256`、`fold_summary_sha256`、逐折行数，
并读取总峰值 RSS、逐折峰值、端到端秒数、`read_stats` 与 Windows 进程 I/O。
这些检查不修改既有 Golden 精度。

初始新增测试使用了包含构建预热期的完整日历，导致原稠密流程在首折无 Train 行，
当次记录见 `new-tests.log/xml`。测试修正为显式研究区间日历，不修改 Splitter 或训练语义；
后续记录按执行次序保留，最终新增及共享列验证结果见 `reviewed-tests.log/xml`。
