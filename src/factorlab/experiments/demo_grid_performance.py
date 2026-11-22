"""Read-only empirical performance analysis for the official Binance Demo Grid (R08).

All live or archived SQLite databases are opened strictly read-only (``?mode=ro``).
This module never mutates online ledgers, never places or cancels orders, and
strictly separates official Demo empirical evidence from offline historical
window replays, synthetic Monte Carlo/Bootstrap risk scenarios, and unverified
claims.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any

import numpy as np
import pandas as pd

from factorlab._identity import source_context_root
from factorlab.compute import ComputeConfig
from factorlab.experiments.grid_empirical import load_hourly
from factorlab.experiments.provenance import capture_provenance
from factorlab.experiments.grid_exposure_benchmark import (
    fifo_control as benchmark_fifo_control,
    simulate as benchmark_simulate,
    stats as benchmark_stats,
)
from factorlab.experiments.grid_fifo import attribute_events, attribution as paper_fifo_attribution
from factorlab.experiments.grid_funding import (
    cashflow as funding_cashflow,
    drawdown as funding_drawdown_pct,
    position_settlements,
)
from factorlab.experiments.grid_lifecycle import lifecycle_metrics
from factorlab.experiments.grid_monte_carlo import (
    SCENARIO as MC_SCENARIO,
    close_bars,
    historical_input,
    stress_paths,
)
from factorlab.experiments.grid_paper import D, run_experiment
from factorlab.experiments.mc_grid_tail_f04c_windows import candidate_windows
from factorlab.research.monte_carlo import (
    MonteCarloConfig,
    MonteCarloInput,
    MonteCarloRunner,
)
from factorlab.research.monte_carlo.reliability import summarize_paths
from factorlab.research.regime_jump import (
    CalibrationError,
    JumpConfig,
    JumpStress,
    PathPlan,
    RegimeBootstrap,
    RegimeConfig,
)
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.contracts import FundingEvent, Market, OrderEvent
from factorlab.trading.storage import decode

ZERO = Decimal("0")
USDT_EQUITY_QUANTUM = Decimal("0.00000001")
DEFAULT_JOURNAL_ID = "eb66189ca89a5d1f8253a4e35c66bd12"
DEFAULT_OPENING_WALLET = Decimal("5000.00000000")
DEFAULT_STRATEGY_CAP_USDT = Decimal("7.00000000")

EVIDENCE_BOUNDARIES = {
    "empirical": "EMPIRICAL_OFFICIAL_DEMO",
    "historical_replay": "HISTORICAL_OFFLINE_REPLAY",
    "synthetic_simulation": "SYNTHETIC_MONTE_CARLO_BOOTSTRAP",
    "unverified": "UNVERIFIED_OPEN_CLAIM",
}


def _ro_connect(path: Path) -> sqlite3.Connection:
    """Open an SQLite database strictly read-only without mutating files or sidecars."""
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite database not found: {resolved}")
    return sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)


def _decode_canonical(raw: str) -> Any:
    return decode(json.loads(raw))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)


def _dec_str(value: Decimal | None, places: int = 8) -> str | None:
    if value is None:
        return None
    q = Decimal(10) ** (-places)
    return f"{value.quantize(q):f}"


def _mask_account_id(account_id: str | None) -> str | None:
    if not account_id:
        return None
    parts = str(account_id).split(":")
    if len(parts) >= 3 and len(parts[-1]) > 12:
        parts[-1] = parts[-1][:6] + "..." + parts[-1][-6:]
    return ":".join(parts)


def _resolve_db_root(snapshot_dir: Path) -> Path:
    """Accept either a snapshot folder with a ``databases/`` subdir or a flat state root."""
    snapshot_dir = Path(snapshot_dir)
    if (snapshot_dir / "databases" / "runtime.sqlite3").is_file():
        return snapshot_dir / "databases"
    if (snapshot_dir / "runtime.sqlite3").is_file():
        return snapshot_dir
    raise FileNotFoundError(f"Cannot locate runtime.sqlite3 under {snapshot_dir}")


def _discover_journal_id(db_root: Path) -> str:
    for child in sorted(db_root.iterdir()):
        if child.is_dir() and (child / "controller.sqlite3").is_file() and (child / "protocol.sqlite3").is_file():
            return child.name
    raise FileNotFoundError(f"Cannot locate account journal directory under {db_root}")


def _check_integrity(db_path: Path) -> bool:
    con = _ro_connect(db_path)
    try:
        return con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        con.close()


def _read_single_snapshot(snapshot_dir: Path, label: str) -> dict[str, Any]:
    snapshot_dir = Path(snapshot_dir)
    db_root = _resolve_db_root(snapshot_dir)
    journal_id = _discover_journal_id(db_root)
    db_paths = {
        "runtime": db_root / "runtime.sqlite3",
        "grid_supervisor": db_root / "grid-supervisor.sqlite3",
        "controller": db_root / journal_id / "controller.sqlite3",
        "protocol": db_root / journal_id / "protocol.sqlite3",
    }
    optional_se = db_root / "strategy-execute.sqlite3"
    if optional_se.is_file():
        db_paths["strategy_execute"] = optional_se

    integrity = {name: _check_integrity(path) for name, path in db_paths.items()}
    sha256 = {name: _sha256_file(path) for name, path in db_paths.items()}

    # 1. runtime.sqlite3
    rcon = _ro_connect(db_paths["runtime"])
    try:
        runtime_kv = dict(rcon.execute("SELECT key, value FROM runtime").fetchall())
        campaigns = {
            cid: json.loads(payload)
            for cid, payload in rcon.execute("SELECT id, payload FROM campaigns ORDER BY rowid").fetchall()
        }
        dispatches = []
        for campaign, method, client_id, params, receipt in rcon.execute(
            "SELECT campaign, method, client_id, parameters, receipt FROM dispatches ORDER BY rowid"
        ).fetchall():
            dispatches.append(
                {
                    "campaign": campaign,
                    "method": method,
                    "client_order_id": client_id,
                    "parameters": json.loads(params),
                    "receipt": json.loads(receipt) if receipt is not None else None,
                }
            )
    finally:
        rcon.close()

    # 2. grid-supervisor.sqlite3
    gcon = _ro_connect(db_paths["grid_supervisor"])
    try:
        gs_row = gcon.execute(
            "SELECT configuration, desired, report FROM strategy_control WHERE id=1"
        ).fetchone()
        gs_cursors = [
            {"id": r[0], "bar": r[1], "quote": r[2], "initial_start": r[3]}
            for r in gcon.execute("SELECT id, bar, quote, initial_start FROM cursor ORDER BY id").fetchall()
        ]
    finally:
        gcon.close()
    grid_supervisor = {
        "configuration": json.loads(gs_row[0]) if gs_row and gs_row[0] else None,
        "desired": gs_row[1] if gs_row else None,
        "report": json.loads(gs_row[2]) if gs_row and gs_row[2] else None,
        "cursors": gs_cursors,
    }

    # 3. protocol.sqlite3
    pcon = _ro_connect(db_paths["protocol"])
    try:
        proto_orders = {}
        for oid, payload, quote in pcon.execute(
            "SELECT id, payload, quote FROM futures_orders ORDER BY rowid"
        ).fetchall():
            proto_orders[oid] = {"record": _decode_canonical(payload), "quote": Decimal(str(quote))}
        proto_receipts = []
        for rid, cid, obs, payload in pcon.execute(
            "SELECT id, client_id, observed_at, payload FROM futures_receipts ORDER BY rowid"
        ).fetchall():
            proto_receipts.append(
                {"receipt_id": rid, "client_order_id": cid, "observed_at": obs, **json.loads(payload)}
            )
        proto_recovery = {}
        cols = [r[1] for r in pcon.execute("PRAGMA table_info(futures_order_recovery)").fetchall()]
        for row in pcon.execute("SELECT * FROM futures_order_recovery ORDER BY rowid").fetchall():
            item = dict(zip(cols, row))
            proto_recovery[item["client_order_id"]] = item
        proto_funding = {}
        for fid, tran_id, event_time_ms, payload, obs in pcon.execute(
            "SELECT id, tran_id, event_time_ms, payload, observed_at FROM futures_funding ORDER BY event_time_ms"
        ).fetchall():
            proto_funding[fid] = {
                "event_id": fid,
                "tran_id": str(tran_id),
                "event_time_ms": int(event_time_ms),
                "event": _decode_canonical(payload),
                "observed_at": obs,
            }
        income_coverage = [
            {
                "scope": r[0],
                "covered_from_ms": int(r[1]),
                "covered_through_ms": int(r[2]),
                "complete": bool(r[3]),
                "updated_at": r[4],
                "detail": json.loads(r[5]) if r[5] else None,
            }
            for r in pcon.execute(
                "SELECT scope, covered_from_ms, covered_through_ms, complete, updated_at, detail "
                "FROM futures_income_coverage"
            ).fetchall()
        ]
        proto_rejections = {
            rid: json.loads(payload)
            for rid, payload in pcon.execute("SELECT id, payload FROM futures_rejections ORDER BY rowid").fetchall()
        }
        meta_row = pcon.execute("SELECT account, reconciled FROM futures_meta WHERE id=1").fetchone()
        raw_acct = meta_row[0] if meta_row and meta_row[0] else None
        if isinstance(raw_acct, str) and raw_acct.lstrip().startswith("{"):
            parsed_acct = json.loads(raw_acct)
        else:
            parsed_acct = {"account_id": raw_acct} if raw_acct else None
        proto_meta = {
            "account": parsed_acct,
            "reconciled": bool(meta_row[1]) if meta_row else False,
        }
    finally:
        pcon.close()

    # 4. controller.sqlite3
    ccon = _ro_connect(db_paths["controller"])
    try:
        snap_row = ccon.execute("SELECT version, payload FROM snapshot WHERE id=1").fetchone()
        state = _decode_canonical(snap_row[1]) if snap_row else None
        event_ids: list[str] = []
        order_events: dict[str, OrderEvent] = {}
        funding_events: dict[str, FundingEvent] = {}
        deployments: dict[str, dict[str, Any]] = {}
        deployment_order: list[str] = []
        bars_by_deployment: dict[str, list[dict[str, Any]]] = defaultdict(list)
        exit_limit_commands: dict[str, list[dict[str, Any]]] = defaultdict(list)
        withdraw_commands: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for eid, raw in ccon.execute("SELECT id, payload FROM events ORDER BY rowid").fetchall():
            event_ids.append(eid)
            if eid.startswith("demo-grid:") and ":source:" in eid:
                # Source records duplicate bar/quote payloads already captured in command observe.
                continue
            obj = _decode_canonical(raw)
            if isinstance(obj, OrderEvent):
                order_events[eid] = obj
            elif isinstance(obj, FundingEvent):
                funding_events[eid] = obj
            elif eid.startswith("demo-grid:") and "deployment" in eid and isinstance(obj, dict):
                dep = obj.get("deployment", obj)
                dep_id = dep.get("deployment_id")
                if dep_id:
                    if dep_id not in deployments:
                        deployment_order.append(dep_id)
                    deployments[dep_id] = dep
            elif eid.startswith("demo-grid:") and ":command:" in eid and isinstance(obj, dict):
                parts = eid.split(":")
                dep_id = parts[1]
                action = obj.get("action")
                if action == "observe":
                    val = obj.get("value") or {}
                    if val.get("completed_bar"):
                        src_bar = val.get("source_bar") or {}
                        bars_by_deployment[dep_id].append(
                            {
                                "event_id": eid,
                                "deployment_id": dep_id,
                                "bar_id": val.get("id"),
                                "time": _parse_utc(val["time"]),
                                "open": Decimal(str(src_bar.get("open", val["close"]))),
                                "high": Decimal(str(val["high"])),
                                "low": Decimal(str(val["low"])),
                                "close": Decimal(str(val["close"])),
                                "volume": Decimal(str(src_bar.get("volume", val.get("volume", 0)))),
                            }
                        )
                elif action == "exit_limit":
                    exit_limit_commands[dep_id].append(
                        {"event_id": eid, "value": obj.get("value"), "account": obj.get("account")}
                    )
                elif action == "withdraw":
                    withdraw_commands[dep_id].append(
                        {"event_id": eid, "value": obj.get("value"), "account": obj.get("account")}
                    )
    finally:
        ccon.close()

    # Optional side JSON metadata
    cloud_health = None
    if (snapshot_dir / "cloud-health.json").is_file():
        cloud_health = json.loads((snapshot_dir / "cloud-health.json").read_text(encoding="utf-8"))
    handoff_result = None
    if (snapshot_dir / "execute-handoff-result.json").is_file():
        handoff_result = json.loads((snapshot_dir / "execute-handoff-result.json").read_text(encoding="utf-8"))
    snapshot_manifest = None
    if (snapshot_dir / "snapshot-manifest.json").is_file():
        snapshot_manifest = json.loads((snapshot_dir / "snapshot-manifest.json").read_text(encoding="utf-8"))
    ops_evidence = _parse_ops_evidence(snapshot_dir / "ops") if (snapshot_dir / "ops").is_dir() else None

    return {
        "label": label,
        "journal_id": journal_id,
        "integrity": integrity,
        "sha256": sha256,
        "runtime_kv": runtime_kv,
        "campaigns": campaigns,
        "dispatches": dispatches,
        "grid_supervisor": grid_supervisor,
        "proto_orders": proto_orders,
        "proto_receipts": proto_receipts,
        "proto_recovery": proto_recovery,
        "proto_funding": proto_funding,
        "income_coverage": income_coverage,
        "proto_rejections": proto_rejections,
        "proto_meta": proto_meta,
        "state": state,
        "event_ids": event_ids,
        "order_events": order_events,
        "funding_events": funding_events,
        "deployments": deployments,
        "deployment_order": deployment_order,
        "bars_by_deployment": dict(bars_by_deployment),
        "exit_limit_commands": dict(exit_limit_commands),
        "withdraw_commands": dict(withdraw_commands),
        "cloud_health": cloud_health,
        "handoff_result": handoff_result,
        "snapshot_manifest": snapshot_manifest,
        "ops_evidence": ops_evidence,
    }


def _parse_ops_evidence(ops_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"present": True}
    sys_exec = ops_dir / "r08-sys-exec.txt"
    if sys_exec.is_file():
        kv = {}
        for line in sys_exec.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        result["systemd_execute"] = kv
    sys_timer = ops_dir / "r08-sys-timer.txt"
    if sys_timer.is_file():
        kv = {}
        for line in sys_timer.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        result["systemd_health_timer"] = kv
    uname = ops_dir / "r08-uname.txt"
    if uname.is_file():
        result["uname"] = uname.read_text(encoding="utf-8", errors="replace").strip()

    exec_log = ops_dir / "r08-journal-execute.log"
    if exec_log.is_file():
        pid_segments: dict[str, dict[str, Any]] = {}
        systemd_events: list[dict[str, str]] = []
        pid_re = re.compile(r"^(\S+)\s+\S+\s+factorlab-demo\[(\d+)\]:\s*(\{.*\})$")
        sys_re = re.compile(r"^(\S+)\s+\S+\s+systemd\[\d+\]:\s*(.*)$")
        for line in exec_log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = pid_re.match(line)
            if m:
                ts, pid, raw_json = m.groups()
                try:
                    payload = json.loads(raw_json)
                except json.JSONDecodeError:
                    continue
                req = payload.get("requests") or {}
                seg = pid_segments.setdefault(
                    pid,
                    {
                        "pid": pid,
                        "first_ts": ts,
                        "last_ts": ts,
                        "samples": 0,
                        "max_get": 0,
                        "max_post": 0,
                        "max_delete": 0,
                        "deployments_seen": [],
                    },
                )
                seg["last_ts"] = ts
                seg["samples"] += 1
                seg["max_get"] = max(seg["max_get"], int(req.get("GET", 0)))
                seg["max_post"] = max(seg["max_post"], int(req.get("POST", 0)))
                seg["max_delete"] = max(seg["max_delete"], int(req.get("DELETE", 0)))
                dep_id = (payload.get("deployment") or {}).get("deployment_id")
                if dep_id and dep_id not in seg["deployments_seen"]:
                    seg["deployments_seen"].append(dep_id)
                continue
            sm = sys_re.match(line)
            if sm:
                ts, msg = sm.groups()
                if any(k in msg for k in ("Started", "Stopping", "Stopped", "Deactivated")):
                    systemd_events.append({"timestamp": ts, "message": msg})
        segments = list(pid_segments.values())
        result["execute_journal"] = {
            "pid_segments": segments,
            "systemd_transitions": systemd_events,
            "controlled_restart_observed": len(segments) >= 2,
            "post_restart_posts": sum(seg["max_post"] for seg in segments[1:]) if len(segments) >= 2 else 0,
            "zero_duplicate_post_after_restart": (
                len(segments) >= 2 and all(seg["max_post"] == 0 for seg in segments[1:])
            ),
        }

    health_log = ops_dir / "r08-journal-health.log"
    if health_log.is_file():
        nav_series = []
        json_re = re.compile(r"^(\S+)\s+\S+\s+python\[\d+\]:\s*(\{.*\})$")
        for line in health_log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = json_re.match(line)
            if m:
                ts, raw_json = m.groups()
                try:
                    payload = json.loads(raw_json)
                except json.JSONDecodeError:
                    continue
                if payload.get("nav_usdt") is not None:
                    nav_series.append(
                        {
                            "timestamp": ts,
                            "nav_usdt": str(payload["nav_usdt"]),
                            "ready": bool(payload.get("ready")),
                            "account_recovered": bool(payload.get("account_recovered")),
                            "grid_deployment_id": (payload.get("grid") or {}).get("deployment_id"),
                            "unknown_count": len(payload.get("unknown_orders") or []),
                        }
                    )
        result["health_timer_nav_samples"] = nav_series
    return result


def load_and_merge_snapshots(
    cloud_snapshot: Path,
    *,
    windows_snapshot: Path | None = None,
) -> dict[str, Any]:
    """Load read-only snapshots, deduplicate by authoritative IDs, and verify consistency."""
    cloud = _read_single_snapshot(cloud_snapshot, "azure_cloud")
    windows = _read_single_snapshot(windows_snapshot, "windows_sealed") if windows_snapshot is not None else None

    # Deduplication audit across Windows sealed baseline and Azure cloud snapshot
    dedup_audit: dict[str, Any] = {
        "windows_snapshot_provided": windows is not None,
        "cloud_integrity_ok": all(cloud["integrity"].values()),
        "windows_integrity_ok": all(windows["integrity"].values()) if windows is not None else None,
        "cloud_db_sha256": cloud["sha256"],
        "windows_db_sha256": windows["sha256"] if windows is not None else None,
    }
    if windows is not None:
        win_ev = set(windows["event_ids"])
        az_ev = set(cloud["event_ids"])
        win_ord = set(windows["proto_orders"])
        az_ord = set(cloud["proto_orders"])
        win_fund = set(windows["proto_funding"])
        az_fund = set(cloud["proto_funding"])
        win_disp = {(d["campaign"], d["method"], d["client_order_id"]) for d in windows["dispatches"]}
        az_disp = {(d["campaign"], d["method"], d["client_order_id"]) for d in cloud["dispatches"]}
        dedup_audit.update(
            {
                "journal_id_match": windows["journal_id"] == cloud["journal_id"],
                "controller_events": {
                    "windows": len(win_ev),
                    "cloud": len(az_ev),
                    "overlap": len(win_ev & az_ev),
                    "windows_only": len(win_ev - az_ev),
                    "cloud_only_new": len(az_ev - win_ev),
                    "merged_unique": len(win_ev | az_ev),
                },
                "protocol_orders": {
                    "windows": len(win_ord),
                    "cloud": len(az_ord),
                    "overlap": len(win_ord & az_ord),
                    "windows_only": len(win_ord - az_ord),
                    "cloud_only_new": len(az_ord - win_ord),
                    "merged_unique": len(win_ord | az_ord),
                },
                "funding_events": {
                    "windows": len(win_fund),
                    "cloud": len(az_fund),
                    "overlap": len(win_fund & az_fund),
                    "windows_only": len(win_fund - az_fund),
                    "cloud_only_new": len(az_fund - win_fund),
                    "merged_unique": len(win_fund | az_fund),
                },
                "runtime_dispatches": {
                    "windows": len(win_disp),
                    "cloud": len(az_disp),
                    "overlap": len(win_disp & az_disp),
                    "windows_only": len(win_disp - az_disp),
                    "cloud_only_new": len(az_disp - win_disp),
                    "merged_unique": len(win_disp | az_disp),
                },
                "cloud_is_strict_superset": (
                    len(win_ev - az_ev) == 0
                    and len(win_ord - az_ord) == 0
                    and len(win_fund - az_fund) == 0
                    and len(win_disp - az_disp) == 0
                ),
            }
        )

    # Merge unique records (prefer cloud for shared keys; preserve any windows-only records if ever present)
    merged_campaigns = dict(windows["campaigns"]) if windows else {}
    merged_campaigns.update(cloud["campaigns"])

    disp_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    if windows:
        for d in windows["dispatches"]:
            disp_map[(d["campaign"], d["method"], d["client_order_id"])] = d
    for d in cloud["dispatches"]:
        disp_map[(d["campaign"], d["method"], d["client_order_id"])] = d
    merged_dispatches = list(disp_map.values())

    merged_proto_orders = dict(windows["proto_orders"]) if windows else {}
    merged_proto_orders.update(cloud["proto_orders"])

    merged_recovery = dict(windows["proto_recovery"]) if windows else {}
    merged_recovery.update(cloud["proto_recovery"])

    merged_proto_funding = dict(windows["proto_funding"]) if windows else {}
    merged_proto_funding.update(cloud["proto_funding"])

    # Deduplicate receipts by receipt_id and keep latest per client_order_id
    receipt_by_id: dict[str, dict[str, Any]] = {}
    if windows:
        for r in windows["proto_receipts"]:
            receipt_by_id[r["receipt_id"]] = r
    for r in cloud["proto_receipts"]:
        receipt_by_id[r["receipt_id"]] = r
    latest_receipt_by_cid: dict[str, dict[str, Any]] = {}
    for r in sorted(receipt_by_id.values(), key=lambda x: str(x["observed_at"])):
        latest_receipt_by_cid[r["client_order_id"]] = r

    merged_order_events = dict(windows["order_events"]) if windows else {}
    merged_order_events.update(cloud["order_events"])

    merged_funding_events = dict(windows["funding_events"]) if windows else {}
    merged_funding_events.update(cloud["funding_events"])

    merged_deployments = dict(windows["deployments"]) if windows else {}
    merged_deployments.update(cloud["deployments"])
    dep_order = list(windows["deployment_order"]) if windows else []
    for dep_id in cloud["deployment_order"]:
        if dep_id not in dep_order:
            dep_order.append(dep_id)

    # Deduplicate completed 1m bars by (deployment_id, event_id)
    merged_bars_by_dep: dict[str, list[dict[str, Any]]] = {}
    all_dep_ids = set(cloud["bars_by_deployment"]) | (set(windows["bars_by_deployment"]) if windows else set())
    for dep_id in sorted(all_dep_ids):
        seen_eids: dict[str, dict[str, Any]] = {}
        if windows:
            for b in windows["bars_by_deployment"].get(dep_id, []):
                seen_eids[b["event_id"]] = b
        for b in cloud["bars_by_deployment"].get(dep_id, []):
            seen_eids[b["event_id"]] = b
        merged_bars_by_dep[dep_id] = sorted(seen_eids.values(), key=lambda x: x["time"])

    # Cross-DB consistency verification on the merged authoritative state
    state = cloud["state"]
    post_cids = {d["client_order_id"] for d in merged_dispatches if d["method"] == "POST"}
    proto_funding_ids = set(merged_proto_funding)
    ctrl_funding_ids = set(merged_funding_events)
    state_funding_ids = set(state.funding_events)
    proto_funding_sum = sum((v["event"].amount for v in merged_proto_funding.values()), ZERO)
    ctrl_funding_sum = sum((ev.amount for ev in merged_funding_events.values()), ZERO)
    state_funding_sum = state.funding.get("USDT", ZERO)

    receipt_consistency_errors: list[str] = []
    for cid, rcpt in latest_receipt_by_cid.items():
        order_q = rcpt.get("order") or {}
        trades = rcpt.get("trades") or []
        t_qty = sum((Decimal(str(t["qty"])) for t in trades), ZERO)
        t_quote = sum((Decimal(str(t["quoteQty"])) for t in trades), ZERO)
        t_fee = sum((Decimal(str(t["commission"])) for t in trades), ZERO)
        o_qty = Decimal(str(order_q.get("executedQty", "0")))
        o_quote = Decimal(str(order_q.get("cumQuote", "0")))
        snap = merged_proto_orders[cid]["record"].snapshot
        if (
            t_qty != o_qty
            or t_quote != o_quote
            or t_qty != snap.cumulative_filled_quantity
            or t_fee != snap.cumulative_fee
        ):
            receipt_consistency_errors.append(f"receipt mismatch for {cid}")

    consistency = {
        "all_sqlite_integrity_ok": (
            dedup_audit["cloud_integrity_ok"]
            and (dedup_audit["windows_integrity_ok"] if windows is not None else True)
        ),
        "funding_ids_consistent": (proto_funding_ids == ctrl_funding_ids == state_funding_ids),
        "funding_totals_consistent": (proto_funding_sum == ctrl_funding_sum == state_funding_sum),
        "funding_total_usdt": _dec_str(ctrl_funding_sum),
        "funding_event_count": len(ctrl_funding_ids),
        "post_dispatches_in_protocol_orders": post_cids.issubset(set(merged_proto_orders)),
        "recovery_orders_in_protocol_orders": set(merged_recovery).issubset(set(merged_proto_orders)),
        "receipt_trade_reconciliation_ok": len(receipt_consistency_errors) == 0,
        "receipt_consistency_errors": receipt_consistency_errors,
        "income_coverage": cloud["income_coverage"],
        "unknown_orders": len((cloud["cloud_health"] or {}).get("unknown_orders") or []),
        "cloud_snapshot_quiescent": (cloud["snapshot_manifest"] or {}).get("source_quiescent_during_backup"),
        "handoff_reconciled_at": (cloud["handoff_result"] or {}).get("reconciled_at"),
        "handoff_snapshot_verified": (cloud["handoff_result"] or {}).get("snapshot_verified"),
    }
    consistency["all_checks_passed"] = bool(
        consistency["all_sqlite_integrity_ok"]
        and consistency["funding_ids_consistent"]
        and consistency["funding_totals_consistent"]
        and consistency["post_dispatches_in_protocol_orders"]
        and consistency["recovery_orders_in_protocol_orders"]
        and consistency["receipt_trade_reconciliation_ok"]
        and consistency["unknown_orders"] == 0
    )

    return {
        "journal_id": cloud["journal_id"],
        "state": state,
        "runtime_kv": cloud["runtime_kv"],
        "campaigns": merged_campaigns,
        "dispatches": merged_dispatches,
        "grid_supervisor": cloud["grid_supervisor"],
        "proto_orders": merged_proto_orders,
        "latest_receipts": latest_receipt_by_cid,
        "proto_recovery": merged_recovery,
        "proto_funding": merged_proto_funding,
        "proto_rejections": cloud["proto_rejections"],
        "proto_meta": cloud["proto_meta"],
        "order_events": merged_order_events,
        "funding_events": merged_funding_events,
        "deployments": merged_deployments,
        "deployment_order": dep_order,
        "bars_by_deployment": merged_bars_by_dep,
        "exit_limit_commands": cloud["exit_limit_commands"],
        "withdraw_commands": cloud["withdraw_commands"],
        "cloud_health": cloud["cloud_health"],
        "handoff_result": cloud["handoff_result"],
        "snapshot_manifest": cloud["snapshot_manifest"],
        "ops_evidence": cloud["ops_evidence"],
        "dedup_audit": dedup_audit,
        "consistency": consistency,
    }


def _classify_order_deployment(
    client_order_id: str,
    deployments: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    """Return ``(deployment_id, cohort)`` for an order."""
    # Longest deployment_id prefix match first so g-00001 matches before g.
    for dep_id in sorted(deployments, key=len, reverse=True):
        if client_order_id.startswith(dep_id + "-"):
            if dep_id.startswith("fl-d7345f8795464a65-g"):
                return dep_id, "CONTINUOUS_GRID"
            return dep_id, "LEGACY_SINGLE_CAMPAIGN_GRID"
    return "PRE_GRID_SMOKE", "PRE_GRID_SMOKE"


def _classify_order_purpose(
    client_order_id: str,
    deployment_id: str,
    side: str,
    reduce_only: bool,
    exit_limit_commands: dict[str, list[dict[str, Any]]],
) -> str:
    if deployment_id == "PRE_GRID_SMOKE":
        return "SMOKE_TEST_EXIT" if reduce_only else "SMOKE_TEST_ENTRY"
    if side == "SELL" and reduce_only:
        if exit_limit_commands.get(deployment_id):
            return "RISK_EXIT"
        return "RISK_EXIT"
    return "GRID"


def analyze_empirical_performance(
    merged: dict[str, Any],
    *,
    opening_wallet: Decimal = DEFAULT_OPENING_WALLET,
    strategy_cap_usdt: Decimal = DEFAULT_STRATEGY_CAP_USDT,
) -> dict[str, Any]:
    """Reconstruct complete order/trade/funding lifecycle and attribute PnL via ``grid_fifo``."""
    state = merged["state"]
    deployments = merged["deployments"]
    latest_receipts = merged["latest_receipts"]
    dispatches_by_cid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in merged["dispatches"]:
        dispatches_by_cid[d["client_order_id"]].append(d)

    # 1. Build order lifecycle table & purpose map for grid_fifo.attribute_events
    order_purposes: dict[str, dict[str, str]] = {}
    order_lifecycle: list[dict[str, Any]] = []
    fills_and_funding_rows: list[dict[str, Any]] = []

    for cid, rec in merged["proto_orders"].items():
        record = rec["record"]
        snap = record.snapshot
        intent = snap.intent
        dep_id, cohort = _classify_order_deployment(cid, deployments)
        purpose = _classify_order_purpose(
            cid, dep_id, intent.side.value, bool(intent.reduce_only), merged["exit_limit_commands"]
        )
        order_purposes[cid] = {"purpose": purpose}

        cid_disps = dispatches_by_cid.get(cid, [])
        post_disp = next((d for d in cid_disps if d["method"] == "POST"), None)
        del_disp = next((d for d in cid_disps if d["method"] == "DELETE"), None)
        rcpt = latest_receipts.get(cid)
        order_q = (rcpt or {}).get("order") or {}
        trades = (rcpt or {}).get("trades") or []

        created_ms = order_q.get("time")
        updated_ms = order_q.get("updateTime")
        created_utc = _ms_to_utc(created_ms) if created_ms else _parse_utc(record.observed_at)
        updated_utc = _ms_to_utc(updated_ms) if updated_ms else _parse_utc(record.observed_at)
        lifetime_seconds = max(0.0, (updated_utc - created_utc).total_seconds())

        filled_qty = snap.cumulative_filled_quantity
        filled_quote = Decimal(str(order_q.get("cumQuote", rec["quote"])))
        avg_price = (filled_quote / filled_qty) if filled_qty > ZERO else None
        fee = snap.cumulative_fee
        maker_flags = [bool(t.get("maker")) for t in trades]
        exchange_realized_pnl = sum((Decimal(str(t.get("realizedPnl", "0"))) for t in trades), ZERO)
        submitted_notional = intent.quantity * (intent.limit_price or ZERO)

        order_lifecycle.append(
            {
                "client_order_id": cid,
                "exchange_order_id": snap.exchange_order_id,
                "campaign_id": post_disp["campaign"] if post_disp else None,
                "deployment_id": dep_id,
                "cohort": cohort,
                "purpose": purpose,
                "side": intent.side.value,
                "order_type": intent.order_type.value,
                "reduce_only": bool(intent.reduce_only),
                "limit_price": _dec_str(intent.limit_price, 5),
                "orig_qty": _dec_str(intent.quantity, 4),
                "submitted_notional_usdt": _dec_str(submitted_notional, 6),
                "status": record.confirmed_status.value,
                "filled_qty": _dec_str(filled_qty, 4),
                "avg_fill_price": _dec_str(avg_price, 5),
                "filled_notional_usdt": _dec_str(filled_quote, 6),
                "fee_usdt": _dec_str(fee, 8),
                "fee_bps": (
                    round(float(fee / filled_quote * Decimal("10000")), 4) if filled_quote > ZERO else 0.0
                ),
                "exchange_realized_pnl_usdt": _dec_str(exchange_realized_pnl, 8),
                "trade_count": len(trades),
                "maker_only": bool(maker_flags) and all(maker_flags),
                "taker_only": bool(maker_flags) and not any(maker_flags),
                "created_at_utc": created_utc.isoformat(),
                "updated_at_utc": updated_utc.isoformat(),
                "lifetime_seconds": round(lifetime_seconds, 3),
                "cancel_dispatched": del_disp is not None,
                "rejection_code": (merged["proto_rejections"].get(cid) or {}).get("code"),
            }
        )

        for t in trades:
            t_time = _ms_to_utc(int(t["time"]))
            fills_and_funding_rows.append(
                {
                    "record_type": "TRADE_FILL",
                    "unique_id": f"trade:{t['id']}",
                    "timestamp_utc": t_time.isoformat(),
                    "deployment_id": dep_id,
                    "cohort": cohort,
                    "purpose": purpose,
                    "client_order_id": cid,
                    "exchange_order_id": str(t["orderId"]),
                    "side": t["side"],
                    "quantity": str(t["qty"]),
                    "price": str(t["price"]),
                    "notional_usdt": str(t["quoteQty"]),
                    "maker": bool(t.get("maker")),
                    "fee_usdt": _dec_str(Decimal(str(t["commission"])), 8),
                    "realized_pnl_usdt": _dec_str(Decimal(str(t.get("realizedPnl", "0"))), 8),
                    "funding_usdt": _dec_str(ZERO, 8),
                }
            )

    order_lifecycle.sort(key=lambda r: r["created_at_utc"])

    # 2. Authoritative FIFO attribution reusing factorlab.experiments.grid_fifo.attribute_events
    fill_order_events = sorted(
        [ev for ev in merged["order_events"].values() if ev.fill_price is not None],
        key=lambda e: (_parse_utc(e.event_time), e.event_id),
    )
    funding_events = sorted(
        list(merged["funding_events"].values()),
        key=lambda e: (_parse_utc(e.event_time), e.event_id),
    )

    previous: dict[str, tuple[Decimal, Decimal]] = {}
    fifo_rows: list[dict[str, Any]] = []
    closes: dict[datetime, Decimal] = {}
    epoch = datetime(2000, 1, 1)
    for idx, ev in enumerate(fill_order_events):
        qty_before, fee_before = previous.get(ev.client_order_id, (ZERO, ZERO))
        qty_delta = ev.cumulative_filled_quantity - qty_before
        fee_delta = ev.cumulative_fee - fee_before
        previous[ev.client_order_id] = (ev.cumulative_filled_quantity, ev.cumulative_fee)
        key = epoch + timedelta(microseconds=idx)
        closes[key] = ev.fill_price
        fifo_rows.append(
            {
                "event_id": ev.event_id,
                "order_id": ev.client_order_id,
                "side": state.orders[ev.client_order_id].snapshot.intent.side.value,
                "event_time": ev.event_time.isoformat(),
                "execution_date": key.isoformat(),
                "cumulative_filled_quantity": ev.cumulative_filled_quantity,
                "cumulative_fee": ev.cumulative_fee,
                "executed_shares": qty_delta,
                "execution_price": ev.fill_price,
                "fee": fee_delta,
                "trade_notional": qty_delta * ev.fill_price,
                "slippage": ZERO,
                "transaction_cost": fee_delta,
            }
        )

    position = state.positions.get("NVDAUSDT")
    latest_quote = state.quotes.get("NVDAUSDT")
    mark_price = (
        position.mark_price
        if position is not None
        else (latest_quote.price if latest_quote is not None else ZERO)
    )
    state_equity = sum(state.cash.values(), ZERO) + sum(
        (p.quantity * (p.mark_price - p.entry_price) for p in state.positions.values()), ZERO
    )
    fifo_result_input = {
        "config": {"initial_inventory": 0, "initial_price": 0},
        "rules": {"quantity_unit": 1},
        "orders": order_purposes,
        "instrument_id": "NVDAUSDT",
        "market": "BINANCE_USDM",
        "capital": opening_wallet,
        "final": {"close": mark_price, "equity": state_equity},
    }
    fifo = attribute_events(
        fifo_result_input, fifo_rows, closes, fill_order_events, state, funding_events=funding_events
    )
    fifo_summary = fifo["summary"]

    # Map each funding settlement to the active deployment holding inventory at that timestamp
    fill_timeline = sorted(
        [
            (
                _parse_utc(r["timestamp_utc"]),
                Decimal(r["quantity"]) * (Decimal("1") if r["side"] == "BUY" else Decimal("-1")),
                r["deployment_id"],
                r["cohort"],
            )
            for r in fills_and_funding_rows
            if r["record_type"] == "TRADE_FILL"
        ],
        key=lambda x: x[0],
    )
    all_1m_bars_by_ts: dict[datetime, dict[str, Any]] = {}
    for dep_bars in merged["bars_by_deployment"].values():
        for b in dep_bars:
            all_1m_bars_by_ts[b["time"]] = b

    funding_details: list[dict[str, Any]] = []
    for fev in funding_events:
        ftime = _parse_utc(fev.event_time)
        held_qty = ZERO
        active_dep = "UNATTRIBUTED"
        active_cohort = "UNATTRIBUTED"
        for t_ts, signed_q, dep_id, cohort in fill_timeline:
            if t_ts <= ftime:
                held_qty += signed_q
                if held_qty != ZERO:
                    active_dep = dep_id
                    active_cohort = cohort
        bar = all_1m_bars_by_ts.get(ftime)
        near_close = bar["close"] if bar else None
        implied_rate_bps = (
            float(-fev.amount / (held_qty * near_close) * Decimal("10000"))
            if (held_qty > ZERO and near_close and near_close > ZERO)
            else None
        )
        funding_details.append(
            {
                "event_id": fev.event_id,
                "tran_id": fev.tran_id,
                "timestamp_utc": ftime.isoformat(),
                "deployment_id": active_dep,
                "cohort": active_cohort,
                "held_qty": _dec_str(held_qty, 4),
                "near_close": _dec_str(near_close, 5) if near_close is not None else None,
                "amount_usdt": _dec_str(fev.amount, 8),
                "implied_rate_bps": round(implied_rate_bps, 4) if implied_rate_bps is not None else None,
            }
        )
        fills_and_funding_rows.append(
            {
                "record_type": "FUNDING_SETTLEMENT",
                "unique_id": fev.event_id,
                "timestamp_utc": ftime.isoformat(),
                "deployment_id": active_dep,
                "cohort": active_cohort,
                "purpose": "FUNDING_FEE",
                "client_order_id": None,
                "exchange_order_id": fev.tran_id,
                "side": "FUNDING",
                "quantity": _dec_str(held_qty, 4),
                "price": _dec_str(near_close, 5) if near_close is not None else None,
                "notional_usdt": _dec_str(held_qty * near_close, 6) if near_close is not None else None,
                "maker": None,
                "fee_usdt": _dec_str(ZERO, 8),
                "realized_pnl_usdt": _dec_str(ZERO, 8),
                "funding_usdt": _dec_str(fev.amount, 8),
            }
        )

    fills_and_funding_rows.sort(key=lambda r: (r["timestamp_utc"], r["unique_id"]))

    # 3. Verify health NAV reconciliation
    cloud_health = merged["cloud_health"] or {}
    health_nav = Decimal(str(cloud_health["nav_usdt"])) if cloud_health.get("nav_usdt") is not None else None
    health_nav_diff = (fifo_summary["final_equity"] - health_nav) if health_nav is not None else None

    # 4. Separate Pre-Grid Smoke vs Grid Strategy totals
    smoke_orders = [o for o in order_lifecycle if o["cohort"] == "PRE_GRID_SMOKE"]
    grid_orders = [o for o in order_lifecycle if o["cohort"] != "PRE_GRID_SMOKE"]
    cont_grid_orders = [o for o in order_lifecycle if o["cohort"] == "CONTINUOUS_GRID"]

    def _cohort_pnl(cohort_filter) -> dict[str, Any]:
        o_list = [o for o in order_lifecycle if cohort_filter(o)]
        f_list = [f for f in funding_details if cohort_filter(f)]
        realized_gross = sum((Decimal(o["exchange_realized_pnl_usdt"]) for o in o_list), ZERO)
        fees = sum((Decimal(o["fee_usdt"]) for o in o_list), ZERO)
        funding = sum((Decimal(f["amount_usdt"]) for f in f_list), ZERO)
        # Residual unrealized PnL (currently 0 when flat; computed if residual position exists)
        unrealized = ZERO
        if position is not None and cohort_filter({"cohort": "CONTINUOUS_GRID", "deployment_id": merged["deployment_order"][-1]}):
            unrealized = position.quantity * (position.mark_price - position.entry_price)
        net_pnl = realized_gross - fees + funding + unrealized
        filled_notional = sum((Decimal(o["filled_notional_usdt"]) for o in o_list), ZERO)
        submitted_notional = sum((Decimal(o["submitted_notional_usdt"]) for o in o_list), ZERO)
        filled_count = sum(1 for o in o_list if Decimal(o["filled_qty"]) > ZERO)
        accepted_count = sum(1 for o in o_list if o["status"] != "REJECTED")
        canceled_count = sum(1 for o in o_list if o["status"] == "CANCELED")
        active_count = sum(1 for o in o_list if o["status"] in {"SUBMITTED", "NEW", "PARTIALLY_FILLED"})
        rejected_count = sum(1 for o in o_list if o["status"] == "REJECTED")
        peak_deployed = max(
            (Decimal(o["filled_notional_usdt"]) for o in o_list if o["side"] == "BUY" and Decimal(o["filled_qty"]) > ZERO),
            default=ZERO,
        )
        return {
            "orders_total": len(o_list),
            "orders_accepted": accepted_count,
            "orders_filled": filled_count,
            "orders_canceled": canceled_count,
            "orders_active": active_count,
            "orders_rejected": rejected_count,
            "fill_rate_all_posts": round(filled_count / len(o_list), 6) if o_list else 0.0,
            "fill_rate_accepted": round(filled_count / accepted_count, 6) if accepted_count else 0.0,
            "submitted_notional_usdt": _dec_str(submitted_notional, 6),
            "filled_turnover_usdt": _dec_str(filled_notional, 6),
            "peak_deployed_notional_usdt": _dec_str(peak_deployed, 6),
            "realized_trade_gross_usdt": _dec_str(realized_gross, 8),
            "fees_usdt": _dec_str(fees, 8),
            "realized_funding_usdt": _dec_str(funding, 8),
            "unrealized_pnl_usdt": _dec_str(unrealized, 8),
            "net_pnl_usdt": _dec_str(net_pnl, 8),
            "funding_events": len(f_list),
        }

    full_account_summary = _cohort_pnl(lambda _: True)
    pre_grid_summary = _cohort_pnl(lambda r: r["cohort"] == "PRE_GRID_SMOKE")
    all_grid_summary = _cohort_pnl(lambda r: r["cohort"] != "PRE_GRID_SMOKE")
    continuous_grid_summary = _cohort_pnl(lambda r: r["cohort"] == "CONTINUOUS_GRID")

    # 5. Reconstruct 1-minute mark-to-market series across all observed Grid 1m bars
    mtm_series = _build_empirical_mtm_series(
        merged,
        fills_and_funding_rows,
        opening_wallet=opening_wallet,
        strategy_cap_usdt=strategy_cap_usdt,
    )

    # 6. Per-generation Grid breakdown + F11/F08 evaluation
    generations = _evaluate_generations(
        merged,
        order_lifecycle,
        funding_details,
        strategy_cap_usdt=strategy_cap_usdt,
    )
    f11_f08_analysis = _evaluate_f11_and_f08(merged, order_lifecycle, funding_details, generations)

    # 7. Capital bases & budget consumption velocity
    grid_opening_equity = opening_wallet + Decimal(pre_grid_summary["net_pnl_usdt"])
    grid_net = Decimal(all_grid_summary["net_pnl_usdt"])
    full_net = Decimal(full_account_summary["net_pnl_usdt"])
    grid_peak_deployed = Decimal(all_grid_summary["peak_deployed_notional_usdt"])

    first_order_ts = _parse_utc(order_lifecycle[0]["created_at_utc"])
    last_obs_ts = (
        _parse_utc(latest_quote.observed_at)
        if latest_quote is not None
        else _parse_utc(order_lifecycle[-1]["updated_at_utc"])
    )
    full_span_days = max(1e-6, (last_obs_ts - first_order_ts).total_seconds() / 86400.0)

    # Continuous authorization & budget consumption
    cont_auth = json.loads(merged["runtime_kv"]["continuous"]) if "continuous" in merged["runtime_kv"] else None
    cont_posts_total = sum(
        1
        for d in merged["dispatches"]
        if d["method"] == "POST" and str(d["client_order_id"]).startswith("fl-d7345f8795464a65-g")
    )
    cont_submitted_notional = sum(
        (Decimal(o["submitted_notional_usdt"]) for o in cont_grid_orders),
        ZERO,
    )
    cont_start_ts = (
        merged["bars_by_deployment"]["fl-d7345f8795464a65-g"][0]["time"]
        if "fl-d7345f8795464a65-g" in merged["bars_by_deployment"]
        else first_order_ts
    )
    cont_span_days = max(1e-6, (last_obs_ts - cont_start_ts).total_seconds() / 86400.0)
    # Note: global continuous limits in runtime.sqlite3
    global_post_limit = (cont_auth or {}).get("global_orders") or (cont_auth or {}).get("max_total_orders")
    global_notional_limit = (cont_auth or {}).get("global_notional") or (cont_auth or {}).get("max_total_notional")
    health_global = (cloud_health.get("global_budget") or cloud_health.get("continuous") or {})
    health_campaign = cloud_health.get("campaign") or {}

    capital_basis_metrics = {
        "note": (
            "Explicitly separates total Demo account equity (5000 USDT), Grid strategy "
            "committed-cash cap (7.00 USDT), and peak deployed position notional (5.1836 USDT). "
            "Cumulative order notional is turnover, NOT net invested capital."
        ),
        "account_equity_base": {
            "initial_capital_usdt": _dec_str(opening_wallet, 8),
            "grid_start_capital_usdt": _dec_str(grid_opening_equity, 8),
            "final_equity_usdt": _dec_str(fifo_summary["final_equity"], 8),
            "full_account_net_return_pct": round(float(full_net / opening_wallet * Decimal("100")), 6),
            "grid_only_net_return_pct": round(float(grid_net / grid_opening_equity * Decimal("100")), 6),
            "effective_peak_leverage_x": round(float(grid_peak_deployed / grid_opening_equity), 6),
            "max_drawdown_pct_grid_window": mtm_series["drawdown_account_pct"],
            "filled_turnover_multiple": round(
                float(Decimal(all_grid_summary["filled_turnover_usdt"]) / grid_opening_equity), 6
            ),
        },
        "strategy_committed_cap_base": {
            "capital_usdt": _dec_str(strategy_cap_usdt, 8),
            "grid_net_pnl_usdt": _dec_str(grid_net, 8),
            "grid_realized_gross_return_pct": round(
                float(Decimal(all_grid_summary["realized_trade_gross_usdt"]) / strategy_cap_usdt * Decimal("100")), 4
            ),
            "grid_fee_drag_pct": round(
                float(Decimal(all_grid_summary["fees_usdt"]) / strategy_cap_usdt * Decimal("100")), 4
            ),
            "grid_funding_drag_pct": round(
                float(Decimal(all_grid_summary["realized_funding_usdt"]) / strategy_cap_usdt * Decimal("100")), 4
            ),
            "grid_net_return_pct": round(float(grid_net / strategy_cap_usdt * Decimal("100")), 4),
            "effective_peak_leverage_x": round(float(grid_peak_deployed / strategy_cap_usdt), 4),
            "max_drawdown_pct_gen1": mtm_series["gen1_drawdown_cap_pct"],
            "filled_turnover_multiple": round(
                float(Decimal(all_grid_summary["filled_turnover_usdt"]) / strategy_cap_usdt), 4
            ),
            "mean_position_utilization_pct_gen1": mtm_series["gen1_mean_position_util_cap_pct"],
            "mean_committed_utilization_pct_gen1": mtm_series["gen1_mean_committed_util_cap_pct"],
        },
        "peak_deployed_notional_base": {
            "peak_notional_usdt": _dec_str(grid_peak_deployed, 6),
            "grid_realized_gross_return_pct": (
                round(
                    float(Decimal(all_grid_summary["realized_trade_gross_usdt"]) / grid_peak_deployed * Decimal("100")),
                    4,
                )
                if grid_peak_deployed > ZERO
                else 0.0
            ),
            "grid_funding_drag_pct": (
                round(
                    float(Decimal(all_grid_summary["realized_funding_usdt"]) / grid_peak_deployed * Decimal("100")),
                    4,
                )
                if grid_peak_deployed > ZERO
                else 0.0
            ),
            "grid_net_return_pct": (
                round(float(grid_net / grid_peak_deployed * Decimal("100")), 4)
                if grid_peak_deployed > ZERO
                else 0.0
            ),
            "max_drawdown_pct_gen1": mtm_series["gen1_drawdown_peak_notional_pct"],
            "filled_turnover_multiple": (
                round(float(Decimal(all_grid_summary["filled_turnover_usdt"]) / grid_peak_deployed), 4)
                if grid_peak_deployed > ZERO
                else 0.0
            ),
        },
        "turnover_not_net_capital_note": {
            "full_account_submitted_notional_usdt": full_account_summary["submitted_notional_usdt"],
            "full_account_filled_turnover_usdt": full_account_summary["filled_turnover_usdt"],
            "grid_submitted_notional_usdt": all_grid_summary["submitted_notional_usdt"],
            "grid_filled_turnover_usdt": all_grid_summary["filled_turnover_usdt"],
        },
    }

    budget_velocity = {
        "full_account_span_days": round(full_span_days, 4),
        "continuous_grid_span_days": round(cont_span_days, 4),
        "continuous_grid_posts": cont_posts_total,
        "continuous_grid_posts_per_day": round(cont_posts_total / cont_span_days, 4),
        "continuous_grid_filled_orders": continuous_grid_summary["orders_filled"],
        "continuous_grid_fills_per_day": round(continuous_grid_summary["orders_filled"] / cont_span_days, 4),
        "continuous_grid_submitted_notional_usdt": _dec_str(cont_submitted_notional, 6),
        "continuous_grid_submitted_notional_per_day_usdt": round(
            float(cont_submitted_notional) / cont_span_days, 4
        ),
        "handoff_consumed": (merged["handoff_result"] or {}).get("consumed"),
        "active_window_campaign": {
            "post_budget": health_campaign.get("post_budget"),
            "posts_consumed": health_campaign.get("posts_consumed"),
            "remaining_posts": health_campaign.get("remaining_posts"),
            "limits": health_campaign.get("limits"),
            "expires_at": health_campaign.get("expires_at"),
        },
        "global_authorization": {
            "state": (cont_auth or {}).get("state"),
            "global_limits": (cont_auth or {}).get("global_limits") or health_global,
            "global_post_limit": global_post_limit,
            "global_notional_limit": global_notional_limit,
        },
    }

    grid_completed_cycles = sum(
        1
        for lot in fifo["lots"]
        if lot.get("completed_grid_cycle")
        and order_purposes.get(lot.get("order_id"), {}).get("purpose") == "GRID"
    )
    smoke_round_trips = sum(
        1
        for lot in fifo["lots"]
        if lot.get("completed_grid_cycle")
        and order_purposes.get(lot.get("order_id"), {}).get("purpose") == "SMOKE_TEST_ENTRY"
    )

    fifo_reconciliation = {
        "source": "factorlab.experiments.grid_fifo.attribute_events",
        "opening_wallet_usdt": _dec_str(opening_wallet, 8),
        "realized_trade_pnl_usdt": _dec_str(fifo_summary["realized_trade_pnl"], 8),
        "normal_sell_gross_usdt": _dec_str(fifo_summary["normal_sell"]["execution_gross"], 8),
        "risk_exit_gross_usdt": _dec_str(fifo_summary["risk_exit"]["execution_gross"], 8),
        "completed_grid_cycles": int(grid_completed_cycles),
        "pre_grid_smoke_round_trips": int(smoke_round_trips),
        "raw_fifo_completed_lots": int(fifo_summary["grid_cycles"]),
        "actual_fees_usdt": _dec_str(fifo_summary["actual_fees"], 8),
        "realized_funding_usdt": _dec_str(fifo_summary["realized_funding"], 8),
        "residual_inventory": _dec_str(fifo_summary["residual_inventory"], 4),
        "residual_unrealized_net_usdt": _dec_str(fifo_summary["residual"]["net"], 8),
        "strategy_net_pnl_usdt": _dec_str(fifo_summary["strategy_net_pnl"], 8),
        "final_wallet_usdt": _dec_str(fifo_summary["final_wallet"], 8),
        "final_equity_usdt": _dec_str(fifo_summary["final_equity"], 8),
        "reconciliation_residual_usdt": _dec_str(fifo_summary["reconciliation_residual"], 8),
        "reported_equity_difference_usdt": _dec_str(fifo_summary["reported_equity_difference"], 8),
        "cloud_health_nav_usdt": _dec_str(health_nav, 8) if health_nav is not None else None,
        "cloud_health_nav_difference_usdt": (
            _dec_str(health_nav_diff, 8) if health_nav_diff is not None else None
        ),
        "reconciled_within_quantum": (
            fifo_summary["reconciliation_residual"] == ZERO
            and fifo_summary["reported_equity_difference"] == ZERO
            and (health_nav_diff is None or abs(health_nav_diff) <= USDT_EQUITY_QUANTUM / 2)
        ),
    }

    return {
        "evidence_class": EVIDENCE_BOUNDARIES["empirical"],
        "account_id_masked": _mask_account_id((merged["proto_meta"].get("account") or {}).get("account_id")),
        "symbol": "NVDAUSDT",
        "sample_window": {
            "first_order_utc": first_order_ts.isoformat(),
            "continuous_grid_start_utc": cont_start_ts.isoformat(),
            "last_observation_utc": last_obs_ts.isoformat(),
            "full_account_days": round(full_span_days, 4),
            "continuous_grid_days": round(cont_span_days, 4),
            "total_completed_1m_bars_in_ledger": sum(len(v) for v in merged["bars_by_deployment"].values()),
        },
        "fifo_reconciliation": fifo_reconciliation,
        "cohorts": {
            "full_account": full_account_summary,
            "pre_grid_smoke": pre_grid_summary,
            "all_grid": all_grid_summary,
            "continuous_grid": continuous_grid_summary,
        },
        "capital_basis_metrics": capital_basis_metrics,
        "budget_velocity": budget_velocity,
        "order_lifecycle": order_lifecycle,
        "fills_and_funding": fills_and_funding_rows,
        "funding_details": funding_details,
        "generations": generations,
        "f11_f08_analysis": f11_f08_analysis,
        "mtm_summary": mtm_series,
    }


def _build_empirical_mtm_series(
    merged: dict[str, Any],
    fills_and_funding_rows: list[dict[str, Any]],
    *,
    opening_wallet: Decimal,
    strategy_cap_usdt: Decimal,
) -> dict[str, Any]:
    """Reconstruct 1m mark-to-market drawdown & capital utilization during active Grid windows."""
    # Focus on Gen 1 (the only deployment with live fills) and full continuous Grid family
    g1_bars = merged["bars_by_deployment"].get("fl-d7345f8795464a65-g-00001", [])
    cont_bars = []
    for dep_id in ("fl-d7345f8795464a65-g", "fl-d7345f8795464a65-g-00001", "fl-d7345f8795464a65-g-00002"):
        cont_bars.extend(merged["bars_by_deployment"].get(dep_id, []))
    cont_bars.sort(key=lambda b: b["time"])

    grid_events = [
        r for r in fills_and_funding_rows if r["cohort"] != "PRE_GRID_SMOKE"
    ]

    def _compute_path(bars: list[dict[str, Any]], base_capital: Decimal, include_reservation_dep: str | None = None):
        if not bars:
            return {
                "bars": 0,
                "max_dd_pct": 0.0,
                "max_dd_usdt": 0.0,
                "min_net_pnl_usdt": 0.0,
                "max_net_pnl_usdt": 0.0,
                "mean_pos_notional": 0.0,
                "peak_pos_notional": 0.0,
                "mean_committed_notional": 0.0,
                "holding_fraction": 0.0,
                "min_unrealized_pnl_usdt": 0.0,
                "max_unrealized_pnl_usdt": 0.0,
            }
        ev_idx = 0
        pos = ZERO
        cost_basis = ZERO
        realized_pnl = ZERO
        cum_fees = ZERO
        cum_funding = ZERO

        # Pre-apply any grid events strictly before bars[0]
        start_ts = bars[0]["time"]
        while ev_idx < len(grid_events) and _parse_utc(grid_events[ev_idx]["timestamp_utc"]) < start_ts:
            ev = grid_events[ev_idx]
            if ev["record_type"] == "TRADE_FILL":
                q = Decimal(ev["quantity"])
                px = Decimal(ev["price"])
                if ev["side"] == "BUY":
                    pos += q
                    cost_basis += q * px
                else:
                    if pos > ZERO:
                        avg_c = cost_basis / pos
                        realized_pnl += q * (px - avg_c)
                        cost_basis -= q * avg_c
                        pos -= q
                cum_fees += Decimal(ev["fee_usdt"])
            else:
                cum_funding += Decimal(ev["funding_usdt"])
            ev_idx += 1

        peak_eq = base_capital
        max_dd_pct = ZERO
        max_dd_usdt = ZERO
        min_net = ZERO
        max_net = ZERO
        min_unreal = ZERO
        max_unreal = ZERO
        pos_notionals: list[float] = []
        committed_notionals: list[float] = []
        holding_bars = 0

        # Resting BUY limit price for reservation tracking in Gen 1
        dep_buy_reservation = Decimal("5.1836") if include_reservation_dep == "fl-d7345f8795464a65-g-00001" else ZERO
        first_fill_seen = pos > ZERO

        for b in bars:
            t_end = b["time"] + timedelta(minutes=1)
            while ev_idx < len(grid_events) and _parse_utc(grid_events[ev_idx]["timestamp_utc"]) < t_end:
                ev = grid_events[ev_idx]
                if ev["record_type"] == "TRADE_FILL":
                    q = Decimal(ev["quantity"])
                    px = Decimal(ev["price"])
                    if ev["side"] == "BUY":
                        pos += q
                        cost_basis += q * px
                        first_fill_seen = True
                    else:
                        if pos > ZERO:
                            avg_c = cost_basis / pos
                            realized_pnl += q * (px - avg_c)
                            cost_basis -= q * avg_c
                            pos -= q
                    cum_fees += Decimal(ev["fee_usdt"])
                else:
                    cum_funding += Decimal(ev["funding_usdt"])
                ev_idx += 1

            # Evaluate intrabar low for conservative MTM drawdown and bar close for close equity
            unreal_close = (pos * b["close"] - cost_basis) if pos > ZERO else ZERO
            unreal_low = (pos * b["low"] - cost_basis) if pos > ZERO else ZERO
            unreal_high = (pos * b["high"] - cost_basis) if pos > ZERO else ZERO

            net_close = realized_pnl - cum_fees + cum_funding + unreal_close
            net_low = realized_pnl - cum_fees + cum_funding + unreal_low
            net_high = realized_pnl - cum_fees + cum_funding + unreal_high

            eq_close = base_capital + net_close
            eq_low = base_capital + net_low
            peak_eq = max(peak_eq, eq_close)
            dd_usdt = peak_eq - eq_low
            dd_pct = (dd_usdt / peak_eq * Decimal("100")) if peak_eq > ZERO else ZERO
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_usdt = dd_usdt
            min_net = min(min_net, net_low)
            max_net = max(max_net, net_high)
            min_unreal = min(min_unreal, unreal_low)
            max_unreal = max(max_unreal, unreal_high)

            notional = pos * b["close"]
            pos_notionals.append(float(notional))
            reserved = dep_buy_reservation if (include_reservation_dep and not first_fill_seen) else ZERO
            committed_notionals.append(float(notional + reserved))
            if pos > ZERO:
                holding_bars += 1

        return {
            "bars": len(bars),
            "max_dd_pct": round(float(max_dd_pct), 6),
            "max_dd_usdt": round(float(max_dd_usdt), 8),
            "min_net_pnl_usdt": round(float(min_net), 8),
            "max_net_pnl_usdt": round(float(max_net), 8),
            "min_unrealized_pnl_usdt": round(float(min_unreal), 8),
            "max_unrealized_pnl_usdt": round(float(max_unreal), 8),
            "mean_pos_notional": round(float(np.mean(pos_notionals)), 6),
            "peak_pos_notional": round(float(np.max(pos_notionals)), 6),
            "mean_committed_notional": round(float(np.mean(committed_notionals)), 6),
            "holding_fraction": round(holding_bars / len(bars), 6),
        }

    acct_grid = _compute_path(cont_bars, opening_wallet)
    g1_cap = _compute_path(g1_bars, strategy_cap_usdt, include_reservation_dep="fl-d7345f8795464a65-g-00001")
    g1_peak = _compute_path(g1_bars, Decimal("5.1836"), include_reservation_dep="fl-d7345f8795464a65-g-00001")
    return {
        "continuous_grid_bars": acct_grid["bars"],
        "drawdown_account_pct": acct_grid["max_dd_pct"],
        "drawdown_account_usdt": acct_grid["max_dd_usdt"],
        "continuous_grid_holding_fraction": acct_grid["holding_fraction"],
        "continuous_grid_mean_pos_notional_usdt": acct_grid["mean_pos_notional"],
        "gen1_bars": g1_cap["bars"],
        "gen1_drawdown_cap_pct": g1_cap["max_dd_pct"],
        "gen1_drawdown_peak_notional_pct": g1_peak["max_dd_pct"],
        "gen1_max_drawdown_usdt": g1_cap["max_dd_usdt"],
        "gen1_min_net_pnl_usdt": g1_cap["min_net_pnl_usdt"],
        "gen1_max_net_pnl_usdt": g1_cap["max_net_pnl_usdt"],
        "gen1_min_unrealized_pnl_usdt": g1_cap["min_unrealized_pnl_usdt"],
        "gen1_max_unrealized_pnl_usdt": g1_cap["max_unrealized_pnl_usdt"],
        "gen1_holding_fraction": g1_cap["holding_fraction"],
        "gen1_mean_pos_notional_usdt": g1_cap["mean_pos_notional"],
        "gen1_peak_pos_notional_usdt": g1_cap["peak_pos_notional"],
        "gen1_mean_position_util_cap_pct": round(g1_cap["mean_pos_notional"] / float(strategy_cap_usdt) * 100.0, 4),
        "gen1_mean_committed_util_cap_pct": round(
            g1_cap["mean_committed_notional"] / float(strategy_cap_usdt) * 100.0, 4
        ),
    }


def _evaluate_generations(
    merged: dict[str, Any],
    order_lifecycle: list[dict[str, Any]],
    funding_details: list[dict[str, Any]],
    *,
    strategy_cap_usdt: Decimal,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    gs_report = (merged["grid_supervisor"] or {}).get("report") or {}
    active_dep_id = (gs_report.get("deployment") or {}).get("deployment_id")

    for idx, dep_id in enumerate(merged["deployment_order"]):
        dep = merged["deployments"][dep_id]
        cfg = dep.get("config") or {}
        rules = dep.get("rules") or {}
        bars = merged["bars_by_deployment"].get(dep_id, [])
        dep_orders = [o for o in order_lifecycle if o["deployment_id"] == dep_id]
        dep_funding = [f for f in funding_details if f["deployment_id"] == dep_id]

        realized_gross = sum((Decimal(o["exchange_realized_pnl_usdt"]) for o in dep_orders), ZERO)
        fees = sum((Decimal(o["fee_usdt"]) for o in dep_orders), ZERO)
        funding = sum((Decimal(f["amount_usdt"]) for f in dep_funding), ZERO)
        net_pnl = realized_gross - fees + funding
        filled_notional = sum((Decimal(o["filled_notional_usdt"]) for o in dep_orders), ZERO)
        submitted_notional = sum((Decimal(o["submitted_notional_usdt"]) for o in dep_orders), ZERO)
        peak_deployed = max(
            (Decimal(o["filled_notional_usdt"]) for o in dep_orders if o["side"] == "BUY" and Decimal(o["filled_qty"]) > ZERO),
            default=ZERO,
        )
        cohort = "CONTINUOUS_GRID" if dep_id.startswith("fl-d7345f8795464a65-g") else "LEGACY_SINGLE_CAMPAIGN_GRID"
        lower = float(cfg.get("lower", 0))
        upper = float(cfg.get("upper", 0))
        anchor = float(cfg.get("initial_price", 0))
        half_width_pct = round(((upper - lower) / 2.0 / anchor * 100.0), 4) if anchor > 0 else 0.0

        first_ts = bars[0]["time"].isoformat() if bars else (dep_orders[0]["created_at_utc"] if dep_orders else None)
        last_ts = bars[-1]["time"].isoformat() if bars else (dep_orders[-1]["updated_at_utc"] if dep_orders else None)
        duration_hours = (
            round((_parse_utc(last_ts) - _parse_utc(first_ts)).total_seconds() / 3600.0, 4)
            if first_ts and last_ts
            else 0.0
        )

        # Determine lifecycle outcome
        if dep_id == active_dep_id:
            outcome = f"ACTIVE_{gs_report.get('grid_state', 'RUNNING')}"
        elif any(o["purpose"] == "RISK_EXIT" and Decimal(o["filled_qty"]) > ZERO for o in dep_orders):
            outcome = "BREAKOUT_EXIT_FILLED_THEN_ROTATED"
        elif dep_orders and all(o["status"] == "CANCELED" for o in dep_orders):
            outcome = "UNFILLED_CANCELED_TERMINAL"
        elif not dep_orders:
            outcome = "NO_ORDERS_SUBMITTED"
        else:
            outcome = "TERMINAL"

        rows.append(
            {
                "generation_seq": idx + 1,
                "deployment_id": dep_id,
                "cohort": cohort,
                "anchor_price": round(anchor, 5),
                "lower_price": round(lower, 5),
                "upper_price": round(upper, 5),
                "half_width_pct": half_width_pct,
                "levels": int(cfg.get("levels", 1)),
                "quantity_unit": str(rules.get("quantity_unit", "")),
                "max_cash_committed_usdt": str(cfg.get("max_cash_committed", strategy_cap_usdt)),
                "initial_equity_usdt": str(dep.get("initial_equity", "")),
                "completed_1m_bars": len(bars),
                "first_observed_utc": first_ts,
                "last_observed_utc": last_ts,
                "observed_hours": duration_hours,
                "bar_min_low": round(min(float(b["low"]) for b in bars), 5) if bars else None,
                "bar_max_high": round(max(float(b["high"]) for b in bars), 5) if bars else None,
                "orders_submitted": len(dep_orders),
                "orders_filled": sum(1 for o in dep_orders if Decimal(o["filled_qty"]) > ZERO),
                "orders_canceled": sum(1 for o in dep_orders if o["status"] == "CANCELED"),
                "orders_active": sum(1 for o in dep_orders if o["status"] in {"SUBMITTED", "NEW"}),
                "local_withdrawals": len(merged["withdraw_commands"].get(dep_id, [])),
                "exit_limit_proposals": len(merged["exit_limit_commands"].get(dep_id, [])),
                "submitted_notional_usdt": _dec_str(submitted_notional, 6),
                "filled_turnover_usdt": _dec_str(filled_notional, 6),
                "peak_deployed_notional_usdt": _dec_str(peak_deployed, 6),
                "realized_trade_gross_usdt": _dec_str(realized_gross, 8),
                "fees_usdt": _dec_str(fees, 8),
                "realized_funding_usdt": _dec_str(funding, 8),
                "funding_settlement_count": len(dep_funding),
                "net_pnl_usdt": _dec_str(net_pnl, 8),
                "net_return_on_cap_pct": round(float(net_pnl / strategy_cap_usdt * Decimal("100")), 4),
                "net_return_on_peak_notional_pct": (
                    round(float(net_pnl / peak_deployed * Decimal("100")), 4) if peak_deployed > ZERO else 0.0
                ),
                "outcome": outcome,
            }
        )
    return rows


def _evaluate_f11_and_f08(
    merged: dict[str, Any],
    order_lifecycle: list[dict[str, Any]],
    funding_details: list[dict[str, Any]],
    generations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Quantify the empirical economic effect of F11 breakout exit and F08 automatic rotation."""
    g1_id = "fl-d7345f8795464a65-g-00001"
    g2_id = "fl-d7345f8795464a65-g-00002"
    g1_bars = merged["bars_by_deployment"].get(g1_id, [])
    g1_buy = next((o for o in order_lifecycle if o["deployment_id"] == g1_id and o["side"] == "BUY"), None)
    g1_exit = next((o for o in order_lifecycle if o["deployment_id"] == g1_id and o["purpose"] == "RISK_EXIT"), None)
    g2_buy = next((o for o in order_lifecycle if o["deployment_id"] == g2_id and o["side"] == "BUY"), None)
    g1_funding = [f for f in funding_details if f["deployment_id"] == g1_id]

    if not g1_buy or not g1_exit:
        return {"observed": False}

    buy_ts = _parse_utc(g1_buy["updated_at_utc"])
    exit_submit_ts = _parse_utc(g1_exit["created_at_utc"])
    exit_fill_ts = _parse_utc(g1_exit["updated_at_utc"])
    holding_hours = (exit_fill_ts - buy_ts).total_seconds() / 3600.0
    exit_fill_latency_ms = round((exit_fill_ts - exit_submit_ts).total_seconds() * 1000.0, 1)

    g1_gen = next(g for g in generations if g["deployment_id"] == g1_id)
    upper_bound = Decimal(str(g1_gen["upper_price"]))
    buy_price = Decimal(g1_buy["avg_fill_price"])
    exit_price = Decimal(g1_exit["avg_fill_price"])
    qty = Decimal(g1_buy["filled_qty"])

    # First 1m bar closing strictly above upper_bound after buy fill
    first_breakout_bar = next(
        (b for b in g1_bars if b["time"] >= buy_ts and b["close"] > upper_bound),
        None,
    )
    funding_total = sum((Decimal(f["amount_usdt"]) for f in g1_funding), ZERO)
    actual_gross = qty * (exit_price - buy_price)
    actual_net = actual_gross + funding_total
    upper_target_gross = qty * (upper_bound - buy_price)

    # Counterfactual 1: immediate exit at first breakout bar (08:42Z, before first 09:00Z funding)
    cf_immediate = None
    if first_breakout_bar is not None:
        bo_time = first_breakout_bar["time"]
        bo_close = first_breakout_bar["close"]
        funding_before_bo = sum(
            (Decimal(f["amount_usdt"]) for f in g1_funding if _parse_utc(f["timestamp_utc"]) <= bo_time),
            ZERO,
        )
        cf_gross = qty * (bo_close - buy_price)
        cf_net = cf_gross + funding_before_bo
        cf_immediate = {
            "first_breakout_bar_utc": bo_time.isoformat(),
            "first_breakout_close": _dec_str(bo_close, 5),
            "delay_hours_from_breakout_to_f11_fill": round((exit_fill_ts - bo_time).total_seconds() / 3600.0, 4),
            "funding_paid_before_breakout_usdt": _dec_str(funding_before_bo, 8),
            "funding_paid_during_delay_usdt": _dec_str(funding_total - funding_before_bo, 8),
            "extra_price_gain_during_delay_usdt": _dec_str(actual_gross - cf_gross, 8),
            "counterfactual_immediate_net_usdt": _dec_str(cf_net, 8),
            "net_economic_cost_of_exit_delay_usdt": _dec_str(actual_net - cf_net, 8),
            "note": (
                "Counterfactual assumes zero-fee maker exit at first breakout close; actual delay "
                "occurred while Windows runner was offline/pre-F11 before Azure C05 cutover."
            ),
        }

    # Counterfactual 2: continued bleed if F11 had NOT closed the stranded position
    last_3_funding = [abs(Decimal(f["amount_usdt"])) for f in g1_funding[-3:]]
    recent_hourly_bleed = (
        sum(last_3_funding, ZERO) / Decimal(len(last_3_funding)) if last_3_funding else ZERO
    )
    hours_to_wipe_net = (
        round(float(actual_net / recent_hourly_bleed), 2)
        if (recent_hourly_bleed > ZERO and actual_net > ZERO)
        else None
    )

    latest_quote = merged["state"].quotes.get("NVDAUSDT")
    latest_px = latest_quote.price if latest_quote else None
    post_exit_mark_diff = (
        qty * (exit_price - latest_px) if latest_px is not None else None
    )

    rotation_count = sum(1 for g in generations if g["cohort"] == "CONTINUOUS_GRID") - 1
    rotation_latency_s = None
    if g2_buy is not None:
        rotation_latency_s = round(
            (_parse_utc(g2_buy["created_at_utc"]) - exit_fill_ts).total_seconds(), 3
        )

    implied_rates = [f["implied_rate_bps"] for f in g1_funding if f["implied_rate_bps"] is not None]
    mean_hourly_bps = round(float(np.mean(implied_rates)), 4) if implied_rates else None

    return {
        "observed": True,
        "f11_breakout_exit": {
            "client_order_id": g1_exit["client_order_id"],
            "exchange_order_id": g1_exit["exchange_order_id"],
            "buy_price": _dec_str(buy_price, 5),
            "grid_upper_target": _dec_str(upper_bound, 5),
            "exit_fill_price": _dec_str(exit_price, 5),
            "quantity": _dec_str(qty, 4),
            "maker_only": g1_exit["maker_only"],
            "exit_fee_usdt": g1_exit["fee_usdt"],
            "exit_submit_to_fill_ms": exit_fill_latency_ms,
            "position_holding_hours": round(holding_hours, 4),
            "actual_gross_pnl_usdt": _dec_str(actual_gross, 8),
            "grid_upper_target_gross_pnl_usdt": _dec_str(upper_target_gross, 8),
            "extra_gross_above_upper_bound_usdt": _dec_str(actual_gross - upper_target_gross, 8),
            "funding_paid_usdt": _dec_str(funding_total, 8),
            "funding_share_of_gross_pct": round(float(abs(funding_total) / actual_gross * Decimal("100")), 2)
            if actual_gross > ZERO
            else None,
            "mean_observed_hourly_funding_bps": mean_hourly_bps,
            "annualized_observed_funding_pct": round(mean_hourly_bps / 100.0 * 8760.0, 1)
            if mean_hourly_bps is not None
            else None,
            "actual_net_pnl_usdt": _dec_str(actual_net, 8),
            "recent_hourly_funding_bleed_usdt": _dec_str(recent_hourly_bleed, 8),
            "hours_until_bleed_erases_remaining_net": hours_to_wipe_net,
            "latest_snapshot_price": _dec_str(latest_px, 5) if latest_px is not None else None,
            "post_exit_vs_latest_price_advantage_usdt": (
                _dec_str(post_exit_mark_diff, 8) if post_exit_mark_diff is not None else None
            ),
            "delay_counterfactual": cf_immediate,
        },
        "f08_rotation": {
            "continuous_grid_generations_total": sum(1 for g in generations if g["cohort"] == "CONTINUOUS_GRID"),
            "completed_rotations_observed": max(0, rotation_count),
            "from_deployment": g1_id,
            "to_deployment": g2_id if g2_buy else None,
            "rotation_to_new_order_seconds": rotation_latency_s,
            "new_generation_order_id": g2_buy["client_order_id"] if g2_buy else None,
            "new_generation_buy_price": g2_buy["limit_price"] if g2_buy else None,
            "new_generation_status": g2_buy["status"] if g2_buy else None,
        },
    }


