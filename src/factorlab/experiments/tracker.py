"""Immutable JSON evidence with a disposable SQLite query index."""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from datetime import datetime, timezone

from factorlab.dataset.identity import json_hash, file_hash, check_identity

SAFETY = ["NO LIVE MONEY", "PIT_UNVERIFIED", "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED"]
STATES = {"NOT_RUN", "UNKNOWN", "RUNNING", "SUCCEEDED", "FAILED"}


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid record ID")
    return value


def _json(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _definition(value):
    value = _json(value)
    required = {"model", "parameters", "factors", "dataset", "label", "windows", "walk_forward", "costs"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Experiment definition fields must be: " + str(sorted(required)))
    if not isinstance(value["model"], str) or not value["model"]:
        raise ValueError("Model name required")
    for key in ("parameters", "dataset", "label", "windows", "walk_forward"):
        if not isinstance(value[key], dict):
            raise ValueError(key + " must be an object")
    if "identity_hash" in value["dataset"]:
        check_identity(value["dataset"])
    if value["costs"] is not None and not isinstance(value["costs"], dict):
        raise ValueError("costs must be an object or null")
    if not isinstance(value["factors"], list):
        raise ValueError("factors must be a list of pinned definitions")
    for factor in value["factors"]:
        if not isinstance(factor, dict) or not all(factor.get(k) for k in ("factor_id", "version", "definition_hash")):
            raise ValueError("Factor ID, version and definition_hash required")
    if set(value["windows"]) != {"train", "validation", "test"}:
        raise ValueError("Explicit train/validation/test windows required")
    for window in value["windows"].values():
        if window is not None:
            if not isinstance(window, list) or len(window) != 2:
                raise ValueError("Window must be null or [start, end]")
            start, end = (datetime.fromisoformat(d) for d in window)
            if start > end:
                raise ValueError("Reversed window")
    return value


def _run(value):
    value = _json(value)
    if not isinstance(value, dict) or set(value) != {"status", "test_status", "qualification", "code", "environment", "command", "validation", "prediction_metrics", "portfolio_metrics", "artifacts", "timestamp"}:
        raise ValueError("Invalid run fields")
    if value["status"] not in STATES or value["test_status"] not in STATES:
        raise ValueError("Invalid execution/test status")
    if value["qualification"] not in {"UNKNOWN", "SYNTHETIC", "REAL_CANDIDATE"}:
        raise ValueError("Formal VERIFIED admission is unavailable; evidence is not authorization")
    for key in ("code", "environment", "validation", "prediction_metrics", "portfolio_metrics", "artifacts"):
        if not isinstance(value[key], dict):
            raise ValueError(key + " must be an object")
    for field in ("commit", "tree"):
        identity = value["code"].get(field)
        if field not in value["code"] or (identity is not None and (not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", identity))):
            raise ValueError("Explicit code commit/tree hashes or null required")
    if not isinstance(value["command"], list) or not all(isinstance(s, str) for s in value["command"]):
        raise ValueError("command must be an argument list")
    if datetime.fromisoformat(value["timestamp"]).tzinfo is None:
        raise ValueError("Timestamp requires timezone")
    for group in ("prediction_metrics", "portfolio_metrics"):
        for name, metric in value[group].items():
            if not isinstance(name, str) or (metric is not None and (isinstance(metric, bool) or not isinstance(metric, (int, float)) or not math.isfinite(metric))):
                raise ValueError("Metrics must be finite numbers or null")
    if value["status"] == "NOT_RUN" and any(value[k] for k in ("prediction_metrics", "portfolio_metrics")):
        raise ValueError("NOT_RUN cannot contain metrics")
    for artifact in value["artifacts"].values():
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise ValueError("Artifacts require paths")
    return value


class ExperimentTracker:
    """Keep a private local store. JSON records are authoritative, never overwritten.

    IDs contain ASCII letters, digits, underscores or hyphens. Artifact paths are
    absolute; checksums are integrity receipts, not financial qualifications.
    """

    def __init__(self, root):
        self.root = Path(root).resolve()
        for name in ("experiments", "runs"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.index = self.root / "index.sqlite3"
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, experiment_id TEXT, payload TEXT, PRIMARY KEY(kind,id))")

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.index)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _path(self, kind, identifier):
        return self.root / kind / (_id(identifier) + ".json")

    def _validate(self, record):
        if not isinstance(record, dict) or set(record) != {"schema", "kind", "id", "experiment_id", "payload", "sha256"}:
            raise ValueError("Invalid record envelope")
        if record["schema"] != "FACTORLAB_EXPERIMENT_V1" or record["kind"] not in {"experiments", "runs"}:
            raise ValueError("Invalid record schema")
        _id(record["id"])
        _id(record["experiment_id"])
        raw = {k: v for k, v in record.items() if k != "sha256"}
        if json_hash(raw) != record["sha256"]:
            raise ValueError("Record checksum mismatch")
        if record["kind"] == "experiments":
            if record["id"] != record["experiment_id"]:
                raise ValueError("Experiment identity mismatch")
            _definition(record["payload"])
        else:
            payload = record["payload"]
            if set(payload) != {"definition_hash", "run"}:
                raise ValueError("Invalid run binding")
            _run(payload["run"])
        return record

    def _load(self, kind, identifier):
        record = self._validate(json.loads(self._path(kind, identifier).read_text(encoding="utf-8")))
        if record["kind"] != kind or record["id"] != identifier:
            raise ValueError("Record filename identity mismatch")
        return record

    def _index(self, db, record):
        db.execute("INSERT INTO records VALUES (?,?,?,?)", (record["kind"], record["id"], record["experiment_id"], json.dumps(record, allow_nan=False)))

    def _publish(self, kind, identifier, experiment_id, payload):
        record = {"schema": "FACTORLAB_EXPERIMENT_V1", "kind": kind, "id": _id(identifier), "experiment_id": _id(experiment_id), "payload": _json(payload)}
        record["sha256"] = json_hash(record)
        self._validate(record)
        destination = self._path(kind, identifier)
        fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, allow_nan=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            # Hard-link publication is atomic and fails if the ID already exists.
            os.link(temporary, destination)
        finally:
            os.unlink(temporary)
        with self._db() as db:
            self._index(db, record)
        return record

    def create_experiment(self, experiment_id, definition):
        return self._publish("experiments", experiment_id, experiment_id, _definition(definition))

    def get_experiment(self, experiment_id):
        return self._load("experiments", experiment_id)

    def append_run(self, experiment_id, run_id, *, code, environment, command, status="UNKNOWN", test_status="NOT_RUN", qualification="UNKNOWN", prediction_metrics=None, portfolio_metrics=None, artifacts=None, validation=None, timestamp=None):
        definition = self.get_experiment(experiment_id)
        run = dict(code=code, environment=environment, command=command, status=status, test_status=test_status, qualification=qualification, prediction_metrics=prediction_metrics or {}, portfolio_metrics=portfolio_metrics or {}, artifacts=artifacts or {}, validation=validation or {}, timestamp=timestamp or datetime.now(timezone.utc).isoformat())
        return self._publish("runs", run_id, experiment_id, {"definition_hash": definition["sha256"], "run": _run(run)})

    def get_run(self, run_id):
        record = self._load("runs", run_id)
        if record["payload"]["definition_hash"] != self.get_experiment(record["experiment_id"])["sha256"]:
            raise ValueError("Run experiment binding mismatch")
        return record

    def rebuild_index(self):
        records = []
        for kind in ("experiments", "runs"):
            for path in sorted((self.root / kind).glob("*.json")):
                records.append(self.get_experiment(path.stem) if kind == "experiments" else self.get_run(path.stem))
        with self._db() as db:
            db.execute("DELETE FROM records")
            for record in records:
                self._index(db, record)
        return len(records)

    def query(self, *, model=None, factor=None, windows=None, dataset=None, status=None, qualification=None):
        with self._db() as db:
            identifiers = db.execute("SELECT id FROM records WHERE kind='runs' ORDER BY id").fetchall()
        result = []
        for (identifier,) in identifiers:
            record = self.get_run(identifier)
            definition = self.get_experiment(record["experiment_id"])["payload"]
            run = record["payload"]["run"]
            if model is not None and definition["model"] != model:
                continue
            if factor is not None and factor not in [f["factor_id"] for f in definition["factors"]]:
                continue
            if windows is not None and any(definition["windows"].get(k) != v for k, v in windows.items()):
                continue
            if dataset is not None and definition["dataset"] != dataset:
                continue
            if status is not None and run["status"] != status:
                continue
            if qualification is not None and run["qualification"] != qualification:
                continue
            result.append(record)
        return result

    def compare(self, run_ids):
        if len(run_ids) < 2:
            raise ValueError("At least two runs required")
        rows, bindings = [], []
        for identifier in run_ids:
            record = self.get_run(identifier)
            definition = self.get_experiment(record["experiment_id"])["payload"]
            binding = {k: definition[k] for k in ("dataset", "label", "costs", "walk_forward")}
            binding["test"] = definition["windows"]["test"]
            if any(v is None or v == {} or v == "UNKNOWN" for v in binding.values()):
                raise ValueError("Unknown comparison identity")
            run = record["payload"]["run"]
            if run["status"] != "SUCCEEDED":
                raise ValueError("Only successful runs are comparable")
            if run["qualification"] == "UNKNOWN":
                raise ValueError("Unknown evidence qualification")
            binding["qualification"] = run["qualification"]
            binding["sample_hash"] = run["validation"].get("sample_hash")
            binding["evaluation"] = run["validation"].get("evaluation")
            if not binding["sample_hash"] or not binding["evaluation"]:
                raise ValueError("Sample and evaluation identities required for comparison")
            bindings.append(binding)
            rows.append({"run_id": identifier, "model": definition["model"], "qualification": run["qualification"], "prediction_metrics": run["prediction_metrics"], "portfolio_metrics": run["portfolio_metrics"]})
        if any(b != bindings[0] for b in bindings[1:]):
            raise ValueError("Incomparable dataset, Test window, label, walk-forward or costs")
        return {"comparable": True, "binding": bindings[0], "rows": rows, "safety": SAFETY.copy()}

    def reproduction(self, run_id, *, expected_code=None, expected_dataset=None, verify_artifacts=False):
        record = self.get_run(run_id)
        definition = self.get_experiment(record["experiment_id"])["payload"]
        run = record["payload"]["run"]
        if expected_code is not None and run["code"] != expected_code:
            raise ValueError("Code identity mismatch")
        if expected_dataset is not None and definition["dataset"] != expected_dataset:
            raise ValueError("Data identity mismatch")
        if verify_artifacts:
            for artifact in run["artifacts"].values():
                path = Path(artifact["path"])
                if not path.is_absolute() or not artifact.get("sha256"):
                    raise ValueError("Absolute artifact path and checksum required")
                if file_hash(path) != artifact["sha256"]:
                    raise ValueError("Artifact checksum mismatch")
        return {"definition": definition, "run": run, "safety": SAFETY.copy()}
