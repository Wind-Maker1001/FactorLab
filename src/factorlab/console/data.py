"""Explicit public-evidence catalog and read-only ExperimentTracker adapter."""

from contextlib import contextmanager
import csv
import hashlib
import io
import json
from pathlib import Path, PureWindowsPath
import re
import sqlite3

from factorlab.experiments.tracker import ExperimentTracker, SAFETY

MAX_BYTES = 8 * 1024 * 1024
STATES = {s: s for s in (
    "SOFTWARE_ACCEPTED", "RESEARCH_COMPLETED", "PARTIAL", "BLOCKED", "NOT_RUN", "UNKNOWN")}
STATES.update({
    "P6_R01_REAL_ROBUSTNESS_COMPLETED": "RESEARCH_COMPLETED",
    "P6_D01_RESEARCH_INPUT_READY": "PARTIAL",
    "P6_E01_BLOCKED_EVIDENCE": "BLOCKED",
})
SECRET = re.compile(r"password|passwd|secret|token|credential|api[_-]?key|private[_-]?key", re.I)


def public(value):
    """Remove credential fields and machine-local absolute paths from JSON views."""
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SECRET.search(k) else public(v) for k, v in value.items()}
    if isinstance(value, list):
        return [public(v) for v in value]
    if isinstance(value, str) and (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        return "[LOCAL_PATH]"
    return value


def status(raw):
    return {"status": STATES.get(raw, "UNKNOWN"), "raw_status": raw or "UNKNOWN"}


def within(path, root):
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("Resource outside allowed root")
    return resolved


def read_bytes(path, root):
    # Re-resolve at access time: a symlink introduced after discovery must not escape.
    resolved = within(path, root)
    with resolved.open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("Resource exceeds read limit")
    return content


class ReadOnlyTracker(ExperimentTracker):
    """Reuse tracker validation/query/compare without creating files or directories.

    The disposable query index lives in memory and is rebuilt from authoritative
    JSON; the existing on-disk index is neither trusted nor opened.
    """

    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE records (kind TEXT, id TEXT, experiment_id TEXT, payload TEXT, PRIMARY KEY(kind,id))")
        self.allowed = {}
        for kind in ("experiments", "runs"):
            folder = self.root / kind
            if not folder.exists():
                continue
            if not folder.resolve().is_relative_to(self.root):
                raise ValueError("Tracker directory outside allowed root")
            for path in sorted(folder.glob("*.json")):
                self.allowed[kind, path.stem] = path
        for kind, identifier in self.allowed:
            record = self.get_experiment(identifier) if kind == "experiments" else self.get_run(identifier)
            self._index(self.connection, record)

    @contextmanager
    def _db(self):
        yield self.connection

    def _load(self, kind, identifier):
        # Keep ID validation from the tracker even for programmatic callers.
        self._path(kind, identifier)
        path = self.allowed[kind, identifier]
        record = self._validate(json.loads(read_bytes(path, self.root)))
        if record["kind"] != kind or record["id"] != identifier:
            raise ValueError("Record filename identity mismatch")
        return record

    def close(self):
        self.connection.close()


class Catalog:
    def __init__(self, config):
        config = Path(config).resolve(strict=True)
        settings = json.loads(config.read_text(encoding="utf-8"))
        self.projects = {}
        self.resources = {}
        self.stores = {}
        self.milestones = settings.get("milestones", [])
        for project in settings["projects"]:
            identifier = project["id"]
            if not re.fullmatch(r"[a-z0-9-]+", identifier) or identifier in self.projects:
                raise ValueError("Invalid or duplicate project ID")
            root = (config.parent / project["root"]).resolve(strict=True)
            files = {}
            for name in project["files"]:
                if Path(name).name != name or PureWindowsPath(name).name != name:
                    raise ValueError("Files must be explicit basenames")
                path = root / name
                # Missing configured evidence is displayed, not synthesized.
                if path.exists():
                    within(path, root)
                key = identifier + ":" + name
                self.resources[key] = (path, root)
                files[name] = key
            self.projects[identifier] = {"id": identifier, "title": project["title"], "files": files}
        for store in settings.get("trackers", []):
            identifier = store["id"]
            if not re.fullmatch(r"[a-z0-9-]+", identifier) or identifier in self.stores:
                raise ValueError("Invalid or duplicate tracker ID")
            self.stores[identifier] = {
                "root": (config.parent / store["root"]).resolve(strict=True),
                "project": store.get("project", "UNKNOWN"),
                "strategy": store.get("strategy", "UNKNOWN"),
            }

    def content(self, key):
        path, root = self.resources[key]
        return read_bytes(path, root)

    def json(self, project, name):
        return json.loads(self.content(project + ":" + name))

    def optional_json(self, project, name):
        key = project + ":" + name
        if key not in self.resources or not self.resources[key][0].exists():
            return None
        return self.json(project, name)

    def report(self, key):
        suffix = Path(self.resources[key][0]).suffix.lower()
        types = {".md": "text/plain; charset=utf-8", ".csv": "text/plain; charset=utf-8", ".png": "image/png"}
        if suffix not in types:
            raise ValueError("Not a public report type")
        return self.content(key), types[suffix]

    def table(self, key, filters, offset=0, limit=50):
        if not key.endswith(".csv"):
            raise ValueError("Not a table")
        rows = csv.DictReader(io.StringIO(self.content(key).decode("utf-8-sig")))
        columns = rows.fieldnames or []
        if any(k not in columns for k in filters):
            raise ValueError("Unknown filter column")
        result, total, options = [], 0, {}
        for row in rows:
            for column in ("strategy", "year", "symbol", "industry", "omitted_symbol"):
                if column in row:
                    options.setdefault(column, set()).add(row[column])
            if any(row[k] != v for k, v in filters.items() if v):
                continue
            if offset <= total < offset + limit:
                # CSV empty cells mean missing; a numeric zero remains "0".
                result.append({k: v if v != "" else None for k, v in row.items()})
            total += 1
        return {"columns": columns, "rows": result, "total": total, "offset": offset,
                "limit": limit, "options": {k: sorted(v) for k, v in options.items()}, "source": key}

    def overview(self):
        projects = []
        for project in self.projects.values():
            identifier = project["id"]
            names = {"p6-r01": "real-summary.json", "p6-d01": "manifest.json", "p6-e01": "receipt.json"}
            source = names.get(identifier)
            evidence = self.optional_json(identifier, source) if source else None
            reports, missing = [], []
            for name, key in project["files"].items():
                if not self.resources[key][0].exists():
                    missing.append(name)
                elif Path(name).suffix in {".md", ".png", ".csv"}:
                    reports.append({"name": name, "id": key})
            projects.append({"id": identifier, "title": project["title"],
                             **status(evidence.get("status") if evidence else None),
                             "status_source": source or "UNKNOWN", "reports": reports,
                             "missing": missing})
        return {"projects": projects, "safety": SAFETY, "milestones": public(self.milestones)}

    def evidence(self, project):
        if project == "p6-r01":
            data = self.optional_json(project, "real-summary.json")
            fields = ("status", "scope", "return_basis", "total_return", "total_return_status",
                      "source_manifest_sha256", "real_accounts_run", "securities", "years", "frozen_sample",
                      "code", "environment", "strategy_parameters", "observations")
            return {k: public(data.get(k)) for k in fields} if data else {"status": "UNKNOWN"}
        if project == "p6-d01":
            quality = self.optional_json(project, "quality.json")
            manifest = self.optional_json(project, "manifest.json")
            if not manifest:
                return {"status": "UNKNOWN", "quality": quality}
            content = self.content(project + ":manifest.json")
            return {"quality": quality, "manifest_sha256": hashlib.sha256(content).hexdigest(),
                    **{k: manifest.get(k) for k in ("status", "historical_pit", "hfq_verified", "formal_admission",
                                                    "prepared_at", "bars_sha256", "source_manifest_sha256")}}
        if project == "p6-e01":
            receipt = self.optional_json(project, "receipt.json")
            fields = ("status", "total_return_status", "total_return", "evidence_sha256", "blockers", "strategies")
            return {k: public(receipt.get(k)) for k in fields} if receipt else {"status": "UNKNOWN"}
        raise KeyError(project)

    @contextmanager
    def tracker(self, identifier):
        tracker = ReadOnlyTracker(self.stores[identifier]["root"])
        try:
            yield tracker
        finally:
            tracker.close()

    def experiments(self, filters, offset=0, limit=50):
        entries, count, definitions = [], 0, 0
        for store_id, store in self.stores.items():
            if any(filters.get(key) and filters[key] != store[key] for key in ("project", "strategy")):
                continue
            with self.tracker(store_id) as tracker:
                definitions += sum(kind == "experiments" for kind, _ in tracker.allowed)
                for record in tracker.query(model=filters.get("model") or None, status=filters.get("execution_status") or None):
                    run = record["payload"]["run"]
                    definition = tracker.get_experiment(record["experiment_id"])["payload"]
                    entry = {"store": store_id, "id": record["id"], "experiment_id": record["experiment_id"],
                             "project": store["project"], "strategy": store["strategy"],
                             "model": definition["model"], "date": run["timestamp"],
                             **status(run["validation"].get("research_status")),
                             "execution_status": run["status"], "sha256": record["sha256"]}
                    if any(entry.get(k) != v for k, v in filters.items()
                           if v and k not in {"date", "execution_status"}):
                        continue
                    if filters.get("date") and not run["timestamp"].startswith(filters["date"]):
                        continue
                    entries.append(entry)
                    count += 1
        entries.sort(key=lambda row: row["date"], reverse=True)
        return {"rows": entries[offset:offset + limit], "total": count,
                "experiment_count": definitions, "offset": offset, "limit": limit}

    def detail(self, store, identifier):
        with self.tracker(store) as tracker:
            reproduction = tracker.reproduction(identifier, verify_artifacts=False)
            record = tracker.get_run(identifier)
            result = public(reproduction)
            # Saved commands and full process environments may carry credentials.
            result["run"]["command"] = "[LOCAL_COMMAND_REDACTED]"
            environment_keys = {"python", "python_version", "platform", "numpy", "pandas", "scipy", "pyarrow", "sklearn"}
            result["run"]["environment"] = {k: v for k, v in result["run"]["environment"].items() if k in environment_keys}
            result["record_sha256"] = record["sha256"]
            result["artifact_verification"] = "NOT_RUN — external artifact paths are not followed"
            return result

    def compare(self, store, identifiers):
        with self.tracker(store) as tracker:
            return public(tracker.compare(identifiers))