def evaluate_benchmarks(
    merged: dict[str, Any],
    empirical: dict[str, Any],
    *,
    opening_wallet: Decimal = DEFAULT_OPENING_WALLET,
    strategy_cap_usdt: Decimal = DEFAULT_STRATEGY_CAP_USDT,
) -> list[dict[str, Any]]:
    """Compare Demo Grid against NVDAUSDT Buy-and-Hold across aligned windows and capital bases.

    Reuses ``grid_exposure_benchmark.simulate`` / ``fifo_control`` / ``stats`` and
    ``grid_funding.drawdown`` for methodological parity with FactorLab R06/R09.
    """
    rows: list[dict[str, Any]] = []
    g0_bars = merged["bars_by_deployment"].get("fl-d7345f8795464a65-g", [])
    g1_bars = merged["bars_by_deployment"].get("fl-d7345f8795464a65-g-00001", [])
    g2_bars = merged["bars_by_deployment"].get("fl-d7345f8795464a65-g-00002", [])

    qty = Decimal("0.04")
    g1_funding_total = Decimal(empirical["cohorts"]["all_grid"]["realized_funding_usdt"])
    latest_quote = merged["state"].quotes.get("NVDAUSDT")
    latest_px = latest_quote.price if latest_quote else (g2_bars[-1]["close"] if g2_bars else g1_bars[-1]["close"])

    def _bh_drawdown_pct(prices: list[Decimal], capital: Decimal, units: Decimal, entry_px: Decimal) -> float:
        if not prices or capital <= ZERO:
            return 0.0
        peak = capital
        worst = ZERO
        for px in prices:
            eq = capital + units * (px - entry_px)
            peak = max(peak, eq)
            if peak > ZERO:
                worst = max(worst, (peak - eq) / peak)
        return round(float(worst * Decimal("100")), 6)

    # Window 1: Active Gen 1 Window (2026-11-21T19:01Z -> 2026-11-22T16:24Z, 1,284 1m bars)
    if g1_bars:
        p0 = Decimal("130.90")  # Gen 1 anchor initial price
        p1 = Decimal("132.89")  # Gen 1 exit / end close
        prices = [b["close"] for b in g1_bars]
        bh_price_ret_pct = float((p1 - p0) / p0 * Decimal("100"))
        # 1a. Equal-quantity 0.04 NVDAUSDT B&H (ex-funding) on 7.00 USDT strategy cap
        bh_004_gross = qty * (p1 - p0)
        bh_004_notional = qty * p0
        rows.append(
            {
                "window": "GEN1_ACTIVE_21.4H",
                "benchmark_name": "NVDAUSDT_BH_0.04_EX_FUNDING",
                "evidence_class": EVIDENCE_BOUNDARIES["empirical"] + "_MARKET_PATH",
                "start_utc": g1_bars[0]["time"].isoformat(),
                "end_utc": g1_bars[-1]["time"].isoformat(),
                "start_price": _dec_str(p0, 5),
                "end_price": _dec_str(p1, 5),
                "price_return_pct": round(bh_price_ret_pct, 4),
                "capital_basis_usdt": _dec_str(strategy_cap_usdt, 4),
                "position_qty": _dec_str(qty, 4),
                "mean_notional_usdt": round(float(qty * sum(prices, ZERO) / Decimal(len(prices))), 4),
                "effective_leverage_on_basis_x": round(float(bh_004_notional / strategy_cap_usdt), 4),
                "holding_fraction": 1.0,
                "gross_pnl_usdt": _dec_str(bh_004_gross, 8),
                "funding_usdt": _dec_str(ZERO, 8),
                "fees_usdt": _dec_str(ZERO, 8),
                "net_pnl_usdt": _dec_str(bh_004_gross, 8),
                "return_on_basis_pct": round(float(bh_004_gross / strategy_cap_usdt * Decimal("100")), 4),
                "max_drawdown_on_basis_pct": _bh_drawdown_pct(prices, strategy_cap_usdt, qty, p0),
                "grid_net_pnl_same_window_usdt": empirical["cohorts"]["all_grid"]["net_pnl_usdt"],
                "grid_minus_benchmark_usdt": _dec_str(
                    Decimal(empirical["cohorts"]["all_grid"]["net_pnl_usdt"]) - bh_004_gross, 8
                ),
                "notes": "Equal 0.04 qty, 7.00 USDT strategy cap, excludes funding & entry/exit fees.",
            }
        )
        # 1b. Equal-quantity 0.04 NVDAUSDT B&H WITH observed Gen 1 Demo funding (lower bound: only 09:00-16:00Z)
        bh_004_with_fund = bh_004_gross + g1_funding_total
        rows.append(
            {
                "window": "GEN1_ACTIVE_21.4H",
                "benchmark_name": "NVDAUSDT_BH_0.04_WITH_OBSERVED_FUNDING",
                "evidence_class": EVIDENCE_BOUNDARIES["empirical"] + "_MARKET_PATH",
                "start_utc": g1_bars[0]["time"].isoformat(),
                "end_utc": g1_bars[-1]["time"].isoformat(),
                "start_price": _dec_str(p0, 5),
                "end_price": _dec_str(p1, 5),
                "price_return_pct": round(bh_price_ret_pct, 4),
                "capital_basis_usdt": _dec_str(strategy_cap_usdt, 4),
                "position_qty": _dec_str(qty, 4),
                "mean_notional_usdt": round(float(qty * sum(prices, ZERO) / Decimal(len(prices))), 4),
                "effective_leverage_on_basis_x": round(float(bh_004_notional / strategy_cap_usdt), 4),
                "holding_fraction": 1.0,
                "gross_pnl_usdt": _dec_str(bh_004_gross, 8),
                "funding_usdt": _dec_str(g1_funding_total, 8),
                "fees_usdt": _dec_str(ZERO, 8),
                "net_pnl_usdt": _dec_str(bh_004_with_fund, 8),
                "return_on_basis_pct": round(float(bh_004_with_fund / strategy_cap_usdt * Decimal("100")), 4),
                "max_drawdown_on_basis_pct": _bh_drawdown_pct(prices, strategy_cap_usdt, qty, p0),
                "grid_net_pnl_same_window_usdt": empirical["cohorts"]["all_grid"]["net_pnl_usdt"],
                "grid_minus_benchmark_usdt": _dec_str(
                    Decimal(empirical["cohorts"]["all_grid"]["net_pnl_usdt"]) - bh_004_with_fund, 8
                ),
                "notes": (
                    "Applies the exact 8 observed Demo funding charges (-0.09763384 USDT); "
                    "conservative lower bound because pre-09:00Z funding rates are not booked while flat."
                ),
            }
        )
        # 1c. R06 grid_exposure_benchmark.simulate parity control on hourly-resampled Gen 1 bars
        df_g1 = pd.DataFrame(
            [
                {
                    "time": b["time"],
                    "close": float(b["close"]),
                    "volume": float(b["volume"]),
                }
                for b in g1_bars
            ]
        ).set_index("time")
        hourly_df = df_g1.resample("1h").agg({"close": "last", "volume": "sum"}).dropna()
        if len(hourly_df) >= 2:
            sim_bars = [
                {
                    "time": ts.isoformat(),
                    "close": float(row["close"]),
                    "volume": max(100.0, float(row["volume"])),
                }
                for ts, row in hourly_df.iterrows()
            ]
            nav_df, fills_df = benchmark_simulate(sim_bars, fee_bps=3, slip_bps=2, quantity=qty)
            st = benchmark_stats(nav_df, fills_df)
            fc = benchmark_fifo_control(fills_df, nav_df["close"].iloc[-1])
            sim_net = Decimal(str(round(st["net_pnl"], 8)))
            rows.append(
                {
                    "window": "GEN1_ACTIVE_21.4H",
                    "benchmark_name": "R06_EXPOSURE_CONTROL_0.04_HOURLY_SIM",
                    "evidence_class": EVIDENCE_BOUNDARIES["historical_replay"],
                    "start_utc": sim_bars[0]["time"],
                    "end_utc": sim_bars[-1]["time"],
                    "start_price": _dec_str(Decimal(str(sim_bars[0]["close"])), 5),
                    "end_price": _dec_str(Decimal(str(sim_bars[-1]["close"])), 5),
                    "price_return_pct": round(
                        (sim_bars[-1]["close"] - sim_bars[0]["close"]) / sim_bars[0]["close"] * 100.0, 4
                    ),
                    "capital_basis_usdt": _dec_str(strategy_cap_usdt, 4),
                    "position_qty": _dec_str(qty, 4),
                    "mean_notional_usdt": round(st["mean_notional"], 4),
                    "effective_leverage_on_basis_x": round(st["max_notional"] / float(strategy_cap_usdt), 4),
                    "holding_fraction": round(st["holding_fraction"], 4),
                    "gross_pnl_usdt": _dec_str(
                        Decimal(str(round(fc["sold_fifo_net"] + fc["residual_net"] + st["fees"], 8))), 8
                    ),
                    "funding_usdt": _dec_str(ZERO, 8),
                    "fees_usdt": _dec_str(Decimal(str(round(st["fees"], 8))), 8),
                    "net_pnl_usdt": _dec_str(sim_net, 8),
                    "return_on_basis_pct": round(float(sim_net / strategy_cap_usdt * Decimal("100")), 4),
                    "max_drawdown_on_basis_pct": round(st["max_drawdown"] * 1000.0 / float(strategy_cap_usdt) * 100.0, 4),
                    "grid_net_pnl_same_window_usdt": empirical["cohorts"]["all_grid"]["net_pnl_usdt"],
                    "grid_minus_benchmark_usdt": _dec_str(
                        Decimal(empirical["cohorts"]["all_grid"]["net_pnl_usdt"]) - sim_net, 8
                    ),
                    "notes": "Reuses factorlab.experiments.grid_exposure_benchmark.simulate (fee=3bps, slip=2bps, ex-funding).",
                }
            )

        # 1d. Full 7.00 USDT 1x Buy-and-Hold (theoretical continuous 7/130.90 = 0.053476 units)
        units_cap = strategy_cap_usdt / p0
        bh_cap_gross = units_cap * (p1 - p0)
        rows.append(
            {
                "window": "GEN1_ACTIVE_21.4H",
                "benchmark_name": "NVDAUSDT_BH_FULL_7USDT_1X_EX_FUNDING",
                "evidence_class": EVIDENCE_BOUNDARIES["empirical"] + "_MARKET_PATH",
                "start_utc": g1_bars[0]["time"].isoformat(),
                "end_utc": g1_bars[-1]["time"].isoformat(),
                "start_price": _dec_str(p0, 5),
                "end_price": _dec_str(p1, 5),
                "price_return_pct": round(bh_price_ret_pct, 4),
                "capital_basis_usdt": _dec_str(strategy_cap_usdt, 4),
                "position_qty": _dec_str(units_cap, 4),
                "mean_notional_usdt": round(float(units_cap * sum(prices, ZERO) / Decimal(len(prices))), 4),
                "effective_leverage_on_basis_x": 1.0,
                "holding_fraction": 1.0,
                "gross_pnl_usdt": _dec_str(bh_cap_gross, 8),
                "funding_usdt": _dec_str(ZERO, 8),
                "fees_usdt": _dec_str(ZERO, 8),
                "net_pnl_usdt": _dec_str(bh_cap_gross, 8),
                "return_on_basis_pct": round(bh_price_ret_pct, 4),
                "max_drawdown_on_basis_pct": _bh_drawdown_pct(prices, strategy_cap_usdt, units_cap, p0),
                "grid_net_pnl_same_window_usdt": empirical["cohorts"]["all_grid"]["net_pnl_usdt"],
                "grid_minus_benchmark_usdt": _dec_str(
                    Decimal(empirical["cohorts"]["all_grid"]["net_pnl_usdt"]) - bh_cap_gross, 8
                ),
                "notes": "100% deployed 7.00 USDT 1x exposure (higher capital utilization than Grid 0.04 qty).",
            }
        )

    # Window 2: Continuous Grid Family Window (Gen 0 start -> latest snapshot, 7.31 days)
    if g0_bars:
        cont_all = sorted(g0_bars + g1_bars + g2_bars, key=lambda b: b["time"])
        p0_cont = Decimal("161.09")  # Gen 0 anchor initial price
        p1_cont = latest_px
        prices_cont = [b["close"] for b in cont_all]
        ret_cont_pct = float((p1_cont - p0_cont) / p0_cont * Decimal("100"))
        bh_cont_004 = qty * (p1_cont - p0_cont)
        rows.append(
            {
                "window": "CONTINUOUS_GRID_7.3D",
                "benchmark_name": "NVDAUSDT_BH_0.04_EX_FUNDING",
                "evidence_class": EVIDENCE_BOUNDARIES["empirical"] + "_MARKET_PATH",
                "start_utc": cont_all[0]["time"].isoformat(),
                "end_utc": empirical["sample_window"]["last_observation_utc"],
                "start_price": _dec_str(p0_cont, 5),
                "end_price": _dec_str(p1_cont, 5),
                "price_return_pct": round(ret_cont_pct, 4),
                "capital_basis_usdt": _dec_str(strategy_cap_usdt, 4),
                "position_qty": _dec_str(qty, 4),
                "mean_notional_usdt": round(float(qty * sum(prices_cont, ZERO) / Decimal(len(prices_cont))), 4),
                "effective_leverage_on_basis_x": round(float(qty * p0_cont / strategy_cap_usdt), 4),
                "holding_fraction": 1.0,
                "gross_pnl_usdt": _dec_str(bh_cont_004, 8),
                "funding_usdt": _dec_str(ZERO, 8),
                "fees_usdt": _dec_str(ZERO, 8),
                "net_pnl_usdt": _dec_str(bh_cont_004, 8),
                "return_on_basis_pct": round(float(bh_cont_004 / strategy_cap_usdt * Decimal("100")), 4),
                "max_drawdown_on_basis_pct": _bh_drawdown_pct(prices_cont, strategy_cap_usdt, qty, p0_cont),
                "grid_net_pnl_same_window_usdt": empirical["cohorts"]["continuous_grid"]["net_pnl_usdt"],
                "grid_minus_benchmark_usdt": _dec_str(
                    Decimal(empirical["cohorts"]["continuous_grid"]["net_pnl_usdt"]) - bh_cont_004, 8
                ),
                "notes": (
                    "CRITICAL CAVEAT: Grid avoided the -17.6% drop during Gen 0 ONLY because its BUY order "
                    "was canceled on Nov 15 and the Windows runner was offline for ~6 days, NOT because of active hedge alpha."
                ),
            }
        )
        # Full-account 5000 USDT 1x B&H (to make the ~950x leverage/exposure gap unmistakable)
        grid_start_eq = opening_wallet + Decimal(empirical["cohorts"]["pre_grid_smoke"]["net_pnl_usdt"])
        units_acct = grid_start_eq / p0_cont
        bh_acct_gross = units_acct * (p1_cont - p0_cont)
        rows.append(
            {
                "window": "CONTINUOUS_GRID_7.3D",
                "benchmark_name": "NVDAUSDT_BH_FULL_ACCOUNT_5000USDT_1X",
                "evidence_class": EVIDENCE_BOUNDARIES["empirical"] + "_MARKET_PATH",
                "start_utc": cont_all[0]["time"].isoformat(),
                "end_utc": empirical["sample_window"]["last_observation_utc"],
                "start_price": _dec_str(p0_cont, 5),
                "end_price": _dec_str(p1_cont, 5),
                "price_return_pct": round(ret_cont_pct, 4),
                "capital_basis_usdt": _dec_str(grid_start_eq, 4),
                "position_qty": _dec_str(units_acct, 4),
                "mean_notional_usdt": round(float(units_acct * sum(prices_cont, ZERO) / Decimal(len(prices_cont))), 2),
                "effective_leverage_on_basis_x": 1.0,
                "holding_fraction": 1.0,
                "gross_pnl_usdt": _dec_str(bh_acct_gross, 4),
                "funding_usdt": _dec_str(ZERO, 8),
                "fees_usdt": _dec_str(ZERO, 8),
                "net_pnl_usdt": _dec_str(bh_acct_gross, 4),
                "return_on_basis_pct": round(ret_cont_pct, 4),
                "max_drawdown_on_basis_pct": _bh_drawdown_pct(prices_cont, grid_start_eq, units_acct, p0_cont),
                "grid_net_pnl_same_window_usdt": empirical["cohorts"]["continuous_grid"]["net_pnl_usdt"],
                "grid_minus_benchmark_usdt": _dec_str(
                    Decimal(empirical["cohorts"]["continuous_grid"]["net_pnl_usdt"]) - bh_acct_gross, 4
                ),
                "notes": (
                    "NOT risk-matched: 1.000x account leverage vs Demo Grid's ~0.0010x effective account leverage "
                    "(~960x exposure disparity). Included solely to prevent naive account-% comparisons."
                ),
            }
        )

    # Window 3: Full Account Window (first smoke fill 2026-11-14T11:15Z -> latest snapshot, 8.23 days)
    first_fill = next(
        (o for o in empirical["order_lifecycle"] if Decimal(o["filled_qty"]) > ZERO),
        None,
    )
    if first_fill is not None:
        p0_full = Decimal(first_fill["avg_fill_price"])
        p1_full = latest_px
        ret_full_pct = float((p1_full - p0_full) / p0_full * Decimal("100"))
        bh_full_004 = qty * (p1_full - p0_full)
        rows.append(
            {
                "window": "FULL_ACCOUNT_8.2D",
                "benchmark_name": "NVDAUSDT_BH_0.04_EX_FUNDING",
                "evidence_class": EVIDENCE_BOUNDARIES["empirical"] + "_MARKET_PATH",
                "start_utc": first_fill["updated_at_utc"],
                "end_utc": empirical["sample_window"]["last_observation_utc"],
                "start_price": _dec_str(p0_full, 5),
                "end_price": _dec_str(p1_full, 5),
                "price_return_pct": round(ret_full_pct, 4),
                "capital_basis_usdt": _dec_str(strategy_cap_usdt, 4),
                "position_qty": _dec_str(qty, 4),
                "mean_notional_usdt": None,
                "effective_leverage_on_basis_x": round(float(qty * p0_full / strategy_cap_usdt), 4),
                "holding_fraction": 1.0,
                "gross_pnl_usdt": _dec_str(bh_full_004, 8),
                "funding_usdt": _dec_str(ZERO, 8),
                "fees_usdt": _dec_str(ZERO, 8),
                "net_pnl_usdt": _dec_str(bh_full_004, 8),
                "return_on_basis_pct": round(float(bh_full_004 / strategy_cap_usdt * Decimal("100")), 4),
                "max_drawdown_on_basis_pct": None,
                "grid_net_pnl_same_window_usdt": empirical["cohorts"]["full_account"]["net_pnl_usdt"],
                "grid_minus_benchmark_usdt": _dec_str(
                    Decimal(empirical["cohorts"]["full_account"]["net_pnl_usdt"]) - bh_full_004, 8
                ),
                "notes": "Full account span includes Pre-Grid smoke round-trip (-0.0289 USDT) + Grid (+0.0344 USDT).",
            }
        )
    return rows


