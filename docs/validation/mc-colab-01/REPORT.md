# MC-COLAB-01 execution evidence

## Azure CLI verification — 2026-11-27

The existing Azure Linux VM was reached from Windows using native OpenSSH.
No WSL command was used. The control environment was installed in a separate
management-user virtual environment, without changing the trading environment,
service configuration, trading state or credentials.

Observed host: Linux `7.0.0-1020-azure`, Python `3.12.3`, systemd `255`,
34 GiB free on the root filesystem. Existing uv installed the official PyPI
package `google-colab-cli==0.8.0`. `colab version` returned `Version: 0.8.0`.
`new --help` lists T4/L4/G4/H100/A100; `exec --help` defines `--timeout` as
seconds without output, not a total job deadline. `download --help` accepts
remote and local paths; `log --help` supports JSONL export.

The installed `sessions_command`, `stop` and `format_consumption_status` sources
were inspected. Sessions are listed from the server; stop calls unassign before
removing local state. Usage displays account balance and hourly rate with two
decimal places and the active assignment count. The controller does not import
these internals; it uses only CLI subprocesses.

Authentication probe:

```text
$ colab --auth adc usage
[colab] Warning: failed to fetch compute-unit info: Your default credentials were not found.
```

Google Cloud SDK `594.0.1` was prepared separately for the initial ADC consent.
No runtime has been allocated by this evidence capture. The authorized first-run
budget is 2 CU. Cloud execution, result retrieval and runtime release evidence
were not performed during this initial capture. Subsequent authorization and attempts are recorded below.

Read-only trading baseline: `factorlab-demo-execute.service` was active,
MainPID `959008`, NRestarts `0`, start timestamp
`2026-11-27 03:15:19 UTC`. The queried `factorlab-demo-observer.service` was
inactive. These are baseline observations, not an after-run comparison.

## Offline lifecycle tests

Python 3.13, `tests/research/test_colab_job.py`: **14 passed in 0.57 seconds**.
The tests cover usage parsing, budget refusal, CLI argv and nonzero exits,
endpoint-based release verification, idempotent recovery, interrupted allocation,
execution/download timeouts and missing GPU refusal before installation.
They are subprocess substitutes, not evidence of a Colab GPU run.

## Authorized cloud attempts

ADC authorization succeeded once. The initial official account query returned
199.43 CU, 0.00 CU/hour and zero active assignments.

- L4 allocation returned `ColabRequestError ... Service Unavailable` after
  approximately 217 seconds. The server subsequently listed no assignments;
  displayed balance remained 199.43 CU.
- T4 allocation succeeded: Tesla T4, 15360 MiB, driver 580.82.07, Python
  3.13.16, 2 vCPU and 13286932 kB total RAM. Official account rate: 1.24 CU/hour.
  The FactorLab wheel uploaded successfully. The next exec failed during its
  `/content` prelude with `RuntimeError: Connection was lost.` and the CLI
  process did not exit. No simulation ran. Stopping the transient service
  triggered cleanup; a fresh server listing confirmed no assignments, rate
  0.00 CU/hour and balance 199.37 CU. This rounded account balance change is
  not exact per-task billing.
- The trading Execute service retained PID 959008, its original start timestamp
  and NRestarts=0 after the T4 failure.

