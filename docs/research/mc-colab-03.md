# MC-COLAB-03 — registered research batches and resource placement

## Scope and execution protocol

Provide one versioned manifest for registered research tasks, explicit resource
placement, isolated execution, retained outcomes and downloadable artifacts.
Numerical definitions remain those of the existing Monte Carlo and static-grid
engines. The implementation and actual run status are tracked in the
[validation report](../validation/mc-colab-03/REPORT.md).

Tasks execute sequentially in fresh Python subprocesses. An independent task
failure or timeout must not prevent later tasks from running while the batch
still has time and CU allowance. Do not retry a failed numerical execution on a
different backend. Batch outcomes distinguish `SUCCEEDED`, `FAILED`,
`TIMED_OUT` and `NOT_STARTED`; the aggregate is `SUCCEEDED`, `PARTIAL` or
`FAILED`. A non-success aggregate returns a nonzero CLI exit status even when
some tasks completed successfully.

The authorized cloud envelope for this study is **10 CU total**, including
failed attempts, setup, execution, transfers and release. It is not 10 CU per
submission. Before another submission, deduct prior consumption and outstanding
reservations from that envelope and set its manifest budget no higher than the
remaining allowance. The controller enforces a submission budget, not a
persistent cross-submission 10 CU ledger. Continue independent tasks after
failures within the remaining limits; budget exhaustion must leave later tasks
`NOT_STARTED`, not trigger a larger allocation or a renewed budget.

## Manifest schema

Schema version 1 has exactly these top-level fields:

- `schema_version`: integer `1`.
- `commit`: full 40-character lowercase hexadecimal Git commit.
- `timeout_seconds`: positive batch wall-time allowance.
- `budget_cu`: finite nonnegative CU budget; explicit Colab tasks require a
  positive value.
- `tasks`: nonempty ordered list of tasks with unique filesystem-safe IDs.

Each task has exactly `id`, `entry`, `parameters`, `inputs`, `device`,
`memory_budget_mb`, `gpu_memory_limit_mb`, `timeout_seconds` and `outputs`.
Unknown fields and unregistered parameters are rejected. Memory budgets use
positive integer MiB; the GPU limit may be `null`. Task timeouts are positive.
IDs contain letters, digits, underscores or hyphens, start with a letter or
number and are at most 64 characters. Seeds are uint32 values.

| Entry | Allowlisted parameters | Inputs | Normal outputs |
|---|---|---|---|
| `gbm` | `paths`, `steps`, `assets`, `batch_size`, `seed` | Empty mapping | `report.json`, `samples.npz` |
| `bootstrap` | `paths`, `steps`, `assets`, `batch_size`, `seed` | Optional `returns` | `report.json`, `samples.npz` |
| `static_grid` | `paths`, `batch_size`, `seed` | Required `definition` | `report.json`, `terminal.npy` |
| `benchmark` | `benchmark`, `first_seed_only`, `statistics`, `workers` | Empty mapping | `report.json`, files under `study/` |

Registered benchmark names are `mc_colab_02_reliability` and
`mc_colab_02_scale`. They use packaged study configurations, not arbitrary
scripts or import paths. GBM and bootstrap execution defaults are 100000 paths,
252 steps, one asset, batch 1024 and seed 1729. A bootstrap `returns` input is a
NumPy archive containing `returns` and `timestamps`; it is loaded without pickle.
Without that input, bootstrap uses the registered synthetic input generator.
Static-grid definitions are consumed by the existing grid-batch configuration
loader. Its generated prices are evaluated in batches, with terminal rows
written to `terminal.npy`.

`outputs` is a nonempty list of distinct relative file paths. It declares files
that must exist after successful execution; it does not rename the engine's
outputs. A missing declared output fails the task. Additional diagnostics and
partial outputs can still be retained.

Inputs are named mappings to existing regular files beneath the manifest's
parent directory. Use forward-slash relative paths without absolute paths,
traversal, drive prefixes or symlinks. Allowed input extensions are `.json`,
`.csv`, `.npy`, `.npz` and `.parquet`; individual entrypoints impose their own
format requirements. Cloud upload applies additional credential-like and
hidden-path exclusions. Only the manifest and its explicitly declared input
files are uploaded as research inputs, alongside the built wheel. Do not put
credentials in task inputs or include a repository/environment directory as an
input.

Example local manifest, replacing the commit placeholder before validation:

```json
{
  "schema_version": 1,
  "commit": "<full-40-character-lowercase-git-sha>",
  "timeout_seconds": 900,
  "budget_cu": 0,
  "tasks": [
    {
      "id": "gbm-cold-cpu",
      "entry": "gbm",
      "parameters": {
        "paths": 100000,
        "steps": 252,
        "assets": 1,
        "batch_size": 1024,
        "seed": 104729
      },
      "inputs": {},
      "device": "cpu",
      "memory_budget_mb": 8192,
      "gpu_memory_limit_mb": null,
      "timeout_seconds": 300,
      "outputs": ["report.json", "samples.npz"]
    }
  ]
}
```