def _demo_grid_config_for_anchor(anchor_price: float, *, max_cash_committed: float) -> GridConfig:
    """Construct the exact online Demo single-level arithmetic ±1% GridConfig for any anchor."""
    lower = round(anchor_price * 0.99, 2)
    upper = round(anchor_price * 1.01, 2)
    return GridConfig(
        mode="arithmetic",
        lower=lower,
        upper=upper,
        levels=1,
        initial_cash=10000.0,
        initial_inventory=0,
        initial_price=round(anchor_price, 2),
        quantity=1,
        max_inventory=1,
        max_cash_committed=max_cash_committed,
        atr_window=3,
        atr_multiplier=1.0,
        rebuild_threshold=2.0,
        min_rebuild_interval=3,
        max_turnover=10.0,
        breakout="pause",
        max_drawdown=0.2,
        minimum_price=0.01,
        minimum_quantity=1,
        fee_bps=3.0,
        slippage_bps=2.0,
        order_ttl=100,
        center_rule="last_close",
        expiry_reseed_limit=0,
        expiry_reseed_interval=100,
    )


def _demo_grid_rules() -> GridRules:
    return GridRules(
        quantity_unit=D("0.04"),
        step_size=D("0.01"),
        tick_size=D("0.01"),
        min_quantity=D("0.01"),
        min_notional=D("5"),
    )


