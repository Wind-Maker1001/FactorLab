"""Task-scoped Linux controller using only the official Colab CLI."""

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid


GPUS = ("T4", "L4", "G4", "H100", "A100")
CLEANUP_SECONDS = 120
SALVAGE_SECONDS = 30


def manifest_inputs(path):
    """Resolve only explicitly declared files within the manifest directory."""
    root = path.resolve().parent
    from factorlab.experiments.research_tasks import load_manifest
    document = load_manifest(path)
    files = {}
    for task in document["tasks"]:
        for name in task.get("inputs", {}).values():
            relative = Path(name)
            if (relative.is_absolute() or ".." in relative.parts or "\\" in name
                    or ":" in name or not relative.parts or name == "manifest.json"):
                raise ValueError(f"Input must be a manifest-root relative path: {name}")
            source = root / relative
            if (not source.resolve().is_relative_to(root) or not source.is_file()
                    or any((root / Path(*relative.parts[:i])).is_symlink()
                           for i in range(1, len(relative.parts) + 1))):
                raise ValueError(f"Input must be a regular file inside manifest root: {name}")
            if any(part.startswith(".") or re.search(
                    r"(?i)(secret|credential|token|password|private[_-]?key|id_rsa|id_ed25519)", part)
                   or part.lower().endswith((".pem", ".key", ".p12", ".pfx"))
                   for part in relative.parts):
                raise ValueError(f"Secret-like input is not uploadable: {name}")
            files[relative.as_posix()] = str(source)
    return document, files


def usage(text):
    """Parse the pinned CLI 0.8.0 account-level display; unknown formats fail closed."""
    patterns = (r"Current balance: ([\d.]+) compute units", r"Usage rate: ([\d.]+)/hr",
                r"Active assignments: (\d+)")
    values = []
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is None:
            raise ValueError("Unsupported colab usage output")
        values.append(float(match[1]))
    if not all(math.isfinite(x) and x >= 0 for x in values):
        raise ValueError("Invalid account usage")
    return dict(zip(("balance", "rate_per_hour", "assignments"), values))


def budget_seconds(budget, balance, rate, elapsed, timeout):
    if not all(math.isfinite(x) for x in (budget, balance, rate, elapsed, timeout)):
        raise ValueError("Budget inputs must be finite")
    if budget <= 0 or balance < budget or rate <= 0 or timeout <= 0:
        raise ValueError("Insufficient balance or unavailable billable rate")
    remaining = min(timeout, budget * 3600 / rate) - elapsed - CLEANUP_SECONDS
    if remaining <= 0:
        raise TimeoutError("Budget cannot accommodate work and cleanup reserve")
    return remaining


def research_code(arguments):
    return ("import subprocess, sys\n"
            f"subprocess.run([sys.executable, *{arguments!r}], check=True)\n")


class AccountBusy(Exception):
    pass


@contextmanager
def account_lock(root):
    import fcntl
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "account.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AccountBusy from exc
        yield


