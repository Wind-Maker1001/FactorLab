"""Capture a verified, strictly read-only SQLite Backup API snapshot from Azure for R08.

This tool never writes to the live SQLite databases, never restarts systemd services,
never alters trading control state, and never accesses or downloads API secrets.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys


REMOTE_BACKUP_SCRIPT = r"""
import hashlib
import json
import pathlib
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone

STATE_ROOT = pathlib.Path(sys.argv[1])
OUT_DIR = pathlib.Path(sys.argv[2])
MAX_ATTEMPTS = int(sys.argv[3])


def discover_journal_id(root: pathlib.Path) -> str:
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "controller.sqlite3").is_file() and (child / "protocol.sqlite3").is_file():
            return child.name
    raise FileNotFoundError(f"no account journal directory found under {root}")


JID = discover_journal_id(STATE_ROOT)
DBS = [
    "runtime.sqlite3",
    "grid-supervisor.sqlite3",
    f"{JID}/controller.sqlite3",
    f"{JID}/protocol.sqlite3",
]
if (STATE_ROOT / "strategy-execute.sqlite3").is_file():
    DBS.append("strategy-execute.sqlite3")

META_FILES = [
    "cloud-health.json",
    "execute-handoff-result.json",
    "binding.json",
]


def stat_signature():
    sig = {}
    for rel in DBS + META_FILES:
        p = STATE_ROOT / rel
        if p.exists():
            st = p.stat()
            sig[rel] = (st.st_size, st.st_mtime_ns)
        for suf in ("-wal", "-shm", "-journal"):
            sp = pathlib.Path(str(p) + suf)
            if sp.exists():
                st = sp.stat()
                sig[rel + suf] = (st.st_size, st.st_mtime_ns)
    return sig


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_cross_db(snap_dir: pathlib.Path) -> dict:
    integrity = {}
    for rel in DBS:
        dst = snap_dir / "databases" / rel
        con = sqlite3.connect(dst.as_uri() + "?mode=ro", uri=True)
        try:
            integrity[rel] = con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        finally:
            con.close()

    p_con = sqlite3.connect((snap_dir / "databases" / f"{JID}/protocol.sqlite3").as_uri() + "?mode=ro", uri=True)
    c_con = sqlite3.connect((snap_dir / "databases" / f"{JID}/controller.sqlite3").as_uri() + "?mode=ro", uri=True)
    r_con = sqlite3.connect((snap_dir / "databases" / "runtime.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        proto_orders = {r[0] for r in p_con.execute("SELECT id FROM futures_orders")}
        proto_funding = {r[0] for r in p_con.execute("SELECT id FROM futures_funding")}
        ctrl_funding = {r[0] for r in c_con.execute("SELECT id FROM events WHERE id LIKE 'funding:%'")}
        post_cids = {r[0] for r in r_con.execute("SELECT client_id FROM dispatches WHERE method='POST'")}
    finally:
        p_con.close()
        c_con.close()
        r_con.close()

    consistent = (
        all(integrity.values())
        and proto_funding == ctrl_funding
        and post_cids.issubset(proto_orders)
    )
    return {
        "sqlite_integrity": integrity,
        "all_integrity_ok": all(integrity.values()),
        "funding_ids_match": proto_funding == ctrl_funding,
        "post_orders_in_protocol": post_cids.issubset(proto_orders),
        "consistent": consistent,
    }


for attempt in range(1, MAX_ATTEMPTS + 1):
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    (OUT_DIR / "databases" / JID).mkdir(parents=True, exist_ok=True)
    pre_sig = stat_signature()
    started = datetime.now(timezone.utc).isoformat()
    for rel in DBS:
        src_con = sqlite3.connect((STATE_ROOT / rel).as_uri() + "?mode=ro", uri=True, timeout=15)
        dst_con = sqlite3.connect(str(OUT_DIR / "databases" / rel))
        try:
            src_con.backup(dst_con)
        finally:
            dst_con.close()
            src_con.close()
    for rel in META_FILES:
        src = STATE_ROOT / rel
        if src.exists():
            shutil.copyfile(src, OUT_DIR / rel)
    post_sig = stat_signature()
    finished = datetime.now(timezone.utc).isoformat()
    quiescent = pre_sig == post_sig
    checks = verify_cross_db(OUT_DIR)
    manifest = {
        "captured_started_at_utc": started,
        "captured_finished_at_utc": finished,
        "attempt": attempt,
        "source_quiescent_during_backup": quiescent,
        "cross_db_checks": checks,
        "sha256": {f"databases/{rel}": sha256_file(OUT_DIR / "databases" / rel) for rel in DBS},
    }
    (OUT_DIR / "snapshot-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    if quiescent and checks["consistent"]:
        print(json.dumps(manifest, indent=2))
        sys.exit(0)
    time.sleep(2.0)

print("ERROR: failed to obtain consistent quiescent read-only snapshot", file=sys.stderr)
sys.exit(1)
"""


def _run(cmd: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=input_text, text=True, capture_output=True, check=True)


def capture_ssh_snapshot(
    *,
    ssh_host: str,
    ssh_user: str,
    ssh_key: Path,
    output_dir: Path,
    remote_state_root: str = "/var/lib/factorlab-demo/execute-state",
    remote_service_user: str = "factorlab-demo",
    remote_python: str = "/opt/factorlab/venv/bin/python",
    max_attempts: int = 6,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    remote_dir = f"/tmp/r08-ro-snapshot-{stamp}"
    remote_tar = f"{remote_dir}.tar.gz"
    ssh_base = [
        "ssh",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-i",
        str(ssh_key),
        f"{ssh_user}@{ssh_host}",
    ]
    scp_base = [
        "scp",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-i",
        str(ssh_key),
    ]

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    local_tar = output_dir / "snapshot.tar.gz"

    try:
        # 1. Take verified read-only SQLite Backup API snapshot as service user
        res = _run(
            ssh_base
            + [
                f"sudo -u {remote_service_user} {remote_python} - "
                f"'{remote_state_root}' '{remote_dir}' {max_attempts}"
            ],
            input_text=REMOTE_BACKUP_SCRIPT,
        )
        print(res.stdout)

        # 2. Collect read-only systemd & journald operational records (zero secrets)
        ops_cmd = (
            f"set -e; sudo mkdir -p '{remote_dir}/ops'; "
            f"sudo systemctl show factorlab-demo-execute.service -p Id -p ActiveState -p SubState "
            f"-p MainPID -p NRestarts -p ExecMainStartTimestamp -p ExecMainStatus -p MemoryCurrent "
            f"> /tmp/r08-sys-exec.txt; "
            f"sudo systemctl show factorlab-demo-health.timer -p Id -p ActiveState -p SubState "
            f"-p LastTriggerUSec -p NextElapseUSecRealtime > /tmp/r08-sys-timer.txt; "
            f"sudo journalctl --no-pager --output=short-iso -u factorlab-demo-execute.service "
            f"> /tmp/r08-journal-execute.log || true; "
            f"sudo journalctl --no-pager --output=short-iso -u factorlab-demo-health.service "
            f"> /tmp/r08-journal-health.log || true; "
            f"sudo journalctl --no-pager --output=short-iso -u factorlab-demo-handoff.service "
            f"> /tmp/r08-journal-handoff.log || true; "
            f"uname -a > /tmp/r08-uname.txt; "
            f"sudo mv /tmp/r08-sys-exec.txt /tmp/r08-sys-timer.txt /tmp/r08-journal-execute.log "
            f"/tmp/r08-journal-health.log /tmp/r08-journal-handoff.log /tmp/r08-uname.txt '{remote_dir}/ops/'; "
            f"sudo tar -czf '{remote_tar}' -C '{remote_dir}' .; "
            f"sudo chown {ssh_user}:{ssh_user} '{remote_tar}'; chmod 600 '{remote_tar}'"
        )
        _run(ssh_base + [ops_cmd])

        # 3. Download archive to private local directory and unpack
        _run(scp_base + [f"{ssh_user}@{ssh_host}:{remote_tar}", str(local_tar)])
        shutil.unpack_archive(str(local_tar), str(output_dir))
        local_tar.unlink(missing_ok=True)
    finally:
        # 4. Always clean temporary remote staging files
        subprocess.run(ssh_base + [f"sudo rm -rf '{remote_dir}' '{remote_tar}'"], check=False)

    return output_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", required=True, help="Azure VM IP or hostname")
    parser.add_argument("--ssh-user", default="azureuser", help="SSH username")
    parser.add_argument("--ssh-key", required=True, type=Path, help="Path to SSH private key (.pem)")
    parser.add_argument("--output", required=True, type=Path, help="Local private output directory (outside Git)")
    parser.add_argument("--remote-state-root", default="/var/lib/factorlab-demo/execute-state")
    parser.add_argument("--remote-service-user", default="factorlab-demo")
    parser.add_argument("--remote-python", default="/opt/factorlab/venv/bin/python")
    parser.add_argument("--max-attempts", type=int, default=6)
    args = parser.parse_args(argv)

    out = capture_ssh_snapshot(
        ssh_host=args.ssh_host,
        ssh_user=args.ssh_user,
        ssh_key=args.ssh_key,
        output_dir=args.output,
        remote_state_root=args.remote_state_root,
        remote_service_user=args.remote_service_user,
        remote_python=args.remote_python,
        max_attempts=args.max_attempts,
    )
    print(f"Verified read-only snapshot saved to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
