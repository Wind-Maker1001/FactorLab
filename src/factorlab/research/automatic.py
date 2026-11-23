"""Bounded custom-strategy research over existing execution and tracker APIs."""
from copy import deepcopy
from datetime import datetime, timezone
from itertools import product
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

import pandas as pd

from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.dataset.identity import file_hash, frame_hash, json_hash
from factorlab.experiments import ExperimentTracker
from factorlab.experiments.provenance import capture_provenance
from factorlab.forward.integrity import research_source_sha256, workflow_lock
from factorlab.research.champion import (ChampionSelector, SelectionPolicy,
                                        qualification, risk_metrics)
from factorlab.strategies.custom import load_strategy, run_custom_strategy
from factorlab.strategies.dual_ma import daily_dates

ROOT = Path(__file__).resolve().parents[3]
STATE_SCHEMA = "FACTORLAB_CHAMPION_STATE_V1"
AUDIT_STATE_SCHEMA = "FACTORLAB_CHAMPION_AUDIT_STATE_V1"
REPLAY_SCHEMA = "FACTORLAB_AUTO_RESEARCH_REPLAY_V1"
SNAPSHOT_SCHEMA = "FACTORLAB_CHAMPION_PRE_RUN_STATE_V1"
BAR_FIELDS = {"raw_open", "raw_high", "raw_low", "raw_close", "adj_close", "volume",
              "trade_status", "is_st"}


