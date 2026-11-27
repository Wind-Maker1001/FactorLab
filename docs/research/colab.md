# Colab GPU research jobs

The Linux controller `tools/research/colab_job.py` uses the official
[Google Colab CLI](https://github.com/googlecolab/google-colab-cli), pinned to
`google-colab-cli==0.8.0`. Follow the upstream
[colab-operator skill](https://github.com/googlecolab/google-colab-cli/blob/main/skills/colab-operator/SKILL.md)
for authentication and session operations. On Windows, the preferred research
controller is an on-demand WSL2 Linux environment; the official CLI runs inside
Linux, not natively on Windows. An independent Linux host reached over OpenSSH
is also supported. Keep research control separate from trading execution.
The small shared Azure Standard B2ats v2 deployment has **not passed routine
coexistence qualification**; its optional SSH deployment capability is not a
prerequisite for accepting the independent controller tool.

## On-demand WSL2 control

Use an existing Linux distribution with systemd and Python >=3.12. Keep the CLI
virtual environment, controller scripts, jobs and ADC inside its Linux home,
not a trading service directory. Authorize Google independently in WSL; do not
copy another host's ADC file. Run the same submit/status/logs/recover commands
below through `wsl.exe -d <distribution> -- <linux-python> <controller> ...`.
Copy all three `colab_*.py` scripts together. Research dependencies and simulations
still execute on Colab, not WSL.

If Windows uses a localhost-only proxy, NAT-mode WSL cannot reach that listener
through the Windows gateway. Mirrored networking can expose the existing
localhost proxy to WSL; changing networking requires an explicitly scheduled
WSL restart. Set `HTTPS_PROXY`/`HTTP_PROXY` to the working Linux-reachable proxy
before authorization and submission. The submit command copies standard
uppercase/lowercase proxy environment variables through sudo's explicit
`--preserve-env` allowlist and into the transient unit by name, without
including their values in the systemd-run command line. Proxy
configuration is not a credential-transfer mechanism.

WSL is needed only while controlling a job. Keep a WSL foreground session open
through completion and release: systemd services alone do not guarantee WSL
instance lifetime. Closing a terminal is not cancellation, and terminating WSL
is not cleanup. After the task is released, the foreground session can exit;
no research controller daemon or permanent WSL keepalive is installed. Never
use `wsl --shutdown` while another workload uses a distribution. After an
unexpected host/WSL shutdown, start WSL and invoke `recover` before submitting
another job.

## Host setup

Install the CLI in its own Python >=3.12 environment. The controller uses only
Python's standard library and does not require installing FactorLab on the
control host. Keep its environment, task directories and Google credentials
separate from trading service environments and state. No trading credential is
needed. Use an account able to run `sudo -n systemd-run`; the submit command
creates a transient per-job service under the submitting Linux UID, with a
256 MiB memory limit, no cgroup swap, 25% CPU quota and CPUWeight=10.
No persistent scheduler is installed. Copy `colab_resources.py` alongside the
controller; it records task-scoped procfs/cgroup evidence in `resources.json`
and one-second system samples in `resources.jsonl`. These limits were measured
on Standard B2ats v2; system-wide pressure still requires operational review,
not merely checking the cgroup limit.

Authorize once on that host:

```sh
gcloud auth application-default login --no-launch-browser \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
colab --auth adc usage
```

Protect the resulting ADC file and do not include it in job artifacts. Use the
Google account holding the Colab subscription. `colab auth` is for VM-side GCP
access and is not a substitute for CLI authentication. Drive mounting is not
required.

## Submit and inspect

Build a wheel from the intended clean Git commit using the existing packaging
workflow, and copy it plus both scripts to a private directory on the Linux
host. Colab must supply Python 3.13 or 3.14 for the FactorLab wheel. Dependencies
are installed into Colab, not the trading environment.

```sh
python3 colab_job.py submit \
  --job /absolute/private/jobs/unique-job \
  --colab /absolute/colab-venv/bin/colab \
  --wheel /absolute/factorlab-0.1.0.dev0-py3-none-any.whl \
  --script /absolute/colab_monte_carlo.py \
  --commit <full-source-commit> --gpu L4 --budget-cu 2 \
  --timeout 1800 --paths 1000000 --steps 252 --seed 0

python3 colab_job.py status --job /absolute/private/jobs/unique-job
python3 colab_job.py logs --job /absolute/private/jobs/unique-job
```

Submit returns a systemd unit name and task directory. The task continues after
SSH disconnects. `controller.log` receives CLI output while commands run;
`journalctl -u <unit>` contains controller failures. Local status does not submit
or restart a task. The official CLI has no detached-job subcommand; background
ownership and the wall-clock limit belong to systemd and the controller.

The controller sets the installed kernel client's supported `REQUEST_TIMEOUT=60`
for cold-kernel WebSocket handshakes. Its default 10-second wait can expire
before a healthy Colab kernel starts, producing a misleading connection-lost
error and leaving the CLI process alive ([upstream issue #285](https://github.com/googlecolab/google-colab-cli/issues/285)).
This connection timeout is separate from both the execution idle timeout and
the task's total wall-clock deadline.

Only one Colab account assignment is allowed during a job, because `usage`
reports account-wide consumption rather than a task-specific rate. Existing
assignments cause submission execution to refuse before allocating a new VM.
A nonblocking `flock` at `~/.local/state/factorlab-colab/account.lock` covers
preflight through release, including recovery and manual downloads. A competing
controller exits 75 (`BUSY`) without allocating or releasing anything; process
exit releases the lock. All tasks sharing an account must use the same Linux
control identity. Browser sessions and other hosts are outside this lock, so
the server assignment checks remain necessary.
Requested GPU and the actual `nvidia-smi` hardware are recorded separately.
Allocation of a different GPU is accepted only against its actual account rate;
missing GPU refuses before dependency installation. The native runner receives
`ComputeConfig(device="gpu", allow_fallback=False)`.

The native GBM example uses a single fully invested asset, annual drift 0.08,
volatility 0.2, 252 steps/year and the supplied seed. Its complete default result
is `results/report.json` plus `results/samples.npz` containing terminal values
and maximum drawdowns for every path. Full path histories are not requested.
Copy the entire `results` directory back with SCP. The report contains native
compute evidence, source commit, parameters, dependency versions and actual
GPU information; `job.json` adds lifecycle timing and CU snapshots. Research
starts in a fresh Python subprocess after pip installation, rather than reusing
kernel imports. Loaded NumPy/CuPy and installed dependencies are checked against
FactorLab metadata before simulation. A unique success receipt supplements the
official CLI exit code, since CLI 0.8.0 can return zero after a remote exception.
Keep the
original wheel with the private run artifacts.

## Offline and Linux platform acceptance

Run portable controller behavior checks with
`python -m pytest tests/research/test_colab_job.py` on Windows or Linux.
Real Linux process contention, crash-unlock and BUSY isolation are a separate
explicit platform entrypoint:
`python -m pytest tests/research/acceptance_colab_linux.py`.
Run that entrypoint inside Linux/WSL; it requires `fcntl` and contains no
platform skips. Neither command allocates a Colab runtime.

## Budgets and cleanup

The budget is mandatory and never enables purchases. After allocation the
controller reads the actual usage rate, allows for the display's 0.01 CU/hour
rounding, and sets a total work deadline including elapsed allocation time.
It reserves 120 seconds for cleanup; the explicit timeout can shorten the
CU-derived limit. Each cleanup CLI command has a 20-second timeout.

Colab CLI 0.8.0 exposes no server-enforced per-task CU cap. Its balances/rates
are rounded and account-wide; the controller's deadline is a spending control,
not an exact billing guarantee during rate changes, host failure or a backend
that refuses release. `usage_before`, `usage_allocated` and `usage_after` retain
observed account values; balance differences must not be described as exact
job consumption. A task refuses further work if the rate cannot be determined.

When research execution fails, the controller first attempts to download the
same `report.json` and `samples.npz` contract before releasing the runtime.
Scripts must write their failure evidence to those paths themselves. Retrieval
uses the remaining work deadline, preserves any files already downloaded and
never converts execution failure into success. `failure_artifacts_downloaded`
records a complete transfer; `failure_artifact_error` records a failed transfer
separately from the original execution error. Cleanup failures also retain the
original execution exception while recording `cleanup_error`.

Execution, transfer failure, SIGTERM and work timeout enter cleanup.
`ExecStopPost` also invokes recovery after the transient service exits. Release
is confirmed by a new official server-side `sessions` listing, including a
check of the endpoint even if it appears under an orphan name. A successful
`stop` message alone does not establish release.

```sh
# Cancel a running task, including its local process, then invoke cleanup.
sudo systemctl stop <unit-name-returned-by-submit>

# Explicitly recover a terminated task's resource, without re-running research.
python3 colab_job.py recover --job /absolute/private/jobs/unique-job

# Download while a failed task's runtime is still available.
python3 colab_job.py download --job /absolute/private/jobs/unique-job
```

Downloads normally happen before automatic release. Once a VM is released,
remote files are gone; recovery does not silently allocate a replacement or
rerun the simulation. `release` is an explicit CLI cleanup alias; use systemctl
stop when cancelling active work.

An allocation can succeed remotely and lose its local CLI record before the
controller learns the endpoint. The official CLI cannot stop an unnamed orphan
by endpoint. That case records `cleanup_failed` and retains server evidence;
it requires inspecting the official Colab session interface rather than editing
CLI state or calling undocumented APIs. Do not stop unrelated sessions.