The installed kernel dependency defines
`REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 10))` and waits on
`connection_ready` without checking the wait result. This matches upstream
[issue #285](https://github.com/googlecolab/google-colab-cli/issues/285).
The controller now sets the supported environment value to 60 seconds rather
than patching the official client. The 14 offline tests passed again in
0.54 seconds, including an assertion on that subprocess environment.
The corrected T4 attempt completed successfully. Native MonteCarloRunner ran
1,000,000 GBM paths with 252 steps and seed 0 in 3.6146 seconds (3.7286 seconds
including input preparation). Its execution receipt reports `device=gpu`,
`backend=cupy-cuda`, `fallback=false`, batch size 4096, CuPy 14.0.1 and CUDA
runtime/driver API version 13000. Source commit:
`5a1be4d34f6222c186937bea3ac7c90b9d675a14`.

Actual hardware was Tesla T4, 15360 MiB, driver 580.82.07, 2 vCPU and approximately
12.7 GiB RAM. The complete lifecycle took 220.96 seconds. Both result files were
downloaded from Colab to Azure and then to Windows over SCP. Both arrays in
`samples.npz` have shape `(1000000,)` and contain only finite values. The original
report is retained as [cloud-results/report.json](cloud-results/report.json).
The 15 MiB sample archive is retained in the private run artifacts rather than
committed to the source repository.

The server listing after release reported no active assignments and the usage
rate returned to 0.00 CU/hour. Immediate rounded balance snapshots both displayed
199.37 CU; they do not establish zero consumption. At 1.24 CU/hour, the complete
220.96-second lifecycle corresponds to approximately 0.076 CU at a constant rate
(an estimate, not a billing receipt). The run remained below the 2 CU budget.

After result retrieval the trading Execute service still had PID 959008,
NRestarts=0 and the original 03:15:19 UTC start timestamp.

The persistent Colab kernel had NumPy 2.4.6 loaded, while the installed package
inventory after dependency installation reported NumPy 2.3.5. Both observations
are retained in the result instead of treating pip freeze as proof of the loaded
runtime version. This defect is fixed by the fresh research subprocess and
loaded-version validation described in the R01 repeat below.

## Official capability boundaries

CLI 0.8.0 has no detached-task command or server-enforced single-task CU cap.
The controller uses a transient systemd service and work deadline instead.
Account-level rounded CU snapshots are retained without claiming exact task
billing. The official session listing can show unnamed orphans, but the stop
command requires a locally known session. Loss of CLI allocation state can thus
require external session cleanup; the controller records that case as failure,
not successful release.

## R01 repeat — 2026-11-27 10:53:15–10:55:01 UTC

Research now starts with `subprocess.run([sys.executable, ...], check=True)`
after installation. The actual child loaded Python 3.13.16, NumPy **2.3.5**,
CuPy 14.0.1, FactorLab 0.1.0.dev0 and CUDA runtime/driver API 13000. FactorLab
metadata constraints were validated before simulation. No dependency constraint
or official CLI source was changed. An explicit execution success receipt also
rejects remote Python errors when the CLI itself returns zero.

The same native source, GBM parameters, seed and 1,000,000 × 252 workload ran on
Tesla T4 with `cupy-cuda`, no fallback. Native elapsed time was 3.3518 seconds;
the child measured 3.4711 seconds including input preparation. The
[actual process report](r01-evidence/results/report.json) contains loaded
versions and package inventory. Both million-element arrays were downloaded
Colab → Azure → Windows. Full sample files remain private run artifacts.
The [economic check](r01-evidence/economic-check.json) found finite values,
terminal mean 1.0833940 versus theoretical exp(0.08)=1.0832871, a 0.489-standard-
error difference. Drawdowns ranged from 0.04156 to 0.61565; probability above
20% was 0.373209.

The account started with 199.37 CU and zero assignments, allocated at 1.24
CU/hour, and ended with 199.35 CU, zero assignments and 0.00 CU/hour. The
server explicitly reported no active sessions after release. The displayed
balance difference is 0.02 CU; constant-rate estimation over the entire
105.98-second lifecycle is 0.0365 CU. Both are below the round's 2 CU budget;
neither is an exact per-task bill. This round used one allocation, with no
purchases.

### Controller capacity and trading coexistence

IMDS identified Standard B2ats v2, 2 vCPU, Ubuntu 24.04 x64. Linux exposed
908368 KiB total memory and 2 GiB swap. Research ran only on Colab. The measured
controller used MemoryMax=256 MiB, MemorySwapMax=0, CPUQuota=25%, CPUWeight=10;
no trading service was stopped, restarted or reconfigured.

[Resource summary](r01-evidence/resources.json) and
[one-second samples](r01-evidence/resources.jsonl) cover the complete controller
lifecycle, including allocation, installation, research, downloads and release:

| Measurement | Observed |
| --- | ---: |
| System MemAvailable before / minimum / after | 525304 / 357836 / 440316 KiB |
| Controller cgroup memory peak | 170225664 bytes (162.34 MiB) |
| Controller CPU time / wall time | 12.1004 / 105.9849 seconds |
| Average CPU, one-core basis / two-core capacity | 11.42% / 5.71% |
| CPU throttled periods / throttled time | 429 / 28.0871 seconds |
| Cgroup memory max / OOM / OOM-kill events | 0 / 0 / 0 |
| System OOM-kill counter before / after | 0 / 0 |
| System swap used before / after | 626404 / 641552 KiB |
| System swap-in / swap-out delta | 85125 / 85278 pages |
| System memory PSI some / full stall delta | 7.183642 / 6.171535 seconds |
| System CPU PSI some stall delta | 24.155920 seconds |
| Task files at final measurement | approximately 14.34 MiB |

The peak fits 256 MiB with about 94 MiB headroom, so the previous 512 MiB cap
was reduced rather than reserving half this host's memory. No cgroup swap was
permitted. The task incurred CPU throttling, as intended for a low-priority
controller. Host-wide pressure and swap activity were already present before
the task and increased during it; these are whole-host counters, not evidence
that the controller alone caused them. Root disk free space stayed above
34.6 billion bytes in the surrounding telemetry window. Azure CPU-credit
metrics were unavailable through the installed management tooling; no new
management authorization was requested.

All three relevant services remained active with unchanged process identities:
Binance Execute PID 959008, start 03:15:19 UTC; Alpaca paper PID 767635, start
2026-11-26 15:34:13 UTC; TradFi observer PID 959057, start 03:15:27 UTC. Each
retained NRestarts=0. The earlier inactive `factorlab-demo-observer.service`
was not the running TradFi observer.

[Existing Binance telemetry](r01-evidence/trading-window.json) spans two samples
before, two during and two after the task. Execute stayed healthy; cumulative
API errors stayed at 7 and safety counters did not increase. During-run market
age was 3.12–4.43 seconds, account age 0.73–0.98 seconds, and latest recovery
latency 0.89–1.14 seconds. Cumulative API maximum stayed 5.298177 seconds;
round p95 was not populated by this deployment. The pre-existing
STALE_OR_FUTURE_QUOTE marker remained unchanged. Alpaca's before/after health
was healthy, error=null, market CONNECTED; the market clock was closed.

Historical journald records subsequently supplied the missing task-window
Alpaca/management observations; see [sanitized service window](r01-evidence/service-window.json).
Alpaca was healthy immediately before and during the task, but had
StreamDisconnected/RECONNECTING health failures at 10:52:14 (before allocation)
and 10:55:14 (13 seconds after completion), recovering by 10:56:14. These failures
are therefore not a newly introduced error signature. The during-run heartbeat
age was 16.678 seconds, versus 9.150 immediately before and 3.093 immediately
after. The existing health probe itself took approximately 11.49 seconds during
the run (10:54:11–10:54:23), versus approximately 3 seconds immediately before. This is an
observed management-latency regression in the measured window, not proof of its
cause. REST received-at minus recovery-start was 5.938 seconds during the run,
versus 5.778 immediately before; this measures the combined poll span, not an
individual API request latency.

TradFi health remained healthy throughout, with no market errors and fresh
account projections. Its heartbeat age was 14.311 seconds during the run,
versus 9.629 immediately before and 7.403 immediately after. Existing observer
records continued at approximately 30.5-second intervals. Thus service
continuity is established, but latency neutrality is not: management probe
completion slowed while host pressure increased. Closed-market observations
are recorded as such, not elevated into an additional acceptance requirement.

TODO: resolve the observed management-latency regression before approving
routine research control on this shared VM; use a lighter control approach or
a non-trading Linux controller if isolation cannot be demonstrated. Do not
increase controller limits, upgrade the VM, or change trading services to
resolve this issue.

### Deployment qualification versus controller acceptance

The preferred deployment is now an on-demand local Windows → WSL2 → official
Colab CLI control path. Azure remains an optional Linux deployment, with its
small shared-host routine coexistence qualification **not passed**. That host
qualification is separate from the correctness and engineering acceptance of
the independent research controller. No Azure upgrade, new server purchase or
trading-service change is required by this deployment choice. Local WSL must
remain alive during a task and release; it need not remain running between jobs.
Local Google ADC authorization is independent of Azure credentials.

Local minimal evidence: WSL2 Linux with systemd PID 1 and Python 3.14.10 ran
official CLI 0.8.0. The final local regression suite passed **24 tests in 0.30
seconds**, including proxy propagation without embedding values in argv.
A real transient systemd unit acquired the account lock and exported cgroup
resource evidence; a separate transient unit confirmed the proxy environment
arrived intact. Mirrored networking reached the Google OAuth endpoint through
the existing Windows localhost proxy after NAT direct access had timed out.
Independent browser ADC consent completed successfully. The official local
`colab --auth adc usage` query returned balance **199.35 CU**, rate **0.00/hour**
and **zero active assignments**. This establishes authenticated local control
connectivity; no Colab runtime was allocated by these local environment tests.
The on-demand local controller is ready for separately authorized research
jobs, independent of the unqualified shared Azure deployment.

### Concurrency and regression evidence

A nonblocking, per-control-identity account flock covers preflight through
release. A contending task records BUSY, exits 75 and never operates on another
task's session. Process death releases the lock. Server assignment checks are
retained because browser/other-host allocations are outside this local lock.

Regression coverage includes a parent with stale NumPy already imported,
constraint acceptance/rejection, child failure propagation, missing success
receipt followed by cleanup, actual Linux process contention, crash unlock,
BUSY without operation, and resource summary/error propagation. The measured
cloud run used the first equivalent inline dependency validator; the final
helper extraction and lock-error classification are covered by offline tests.
Final targeted results: Linux **23 passed in 0.51 seconds**; Windows Python
3.13 **21 passed, 2 Linux-only skips in 1.33 seconds**.
