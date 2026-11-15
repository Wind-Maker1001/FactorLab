# Compute Backend V1

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## Policy and public contract

Import the public API from `factorlab.compute`. Existing YAML research configs
accept the optional block below through `factorlab.config.load_config`; omitted
blocks use these defaults. Other callers can construct `ComputeConfig` directly.
There is no second loader or configuration registry.

```yaml
compute:
  device: cpu
  allow_fallback: false
  gpu_memory_limit_mb: null
```

- `cpu` probes only declared CPU implementations, never GPU runtimes.
- `gpu` requires a declared GPU implementation **and** a successful target probe.
  If none is usable, `allow_fallback: false` raises `ComputeUnavailableError`;
  `true` tries declared CPU implementations and records the GPU failure reasons.
- `auto` tries declared GPU implementations, then CPU. This is automatic selection,
  not explicit-GPU fallback: `fallback` remains false, regardless of
  `allow_fallback`. No implicit CPU implementation is fabricated.
- Within each device type, declaration order is priority. A failed CUDA path can
  be followed by an OpenCL path; GPU is not an alias for CUDA.
- Unknown modes, non-boolean fallback flags and non-positive/non-integral memory
  budgets fail with `ConfigurationError`. Unexpected probe exceptions propagate.

`BackendCapability(device, backend, probe)` declares one **implemented task path**.
Names should identify the actual path, such as `xgboost-cuda`, `lightgbm-opencl`,
`numpy` or `cupy-cuda`. Pass capabilities explicitly to
`resolve_compute(capabilities, config)`; no plugins or global registry are used.
Its immutable `ResolvedCompute` contains the selected device, backend, probe
status and policy. The caller must dispatch that exact implementation.

A probe accepts `ComputeConfig` and returns `BackendStatus`. It must check the
specific implementation's runtime, supported version and required backend
features. For GPU readiness, test a minimal real operation through that target
backend and synchronize it; merely importing a library, enumerating a device,
seeing a driver, or finding `nvidia-smi` is insufficient. Translate expected
native dependency/version/capability failures into `BackendStatus(False,
reason=...)`; do not hide programming errors with a catch-all exception.

`BackendStatus` separates `driver_visible` (`None` = uninspected/not applicable),
`runtime_available`, and target `available`. A known driver failure or an unusable
runtime cannot accompany `available=True`. Record obtainable backend versions in
`version`. `probe_runtime(module_name, check)` lazily imports an optional library,
reports import/linker failures, and invokes the trusted implementation check.
If the check omits a version, it captures the module's string `__version__` when
available. Import success alone never supplies a successful status.

## Calling and recording

A complete CPU-only example, with no optional GPU imports:

```python
import platform
from factorlab.compute import (
    BackendCapability, BackendStatus, ComputeConfig, Device, resolve_compute,
)

python_cpu = BackendCapability(
    Device.CPU, "python",
    lambda config: BackendStatus(
        True, version=platform.python_version(), runtime_available=True,
    ),
)
selected = resolve_compute((python_cpu,), ComputeConfig())
# Dispatch the declared Python CPU implementation here.
compute_environment = selected.to_dict()
```

For a task with an implemented GPU path, its own adapter supplies the probe.
In this integration template, `check_xgboost_cuda` is owned by that adapter and
must perform the target check described above; V1 does not supply that function:

```python
from factorlab.compute import probe_runtime

# cpu_path declares the task adapter's existing CPU implementation.
gpu_path = BackendCapability(
    Device.GPU, "xgboost-cuda",
    lambda config: probe_runtime(
        "xgboost", lambda runtime: check_xgboost_cuda(runtime, config),
    ),
)
selected = resolve_compute(
    (gpu_path, cpu_path),
    ComputeConfig(device="gpu", allow_fallback=True, gpu_memory_limit_mb=512),
)
implementations[selected.backend](inputs)
# Record only after executing the selected path; readiness is not execution proof.
provenance = capture_provenance(repo_root)  # existing experiments.provenance API
provenance["environment"]["compute"] = selected.to_dict()
tracker.append_run(
    experiment_id, run_id, **provenance,
    command=command, status="SUCCEEDED",
)
```

`to_dict()` is JSON-compatible and distinguishes requested policy from selected
device/backend/version, probe evidence, explicit CPU fallback/reason and budget.
Store it under the existing `environment["compute"]` field; keep requested
configuration in experiment `parameters` when it is part of experiment identity.
Tracker schema, immutable record protocol and database require no changes.
A selection receipt alone does not prove model training or GPU execution.

## Resource and compatibility boundaries

`gpu_memory_limit_mb` uses MiB (1,048,576 bytes) and is a **requested budget**.
V1 passes it to probes and records it, but does not constrain allocation:
`gpu_memory_limit_enforced` is always false. A CPU fallback retains the requested
GPU budget for provenance, without applying it to CPU memory.

A task can report an infeasible budget as unavailable during its probe, allowing
normal selection/fallback before execution. `ComputeMemoryError` is the shared
exception for a native GPU allocation failure or task budget violation during
execution; implementations translate only known native memory failures. V1 does
not retry partially executed work or silently rerun on CPU. `allow_fallback`
authorizes selection fallback, not an automatic recovery wrapper around arbitrary
operations. A later task implementing execution-time fallback must discard partial
GPU output, execute its CPU path and record that actual path and reason.

Batch/chunk sizes remain task-owned. Reuse the existing
`factorlab.dataset.partitioned.ChunkedConfig` for its current factor/dataset paths;
this module adds no competing batching policy, memory pool, allocator, tensor
abstraction, scheduler or resource monitor. GPU budget enforcement and GPU-aware
chunk planning belong to implemented GPU consumers.

V1 itself does not register model implementations or redirect execution. The
[native tree-model consumer](gpu_model_acceleration.md) connects optional compute
policy to XGBoost/LightGBM and Walk-Forward. Omitted policy retains legacy CPU
behavior. Splits/selection, factor-discovery budgets, random seeds, trading and
risk gates remain independent of device policy. Monte Carlo is not changed.

## Parallel consumer contracts

**XGBoost:** an adapter declares its existing CPU path and its actually implemented
CUDA path separately. Probe the installed XGBoost build/version and a tiny native
CUDA operation before declaring CUDA available; a working CuPy installation is
not XGBoost evidence. Dispatch native device parameters only in that new adapter,
never infer them from the request or alter legacy model defaults. LightGBM must
likewise distinguish its OpenCL and CUDA builds.

**Monte Carlo:** its implementation declares CPU and, only once implemented,
CUDA paths. The CUDA probe checks its own runtime and required operations;
`probe_runtime` may load its optional runtime. Use the shared policy and errors,
plan chunks/enforce the budget in the engine, and preserve independent RNG and
numerical reproducibility metadata. Record the backend actually used; do not
infer identical CPU/GPU random streams or numerical results from device policy.