class Job:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / "job.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.deadline = None
        self.timings = {}

    @contextmanager
    def timed(self, phase):
        started = time.monotonic()
        try:
            yield
        finally:
            self.timings[phase] = time.monotonic() - started
            self.save(timings=self.timings)

    def save(self, **fields):
        self.data.update(fields)
        temp = self.root / "job.json.tmp"
        temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def cli(self, *args, bounded=True):
        timeout = 60.0 if bounded else 20.0
        if bounded and self.deadline is not None:
            timeout = self.deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("Task wall-clock deadline reached")
        command = [self.data["colab"], "--auth", "adc", "--config",
                   self.data.get("session_config", str(self.root / "sessions.json")), *map(str, args)]
        with (self.root / "controller.log").open("a+", encoding="utf-8") as log:
            log.write(f"\n$ {shlex.join(command)}\n")
            log.flush()
            offset = log.tell()
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                    text=True, timeout=timeout,
                                    env={**os.environ, "NO_COLOR": "1", "TERM": "dumb",
                                         "REQUEST_TIMEOUT": "60"})
            log.seek(offset)
            output = log.read()
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command, output)
        return output

    def execute(self, code):
        path = self.root / "payload.py"
        receipt = "FACTORLAB_EXEC_OK=" + uuid.uuid4().hex
        path.write_text(code + f"\nprint({receipt!r}, flush=True)\n", encoding="utf-8")
        output = self.cli("exec", "-s", self.data["session"], "--timeout", "0", "-f", path)
        if receipt not in output.splitlines():
            raise RuntimeError("Remote execution did not produce its success receipt")
        return output

    def release(self):
        if self.data.get("released"):
            return
        if not self.data.get("allocation_attempted"):
            self.save(released=True)
            return
        listing = self.cli("sessions", bounded=False)
        endpoint = self.data.get("endpoint")
        named = f"[{self.data['session']}] "
        if named in listing:
            if endpoint and named + endpoint + " |" not in listing:
                raise RuntimeError("Session name now identifies a different runtime; refusing release")
            self.cli("stop", "-s", self.data["session"], bounded=False)
            listing = self.cli("sessions", bounded=False)
        if named in listing or (endpoint and endpoint in listing):
            self.save(state="cleanup_failed", released=False)
            raise RuntimeError("Runtime remains assigned; explicit recovery required")
        # With an interrupted allocation, CLI state may not contain the server's assignment.
        if self.data.get("allocation_attempted") and not endpoint and "Hardware:" in listing:
            self.save(state="cleanup_failed", released=False)
            raise RuntimeError("Unidentified server assignment after allocation; inspect colab sessions")
        self.save(released=True, released_at=time.time(), sessions_after_release=listing)

    def run(self):
        started = time.monotonic()
        self.deadline = started + self.data["timeout"] - CLEANUP_SECONDS
        self.save(state="preflight", started_at=time.time())
        failure = None
        try:
            if self.data.get("manifest"):
                manifest_inputs(Path(self.data["manifest"]))
            version = self.cli("version")
            if version.strip() != "Version: 0.8.0":
                raise ValueError("This adapter requires google-colab-cli==0.8.0")
            # Initialize the official CLI's state locks before allocating a paid runtime.
            self.cli("sessions")
            before = usage(self.cli("usage"))
            self.save(cli_version=version.strip(), usage_before=before)
            if before["assignments"] != 0:
                raise ValueError("Exclusive Colab account allocation required for rate attribution")
            if before["balance"] < self.data["budget_cu"]:
                raise ValueError("Account balance is below task budget")
            self.save(state="allocating", allocation_attempted=True)
            self.cli("new", "-s", self.data["session"], "--gpu", self.data["gpu"])
            listing = self.cli("sessions")
            match = re.search(r"\[" + re.escape(self.data["session"]) + r"\] (\S+) \|", listing)
            if match is None:
                raise RuntimeError("Cannot identify allocated runtime endpoint")
            self.save(endpoint=match[1], state="allocated")
            after = usage(self.cli("usage"))
            if after["assignments"] != 1:
                raise RuntimeError("Concurrent assignments prevent budget attribution")
            remaining = budget_seconds(self.data["budget_cu"], before["balance"],
                                       after["rate_per_hour"] + 0.005, time.monotonic() - started,
                                       self.data["timeout"])
            self.deadline = time.monotonic() + remaining
            self.save(usage_allocated=after, work_deadline=time.time() + remaining)
            hardware = self.execute(
                "import json, os, platform, subprocess\n"
                "gpu = subprocess.check_output(['nvidia-smi', '--query-gpu=name,memory.total,driver_version', "
                "'--format=csv,noheader'], text=True).strip()\n"
                "if not gpu: raise RuntimeError('GPU required')\n"
                "print('FACTORLAB_HARDWARE=' + json.dumps({'gpu': gpu, 'cpu_count': os.cpu_count(), "
                "'python': platform.python_version(), 'memory': open('/proc/meminfo').read()}))\n")
            record = next(line.split("=", 1)[1] for line in hardware.splitlines()
                          if line.startswith("FACTORLAB_HARDWARE="))
            self.save(hardware=json.loads(record), state="installing")
            with self.timed("deployment"):
                self.deploy()
            self.save(state="running")
            if self.data.get("manifest"):
                arguments = ["-m", "factorlab.experiments.research_batch", "remote",
                             "--manifest", "/content/factorlab-input/manifest.json",
                             "--output", "/content/factorlab-result", "--commit", self.data["commit"],
                             "--timeout", str(self.deadline - time.monotonic()),
                             "--cu-rate-per-hour", str(after["rate_per_hour"]),
                             "--cu-balance", str(after["balance"])]
            else:
                arguments = ["/content/monte_carlo.py", "--paths", str(self.data["paths"]),
                             "--steps", str(self.data["steps"]), "--seed", str(self.data["seed"]),
                             "--commit", self.data["commit"]]
            try:
                with self.timed("execution"):
                    self.execute(research_code(arguments))
            except (Exception, KeyboardInterrupt):
                # Salvage has its own bounded share of the cleanup reserve, not the expired work deadline.
                self.deadline = time.monotonic() + SALVAGE_SECONDS
                try:
                    with self.timed("download"):
                        self.download()
                except (Exception, KeyboardInterrupt) as exc:
                    self.save(failure_artifact_error=f"{type(exc).__name__}: {exc}")
                else:
                    self.save(failure_artifacts_downloaded=True)
                raise
            self.save(state="downloading")
            self.deadline = time.monotonic() + SALVAGE_SECONDS
            with self.timed("download"):
                self.download()
            self.save(state="completed", outcome="completed")
        except (Exception, KeyboardInterrupt) as exc:
            failure = exc
            self.save(state="failed", outcome="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            self.deadline = None
            try:
                with self.timed("release"):
                    self.release()
                self.save(cleanup_outcome="released")
            except Exception as exc:
                self.save(state="cleanup_failed", cleanup_outcome="failed",
                          cleanup_error=f"{type(exc).__name__}: {exc}")
                if failure is None:
                    failure = exc
            try:
                final_usage = usage(self.cli("usage", bounded=False))
                observations = {"usage_after": final_usage}
                if "usage_before" in self.data:
                    observations["observed_cu_consumed"] = (
                        self.data["usage_before"]["balance"] - final_usage["balance"])
                self.save(**observations)
            except Exception as exc:
                self.save(usage_after_error=str(exc))
            self.save(elapsed_seconds=time.monotonic() - started, finished_at=time.time())
        if failure is not None:
            raise failure

    def deploy(self):
        self.cli("upload", "-s", self.data["session"], self.data["wheel"], "/content/factorlab.whl")
        wheel_name = Path(self.data["wheel"]).name
        extras = "monte-carlo-cuda"
        if self.data.get("manifest"):
            extras += ",monte-carlo-benchmark,monte-carlo-native"
        self.execute(
            "import os, subprocess, sys\n"
            f"os.rename('/content/factorlab.whl', {('/content/' + wheel_name)!r})\n"
            f"subprocess.run([sys.executable, '-m', 'pip', 'install', "
            f"{('/content/' + wheel_name + '[' + extras + ']')!r}], check=True)\n")
        if not self.data.get("manifest"):
            self.cli("upload", "-s", self.data["session"], self.data["script"], "/content/monte_carlo.py")
            return
        document, files = manifest_inputs(Path(self.data["manifest"]))
        if (document["commit"] != self.data["commit"]
                or document["budget_cu"] != self.data["budget_cu"]
                or document["timeout_seconds"] != self.data["timeout"]):
            raise ValueError("Manifest job limits changed after submission")
        directories = sorted({str(Path(name).parent.as_posix()) for name in files})
        self.execute(
            "from pathlib import Path\n"
            "root = Path('/content/factorlab-input')\n"
            "root.mkdir(mode=0o700)\n"
            f"for directory in {directories!r}:\n"
            "    (root / directory).mkdir(parents=True, exist_ok=True)\n")
        self.cli("upload", "-s", self.data["session"], self.data["manifest"],
                 "/content/factorlab-input/manifest.json")
        for name, source in files.items():
            self.cli("upload", "-s", self.data["session"], source, "/content/factorlab-input/" + name)

    def download(self):
        destination = self.root / "results"
        destination.mkdir(exist_ok=True)
        for name in ("report.json", "samples.npz"):
            self.cli("download", "-s", self.data["session"],
                     f"/content/factorlab-result/{name}", destination / name)
        self.save(result_path=str(destination))


def interrupted(signum, frame):
    raise InterruptedError(f"Task interrupted by signal {signum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("submit", "run", "status", "logs", "download", "release", "recover"))
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--colab", default="colab")
    parser.add_argument("--gpu", choices=GPUS, default="L4")
    parser.add_argument("--budget-cu", type=float)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--wait", action="store_true", help="Wait for the supervised batch to finish")
    parser.add_argument("--script", type=Path, default=Path(__file__).with_name("colab_monte_carlo.py"))
    parser.add_argument("--commit")
    parser.add_argument("--paths", type=int, default=1_000_000)
    parser.add_argument("--steps", type=int, default=252)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    if args.action == "submit":
        if args.manifest is not None:
            document, _ = manifest_inputs(args.manifest)
            for option, field in (("commit", "commit"), ("budget_cu", "budget_cu"),
                                  ("timeout", "timeout_seconds")):
                value = getattr(args, option)
                if value is not None and value != document[field]:
                    parser.error(f"--{option.replace('_', '-')} conflicts with manifest")
                setattr(args, option, document[field])
        if args.timeout is None:
            args.timeout = 1800
        if (args.budget_cu is None or not math.isfinite(args.budget_cu) or args.budget_cu <= 0
                or not math.isfinite(args.timeout) or args.timeout <= CLEANUP_SECONDS
                or args.paths <= 0 or args.steps <= 0 or not 0 <= args.seed < 2**32
                or not args.commit or re.fullmatch(r"[a-f0-9]{40}", args.commit) is None):
            parser.error("Positive CU budget, timeout > 120, valid simulation parameters and full commit required")
        if (args.wheel is None or not args.wheel.is_file()
                or (args.manifest is None and not args.script.is_file())):
            parser.error("Existing FactorLab wheel and research script required")
        import shutil
        executable = shutil.which(args.colab)
        if executable is None:
            parser.error("Official colab executable not found")
        args.job.mkdir(mode=0o700, parents=True, exist_ok=False)
        root = args.job.resolve()
        session = "fl-" + uuid.uuid4().hex
        session_root = Path.home() / ".local/state/factorlab-colab/sessions" / session
        session_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        data = {"session_config": str(session_root / "sessions.json"), "session": session, "state": "submitted", "colab": executable,
                "gpu": args.gpu, "budget_cu": args.budget_cu, "timeout": args.timeout,
                "wheel": str(args.wheel.resolve()), "script": str(args.script.resolve()),
                "commit": args.commit, "paths": args.paths, "steps": args.steps, "seed": args.seed}
        if args.manifest is not None:
            data["manifest"] = str(args.manifest.resolve())
        (root / "job.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        base = [sys.executable, str(Path(__file__).resolve())]
        cleanup = shlex.join([*base, "recover", "--job", str(root)])
        proxy_names = [name for name in
                       ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                        "http_proxy", "https_proxy", "all_proxy", "no_proxy") if name in os.environ]
        preserve = ["--preserve-env=" + ",".join(proxy_names)] if proxy_names else []
        command = ["sudo", "-n", *preserve, "systemd-run", f"--unit={session}", f"--uid={os.getuid()}",
                   "--collect", "--property=Type=exec", "--property=UMask=0077",
                   "--property=MemoryMax=256M", "--property=MemorySwapMax=0",
                   "--property=CPUQuota=25%", "--property=CPUWeight=10",
                   f"--property=RuntimeMaxSec={args.timeout}", "--property=TimeoutStopSec=150",
                   f"--property=ExecStopPost={cleanup}",
                   *[f"--setenv={name}" for name in proxy_names],
                   *base, "run", "--job", str(root)]
        if args.wait:
            command.insert(command.index("systemd-run") + 1, "--wait")
        subprocess.run(command, check=True)
        print(json.dumps({"job": str(root), "unit": session}))
        return
    job = Job(args.job)
    if args.action == "status":
        print(json.dumps(job.data, indent=2))
    elif args.action == "logs":
        print((job.root / "controller.log").read_text(encoding="utf-8"))
    else:
        lock_root = Path.home() / ".local/state/factorlab-colab"
        try:
            with account_lock(lock_root):
                operate(job, args.action)
        except AccountBusy:
            if args.action == "run":
                job.save(state="BUSY")
            print("BUSY: another local controller owns the Colab account", file=sys.stderr)
            raise SystemExit(75)


def operate(job, action):
    if action == "run":
        signal.signal(signal.SIGTERM, interrupted)
        from colab_resources import Resources
        resources = Resources(job.root)
        try:
            job.run()
        finally:
            resources.finish()
    elif action == "download":
        job.download()
    else:
        job.release()


if __name__ == "__main__":
    main()