def write_json(path, value):
    """Publish a whole JSON document atomically on the destination filesystem."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_config(config):
    required = {"schema", "study_id", "data", "windows", "common", "policy", "strategies", "champion"}
    if set(config) != required or config["schema"] != "FACTORLAB_AUTO_RESEARCH_V1":
        raise ValueError("invalid automatic research configuration")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", config["study_id"]):
        raise ValueError("invalid study_id")
    p = SelectionPolicy(**config["policy"])
    windows = config["windows"]
    if set(windows) != {"train", "validation", "test"}:
        raise ValueError("explicit train/validation/test windows required")
    boundaries = []
    for stage in ("train", "validation", "test"):
        window = windows[stage]
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError("windows require inclusive [start, end]")
        dates = [pd.Timestamp(x) for x in window]
        if any(pd.isna(x) or x.tzinfo or x != x.normalize() for x in dates) or dates[0] > dates[1]:
            raise ValueError("invalid window dates")
        boundaries.extend(dates)
    if any(a >= b for a, b in zip(boundaries[1::2][:-1], boundaries[2::2])):
        raise ValueError("train/validation/test windows must be ordered and disjoint")
    if boundaries[-1] >= pd.Timestamp(datetime.now(timezone.utc).date()):
        raise ValueError("test must end before the current UTC day")
    common = config["common"]
    if set(common) != {"symbol", "initial_cash", "max_weight", "board_lot", "funding_bps",
                       "execution", "fee_rules"}:
        raise ValueError("explicit common capital, costs, funding, execution and risk budget required")
    if common["execution"] != "a_share_daily_long_only_next_open_t1" or common["funding_bps"] != 0:
        raise ValueError("adapter supports unlevered A-share next-open execution with zero funding only")
    FeeSchedule([FeeRule(**r) for r in common["fee_rules"]])
    if set(config["data"]) != {"bars", "sha256", "source_kind"} or config["data"]["source_kind"] not in {"SYNTHETIC", "REAL_CANDIDATE"}:
        raise ValueError("pinned parquet bars and explicit evidence qualification required")
    return p


def expand_registry(entries, limit):
    """Deterministic finite grids; reject oversize requests before materializing."""
    if not isinstance(entries, list) or not entries:
        raise ValueError("nonempty strategy registry required")
    expanded, names = [], set()
    for entry in entries:
        if set(entry) != {"strategy_id", "file", "class", "sha256", "parameters", "grid"}:
            raise ValueError("registry entry requires strategy_id/file/class/sha256/parameters/grid")
        name = entry["strategy_id"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) or name in names:
            raise ValueError("unique strategy_id required")
        names.add(name)
        grid = entry["grid"]
        if not isinstance(grid, dict) or not isinstance(entry["parameters"], dict):
            raise ValueError("parameters and grid must be objects")
        keys = sorted(grid)
        if any(not isinstance(grid[k], list) or not grid[k] for k in keys):
            raise ValueError("grid axes must be finite nonempty lists")
        count = math.prod(len(grid[k]) for k in keys)
        if len(expanded)+count > limit:
            raise ValueError("parameter search exceeds max_candidates")
        for values in product(*(grid[k] for k in keys)):
            expanded.append(dict(strategy_id=name, strategy={
                k: entry[k] for k in ("file", "class", "sha256")},
                parameters={**entry["parameters"], **dict(zip(keys, values))}))
    return expanded


def _register(item, base, common, windows, root):
    spec = dict(item["strategy"], parameters=item["parameters"])
    spec["file"] = str((base / spec["file"]).resolve())
    if not re.fullmatch(r"[a-f0-9]{64}", spec["sha256"]):
        raise ValueError("explicit strategy SHA256 code version required")
    research = {k: common[k] for k in ("symbol", "initial_cash", "max_weight", "board_lot")}
    research.update(start=windows["validation"][0], end=windows["validation"][1])
    draft = root / "registration.json"
    write_json(draft, dict(strategy=spec, research=research))
    strategy, _, normalized = load_strategy(draft)
    if not set(strategy.required_fields) <= BAR_FIELDS:
        raise ValueError("research adapters accept market fields only; future labels are forbidden")
    spec = normalized["strategy"]
    identity = dict(strategy_id=item["strategy_id"], code_version=spec["sha256"],
                    class_name=spec["class"], parameters=spec["parameters"])
    return dict(identity, candidate_id="c-"+json_hash(identity)[:24], strategy=spec)


def run(config_path, *, output, store, state_path, command=()):
    """Run once. OS cron/Task Scheduler may call this same entry periodically.

    The state file belongs to ONE deployment mandate. Keep it across invocations.
    Reserving a test window is irreversible even after interruption: new research
    must use a fresh holdout rather than retrying decisions on consumed data.
    """
    config_path = Path(config_path).resolve()
    config = read_json(config_path)
    policy = validate_config(config)
    state_path = Path(state_path).absolute()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with workflow_lock(state_path.with_suffix(state_path.suffix+".lock")):
        old = read_json(state_path) if state_path.exists() else None
        return _run(config, config_path.parent, Path(output).resolve(), store, state_path,
                    policy, command, old=old)


def replay(source, *, audit_dir, command=()):
    """Reproduce a research decision in a new, self-contained audit directory."""
    source = Path(source).resolve()
    manifest = read_json(source / "replay-config.json")
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema", "config", "pre_run_state_sha256"}
            or manifest["schema"] != REPLAY_SCHEMA):
        raise ValueError("invalid audit replay configuration")
    config = manifest["config"]
    policy = validate_config(config)
    snapshot = read_json(source / "pre-run-state.json")
    if json_hash(snapshot) != manifest["pre_run_state_sha256"]:
        raise ValueError("pre-run Champion state checksum mismatch")
    old = _replay_state(snapshot, config)
    audit_dir = Path(audit_dir).resolve()
    audit_dir.mkdir(parents=True, exist_ok=False)
    return _run(config, source, audit_dir / "run", audit_dir / "tracker",
                audit_dir / "champion.json", policy, command, old=old, audit=True)


def _replay_state(snapshot, config):
    if (not isinstance(snapshot, dict)
            or set(snapshot) != {"schema", "config_sha256", "common_hash", "state"}
            or snapshot["schema"] != SNAPSHOT_SCHEMA):
        raise ValueError("invalid pre-run Champion state snapshot schema")
    if (snapshot["config_sha256"] != json_hash(config)
            or snapshot["common_hash"] != json_hash(config["common"])):
        raise ValueError("pre-run Champion state does not match the replay research contract")
    old = snapshot["state"]
    if old is None:
        return None
    fields = {"schema", "champion", "common_hash", "last_test_end", "last_promotion_end",
              "version", "status", "decision", "reserved_study"}
    if (not isinstance(old, dict) or set(old) != fields or old["schema"] != STATE_SCHEMA
            or type(old["version"]) is not int or old["version"] < 0
            or old["status"] not in ("TEST_RESERVED", "COMPLETED")
            or not isinstance(old["reserved_study"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", old["reserved_study"])):
        raise ValueError("invalid pre-run Champion state fields")
    decision = old["decision"]
    if decision is None:
        if old["status"] == "COMPLETED" or old["version"] > 0:
            raise ValueError("pre-run Champion state is missing its prior decision")
    elif (not isinstance(decision, dict) or set(decision) != {"path", "sha256"}
          or not isinstance(decision["path"], str) or not decision["path"]
          or not isinstance(decision["sha256"], str)
          or not re.fullmatch(r"[a-f0-9]{64}", decision["sha256"])):
        raise ValueError("invalid pre-run Champion decision reference")
    last_test = _snapshot_date(old["last_test_end"])
    promotion = old["last_promotion_end"]
    if ((promotion is None) != (old["version"] == 0)
            or (promotion is not None and _snapshot_date(promotion) > last_test)):
        raise ValueError("invalid pre-run Champion promotion continuity")
    return old


def _snapshot_date(value):
    if not isinstance(value, str):
        raise ValueError("pre-run Champion dates require calendar date strings")
    date = pd.Timestamp(value)
    if pd.isna(date) or date.tzinfo or date != date.normalize():
        raise ValueError("invalid pre-run Champion date")
    return date


def _run(config, base, output, store, state_path, policy, command, *, old, audit=False):
    common, windows = config["common"], config["windows"]
    common_hash = json_hash(common)
    if old is not None:
        if old["schema"] != STATE_SCHEMA or old["common_hash"] != common_hash:
            raise ValueError("deployment mandate/common comparison contract changed")
        if pd.Timestamp(windows["test"][0]) <= pd.Timestamp(old["last_test_end"]):
            raise ValueError("OOS window already consumed; supply a fresh nonoverlapping test")
    bars_path = (base / config["data"]["bars"]).resolve()
    payload = bars_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != config["data"]["sha256"]:
        raise ValueError("historical data identity changed")
    bars = pd.read_parquet(BytesIO(payload))
    bars["date"] = daily_dates(bars.date, "research bars")
    for name, (start, end) in windows.items():
        if len(bars.loc[bars.date.between(start, end)]) < 2:
            raise ValueError(name+" window has insufficient observed sessions")
        if pd.Timestamp(start) not in set(bars.date) or pd.Timestamp(end) not in set(bars.date):
            raise ValueError(name+" window boundaries must be observed sessions")
    # Exclude unrequested future data and non-market columns before any adapter.
    bars = bars.loc[bars.date.le(windows["test"][1]), ["date", "symbol", *sorted(BAR_FIELDS)]].copy()
    output.mkdir(parents=True, exist_ok=False)
    (output / "sources").mkdir()
    if not audit:
        write_json(output / "config.json", config)
    provenance = capture_provenance(ROOT)
    provenance["code"]["source_sha256"] = research_source_sha256(ROOT)
    bars.to_parquet(output / "bars.parquet", index=False)
    # Retain exact original bytes as well as the normalized consumed frame.
    (output / "input-bars.parquet").write_bytes(payload)
    candidates = [_register(item, base, common, windows, output)
                  for item in expand_registry(config["strategies"], policy.max_candidates)]
    if old is None or audit:
        champion_item = _register(config["champion"], base, common, windows, output)
        if old is not None:
            archived = dict(champion_item, strategy=config["champion"]["strategy"])
            if old["champion"] != archived:
                raise ValueError("pre-run Champion identity does not match the archived strategy")
    else:
        champion_item = old["champion"]
    # Validate existing Champion's pinned code as well; never replace it on failure.
    if file_hash(champion_item["strategy"]["file"]) != champion_item["code_version"]:
        raise ValueError("Champion code changed")
    if len({c["candidate_id"] for c in candidates}) != len(candidates):
        raise ValueError("duplicate normalized parameter candidates")
    all_candidates = {c["candidate_id"]: c for c in [champion_item, *candidates]}
    for c in all_candidates.values():
        source = Path(c["strategy"]["file"])
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != c["code_version"]:
            raise ValueError("candidate code changed")
        (output / "sources" / (c["code_version"]+".py")).write_bytes(content)
    archived_config = deepcopy(config)
    archived_config["data"]["bars"] = "input-bars.parquet"
    for entry in archived_config["strategies"]:
        entry["file"] = "sources/"+entry["sha256"]+".py"
    archived_champion = dict(champion_item["strategy"],
                             file="sources/"+champion_item["code_version"]+".py")
    archived_config["champion"] = dict(strategy_id=champion_item["strategy_id"],
        strategy=archived_champion,
        parameters=champion_item["parameters"])
    snapshot = dict(schema=SNAPSHOT_SCHEMA, config_sha256=json_hash(archived_config),
        common_hash=common_hash, state=None if old is None else
        dict(old, champion=dict(champion_item, strategy=archived_champion)))
    snapshot_hash = json_hash(snapshot)
    snapshot_path = output / "pre-run-state.json"
    write_json(snapshot_path, snapshot)
    write_json(output / "replay-config.json", dict(schema=REPLAY_SCHEMA, config=archived_config,
        pre_run_state_sha256=snapshot_hash))
    tracker = ExperimentTracker(store)
    catalog = {c["candidate_id"]: dict(candidate=c, status="REGISTERED", stages={}) for c in all_candidates.values()}
    fees = FeeSchedule([FeeRule(**r) for r in common["fee_rules"]])
    train = bars.loc[bars.date.between(*windows["train"])].copy()

    def evaluate(candidate, stage):
        cid = candidate["candidate_id"]
        folder = output / (cid+"-"+stage)
        folder.mkdir()
        stage_config = dict(strategy=dict(candidate["strategy"],
            file=str(output / "sources" / (candidate["code_version"]+".py"))),
            research={k: common[k] for k in ("symbol", "initial_cash", "max_weight", "board_lot")})
        stage_config["research"].update(start=windows[stage][0], end=windows[stage][1])
        write_json(folder / "config.json", stage_config)
        identity = config["study_id"]+"-"+cid+"-"+stage
        tracker.create_experiment(identity, dict(model=candidate["strategy_id"], parameters=candidate,
            factors=[], dataset={"bars_sha256": config["data"]["sha256"]}, label={"name": "daily_net_equity_return"},
            windows=windows, walk_forward={"mode": "fixed_train_validation_then_sealed_test", "stage": stage},
            costs=common))
        try:
            prefix = bars.loc[bars.date.le(windows[stage][1])].copy()
            result = run_custom_strategy(prefix, pd.DatetimeIndex(prefix.date), folder / "config.json",
                fee_schedule=fees, source_kind=config["data"]["source_kind"], fit_bars=train)
            metrics = risk_metrics(result, prefix, common["initial_cash"], policy)
            for name in ("nav", "trades", "rejections"):
                result["strategy"][name].to_csv(folder / (name+".csv"), index=False)
            result["signals"].to_csv(folder / "signals.csv", index=False)
            write_json(folder / "evaluation.json", result["evaluations"]["strategy"].to_dict())
            write_json(folder / "metrics.json", metrics)
            status, failure = "SUCCEEDED", None
        except Exception as exc:
            result, metrics, status, failure = None, {}, "FAILED", str(exc)
        paths = [p for p in folder.iterdir() if p.is_file()]
        paths += [output / "input-bars.parquet", output / "replay-config.json", snapshot_path,
                  output / "sources" / (candidate["code_version"]+".py")]
        artifacts = {p.name: {"path": str(p), "sha256": file_hash(p)} for p in paths}
        record = tracker.append_run(identity, identity, **provenance, command=list(command), status=status,
            qualification=config["data"]["source_kind"], portfolio_metrics=metrics, artifacts=artifacts,
            validation={"sample_hash": frame_hash(bars), "evaluation": {"policy": asdict_policy(policy), "stage": stage},
                        "failure": failure, "train_only_fit": windows["train"], "financial_admission": "FORMAL_ADMISSION_BLOCKED"})
        catalog[cid]["stages"][stage] = dict(status=status, metrics=metrics, failure=failure,
            run_id=identity, record_sha256=record["sha256"])
        return None if result is None else dict(candidate=candidate, metrics=metrics, nav=result["strategy"]["nav"])

    # Search ONLY validation; lock one version per strategy before seeing OOS.
    leaders = {}
    for c in candidates:
        if c["candidate_id"] == champion_item["candidate_id"]:
            continue
        evaluated = evaluate(c, "validation")
        entry = catalog[c["candidate_id"]]
        entry["reasons"] = qualification(evaluated["metrics"], policy) if evaluated else ["validation_execution_failed"]
        entry["status"] = "VALIDATION_REJECTED" if entry["reasons"] else "VALIDATION_PASSED_NOT_SHORTLISTED"
        if evaluated and not entry["reasons"]:
            key = c["strategy_id"]
            previous = leaders.get(key)
            if previous is None or (-evaluated["metrics"]["score"], c["candidate_id"]) < (-previous["metrics"]["score"], previous["candidate"]["candidate_id"]):
                leaders[key] = evaluated
    finalists = sorted(leaders.values(), key=lambda e: (-e["metrics"]["score"], e["candidate"]["candidate_id"]))[:policy.max_finalists]
    frozen = dict(config_sha256=json_hash(config), finalists=[f["candidate"] for f in finalists],
                  champion=champion_item, policy=asdict_policy(policy), family_size=len(candidates),
                  dataset_sha256=config["data"]["sha256"], code=provenance["code"],
                  pre_run_state_sha256=snapshot_hash)
    write_json(output / "frozen-search.json", frozen)
    # Consume before test access. Crash leaves incumbent and reservation intact.
    state = dict(schema=AUDIT_STATE_SCHEMA if audit else STATE_SCHEMA, champion=champion_item,
        common_hash=common_hash, last_test_end=windows["test"][1],
        last_promotion_end=old["last_promotion_end"] if old is not None else None,
        version=old["version"] if old is not None else 0, status="TEST_RESERVED",
        decision=old["decision"] if old is not None and not audit else None,
        reserved_study=config["study_id"])
    write_json(state_path, state)
    incumbent = evaluate(champion_item, "test")
    catalog[champion_item["candidate_id"]]["status"] = "INCUMBENT_EVALUATED" if incumbent else "INCUMBENT_FAILED"
    challengers = []
    for finalist in finalists:
        c = finalist["candidate"]
        evaluated = evaluate(c, "test")
        catalog[c["candidate_id"]]["status"] = "OOS_EVALUATED" if evaluated else "OOS_FAILED"
        if evaluated:
            challengers.append(evaluated)
    if incumbent is None:
        decision = dict(selected=champion_item, promoted=False, decisions=[], reason="retain_champion_comparison_unavailable")
    else:
        cooldown = not state["last_promotion_end"] or (pd.Timestamp(windows["test"][1])-pd.Timestamp(state["last_promotion_end"])).days >= policy.cooldown_days
        decision = ChampionSelector(policy).select(incumbent, challengers,
            family_size=len(candidates), cooldown_elapsed=cooldown)
    # Existing tracker verifies matching costs/data/test samples and qualification.
    if incumbent and challengers:
        tracker.compare([catalog[x["candidate"]["candidate_id"]]["stages"]["test"]["run_id"]
                         for x in [incumbent, *challengers]])
    for d in decision["decisions"]:
        catalog[d["candidate_id"]]["reasons"] = d["reasons"]
        catalog[d["candidate_id"]]["status"] = "OOS_QUALIFIED" if d["eligible"] else "OOS_REJECTED"
    report = dict(schema="FACTORLAB_CHAMPION_SELECTION_V1", study_id=config["study_id"],
        version=state["version"]+int(decision["promoted"]), **decision,
        frozen_search_sha256=file_hash(output / "frozen-search.json"), windows=windows,
        common=common, policy=asdict_policy(policy), candidates=list(catalog.values()),
        source_kind=config["data"]["source_kind"], financial_admission="FORMAL_ADMISSION_BLOCKED",
        interpretation="historical OOS conditional evidence; statistical uncertainty remains; synthetic inputs prove software only")
    write_json(output / "selection.json", report)
    write_json(output / "candidates.json", list(catalog.values()))
    lines = ["# Automatic research evidence", "", "Decision: "+report["reason"],
             "Selected: "+report["selected"]["candidate_id"], "",
             report["interpretation"], "", "| Candidate | Stage | Score | Net return | Drawdown | CVaR |",
             "| --- | --- | --- | --- | --- | --- |"]
    for item in catalog.values():
        for stage, evidence in item["stages"].items():
            m = evidence["metrics"]
            lines.append("| "+" | ".join(str(v) for v in (item["candidate"]["candidate_id"], stage,
                m.get("score"), m.get("net_return"), m.get("max_drawdown"), m.get("daily_cvar")))+" |")
    (output / "REPORT.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    state.update(champion=decision["selected"], version=report["version"], status="COMPLETED",
                 decision={"path": str(output / "selection.json"), "sha256": file_hash(output / "selection.json")})
    if decision["promoted"]:
        state["last_promotion_end"] = windows["test"][1]
    write_json(state_path, state)
    return report


def asdict_policy(policy):
    from dataclasses import asdict
    return asdict(policy)