For a cloud-only batch, use `colab` or `auto` task placement and a positive
batch CU budget within the remaining 10 CU study allowance. An explicit `cpu`
or `local_gpu` task is not silently relocated into a remote batch.

## Placement and numerical backend layers

Manifest placement is `cpu`, `local_gpu`, `colab` or `auto`. It is separate from
`ComputeConfig`, whose numerical device vocabulary remains `cpu`, `gpu` or
`auto`. Both local GPU and Colab placement resolve to numerical `gpu` execution;
Colab is not an additional numerical backend. Explicit placement has no fallback
when the requested resources are unavailable.

`probe_resources(include_gpu=False)` discovers available host RAM without GPU
subprocesses. With GPU discovery enabled, `nvidia-smi` supplies actual GPU names,
free/total memory and utilization; discovery does not import CuPy. Host memory
uses native Windows/Linux facilities, with optional psutil for other hosts.
These are resource snapshots, not guarantees of future availability.

`select_task(task, resources, cloud=None)` returns placement, numerical device,
reason, evidence and conservative host/GPU byte estimates. Unsupported time and
CU estimates are `null`. Memory planning includes model data, assets, steps,
bounded batch workspace, result/summary vectors and overhead; it does not treat
all Monte Carlo paths as simultaneously resident simulation workspace. These
admission estimates are not OS-enforced RSS caps or measurements of CUDA context
memory.

The [MC-COLAB-02 report](../validation/mc-colab-02/REPORT.md) supports a
conservative cold CPU default. At 100000 paths, 252 steps, one asset and batch
1024, fresh-process GBM medians were CPU 2.467 seconds versus GPU 3.596 seconds;
bootstrap was 2.297 versus 3.156 seconds. At one million paths and batch 4096,
steady GBM was 17.041418 versus 0.455560 seconds, but that cell has one independent
seed and two warm repeats. It does not establish a cold crossover or a generic
speed factor for other assets, horizons or hardware. Unknown workloads choose
CPU when it fits, with reasons explaining the evidence limit. Fresh task
subprocesses do not establish a persistent warm CUDA worker.

The selector can consider cloud only with explicit authorization and confirmed
availability, host/GPU capacity, startup allowance, active CU rate, balance and
approved budget. Auto cloud additionally requires local GPU pressure
(utilization at least 20 percent or insufficient memory), an explicit throughput
opt-in, or reliable matched full-lifecycle evidence favoring cloud. Requested
GPU tiers and historical successful allocations do not prove present cloud
availability. Selection itself never allocates a runtime. Throughput opt-in is
an advanced selector API option, not an accepted version-1 manifest field. The
manifest CLI accepts observed cloud metadata with `--cloud-state state.json`
for `plan` and `dispatch`; it must contain authorization, actual capacity, active
rate, balance and startup estimates. Without that evidence, `auto` does not
allocate cloud just to discover its price.

## Wheel provenance and environment

Build research wheels with `tools/research/build_research_wheel.py`, rather than
substituting an ordinary wheel build. Its required provenance artifact is
`factorlab/resources/research-build.json`; installed provenance must match the
manifest's full commit before batch execution and in each task worker. The
helper stages `git archive` of the specified commit in a temporary directory,
adds commit provenance and the registered reliability/scale configurations, then
builds a wheel with an isolated build environment. Uncommitted checkout edits
are not included. The output directory must be new:

```sh
python tools/research/build_research_wheel.py --repository /absolute/path/FactorLab --commit <full-40-character-lowercase-git-sha> --output /absolute/path/new-wheel-directory
```

Install the resulting wheel in the execution environment with the
`monte-carlo-native` and `monte-carlo-benchmark` extras; GPU execution also needs
`monte-carlo-cuda`. For example, replace the wheel placeholder with the helper's
actual output:

```sh
python -m pip install '/absolute/path/factorlab-<version>-py3-none-any.whl[monte-carlo-native,monte-carlo-benchmark]'
```

Use a compatible interpreter and record the actual installed wheel. Workers
run with `python -I -m factorlab.experiments.research_batch` from isolated work
directories, so an editable checkout is not a substitute for installed wheel
provenance. Each worker records its interpreter, package location and loaded
NumPy/SciPy versions, plus CuPy for GPU execution. Dependency constraints and
loaded-versus-installed versions are validated before numerical work.

## Commands

Run these commands in the environment containing the installed research wheel.
Use new output/job directories. The module CLI and
`tools/research/run_research_batch.py` expose the same batch actions.

