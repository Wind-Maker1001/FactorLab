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
runtime version. TODO: execute research in a fresh interpreter after dependency
installation so preloaded Colab modules cannot outlive a dependency replacement.

## Official capability boundaries

CLI 0.8.0 has no detached-task command or server-enforced single-task CU cap.
The controller uses a transient systemd service and work deadline instead.
Account-level rounded CU snapshots are retained without claiming exact task
billing. The official session listing can show unnamed orphans, but the stop
command requires a locally known session. Loss of CLI allocation state can thus
require external session cleanup; the controller records that case as failure,
not successful release.
