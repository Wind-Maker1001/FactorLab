# Native tree-model compute

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

Tree-model execution policy is separate from validation-selected model parameters.
The shared policy is [Compute Backend V1](compute_backend_v1.md). Requested GPU
memory is recorded, not enforced.

## Public execution interfaces

```python
from factorlab.compute import ComputeConfig
from factorlab.models.xgboost import XGBoostResearchModel

policy = ComputeConfig(device="gpu", allow_fallback=False)
model = XGBoostResearchModel(compute=policy).fit(train_features, train_labels)
scores = model.predict(test_features)
model.save(path)
loaded = XGBoostResearchModel.load(path)  # trusted artifacts only
cpu_scores = loaded.predict(test_features, compute=ComputeConfig(device="cpu"))
```

`LightGBMResearchModel` accepts the same policy. `WalkForwardRunner(compute=...)`
and `ResearchPipelineConfig(compute=...)` apply it to tree models only. Existing
YAML `compute` configuration reaches the walk-forward CLI. The synthetic pipeline
JSON and real-model study helper accept an optional `compute` object as well.
`create_legacy_model(..., compute=...)` carries execution configuration separately
from `LegacyModelRequest` and its candidate parameters.

Omitted model policy retains the legacy CPU constructor/state and execution
arguments. Explicit policies produce `compute_` (successful fit receipt) and
`compute_records_` (successful stage receipts). Walk-forward adds fold/candidate
attribution in `WalkForwardResult.compute_records`; experiment adapters expose
these under `environment["compute"]`. Requested policy belongs in experiment
parameters, while native backend/version, fallbacks and actual stage evidence
belong to run provenance. No historical records are rewritten.

LightGBM GPU policy selects training; its native inference stage is explicitly
CPU. A prediction-time explicit GPU request is unsupported and fails unless CPU
fallback is allowed. XGBoost reuses its fitted native device for inference unless
an explicit override is provided. A CPU override does not probe GPU or rewrite
training evidence. Loading does not resolve devices: when moving an artifact to
a CPU-only runtime, explicitly request CPU inference. A saved GPU training receipt
is never evidence that inference on a later machine used GPU.

Selection fallback happens before native training. Native allocation failures
raise `ComputeMemoryError`; a failed fit does not publish partial state or retry
on CPU. Unexpected native errors propagate. CPU/GPU numerical equivalence is not
bitwise equivalence, and GPU LightGBM does not inherit CPU determinism guarantees.

## Test entry points

CPU-only CI discovers the software-contract tests normally. Native hardware
acceptance is an explicit entry point, outside `test_*.py` discovery:

```sh
python -m pytest tests/gpu_tree_model_acceptance.py -q
```

This entry requires working XGBoost CUDA and LightGBM GPU training runtimes;
missing hardware fails rather than skips. It exercises training, prediction
policy, save/load and CPU-only inference, not the performance benchmark.

## Runtime evidence

On Windows with Python 3.13, XGBoost 2.1.4 and LightGBM 4.6.0:

- XGBoost completed native CUDA training on an RTX 2070; the trained booster
  reported `generic_param.device=cuda:0` and completed `DMatrix` prediction.
- LightGBM CUDA was unavailable: `CUDA Tree Learner was not enabled in this build.`
- LightGBM OpenCL completed training. Native diagnostics reported
  `Using GPU Device: NVIDIA GeForce RTX 2070, Vendor: NVIDIA Corporation`,
  `GPU programs have been built`, and features transferred to GPU.
- LightGBM's host prediction is CPU execution; GPU training is not evidence of
  GPU inference.

## Measured CPU/GPU comparison

The reproducible benchmark entry point is `tools/research/gpu_model_benchmark.py`.
[Machine-readable evidence](../research/gpu_model_benchmark.json) contains the
three trial durations, median-trial stage receipts, data hashes, versions,
preprocessing and resolution times, native diagnostics and sampled memory.