```sh
python -m factorlab.experiments.research_batch validate --manifest /absolute/path/manifest.json
python -m factorlab.experiments.research_batch plan --manifest /absolute/path/manifest.json
python -m factorlab.experiments.research_batch local --manifest /absolute/path/manifest.json --output /absolute/path/results/local-run
```

`validate` checks schema and declared input files without executing tasks.
`plan` probes local resources and prints task selection; it neither allocates
cloud nor reserves resources. `local` checks installed provenance, selects each
task and launches isolated workers. The per-task timeout is reduced to the
remaining batch allowance, with time reserved for final bundling. Admission
normalizes omitted Monte Carlo dimensions to execution defaults, derives
static-grid dimensions from its definition and uses the registered benchmark
configuration's dimensions. Schema validation alone is not an execution result.

`dispatch` groups selected tasks into one local batch and at most one cloud
batch, preserving results independently. On Windows, explicitly name the WSL
distribution and its installed controller Python/CLI; for example:

```sh
python -m factorlab.experiments.research_batch dispatch --manifest D:/research/manifest.json --output D:/research/new-results --wheel D:/research/factorlab-0.1.0.dev0-py3-none-any.whl --wsl-distro FactorLab-P8-C03-Debian --controller-python /root/factorlab-colab-cli/bin/python --colab /root/factorlab-colab-cli/bin/colab
```

The controller Python must have the same research package installed. The wheel
packages its matching controller scripts, so dispatch does not require a source
checkout. Explicit Colab selections are revalidated against the live account;
automatic selections require `--cloud-state` evidence before allocation.

Cloud submission uses the existing official-CLI controller on its Linux/systemd
control host, with `google-colab-cli==0.8.0`, authentication and the required
noninteractive systemd privileges already configured. It is not a Windows-local
cloud launcher. Keep the matching manifest and declared inputs together on that
host, along with the provenance-built wheel and controller scripts.

```sh
python tools/research/colab_job.py submit --job /absolute/path/jobs/cloud-run --manifest /absolute/path/cloud-manifest.json --wheel /absolute/path/factorlab-<version>-py3-none-any.whl --gpu L4
python tools/research/colab_job.py status --job /absolute/path/jobs/cloud-run
python tools/research/colab_job.py logs --job /absolute/path/jobs/cloud-run
```

`submit` takes commit, timeout and CU budget from the manifest; conflicting
explicit flags are rejected. Submission starts a transient systemd service.
The controller requires exclusive account allocation, checks balance, allocates
the requested tier and observes actual hardware and active CU rate before
installation. Its timeout must exceed the 120-second cleanup reserve. An
allocation refusal is a failed attempt, not permission to assume another tier
is available or increase the study budget.

The controller installs the wheel and registered extras, uploads declared
inputs and starts the remote batch. It downloads retained artifacts on normal
completion using the original work deadline. After execution failure, retrieval
also uses remaining work time; only an expired work deadline permits up to
30 seconds from the existing cleanup reserve, bounded by the authorized time/CU
lifecycle deadline and leaving 90 seconds for release. Release runs
in cleanup; record server-confirmed release and final usage even if execution
failed. `recover --job ...` and `release --job ...` use the controller's existing
release path; `download --job ...` requests the retained remote result files.
Official CLI session state is private controller data stored under
`~/.local/state/factorlab-colab/sessions/<session>/`, not alongside Windows
artifacts on `/mnt/<drive>`. CLI storage is initialized before paid allocation:
DrvFS inherited permissions can prevent the CLI's SQLite lock from opening even
when artifact files themselves are writable. Keep that native session directory
until server-side release has been confirmed; it can contain runtime credentials.

A batch publishes `report.json` plus `samples.npz`. At batch level the NPZ is an
artifact-byte bundle (`uint8-npz-v1`), not directly the task's terminal samples.
Extract it into a new directory:

```sh
python -m factorlab.experiments.research_batch unpack --source /absolute/path/downloaded-results --output /absolute/path/unpacked-results
```

The bundle index preserves task-relative paths, including each task's actual
sample archive, reports, worker logs, environment, status and partial outputs.
Consult task status and aggregate state before treating an artifact as a
successful result.

## Required real-run evidence

Keep local real execution and cloud lifecycle outcomes separate from portable
subprocess-substitute tests. Record the manifest and wheel commit/provenance,
actual CPU/GPU and runtime versions, requested/resolved placement, per-task
outcomes, batch wall time and retrieved outputs. Include an independent failed
task followed by a completed task when validating failure continuation.

For cloud, retain pre-allocation balance/assignments, allocation outcome, actual
GPU and active rate, deployment/execution/download/release durations, final
balance and server session listing. Distinguish rounded account balance changes
from rate-times-wall estimates; missing usage is not zero cost. Charge failed
attempts against the total 10 CU envelope. Kernel or steady-run timing alone is
not full-lifecycle evidence for cloud placement.