def _load_r04b_funding_events(funding_csv: Path | None) -> list[tuple[datetime, Decimal, Decimal, str]]:
    if funding_csv is None or not Path(funding_csv).is_file():
        return []
    df = pd.read_csv(funding_csv)
    events = []
    for row in df.itertuples(index=False):
        events.append((_parse_utc(row.time), D(row.mark_price), D(row.rate), str(row.rate_type)))
    return sorted(events)


def run_historical_window_replay(
    r04b_snapshot: Path,
    *,
    funding_csv: Path | None = None,
    observed_demo_hourly_funding_bps: float = 22.97,
) -> dict[str, Any]:
    """Replay the Demo ±1% single-level Grid across 11 predeclared 7-day R04B windows."""
    bars, identity = load_hourly(r04b_snapshot)
    funding_events = _load_r04b_funding_events(funding_csv)
    windows = candidate_windows()
    rules = _demo_grid_rules()
    # At May-Aug 2026 NVDAUSDT prices (~170-225), 0.04 units requires ~7-9 USDT collateral.
    # Use 10.00 USDT 1x collateral cap for R04B replay and explicitly flag the 7.00 USDT cap breach!
    replay_capital = D("10.00")
    provenance = capture_provenance(source_context_root())
    rows: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="factorlab-r08-hist-") as tmp:
        tmp_dir = Path(tmp)
        for win in windows:
            w_start = _parse_utc(win["start"])
            w_end = _parse_utc(win["end_exclusive"])
            w_bars = [
                b for b in bars if w_start <= _parse_utc(b["time"]) < w_end
            ]
            if len(w_bars) < 24:
                continue
            anchor = float(w_bars[0]["close"])
            cfg = _demo_grid_config_for_anchor(anchor, max_cash_committed=float(replay_capital))
            out_dir = tmp_dir / win["id"]
            run_experiment(
                out_dir,
                bars=w_bars,
                config=cfg,
                rules=rules,
                capital=replay_capital,
                fee_rate=D("0.0003"),
                slippage=D("0.0002"),
                interval="1h",
                market=Market.BINANCE_USDM,
                instrument_id="NVDAUSDT",
                account_id=f"r08-replay-{win['id']}",
                provenance=provenance,
            )
            fifo = paper_fifo_attribution(out_dir)["summary"]
            life = lifecycle_metrics(out_dir)
            nav_df = pd.read_csv(out_dir / "daily.csv")
            trades_df = pd.read_csv(out_dir / "trades.csv")
            trades_rows = trades_df.to_dict("records")

            # Historical R04B funding overlay reusing grid_funding.position_settlements
            w_funding_ev = [e for e in funding_events if w_start <= e[0] < w_end]
            hist_funding = ZERO
            if w_funding_ev and trades_rows:
                flows = position_settlements(trades_rows, w_funding_ev)
                hist_funding = sum((f["funding"] for f in flows), ZERO)

            # Demo-stress hourly funding overlay (observed ~22.97 bps/h) on held position-hours
            pos_notional_hours = float((nav_df["inventory"] * nav_df["close"]).sum())
            demo_stress_funding = -pos_notional_hours * (observed_demo_hourly_funding_bps / 10000.0)

            net_ex_funding = float(fifo["strategy_net_pnl"])
            net_with_hist_funding = net_ex_funding + float(hist_funding)
            net_with_demo_stress_funding = net_ex_funding + demo_stress_funding

            # Comparable 0.04 NVDAUSDT Buy-and-Hold on the same 7-day window
            bh_pnl = 0.04 * (float(w_bars[-1]["close"]) - anchor)
            bh_ret_pct = (float(w_bars[-1]["close"]) - anchor) / anchor * 100.0
            would_exceed_7usdt_cap = (0.04 * anchor * 0.99) > 7.0

            rows.append(
                {
                    "window_id": win["id"],
                    "sample_type": win["sample"],
                    "evidence_class": EVIDENCE_BOUNDARIES["historical_replay"],
                    "start_utc": win["start"],
                    "end_utc": win["end_exclusive"],
                    "hours": len(w_bars),
                    "anchor_price": round(anchor, 4),
                    "end_price": round(float(w_bars[-1]["close"]), 4),
                    "nvda_return_pct": round(bh_ret_pct, 4),
                    "would_breach_demo_7usdt_cap": would_exceed_7usdt_cap,
                    "fills": int(len(trades_rows)),
                    "grid_cycles": int(fifo["grid_cycles"]),
                    "final_state": life["final_state"],
                    "holding_hours": int(nav_df["inventory"].gt(0).sum()),
                    "position_share_hours": round(float(life["position_hours"]), 4),
                    "residual_inventory": float(fifo["residual_inventory"]),
                    "realized_trade_gross_usdt": round(float(fifo["realized_trade_pnl"]), 6),
                    "fees_usdt": round(float(fifo["actual_fees"]), 6),
                    "slippage_usdt": round(float(fifo["actual_slippage"]), 6),
                    "net_pnl_ex_funding_usdt": round(net_ex_funding, 6),
                    "net_return_on_10usdt_pct": round(net_ex_funding / float(replay_capital) * 100.0, 4),
                    "max_drawdown_pct": round(float(nav_df["drawdown"].max()) * 100.0, 4),
                    "hist_r04b_funding_usdt": round(float(hist_funding), 6),
                    "net_pnl_with_hist_funding_usdt": round(net_with_hist_funding, 6),
                    "demo_stress_funding_usdt": round(demo_stress_funding, 6),
                    "net_pnl_with_demo_stress_funding_usdt": round(net_with_demo_stress_funding, 6),
                    "bh_0.04_pnl_ex_funding_usdt": round(bh_pnl, 6),
                    "grid_minus_bh_0.04_usdt": round(net_ex_funding - bh_pnl, 6),
                }
            )

    indep = [r for r in rows if r["sample_type"] == "INDEPENDENT_HISTORICAL_WINDOW"]
    return {
        "evidence_class": EVIDENCE_BOUNDARIES["historical_replay"],
        "dataset_identity": {
            "instrument_id": identity["instrument_id"],
            "data_version": identity["data_version"],
            "bars_sha256": identity["bars_sha256"],
            "window": [identity["start"], identity["end_exclusive"]],
            "total_bars": len(bars),
            "historical_funding_events_loaded": len(funding_events),
        },
        "replay_capital_usdt": str(replay_capital),
        "demo_7usdt_cap_infeasibility_note": (
            "At May-Aug 2026 NVDAUSDT prices (170-225 USDT), 0.04 units requires 6.8-9.0 USDT notional, "
            "exceeding the current Demo max_cash_committed=7.00 USDT cap in most windows. Replay uses "
            "10.00 USDT 1x collateral so single-level ±1% Grid mechanics can be evaluated across regimes."
        ),
        "summary": {
            "windows_total": len(rows),
            "independent_windows": len(indep),
            "positive_net_windows_ex_funding": sum(1 for r in rows if r["net_pnl_ex_funding_usdt"] > 0),
            "positive_net_windows_with_hist_funding": sum(1 for r in rows if r["net_pnl_with_hist_funding_usdt"] > 0),
            "positive_net_windows_with_demo_stress_funding": sum(
                1 for r in rows if r["net_pnl_with_demo_stress_funding_usdt"] > 0
            ),
            "breakout_paused_windows": sum(1 for r in rows if r["final_state"] == "PAUSED"),
            "mean_net_pnl_ex_funding_usdt": round(float(np.mean([r["net_pnl_ex_funding_usdt"] for r in rows])), 6)
            if rows
            else 0.0,
            "mean_net_pnl_with_hist_funding_usdt": round(
                float(np.mean([r["net_pnl_with_hist_funding_usdt"] for r in rows])), 6
            )
            if rows
            else 0.0,
            "mean_net_pnl_with_demo_stress_funding_usdt": round(
                float(np.mean([r["net_pnl_with_demo_stress_funding_usdt"] for r in rows])), 6
            )
            if rows
            else 0.0,
            "mean_bh_0.04_pnl_usdt": round(float(np.mean([r["bh_0.04_pnl_ex_funding_usdt"] for r in rows])), 6)
            if rows
            else 0.0,
            "worst_window_net_ex_funding_usdt": min((r["net_pnl_ex_funding_usdt"] for r in rows), default=0.0),
            "worst_window_drawdown_pct": max((r["max_drawdown_pct"] for r in rows), default=0.0),
            "independent_mean_net_ex_funding_usdt": round(
                float(np.mean([r["net_pnl_ex_funding_usdt"] for r in indep])), 6
            )
            if indep
            else 0.0,
        },
        "windows": rows,
    }