Both devices use identical synthetic inputs, feature order, hyperparameters,
seed 42, four CPU threads and one expanding fold: three training months, two
validation months, two test months, five purge and five embargo sessions. The
small case has 2,768 rows, eight features and 40 trees; the medium case has 88,576
rows, 32 features and 160 trees. End-to-end includes candidate fitting,
validation selection, Test evaluation and Train diagnostics. Three complete
executions run sequentially per device; the table reports the median execution
and its phase timings. No test suite ran concurrently with these measurements.

| Model / size | CPU train s | GPU train s | CPU predict s | GPU-path predict s | CPU end-to-end s | GPU end-to-end s | End-to-end CPU/GPU ratio |
|---|---:|---:|---:|---:|---:|---:|---:|
| XGBoost / small | 0.0361 | 0.1206 | 0.0201 | 0.0123 | 0.3922 | 0.7071 | 0.555 |
| XGBoost / medium | 0.5542 | 0.7628 | 0.0900 | 0.0499 | 6.4619 | 6.6557 | 0.971 |
| LightGBM / small | 0.0123 | 0.2044 | 0.0034 | 0.0032 | 0.5566 | 0.8878 | 0.627 |
| LightGBM / medium | 0.3327 | 0.8169 | 0.1278 | 0.1292 | 6.5267 | 7.2290 | 0.903 |

LightGBM GPU-path prediction in this table is native **CPU** prediction. Prediction
times sum Validation, Test and Train diagnostic calls; training times exclude the
separately recorded capability probe. A ratio below one means the GPU workflow
was slower. These cases establish functioning optional GPU execution, not a
training or end-to-end speed advantage. XGBoost's native CUDA prediction was
faster in these measurements, but did not offset total workflow overhead.

| Model / size | Maximum absolute prediction difference | Mean absolute difference | CPU RankIC | GPU-path RankIC | RankIC difference | GPU-trial device memory peak MiB |
|---|---:|---:|---:|---:|---:|---:|
| XGBoost / small | 0.00448454 | 0.000908080 | 0.9371658 | 0.9353610 | -0.00180481 | 1981 |
| XGBoost / medium | 0.00375981 | 0.000285237 | 0.9673746 | 0.9673307 | -0.0000439173 | 2006 |
| LightGBM / small | 0.000249017 | 0.00000308805 | 0.9382353 | 0.9382353 | 0 | 1991 |
| LightGBM / medium | 0.000124367 | 0.0000000232979 | 0.9672815 | 0.9672814 | -0.000000136143 | 2079 |

Whole-device `nvidia-smi memory.used` sampling is approximately every 100 ms and
includes other processes. It is not a process-exclusive allocation peak, a
capture of every transient allocation, or an enforced budget. Host preprocessing
and native execution are separate timings; native API transfer/synchronization
costs remain included in native calls, not independently measured. GPU memory
budget enforcement is **false** on every path.

## Acceptance evidence

`ENGINEERING_ACCEPTED / GPU_RUNTIME_VERIFIED / PERFORMANCE_VERIFIED` describes
implemented optional execution and completed measurements, not positive speedup.
XGBoost CUDA and LightGBM OpenCL passed native GPU train/predict-policy,
trusted save/load and explicit CPU-inference tests. XGBoost also loaded a GPU
artifact and performed CPU inference in a subprocess with
`CUDA_VISIBLE_DEVICES=-1`; an explicit GPU request then failed as required.
CUDA visibility masking is a CPU-only compatibility simulation, separate from
native GPU acceptance.

Targeted suites passed 647 distinct tests. The final GPU/model/factory/plumbing
batch passed 476 tests; the partitioned research/tracker/CLI/reference-identity
batch passed 177 tests with eight overlapping model tests; two additional
missing-runtime regression cases passed in the final focused run. No financial
admission or historical certification state changes accompany these results.
The native LightGBM CUDA adapter remains unverified on a CUDA-enabled Linux build;
the tested Windows build reports its missing CUDA learner explicitly.