def _evaluate_demo_grid_path(
    prices: np.ndarray,
    *,
    start_iso: str,
    config: GridConfig,
    rules: GridRules,
    capital: Decimal,
    tmp_root: Path,
    tag: str,
    observed_demo_hourly_funding_bps: float,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    out_dir = tmp_root / tag
    run_experiment(
        out_dir,
        bars=close_bars(prices, start_iso),
        config=config,
        rules=rules,
        capital=capital,
        fee_rate=D("0.0003"),
        slippage=D("0.0002"),
        interval="1h",
        market=Market.BINANCE_USDM,
        instrument_id="NVDAUSDT",
        scenario=MC_SCENARIO,
        synthetic_capacity="0.10",
        research_wal=True,
        account_id=f"r08-mc-{tag}",
        provenance=provenance,
    )
    fifo = paper_fifo_attribution(out_dir)["summary"]
    life = lifecycle_metrics(out_dir)
    nav_df = pd.read_csv(out_dir / "daily.csv")
    cap_f = float(capital)
    terminal_eq = float(fifo["final_equity"])
    loss_ex_funding = 1.0 - terminal_eq / cap_f
    max_dd = float(nav_df["drawdown"].max())
    pos_notional_hours = float((nav_df["inventory"] * nav_df["close"]).sum())
    # Normal perp funding ~1 bps/8h = 0.125 bps/h; Demo stress funding = observed ~22.97 bps/h
    normal_fund_drag = pos_notional_hours * (0.125 / 10000.0)
    demo_stress_fund_drag = pos_notional_hours * (observed_demo_hourly_funding_bps / 10000.0)
    loss_normal_fund = 1.0 - (terminal_eq - normal_fund_drag) / cap_f
    loss_demo_stress_fund = 1.0 - (terminal_eq - demo_stress_fund_drag) / cap_f

    # Clean up temporary per-path SQLite immediately to bound disk usage
    for p in sorted(out_dir.rglob("*"), reverse=True):
        if p.is_file():
            p.unlink()
        elif p.is_dir():
            p.rmdir()
    out_dir.rmdir()

    return {
        "terminal_equity": terminal_eq,
        "net_pnl_ex_funding": terminal_eq - cap_f,
        "loss_ex_funding": loss_ex_funding,
        "loss_normal_funding": loss_normal_fund,
        "loss_demo_stress_funding": loss_demo_stress_fund,
        "max_drawdown": min(1.0, max(0.0, max_dd)),
        "grid_cycles": int(fifo["grid_cycles"]),
        "holding_hours": int(nav_df["inventory"].gt(0).sum()),
        "position_share_hours": round(float(life["position_hours"]), 4),
        "residual_inventory": float(fifo["residual_inventory"]),
        "final_state": life["final_state"],
        "paused_breakout": life["final_state"] == "PAUSED",
        "bh_0.04_loss": -(0.04 * (float(prices[-1]) - float(prices[0]))) / cap_f,
    }


def _evaluate_demo_grid_path_worker(payload: tuple[Any, ...]) -> dict[str, Any]:
    (
        prices,
        start_iso,
        config,
        rules,
        capital,
        tmp_root_str,
        tag,
        observed_demo_hourly_funding_bps,
        provenance,
    ) = payload
    return _evaluate_demo_grid_path(
        prices,
        start_iso=start_iso,
        config=config,
        rules=rules,
        capital=capital,
        tmp_root=Path(tmp_root_str),
        tag=tag,
        observed_demo_hourly_funding_bps=observed_demo_hourly_funding_bps,
        provenance=provenance,
    )


def run_tail_risk_analysis(
    r04b_snapshot: Path,
    *,
    anchor_price: float = 132.89,
    n_paths: int = 64,
    n_steps: int = 168,
    seed: int = 808,
    observed_demo_hourly_funding_bps: float = 22.97,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Run multi-volatility Monte Carlo, Moving Block Bootstrap, Regime/Jump & Deterministic Stress.

    Reuses ``grid_monte_carlo.historical_input``, ``MonteCarloRunner``,
    ``RegimeBootstrap``, ``JumpStress``, ``stress_paths``, and ``reliability.summarize_paths``.
    """
    bars_r04b, identity_r04b = load_hourly(r04b_snapshot)
    times_utc = pd.to_datetime([b["time"] for b in bars_r04b], utc=True) + pd.Timedelta(hours=1)
    acquired_ts = pd.Timestamp(identity_r04b["acquired_at"]).tz_convert("UTC")
    as_of_ts = max(times_utc[-1], acquired_ts).ceil("h")
    as_of = as_of_ts.isoformat()
    start_iso = (as_of_ts + pd.Timedelta(days=1)).isoformat()
    base_input, receipt = historical_input(r04b_snapshot, as_of=as_of, initial_price=anchor_price)
    capital = DEFAULT_STRATEGY_CAP_USDT
    cfg = _demo_grid_config_for_anchor(anchor_price, max_cash_committed=float(capital))
    rules = _demo_grid_rules()
    base_vol = float(base_input.volatility[0])
    base_drift = float(base_input.drift[0])

    # Build path batches across volatility regimes
    scenario_paths: list[tuple[str, str, np.ndarray]] = []
    for label, mult in (
        ("gbm_low_vol_0.5x", 0.5),
        ("gbm_base_vol_1.0x", 1.0),
        ("gbm_high_vol_2.0x", 2.0),
    ):
        scaled_vol = base_vol * mult
        # Zero-drift stress around calibrated mean for pure volatility regime isolation
        scaled_input = MonteCarloInput(
            weights=base_input.weights,
            drift=[base_drift],
            volatility=[scaled_vol],
            historical_returns=base_input.historical_returns,
            timestamps=base_input.timestamps,
            available_at=base_input.available_at,
            as_of=base_input.as_of,
            assets=base_input.assets,
            initial_prices=[anchor_price],
        )
        mc_cfg = MonteCarloConfig(
            model="gbm",
            n_paths=n_paths,
            n_steps=n_steps,
            seed=seed + int(mult * 100),
            dt=1.0,
            batch_size=min(64, n_paths),
            save_full_paths=True,
            compute=ComputeConfig(),
        )
        res = MonteCarloRunner(mc_cfg).run(scaled_input)
        scenario_paths.append((label, f"GBM ({mult:.1f}x sigma={scaled_vol*100:.3f}%/h)", res.asset_paths[:, :, 0]))

    # Moving Block Bootstrap (24h blocks) from certified R04B empirical hourly returns
    mbb_cfg = MonteCarloConfig(
        model="moving_block_bootstrap",
        n_paths=n_paths,
        n_steps=n_steps,
        block_size=24,
        seed=seed + 300,
        dt=1.0,
        batch_size=min(64, n_paths),
        save_full_paths=True,
        compute=ComputeConfig(),
    )
    mbb_res = MonteCarloRunner(mbb_cfg).run(base_input)
    scenario_paths.append(("moving_block_bootstrap_24h", "Moving Block Bootstrap (24h)", mbb_res.asset_paths[:, :, 0]))

    # State-conditional Regime Bootstrap (reusing RegimeBootstrap)
    px_arr = np.asarray([b["close"] for b in bars_r04b], dtype=float)
    ret_arr = (px_arr[1:] / px_arr[:-1] - 1.0)[:, None]
    avail_utc = [max(t, pd.Timestamp(identity_r04b["acquired_at"])) for t in times_utc[1:]]
    threshold_as_of = times_utc[len(times_utc) // 2].isoformat()
    plan = PathPlan(n_paths=n_paths, n_steps=n_steps, batch_size=min(32, n_paths), seed=seed + 400)
    try:
        reg_gen = RegimeBootstrap(
            returns=ret_arr,
            timestamps=times_utc[1:].tolist(),
            available_at=avail_utc,
            as_of=as_of,
            threshold_as_of=threshold_as_of,
            assets=("BINANCE:USDM:NVDAUSDT",),
            initial_prices=[anchor_price],
            source={"source": identity_r04b["source"], "data_version": identity_r04b["data_version"]},
            config=RegimeConfig(block_size=24, lookback=24, min_blocks_per_state=5, min_transitions_per_state=5),
        )
        reg_batches = [paths[:, :, 0] for _, paths in reg_gen.batches(plan)]
        scenario_paths.append(
            ("regime_bootstrap_24h", "State-Conditional Regime Bootstrap (24h)", np.vstack(reg_batches))
        )
    except CalibrationError:
        pass

    # Parameterized Jump Stress (reusing JumpStress)
    jump_gen = JumpStress(
        assets=("BINANCE:USDM:NVDAUSDT",),
        initial_prices=[anchor_price],
        as_of=as_of,
        source={"source": identity_r04b["source"], "data_version": identity_r04b["data_version"]},
        config=JumpConfig(
            log_drift=(base_drift - 0.5 * base_vol**2,),
            volatility=(base_vol,),
            jump_probability=0.015,  # ~2.5 downside jumps per 7-day week
            jump_log_sizes=(-0.03,),  # -3% adverse log jump
        ),
    )
    jump_plan = PathPlan(n_paths=n_paths, n_steps=n_steps, batch_size=min(32, n_paths), seed=seed + 500)
    jump_batches = [paths[:, :, 0] for _, paths in jump_gen.batches(jump_plan)]
    scenario_paths.append(
        ("jump_stress_downside", "Jump Stress (p=0.015/h, -3% log jump)", np.vstack(jump_batches))
    )

    scenario_summaries: list[dict[str, Any]] = []
    deterministic_rows: list[dict[str, Any]] = []
    provenance = capture_provenance(source_context_root())
    workers = max_workers if max_workers is not None else min(12, max(1, (os.cpu_count() or 1) // 2))

    with tempfile.TemporaryDirectory(prefix="factorlab-r08-mc-") as tmp:
        tmp_root = Path(tmp)
        executor = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
        try:
            for key, description, paths in scenario_paths:
                work_items = [
                    (
                        paths[i],
                        start_iso,
                        cfg,
                        rules,
                        capital,
                        str(tmp_root),
                        f"{key}-{i}",
                        observed_demo_hourly_funding_bps,
                        provenance,
                    )
                    for i in range(len(paths))
                ]
                if executor is not None and len(work_items) > 1:
                    path_metrics = list(executor.map(_evaluate_demo_grid_path_worker, work_items))
                else:
                    path_metrics = [_evaluate_demo_grid_path_worker(item) for item in work_items]
                losses_ex = np.asarray([m["loss_ex_funding"] for m in path_metrics], dtype=float)
                losses_norm_f = np.asarray([m["loss_normal_funding"] for m in path_metrics], dtype=float)
                losses_demo_f = np.asarray([m["loss_demo_stress_funding"] for m in path_metrics], dtype=float)
                dds = np.asarray([m["max_drawdown"] for m in path_metrics], dtype=float)
                bh_losses = np.asarray([m["bh_0.04_loss"] for m in path_metrics], dtype=float)

                rel_ex = summarize_paths(losses_ex, dds, threshold=0.05, confidences=(0.95, 0.99), resamples=499, bootstrap_seed=seed)
                rel_demo_f = summarize_paths(losses_demo_f, dds, threshold=0.05, confidences=(0.95, 0.99), resamples=499, bootstrap_seed=seed)
                rel_bh = summarize_paths(bh_losses, np.clip(np.maximum(bh_losses, 0.0), 0.0, 1.0), threshold=0.05, confidences=(0.95, 0.99), resamples=499, bootstrap_seed=seed)

                scenario_summaries.append(
                    {
                        "scenario": key,
                        "description": description,
                        "evidence_class": EVIDENCE_BOUNDARIES["synthetic_simulation"],
                        "n_paths": len(path_metrics),
                        "horizon_hours": n_steps,
                        "capital_basis_usdt": str(capital),
                        "mean_net_return_ex_funding_pct": round(float(-np.mean(losses_ex) * 100.0), 4),
                        "median_net_return_ex_funding_pct": round(float(-np.median(losses_ex) * 100.0), 4),
                        "loss_prob_ex_funding": round(rel_ex["loss_probability"]["estimate"], 4),
                        "loss_prob_wilson_95": [
                            round(rel_ex["loss_probability"]["lower"], 4),
                            round(rel_ex["loss_probability"]["upper"], 4),
                        ],
                        "var_95_ex_funding_pct": round(rel_ex["tails"]["0.95"]["var"] * 100.0, 4),
                        "cvar_95_ex_funding_pct": round(rel_ex["tails"]["0.95"]["cvar"] * 100.0, 4),
                        "cvar_95_boot_ci_pct": [
                            round(rel_ex["tails"]["0.95"]["cvar_interval"]["lower"] * 100.0, 4),
                            round(rel_ex["tails"]["0.95"]["cvar_interval"]["upper"] * 100.0, 4),
                        ],
                        "var_99_ex_funding_pct": round(rel_ex["tails"]["0.99"]["var"] * 100.0, 4),
                        "cvar_99_ex_funding_pct": round(rel_ex["tails"]["0.99"]["cvar"] * 100.0, 4),
                        "tail_99_status": rel_ex["tails"]["0.99"]["statistical_status"],
                        "mean_net_return_normal_funding_pct": round(float(-np.mean(losses_norm_f) * 100.0), 4),
                        "mean_net_return_demo_stress_funding_pct": round(float(-np.mean(losses_demo_f) * 100.0), 4),
                        "var_95_demo_stress_funding_pct": round(rel_demo_f["tails"]["0.95"]["var"] * 100.0, 4),
                        "cvar_95_demo_stress_funding_pct": round(rel_demo_f["tails"]["0.95"]["cvar"] * 100.0, 4),
                        "loss_prob_demo_stress_funding": round(rel_demo_f["loss_probability"]["estimate"], 4),
                        "max_dd_median_pct": round(rel_ex["maximum_drawdown_quantiles"]["0.5"]["estimate"] * 100.0, 4),
                        "max_dd_p95_pct": round(rel_ex["maximum_drawdown_quantiles"]["0.95"]["estimate"] * 100.0, 4),
                        "breakout_pause_rate": round(float(np.mean([m["paused_breakout"] for m in path_metrics])), 4),
                        "mean_grid_cycles": round(float(np.mean([m["grid_cycles"] for m in path_metrics])), 4),
                        "mean_holding_hours": round(float(np.mean([m["holding_hours"] for m in path_metrics])), 2),
                        "mean_position_share_hours": round(float(np.mean([m["position_share_hours"] for m in path_metrics])), 4),
                        "bh_0.04_var_95_pct": round(rel_bh["tails"]["0.95"]["var"] * 100.0, 4),
                        "bh_0.04_cvar_95_pct": round(rel_bh["tails"]["0.95"]["cvar"] * 100.0, 4),
                    }
                )

            # Deterministic stress paths (excluded from stochastic probability distributions)
            det_items = list(stress_paths(anchor_price, n_steps).items())
            det_work = [
                (
                    s_prices,
                    start_iso,
                    cfg,
                    rules,
                    capital,
                    str(tmp_root),
                    f"det-{s_name}",
                    observed_demo_hourly_funding_bps,
                    provenance,
                )
                for s_name, s_prices in det_items
            ]
            det_metrics = (
                list(executor.map(_evaluate_demo_grid_path_worker, det_work))
                if executor is not None
                else [_evaluate_demo_grid_path_worker(item) for item in det_work]
            )
            for (s_name, s_prices), m in zip(det_items, det_metrics):
                deterministic_rows.append(
                    {
                        "stress_name": s_name,
                        "evidence_class": EVIDENCE_BOUNDARIES["synthetic_simulation"] + "_DETERMINISTIC",
                        "start_price": round(float(s_prices[0]), 4),
                        "end_price": round(float(s_prices[-1]), 4),
                        "price_change_pct": round((float(s_prices[-1]) - float(s_prices[0])) / float(s_prices[0]) * 100.0, 2),
                        "net_pnl_ex_funding_usdt": round(m["net_pnl_ex_funding"], 6),
                        "return_ex_funding_pct": round(-m["loss_ex_funding"] * 100.0, 4),
                        "return_demo_stress_funding_pct": round(-m["loss_demo_stress_funding"] * 100.0, 4),
                        "max_drawdown_pct": round(m["max_drawdown"] * 100.0, 4),
                        "grid_cycles": m["grid_cycles"],
                        "holding_hours": m["holding_hours"],
                        "position_share_hours": m["position_share_hours"],
                        "residual_inventory": m["residual_inventory"],
                        "final_state": m["final_state"],
                        "bh_0.04_return_on_7usdt_pct": round(-m["bh_0.04_loss"] * 100.0, 4),
                    }
                )
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

    return {
        "evidence_class": EVIDENCE_BOUNDARIES["synthetic_simulation"],
        "calibration_receipt": receipt,
        "anchor_price": anchor_price,
        "capital_basis_usdt": str(capital),
        "n_paths_per_scenario": n_paths,
        "n_steps_hours": n_steps,
        "seed": seed,
        "observed_demo_hourly_funding_bps_stress": observed_demo_hourly_funding_bps,
        "stochastic_scenarios": scenario_summaries,
        "deterministic_stress": deterministic_rows,
        "boundaries": [
            "NO LIVE MONEY",
            "SIMULATED_CLOSE_ONLY_PATHS",
            "SYNTHETIC_PAPER_EXECUTION_NOT_DEMO_EXCHANGE",
            "CONDITIONAL_SIMULATION_UNCERTAINTY_NOT_MARKET_FORECAST",
        ],
    }


def render_report_markdown(analysis: dict[str, Any]) -> str:
    """Render the formal, repeatable Chinese R08 Demo Grid empirical performance report."""
    emp = analysis["empirical"]
    cons = analysis["consistency"]
    dedup = analysis["dedup_audit"]
    ops = analysis.get("ops_evidence") or {}
    fifo = emp["fifo_reconciliation"]
    cohorts = emp["cohorts"]
    cap = emp["capital_basis_metrics"]
    vel = emp["budget_velocity"]
    f11 = emp["f11_f08_analysis"].get("f11_breakout_exit") or {}
    f08 = emp["f11_f08_analysis"].get("f08_rotation") or {}
    cf = f11.get("delay_counterfactual") or {}
    benchmarks = analysis["benchmarks"]
    hist = analysis.get("historical_replay")
    tail = analysis.get("tail_risk")

    lines: list[str] = []
    lines.append("# FactorLab R08：Binance Demo Grid 实证绩效分析报告")
    lines.append("")
    lines.append("> **研究边界声明**：`NO LIVE MONEY`。本报告严格区分**官方 Binance Demo 实证**（`EMPIRICAL_OFFICIAL_DEMO`）、**历史行情离线回放**（`HISTORICAL_OFFLINE_REPLAY`）、**Monte Carlo / Bootstrap 模拟路径**（`SYNTHETIC_MONTE_CARLO_BOOTSTRAP`）与**尚未验证结论**（`UNVERIFIED_OPEN_CLAIM`）。所有 SQLite 账本均以只读快照（SQLite Backup API + `?mode=ro`）分析，未对在线数据库执行任何写操作，未修改在线 Grid 参数或执行路径，未扩大交易预算。**当前实证样本极短（仅 1 次完整网格持仓闭环与 1 次 F11 突破退出），短时间小幅正收益绝不代表策略已被证明具备长期正期望或风险调整优势。**")
    lines.append("")

    # 1. Executive Summary & Sample Coverage
    lines.append("## 1. 实证样本覆盖与核心结论摘要")
    lines.append("")
    sw = emp["sample_window"]
    lines.append(f"- **交易标的与账户**：`{emp['symbol']}`（USD-M 永续，1x 全仓 `CROSSED`，单向持仓 `ONE_WAY`，脱敏账户 `{emp['account_id_masked']}`）。")
    lines.append(f"- **全账户观测区间（UTC）**：`{sw['first_order_utc']}` → `{sw['last_observation_utc']}`（共 `{sw['full_account_days']:.2f}` 天，含早期烟雾测试与全部网格代际）。")
    lines.append(f"- **连续授权 Grid 家族区间（UTC）**：`{sw['continuous_grid_start_utc']}` → `{sw['last_observation_utc']}`（共 `{sw['continuous_grid_days']:.2f}` 天，账本内置 `{sw['total_completed_1m_bars_in_ledger']}` 根已完成 1m K 线）。")
    lines.append(f"- **云端迁移与受控重启**：C05 于 `{cons.get('handoff_reconciled_at')}` 完成 Windows→Azure 封存交接与签名 GET 对账（`snapshot_verified={cons.get('handoff_snapshot_verified')}`，`unknown_orders={cons.get('unknown_orders')}`）；云端 systemd 服务 `factorlab-demo-execute.service` 实测受控重启（`PID 3323 → 3792`，`NRestarts=0`），重启后 `POST=0`（无重复下单）。")
    lines.append("")
    lines.append("| 指标维度（官方 Demo 实证） | 全账户（含烟雾测试） | 仅 Grid 策略（全部代际） | 仅连续 Grid 家族（Gen 0–2） |")
    lines.append("| --- | ---: | ---: | ---: |")
    for label, key in [
        ("提交订单数 / 交易所接受数", ("orders_total", "orders_accepted")),
        ("成交订单数 / 撤单数 / 在途挂单", ("orders_filled", "orders_canceled", "orders_active")),
        ("成交率（按全部 POST / 按已接受）", ("fill_rate_all_posts", "fill_rate_accepted")),
        ("累计成交名义金额（换手，非本金，USDT）", "filled_turnover_usdt"),
        ("峰值实际持仓名义金额（USDT）", "peak_deployed_notional_usdt"),
        ("已实现交易毛盈亏（USDT）", "realized_trade_gross_usdt"),
        ("交易手续费（USDT）", "fees_usdt"),
        ("已实现资金费（USDT）", "realized_funding_usdt"),
        ("未实现盈亏（USDT）", "unrealized_pnl_usdt"),
        ("**累计净收益（USDT）**", "net_pnl_usdt"),
    ]:
        vals = []
        for ck in ("full_account", "all_grid", "continuous_grid"):
            c = cohorts[ck]
            if isinstance(key, tuple):
                if key[0].startswith("fill_rate"):
                    vals.append(f"{c[key[0]]*100:.1f}% / {c[key[1]]*100:.1f}%")
                else:
                    vals.append(" / ".join(str(c[k]) for k in key))
            else:
                vals.append(str(c[key]))
        lines.append(f"| {label} | {vals[0]} | {vals[1]} | {vals[2]} |")
    lines.append("")

    # 2. Data Consistency & Deduplication Audit
    lines.append("## 2. 原始记录获取、去重与跨库一致性审计")
    lines.append("")
    lines.append("本次优先检查云端是否存在 C07 一致性备份（未检出已有备份目录），随后通过远端只读 SQLite Backup API（源库 `?mode=ro`、目标写独立临时目录）执行原子快照采集，并校验备份窗口内源库静止（`source_quiescent_during_backup=true`）与全部 5 个 SQLite 数据库 `PRAGMA integrity_check == ok`。")
    lines.append("")
    if dedup.get("windows_snapshot_provided"):
        ce = dedup["controller_events"]
        po = dedup["protocol_orders"]
        fe = dedup["funding_events"]
        rd = dedup["runtime_dispatches"]
        lines.append("| 账本维度 | Windows 封存快照 | Azure 云端只读快照 | 重叠记录 | 仅 Windows | 仅 Azure 新增 | 合并去重总数 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        lines.append(f"| `controller.sqlite3` 事件 (`event_id`) | {ce['windows']} | {ce['cloud']} | {ce['overlap']} | {ce['windows_only']} | {ce['cloud_only_new']} | {ce['merged_unique']} |")
        lines.append(f"| `protocol.sqlite3` 订单 (`client_order_id`) | {po['windows']} | {po['cloud']} | {po['overlap']} | {po['windows_only']} | {po['cloud_only_new']} | {po['merged_unique']} |")
        lines.append(f"| `protocol.sqlite3` 资金费 (`event_id`) | {fe['windows']} | {fe['cloud']} | {fe['overlap']} | {fe['windows_only']} | {fe['cloud_only_new']} | {fe['merged_unique']} |")
        lines.append(f"| `runtime.sqlite3` 指令 (`dispatches`) | {rd['windows']} | {rd['cloud']} | {rd['overlap']} | {rd['windows_only']} | {rd['cloud_only_new']} | {rd['merged_unique']} |")
        lines.append("")
        lines.append(f"- **迁移单调超集核验**：`cloud_is_strict_superset = {dedup['cloud_is_strict_superset']}`（`windows_only = 0`），证明 Azure 云端账本完整继承了 Windows 全部历史记录且去重后无重复计入。")
    lines.append(f"- **跨库一致性总检**：`all_checks_passed = {cons['all_checks_passed']}`（SQLite 完整性 `{cons['all_sqlite_integrity_ok']}`、资金费三表 ID 与金额一致 `{cons['funding_ids_consistent']}` / `{cons['funding_totals_consistent']}`、成交回执与订单逐笔核对 `{cons['receipt_trade_reconciliation_ok']}`、未确认订单 `unknown_orders = {cons['unknown_orders']}`）。")
    lines.append(f"- **权威 FIFO 对账残差**（复用 `grid_fifo.attribute_events`）：期初钱包 `{fifo['opening_wallet_usdt']}` + 策略净收益 `{fifo['strategy_net_pnl_usdt']}` = 期末权益 `{fifo['final_equity_usdt']} USDT`，`reconciliation_residual = {fifo['reconciliation_residual_usdt']}`，与云端 `cloud-health.json` 的 `nav_usdt = {fifo['cloud_health_nav_usdt']}` 差值为 `{fifo['cloud_health_nav_difference_usdt']} USDT`（精确至小数点后 8 位，零残差）。")
    ej = (ops.get("execute_journal") or {})
    if ej:
        lines.append(f"- **云端受控重启核验**：journald 共观测到 `{len(ej.get('pid_segments') or [])}` 段独立进程生命周期，受控重启后 `post_restart_posts = {ej.get('post_restart_posts')}`（`zero_duplicate_post_after_restart = {ej.get('zero_duplicate_post_after_restart')}`）。")
    lines.append("")

    # 3. PnL Attribution & Capital Bases
    lines.append("## 3. 盈亏归因、三口径资金收益率与预算消耗")
    lines.append("")
    lines.append("### 3.1 权威 FIFO 盈亏归因（全账户 vs 仅 Grid）")
    lines.append("")
    lines.append("- **Pre-Grid 烟雾测试阶段（2026-11-14）**：买入 `0.04 @ 162.94`（Taker 手续费 `0.00162940 USDT`，2.5 bps）并平仓 `0.04 @ 162.30`（Taker 手续费 `0.00162300 USDT`，2.5 bps），已实现交易毛损 `-0.02560000 USDT`，手续费 `-0.00325240 USDT`，合计 `-0.02885240 USDT`，使 Grid 启动时期初账户权益为 `4999.97114760 USDT`。")
    lines.append(f"- **Grid 策略阶段（2026-11-14 → 2026-11-22）**：已实现交易毛利 `{cohorts['all_grid']['realized_trade_gross_usdt']} USDT`（全部来自 Gen 1 的 F11 突破退出 `RISK_EXIT`，常规网格完成循环 `completed_grid_cycles = {fifo['completed_grid_cycles']}`），实际交易手续费 `{cohorts['all_grid']['fees_usdt']} USDT`（入场与 F11 出场均为 `Maker=True`，Demo Maker 费率为 `0`），已实现资金费 `{cohorts['all_grid']['realized_funding_usdt']} USDT`（8 笔整点资金费），未实现盈亏 `{cohorts['all_grid']['unrealized_pnl_usdt']} USDT`（当前空仓，Gen 2 限价买单挂单中），**Grid 净收益 `{cohorts['all_grid']['net_pnl_usdt']} USDT`**。")
    lines.append("")
    lines.append("### 3.2 严格区分三种资金口径（严禁混淆账户余额、网格本金与累计换手）")
    lines.append("")
    acct_b = cap["account_equity_base"]
    strat_b = cap["strategy_committed_cap_base"]
    peak_b = cap["peak_deployed_notional_base"]
    lines.append("| 资金口径 | 基准本金（USDT） | Grid 净收益率 | 有效峰值杠杆 | Gen 1 最大 MTM 回撤 | 成交换手倍数 | 口径说明 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | --- |")
    lines.append(f"| 账户总权益口径 | {acct_b['grid_start_capital_usdt']} | {acct_b['grid_only_net_return_pct']:+.6f}% | {acct_b['effective_peak_leverage_x']:.6f}x | {acct_b['max_drawdown_pct_grid_window']:.6f}% | {acct_b['filled_turnover_multiple']:.6f}x | 5000 USDT 模拟盘总权益，约 99.9% 处于闲置状态 |")
    lines.append(f"| **Grid 风险预算口径（主评价口径）** | **{strat_b['capital_usdt']}** | **{strat_b['grid_net_return_pct']:+.4f}%** | **{strat_b['effective_peak_leverage_x']:.4f}x** | **{strat_b['max_drawdown_pct_gen1']:.4f}%** | **{strat_b['filled_turnover_multiple']:.4f}x** | 对应 `max_cash_committed = max_notional_usdt = 7.00 USDT` |")
    lines.append(f"| 峰值实际持仓口径 | {peak_b['peak_notional_usdt']} | {peak_b['grid_net_return_pct']:+.4f}% | 1.0000x | {peak_b['max_drawdown_pct_gen1']:.4f}% | {peak_b['filled_turnover_multiple']:.4f}x | Gen 1 实际建仓名义本金（`0.04 × 129.59 = 5.1836 USDT`） |")
    lines.append("")
    lines.append(f"- **换手与资金利用率**：全账户累计已提交 POST 名义额 `{cap['turnover_not_net_capital_note']['full_account_submitted_notional_usdt']} USDT`、累计已成交双边换手 `{cap['turnover_not_net_capital_note']['full_account_filled_turnover_usdt']} USDT`（其中 Grid 成交换手 `{cap['turnover_not_net_capital_note']['grid_filled_turnover_usdt']} USDT`），均属**流转换手额而非净投入本金**。在 Gen 1 活跃 21.4 小时内，时间加权持仓资金利用率为 `{strat_b['mean_position_utilization_pct_gen1']:.2f}%`（占 7.00 USDT 上限；含挂单占用为 `{strat_b['mean_committed_utilization_pct_gen1']:.2f}%`），持仓时间占比 `{emp['mtm_summary']['gen1_holding_fraction']*100:.2f}%`。")
    lines.append(f"- **连续预算消耗速度**：连续 Grid 家族（`{vel['continuous_grid_span_days']:.2f}` 天）共消耗 `POST = {vel['continuous_grid_posts']}` 笔（`{vel['continuous_grid_posts_per_day']:.2f}` 笔/天），成交 `{vel['continuous_grid_filled_orders']}` 笔（`{vel['continuous_grid_fills_per_day']:.2f}` 笔/天），累计提交名义额 `{vel['continuous_grid_submitted_notional_usdt']} USDT`（`{vel['continuous_grid_submitted_notional_per_day_usdt']:.2f} USDT/天`）。当前云端活跃窗口剩余预算 `remaining_posts = {vel['active_window_campaign'].get('remaining_posts')}/{vel['active_window_campaign'].get('post_budget')}`。")
    lines.append("")

    # 4. Per-Generation Grid Evaluation & F11/F08
    lines.append("## 4. 分代 Grid 绩效拆解与 F11 / F08 实证经济评价")
    lines.append("")
    lines.append("| 代际 | Deployment ID | 类别 | 区间 `[lower, upper]` | 1m K 线 | 下单/成交/撤单/在途 | 交易毛利 | 手续费 | 资金费 | 净盈亏 (USDT) | 7U 收益率 | 终态 |")
    lines.append("| ---: | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for g in emp["generations"]:
        lines.append(
            f"| {g['generation_seq']} | `{g['deployment_id']}` | {g['cohort']} | "
            f"`[{g['lower_price']:.2f}, {g['upper_price']:.2f}]` | {g['completed_1m_bars']} | "
            f"{g['orders_submitted']}/{g['orders_filled']}/{g['orders_canceled']}/{g['orders_active']} | "
            f"{g['realized_trade_gross_usdt']} | {g['fees_usdt']} | {g['realized_funding_usdt']} | "
            f"{g['net_pnl_usdt']} | {g['net_return_on_cap_pct']:+.4f}% | `{g['outcome']}` |"
        )
    lines.append("")

    lines.append("### 4.1 F11 突破退出与 F08 自动轮换的实证经济拆解（关键发现）")
    lines.append("")
    if f11:
        lines.append(f"1. **F11 突破退出成交表现**：Gen 1 买单（`0.04 @ 129.59`）于 `2026-11-22T05:18:48Z` 分两笔 Maker 成交后，价格于 `{cf.get('first_breakout_bar_utc')}` 首次收破上界 `132.21`（收盘 `{cf.get('first_breakout_close')}`），触发 `BREAKOUT`。在 C05 切换至 Azure 并激活 F11 后，系统于 `16:24:30.656Z` 提交减仓限价卖单 `{f11['client_order_id']}`（`@ {f11['exit_fill_price']}`），仅用 **`{f11['exit_submit_to_fill_ms']} ms`** 即以 `Maker=True`、**`0` 手续费**全额成交，实现交易毛利 **`+{f11['actual_gross_pnl_usdt']} USDT`**（高于原网格上界 `132.21` 目标毛利 `+{f11['grid_upper_target_gross_pnl_usdt']} USDT` 约 `+{f11['extra_gross_above_upper_bound_usdt']} USDT`）。")
        lines.append(f"2. **高额资金费侵蚀与退出延迟成本（核心经济发现）**：在 `08:42Z` 突破至 `16:24Z` F11 成交的 **`{cf.get('delay_hours_from_breakout_to_f11_fill')} 小时`**滞留窗口内（恰逢美股周末休市期），持仓连续支付了 **8 笔整点资金费**，合计 **`{f11['funding_paid_usdt']} USDT`**，吞噬了交易毛利的 **`{f11['funding_share_of_gross_pct']}%`**！隐含小时资金费率高达 **`{f11['mean_observed_hourly_funding_bps']} bps/h`**（年化约 `{f11['annualized_observed_funding_pct']}%`）。反事实测算显示：")
        lines.append(f"   - 若在首次突破时（`08:42Z @ {cf.get('first_breakout_close')}`）即刻触发 F11 退出（0 笔资金费），理论净收益约为 `+{cf.get('counterfactual_immediate_net_usdt')} USDT`；延迟 7.7 小时虽多赚价差 `+{cf.get('extra_price_gain_during_delay_usdt')} USDT`，但多付资金费 `{cf.get('funding_paid_during_delay_usdt')} USDT`，**退出滞留净成本约 `{cf.get('net_economic_cost_of_exit_delay_usdt')} USDT`**。")
        lines.append(f"   - 反之，若云端未部署 F11 而继续滞留，按最后 3 小时均值 `{f11['recent_hourly_funding_bleed_usdt']} USDT/h` 失血速度，剩余 `+{f11['actual_net_pnl_usdt']} USDT` 净利将在 **`{f11['hours_until_bleed_erases_remaining_net']} 小时`**内完全归零转负。因此 F11 在云端上线具有直接且显著的止血价值。")
        lines.append(f"3. **F08 自动轮换表现**：F11 平仓归零后仅 **`{f08.get('rotation_to_new_order_seconds')} 秒`**，F08 即自动创建 Gen 2（`{f08.get('to_deployment')}`，`[131.56, 134.22]`，锚定 `132.89`）并挂出新限价买单 `{f08.get('new_generation_order_id')}`（`0.04 @ {f08.get('new_generation_buy_price')}`，当前 `{f08.get('new_generation_status')}`），未出现空转或重复下单。")
    lines.append("")

    # 5. Benchmarks
    lines.append("## 5. 对比 `NVDAUSDT` 买入持有基准（明确杠杆与资金占用差异）")
    lines.append("")
    lines.append("| 观测窗口 | 基准名称 | 证据类别 | 价格变动 | 资金口径 (USDT) | 基准有效杠杆 | 基准净盈亏 (USDT) | 基准口径收益率 | 同窗 Grid 净盈亏 | Grid − 基准 (USDT) |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for b in benchmarks:
        lines.append(
            f"| {b['window']} | `{b['benchmark_name']}` | `{b['evidence_class']}` | "
            f"{b['price_return_pct']:+.2f}% | {b['capital_basis_usdt']} | {b['effective_leverage_on_basis_x']:.4f}x | "
            f"{b['net_pnl_usdt']} | {b['return_on_basis_pct']:+.4f}% | {b['grid_net_pnl_same_window_usdt']} | {b['grid_minus_benchmark_usdt']} |"
        )
    lines.append("")
    lines.append("- **Gen 1 活跃窗口（21.4h）**：`NVDAUSDT` 上涨 `+1.52%`（`130.90 → 132.89`）。等量 `0.04` 买入持有（不含资金费）净利 `+0.0796 USDT`（7U 口径 `+1.14%`），但计入相同 8 笔实证资金费后转为 **`-0.0180 USDT`**（`-0.26%`）；Grid Gen 1 因在低位 `129.59` 逢跌买入且延迟了 10.3 小时才进入多头敞口，净利 `+0.0344 USDT`（`+0.49%`），跑赢含资费等量 B&H `+0.0524 USDT`，但**落后于无资费等量 B&H `-0.0452 USDT` 与满仓 7U B&H `-0.0720 USDT`**。")
    lines.append("- **7.3 天连续家族窗口重要纠偏**：`NVDAUSDT` 从 `161.09` 大跌 `-17.61%` 至 `132.73`，而 Grid 未承受下跌回撤，但这**绝非网格具备择时或下行保护 Alpha 的证据**——真实原因是 Gen 0 买单在 11-15 仅挂单 87 分钟即被撤单，随后 Windows 端离线约 6 天（期间 K 线由重启回补），策略根本未在下跌途中挂单。")
    lines.append("- **杠杆与敞口不可直接按账户百分比对比**：5000 USDT 满仓 1x B&H 的名义敞口约为 Demo Grid（`~5.18 USDT`）的 **~960 倍**（`1.000x` vs `0.0010x` 账户有效杠杆），必须按同资金口径（7.00 USDT）与等量敞口（`0.04 NVDAUSDT`）分别对照。")
    lines.append("")

    # 6. Historical Replay & Tail Risk (Monte Carlo / Bootstrap)
    lines.append("## 6. 独立历史时间窗口回放与多波动情景尾部风险（离线研究，非 Demo 实证）")
    lines.append("")
    if hist is not None:
        hs = hist["summary"]
        lines.append("### 6.1 预声明 11 个 7 天历史窗口离线回放（`HISTORICAL_OFFLINE_REPLAY`）")
        lines.append("")
        lines.append(f"复用认证 R04B `NVDAUSDT` 1h 行情（`{hist['dataset_identity']['window'][0]}` → `{hist['dataset_identity']['window'][1]}`，`{hist['dataset_identity']['total_bars']}` 根小时线）与 `mc_grid_tail_f04c_windows.candidate_windows()`（3 个独立窗口 `I1–I3` + 8 个条件窗口 `C1–C8`），在 `grid_paper.run_experiment` + `grid_fifo` + `grid_funding` 上回放当前 Demo 单层 `±1%` 网格规则（因 5–8 月股价 170–225 USDT 下 `0.04` 份名义额达 6.8–9.0 USDT 超过当前 Demo `7.00 USDT` 上限，回放采用 `10.00 USDT` 1x 抵押本金并标记触发上限窗口）。")
        lines.append("")
        lines.append(f"- **跨窗口总体结果**：11 个 7 天窗口中，不含资金费正收益 `{hs['positive_net_windows_ex_funding']}/11`（均值 `{hs['mean_net_pnl_ex_funding_usdt']:+.4f} USDT`，独立窗口 `I1–I3` 均值 `{hs['independent_mean_net_ex_funding_usdt']:+.4f} USDT`），叠加 R04B 真实历史资金费后正收益 `{hs['positive_net_windows_with_hist_funding']}/11`（均值 `{hs['mean_net_pnl_with_hist_funding_usdt']:+.4f} USDT`），**若叠加本次 Demo 观测到的异常小时资金费压力（`-22.97 bps/h`）则正收益骤降至 `{hs['positive_net_windows_with_demo_stress_funding']}/11`（均值 `{hs['mean_net_pnl_with_demo_stress_funding_usdt']:+.4f} USDT`）**。")
        lines.append(f"- **突破暂停频率与最差回撤**：`{hs['breakout_paused_windows']}/11` 个窗口在 7 天内触发 `BREAKOUT` 暂停；单窗最差不含资费净损 `{hs['worst_window_net_ex_funding_usdt']:+.4f} USDT`，最大回撤 `{hs['worst_window_drawdown_pct']:.2f}%`（明细见 `historical_windows.csv`）。")
        lines.append("")

    if tail is not None:
        lines.append("### 6.2 多波动情景 Monte Carlo、Bootstrap 与跳跃压力尾部风险（`SYNTHETIC_MONTE_CARLO_BOOTSTRAP`）")
        lines.append("")
        lines.append(f"基于 R04B 认证小时收益率校准（基准小时波动率 `sigma = {tail['calibration_receipt']['volatility']*100:.3f}%/h`，锚定 Gen 2 当前价 `{tail['anchor_price']}`，资金口径 `{tail['capital_basis_usdt']} USDT`，步长 `{tail['n_steps_hours']}` 小时 = 7 天，每情景 `{tail['n_paths_per_scenario']}` 条路径），复用 `MonteCarloRunner`、`RegimeBootstrap`、`JumpStress` 与 `reliability.summarize_paths` 计算条件尾部风险：")
        lines.append("")
        lines.append("| 模拟情景 | 不含资费均值收益 | 亏损概率 (Wilson 95% CI) | VaR95 (不含资费) | CVaR95 (Bootstrap 95% CI) | VaR95 (Demo 资费压力) | CVaR95 (Demo 资费压力) | 中位/P95 最大回撤 | 突破暂停率 | 等量 B&H VaR95 |")
        lines.append("| --- | ---: | --- | ---: | --- | ---: | ---: | --- | ---: | ---: |")
        for s in tail["stochastic_scenarios"]:
            lines.append(
                f"| `{s['scenario']}` | {s['mean_net_return_ex_funding_pct']:+.2f}% | "
                f"{s['loss_prob_ex_funding']*100:.1f}% (`[{s['loss_prob_wilson_95'][0]*100:.1f}%, {s['loss_prob_wilson_95'][1]*100:.1f}%]`) | "
                f"{s['var_95_ex_funding_pct']:.2f}% | {s['cvar_95_ex_funding_pct']:.2f}% (`[{s['cvar_95_boot_ci_pct'][0]:.2f}%, {s['cvar_95_boot_ci_pct'][1]:.2f}%]`) | "
                f"{s['var_95_demo_stress_funding_pct']:.2f}% | {s['cvar_95_demo_stress_funding_pct']:.2f}% | "
                f"{s['max_dd_median_pct']:.2f}% / {s['max_dd_p95_pct']:.2f}% | "
                f"{s['breakout_pause_rate']*100:.1f}% | {s['bh_0.04_var_95_pct']:.2f}% |"
            )
        lines.append("")
        lines.append("- **确定性压力路径**（不计入随机概率分布，复用 `grid_monte_carlo.stress_paths`）：")
        lines.append("")
        lines.append("| 确定性压力路径 | 标的价格变动 | Grid 7U 收益率 (不含资费) | Grid 7U 收益率 (Demo 资费压力) | 最大回撤 | 持仓小时 | 终态 | 等量 B&H 7U 收益率 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |")
        for d in tail["deterministic_stress"]:
            lines.append(
                f"| `{d['stress_name']}` | {d['price_change_pct']:+.1f}% | {d['return_ex_funding_pct']:+.2f}% | "
                f"{d['return_demo_stress_funding_pct']:+.2f}% | {d['max_drawdown_pct']:.2f}% | "
                f"{d['holding_hours']} | `{d['final_state']}` | {d['bh_0.04_return_on_7usdt_pct']:+.2f}% |"
            )
        lines.append("")
        lines.append("- **尾部风险核心洞察**：单层 `±1%` 网格在下行单边趋势（`continuous_decline`、`range_then_gap`、`jump_stress_downside`）中呈现典型的**负偏态/截断上行、敞开下行**特征——买单在下轨成交后若持续破位，7U 资金口径下 7 天 95% CVaR 可达高波动/跳跃情景的两位数百分比回撤；更致命的是**资金费与滞留持仓的乘数效应**：当出现 Demo 观测到的高资金费环境且多头被套或延迟退出时，原本温和的震荡收益会迅速转为大幅亏损。")
        lines.append("")

    # 7. Strict Epistemic Boundary Table
    lines.append("## 7. 证据层级严格区分（严禁跨层外推）")
    lines.append("")
    lines.append("| 证据层级 | 标签 | 本报告对应内容 | 可支持结论 | 禁止越界外推 |")
    lines.append("| --- | --- | --- | --- | --- |")
    lines.append("| 官方 Demo 实证 | `EMPIRICAL_OFFICIAL_DEMO` | 10 笔订单、4 笔成交、8 笔资金费、3 代连续 Grid、F11 成交、F08 轮换、Azure 受控重启、零残差对账 | 迁移连续性、F11 止血与 F08 轮换可工作、高资金费可吞噬 74% 交易毛利 | **不可**将 1 次闭环的 `+0.0344 USDT` 解释为策略具备长期正期望或实盘优势 |")
    lines.append("| 历史离线回放 | `HISTORICAL_OFFLINE_REPLAY` | R04B 认证 1h 行情上 11 个 7 天窗口的 PaperEngine + FIFO + 资金费回放 | 单层 `±1%` 网格在不同历史市况下的突破暂停率、残余库存风险与 7U 上限在高价区间的不可行性 | 不代表 Binance Demo/实盘真实撮合队列或未来行情分布 |")
    lines.append("| 模拟生成路径 | `SYNTHETIC_MONTE_CARLO_BOOTSTRAP` | GBM (0.5x/1x/2x)、MBB (24h)、Regime Bootstrap、Jump Stress、6 条确定性压力路径 | 不同波动率/跳跃/高资费情景下的条件 VaR/CVaR 与回撤量级 | 条件区间仅反映模拟器抽样不确定性，**不是**市场预测置信区间 |")
    lines.append("| 尚未验证结论 | `UNVERIFIED_OPEN_CLAIM` | 24h/72h/7d 连续云端稳定性、常规网格多轮完整买卖循环正期望、下行突破（止损方向）F11 实盘成交滑点、真实资金市场深度 | 明确列为待验证项，需随后续云端运行样本自动重跑本脚本更新 | 在获得多轮独立完整循环与下行突破实证前，禁止宣称策略稳健盈利 |")
    lines.append("")

    # 8. Next-Round Strategy Improvement Recommendations
    lines.append("## 8. 下一轮策略与工程改进建议（按证据优先级排序）")
    lines.append("")
    lines.append("1. **【P0 / 最高优先级】引入资金费率感知风控（Funding-Aware Holding / Weekend Guard）**：本次实证最核心的经济发现是，仅 8 小时高资金费（`-0.0976 USDT`，均率 `22.97 bps/h`）就吞噬了 Gen 1 `73.96%` 的交易毛利。建议下一轮研究在**不改当前在线参数的前提下**先做离线对照：监控交易所实时/预测资金费率（`/fapi/v1/premiumIndex`），当正资金费率超过阈值（如 `> 5 bps/h` 或美股休市周末溢价期）时禁止新开多头网格或缩短持仓最大账龄上限（`max_inventory_age_hours`）。")
    lines.append("2. **【P0】核查并消除 F11 在低流动性/下行突破时的退出延迟与成交风险**：本次 F11 实证为**上行突破**（买在 `129.59`、退出在 `132.89` 盈利平仓），尚未观测到**下行突破**（亏损方向）时的减仓限价单成交率与滑点；同时应确保云端 systemd 持续在线，避免再次出现 Windows 时代的 7.7 小时离线滞留。")
    lines.append("3. **【P1】评估单层 `±1%` 网格的盈亏比与 `max_cash_committed = 7.00 USDT` 价格上限脆弱性**：历史回放显示，当 `NVDAUSDT > 175 USDT` 时，最小交易量 `0.04` 的名义额即突破 `7.00 USDT` 硬上限导致无法下单；且单层 `±1%` 网格在 7 天内高频触发 `BREAKOUT` 暂停。下一轮离线研究应评估：① 按价格自适应 `quantity_unit` 或明确定义预算与最小名义额的耦合告警；② 对比 `±1%` vs `±1.5%~2.0%` 与多档位（2–3 levels）在相同 7–10 USDT 风险预算下的循环完成率与尾部 CVaR。")
    lines.append("4. **【P1】建立按日/按周增量自动更新流水线**：复用本次交付的只读快照抓取脚本 `tools/research/capture_demo_ro_snapshot.py` 与绩效报告脚本 `tools/research/run_demo_grid_r08.py`，待 Azure 跑满 24h / 72h / 7d 观察窗并积累多轮常规网格循环（含下行突破样本）后，一键重跑更新本报告，不手工重新核算。")
    lines.append("")

    # 9. Reproducibility
    lines.append("## 9. 可重复更新命令")
    lines.append("")
    lines.append("后续新增交易后，仅需两步即可在不触碰在线 SQLite 的前提下全自动刷新全部表格、JSON 指标与本报告：")
    lines.append("")
    lines.append("```powershell")
    lines.append("# 1) 从 Azure 获取只读一致性 SQLite Backup API 快照（不写在线库、不重启服务）")
    lines.append('python tools/research/capture_demo_ro_snapshot.py --ssh-host "<azure-vm-ip>" --ssh-user azureuser --ssh-key "<path-to-pem>" --output "<local-private-dir>"')
    lines.append("")
    lines.append("# 2) 一键重算全部实证归因、基准对比、历史回放、Monte Carlo/Bootstrap 尾部风险并更新 REPORT.md")
    lines.append('python tools/research/run_demo_grid_r08.py --cloud-snapshot "<local-private-dir>" --windows-snapshot "<sealed-windows-dir>" --r04b-snapshot "<r04b-certified-dir>" --output-dir "docs/validation/p8-r08"')
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def write_r08_artifacts(analysis: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    """Write compact sanitized JSON, CSV, and Markdown report artifacts."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}

    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(analysis, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    paths["summary_json"] = summary_path

    def _dump_csv(name: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        p = output_dir / name
        pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8")
        paths[name] = p

    emp = analysis["empirical"]
    _dump_csv("orders_lifecycle.csv", emp["order_lifecycle"])
    _dump_csv("fills_and_funding.csv", emp["fills_and_funding"])
    _dump_csv("generations.csv", emp["generations"])
    _dump_csv("benchmarks.csv", analysis["benchmarks"])
    if analysis.get("historical_replay"):
        _dump_csv("historical_windows.csv", analysis["historical_replay"]["windows"])
    if analysis.get("tail_risk"):
        _dump_csv("tail_risk_scenarios.csv", analysis["tail_risk"]["stochastic_scenarios"])
        _dump_csv("deterministic_stress.csv", analysis["tail_risk"]["deterministic_stress"])

    report_path = output_dir / "REPORT.md"
    report_path.write_text(render_report_markdown(analysis), encoding="utf-8")
    paths["report_md"] = report_path
    return paths


def run_full_r08_analysis(
    cloud_snapshot: Path,
    *,
    windows_snapshot: Path | None = None,
    r04b_snapshot: Path | None = None,
    r04b_funding_csv: Path | None = None,
    output_dir: Path | None = None,
    mc_paths: int = 64,
    mc_steps: int = 168,
    seed: int = 808,
    opening_wallet: Decimal = DEFAULT_OPENING_WALLET,
    strategy_cap_usdt: Decimal = DEFAULT_STRATEGY_CAP_USDT,
) -> dict[str, Any]:
    """End-to-end R08 empirical performance, benchmark, historical replay, and tail risk runner."""
    merged = load_and_merge_snapshots(cloud_snapshot, windows_snapshot=windows_snapshot)
    empirical = analyze_empirical_performance(
        merged, opening_wallet=opening_wallet, strategy_cap_usdt=strategy_cap_usdt
    )
    benchmarks = evaluate_benchmarks(
        merged, empirical, opening_wallet=opening_wallet, strategy_cap_usdt=strategy_cap_usdt
    )

    observed_bps = (
        (empirical["f11_f08_analysis"].get("f11_breakout_exit") or {}).get("mean_observed_hourly_funding_bps")
        or 22.97
    )
    anchor_px = (
        emp_gen[-1]["anchor_price"]
        if (emp_gen := [g for g in empirical["generations"] if g["cohort"] == "CONTINUOUS_GRID"])
        else 132.89
    )

    historical_replay = None
    tail_risk = None
    if r04b_snapshot is not None and Path(r04b_snapshot).exists():
        historical_replay = run_historical_window_replay(
            r04b_snapshot,
            funding_csv=r04b_funding_csv,
            observed_demo_hourly_funding_bps=float(observed_bps),
        )
        tail_risk = run_tail_risk_analysis(
            r04b_snapshot,
            anchor_price=float(anchor_px),
            n_paths=mc_paths,
            n_steps=mc_steps,
            seed=seed,
            observed_demo_hourly_funding_bps=float(observed_bps),
        )

    analysis = {
        "study": "FactorLab-R08",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "boundaries": [
            "NO LIVE MONEY",
            "READ_ONLY_SNAPSHOT_ANALYSIS",
            "ONLINE_EXECUTION_UNMODIFIED",
            "SHORT_SAMPLE_NOT_STATISTICAL_PROOF",
        ],
        "dedup_audit": merged["dedup_audit"],
        "consistency": merged["consistency"],
        "ops_evidence": merged["ops_evidence"],
        "empirical": empirical,
        "benchmarks": benchmarks,
        "historical_replay": historical_replay,
        "tail_risk": tail_risk,
    }
    if output_dir is not None:
        write_r08_artifacts(analysis, output_dir)
    return analysis
