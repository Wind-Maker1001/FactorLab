"""Bounded full-history execution and atomic, verified date partitions.

Only the registry's closed stock-local builtin/AST language is supported.
Cross-sectional preprocessing happens after ALL security shards are gathered.
Receipts are checksums, not provenance or financial admission certificates.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from factorlab.dataset.identity import (
    IDENTITY_ATTR, check_identity, file_hash, frame_hash, seal,
)
from factorlab.errors import ConfigurationError, SchemaError
from factorlab.exploratory.factor_discovery.contracts import DiscoveryConfig
from factorlab.exploratory.factor_discovery.engine import ExpressionEngine
from factorlab.factors.registry_v2 import (
    FactorDataContext, _BUILTINS, _decode_expression, _prepare_panel,
)
from factorlab.factors.preprocess import preprocess_cross_sections
from factorlab.labels.forward_return import build_forward_excess_return_labels
from factorlab.market.regime import MARKET_FEATURE_COLUMNS, build_market_regime_features
from factorlab.universe.csi500 import validate_daily_membership
from factorlab.universe.filters import build_filter_flags


_STOCK_LOCAL_OPERATORS = frozenset((
    "Field", "Const", "Lag", "Delta", "RollingMean", "RollingStd",
    "Add", "Sub", "Mul", "SafeDiv",
))


@dataclass(frozen=True)
class ChunkedConfig:
    symbols_per_batch: int = 32
    factors_per_batch: int = 16
    dates_per_batch: int = 20
    workers: int = 2
    max_factors: int = 300
    timeout_seconds: float = 14400
    memory_limit_bytes: int = 5 * 1024**3
    disk_limit_bytes: int = 8 * 1024**3
    minimum_available_memory_bytes: int = 1024**3
    minimum_free_disk_bytes: int = 1024**3

    def __post_init__(self):
        for name in ("symbols_per_batch", "factors_per_batch", "dates_per_batch",
                     "workers", "max_factors", "memory_limit_bytes", "disk_limit_bytes",
                     "minimum_available_memory_bytes", "minimum_free_disk_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ConfigurationError("Positive integer required: " + name)
        if self.workers > 4 or self.max_factors > 300:
            raise ConfigurationError("Chunked limits: 1..4 workers, <=300 requested factors")
        if (type(self.timeout_seconds) not in (int, float)
                or not np.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ConfigurationError("Positive finite timeout required")


class ResourceLimitError(RuntimeError):
    pass


def _require_psutil():
    try:
        import psutil
    except ModuleNotFoundError as exc:
        if exc.name != "psutil":
            raise
        raise ConfigurationError(
            "Chunked computation/resource monitoring requires optional psutil. "
            "Install with: python -m pip install -r requirements/scalable-compute.txt"
        ) from exc
    return psutil


def tree_rss():
    psutil = _require_psutil()
    process = psutil.Process()
    return sum(p.memory_info().rss for p in [process] + process.children(recursive=True)
               if p.is_running())


def directory_bytes(path):
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except FileNotFoundError:
            # A completed temporary shard may be removed during sampling.
            pass
    return total


class _Guard:
    """Cooperative 100ms checks; external benchmark supervisor also enforces caps."""

    def __init__(self, config, path):
        self.config, self.path = config, path
        self.started = time.perf_counter()
        self.error = None
        self.peak_rss = self.peak_disk = 0
        self.stop = threading.Event()

    def sample(self):
        cfg = self.config
        rss, disk = tree_rss(), directory_bytes(self.path)
        self.peak_rss, self.peak_disk = max(self.peak_rss, rss), max(self.peak_disk, disk)
        if time.perf_counter() - self.started > cfg.timeout_seconds:
            self.error = "TIMEOUT"
        elif rss > cfg.memory_limit_bytes:
            self.error = "MEMORY_LIMIT"
        elif disk > cfg.disk_limit_bytes:
            self.error = "DISK_LIMIT"
        elif shutil.disk_usage(self.path).free < cfg.minimum_free_disk_bytes:
            self.error = "DISK_FREE_LIMIT"

    def check(self):
        if self.error:
            raise ResourceLimitError(self.error)

    def __enter__(self):
        self.sample()
        self.check()

        def poll():
            while not self.stop.wait(0.1):
                try:
                    self.sample()
                except OSError as exc:
                    self.error = str(exc)
        self.thread = threading.Thread(target=poll, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()


def _keys(frame, calendar, name):
    if (not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique
            or any(type(n) is not str for n in frame.columns)
            or not {"date", "symbol"}.issubset(frame)):
        raise SchemaError("Invalid keyed input: " + name)
    dates = pd.to_datetime(frame.date, errors="raise")
    if (pd.api.types.is_numeric_dtype(frame.date.dtype) or dates.dt.tz is not None
            or dates.isna().any() or not dates.equals(dates.dt.normalize())
            or not dates.isin(calendar).all()
            or frame.symbol.map(lambda s: type(s) is not str or not s.strip()).any()
            or frame.duplicated(["date", "symbol"]).any()):
        raise SchemaError("Invalid/duplicate daily primary keys: " + name)


def _roots(snapshot, refs):
    resolved = snapshot.resolve(refs)
    roots = {}
    unique = {}
    for definition in resolved:
        # Registration accepts only these independent stock execution types.
        if definition.kind == "alias":
            root = roots[definition.dependencies[0]]
        elif definition.kind in ("builtin", "expression"):
            if definition.kind == "expression":
                pending = [_decode_expression(definition.expression_json).node]
                while pending:
                    node = pending.pop()
                    if node.op not in _STOCK_LOCAL_OPERATORS:
                        raise ConfigurationError("Expression requires a different execution type")
                    pending.extend(node.args)
            root = unique.setdefault(definition.definition_hash, definition)
        else:
            raise ConfigurationError("Unsupported stock-local execution type")
        roots[definition.ref] = root
    return resolved, roots, tuple(dict.fromkeys(roots[r] for r in refs))


def _factor_batches(snapshot, refs, bars, dates, cfg, guard, context):
    resolved, roots, unique = _roots(snapshot, refs)
    panel, positions, keys = _prepare_panel(bars, resolved, dates)
    # Prepare once per security group, never once per factor.
    for start in range(0, len(unique), cfg.factors_per_batch):
        guard.check()
        values, engine, estimated_operations = {}, None, 0
        for definition in unique[start:start + cfg.factors_per_batch]:
            guard.check()
            try:
                if definition.kind == "builtin":
                    value = _BUILTINS[definition.implementation.removeprefix("phase1-v1:")]()._compute_ordered(panel)
                else:
                    compiled = _decode_expression(definition.expression_json)
                    budget = DiscoveryConfig()
                    # Conservative sub-batches retain all original interpreter limits.
                    # Release the bounded engine when a new expression could exhaust it.
                    if engine is not None:
                        cost = engine._estimate(compiled.node)
                        if (estimated_operations + cost > budget.max_cell_operations
                                or engine._cache_bytes + compiled.complexity["nodes"] * len(panel) * 8 > budget.max_cache_bytes
                                or engine._total_seconds > budget.max_total_seconds / 2):
                            engine = None
                            estimated_operations = 0
                    if engine is None:
                        engine = ExpressionEngine(panel, dates, config=budget)
                    estimated_operations += engine._estimate(compiled.node)
                    result = engine.compute(compiled)
                    if result.status != "SUCCESS":
                        raise ConfigurationError("Expression execution failed: " + result.status)
                    value = result.values
                values[definition] = value.to_numpy(dtype=float, na_value=np.nan)[positions]
            except Exception as exc:
                raise ConfigurationError(
                    f"Factor batch {start}, factor {definition.ref}: {exc}") from exc
        yield pd.DataFrame({**{n: keys[n].to_numpy() for n in ("date", "symbol")},
                            **{r.factor_id: values[roots[r]] for r in refs if roots[r] in values}})


def _schema(frame):
    import pyarrow as pa
    # Ignore pandas attrs in Arrow metadata: manifest carries the explicit identity.
    schema = pa.Table.from_pandas(frame, preserve_index=False).schema.remove_metadata()
    return [[f.name, str(f.type)] for f in schema]


def _record(path, frame):
    return {"file": path.name, "sha256": file_hash(path), "rows": len(frame),
            "content_hash": frame_hash(frame), "keys_hash": frame_hash(frame[["date", "symbol"]]),
            "date_start": frame.date.min().isoformat(), "date_end": frame.date.max().isoformat()}


class PartitionedDataset:
    """Verify manifest and inventory, then stream complete date cross-sections.

    to_frame is an explicit in-memory adapter for existing research consumers.
    Pin identity_hash outside this directory when requiring a trusted receipt.
    """

    def __init__(self, path, *, expected_binding=None, expected_identity_hash=None,
                 expected_input_hashes=None):
        self.path = Path(path)
        try:
            self.manifest = json.loads((self.path / "manifest.json").read_text(encoding="utf-8"))
            payload = check_identity(self.manifest)
            if payload["format"] != "FACTORLAB_PARTITIONED_V1":
                raise SchemaError("Unsupported partition format")
            if expected_identity_hash is not None and self.identity_hash != expected_identity_hash:
                raise SchemaError("Unexpected partition identity hash")
            if expected_binding and any(payload["binding"].get(k) != v for k, v in expected_binding.items()):
                raise SchemaError("Partition dataset binding mismatch")
            if expected_input_hashes is not None and payload["input_hashes"] != expected_input_hashes:
                raise SchemaError("Partition input hashes mismatch")
            records = payload["partitions"]
            files = [r["file"] for r in records]
            if (not records or len(set(files)) != len(files)
                    or any(Path(f).name != f or not f.endswith(".parquet") for f in files)
                    or set(files) != {p.name for p in self.path.iterdir() if p.name != "manifest.json"}):
                raise SchemaError("Missing/extra/duplicate partition inventory")
            previous = None
            for record in records:
                if (record["rows"] < 1 or record["date_start"] > record["date_end"]
                        or (previous is not None and record["date_start"] <= previous)):
                    raise SchemaError("Overlapping or invalid partition date ranges")
                previous = record["date_end"]
                if file_hash(self.path / record["file"]) != record["sha256"]:
                    raise SchemaError("Partition checksum mismatch")
            if (sum(r["rows"] for r in records) != payload["rows"]
                    or payload["rows"] != payload.get("expected_rows", payload["rows"])):
                raise SchemaError("Partition row inventory mismatch")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SchemaError("Invalid/incomplete partition dataset: " + str(exc)) from exc

    @property
    def identity_hash(self):
        return self.manifest["identity_hash"]

    def iter_partitions(self):
        for record in self.manifest["partitions"]:
            path = self.path / record["file"]
            if file_hash(path) != record["sha256"]:
                raise SchemaError("Partition changed after opening")
            frame = pd.read_parquet(path)
            _keys(frame, frame.date.unique(), "partition")
            if (len(frame) != record["rows"] or _schema(frame) != self.manifest["schema"]
                    or frame_hash(frame) != record["content_hash"]
                    or frame_hash(frame[["date", "symbol"]]) != record["keys_hash"]
                    or frame.duplicated(["date", "symbol"]).any()
                    or frame.date.min().isoformat() != record["date_start"]
                    or frame.date.max().isoformat() != record["date_end"]
                    or not frame.equals(frame.sort_values(["date", "symbol"]).reset_index(drop=True))):
                raise SchemaError("Partition schema/content/primary key mismatch")
            yield frame

    def verify(self):
        for _ in self.iter_partitions():
            pass
        return self

    def to_frame(self):
        result = pd.concat(self.iter_partitions(), ignore_index=True)
        if self.manifest["kind"] == "dataset":
            result.attrs[IDENTITY_ATTR] = seal({
                **self.manifest["binding"], "input_hashes": self.manifest["input_hashes"],
                "research_interval": self.manifest["research_interval"],
                "dataset_content_hash": frame_hash(result),
            })
        else:
            result.attrs["factor_registry_v2"] = {
                k: v for k, v in self.manifest["binding"].items() if k != "calendar"}
        return result


def build_partitioned(snapshot, refs, bars, calendar, output_dir, *, context,
                      config=None, builder=None, benchmark=None, membership=None,
                      status=None, listings=None):
    psutil = _require_psutil()
    from factorlab.dataset.builder import _full_bar_grid, DatasetBuilder
    cfg = config or ChunkedConfig()
    if type(cfg) is not ChunkedConfig:
        raise ConfigurationError("ChunkedConfig required")
    cfg.__post_init__()
    resolved, roots, unique = _roots(snapshot, refs)
    if not refs or len(refs) > cfg.max_factors:
        raise ConfigurationError("Nonempty bounded FactorRefs required")
    if type(context) is not FactorDataContext or context != FactorDataContext(context.data_source, context.availability_evidence):
        raise ConfigurationError("Unmodified FactorDataContext required")
    dates = DatasetBuilder._v2_calendar(calendar)
    _keys(bars, dates, "bars")
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError("Refusing to overwrite research output: " + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    if psutil.virtual_memory().available < cfg.minimum_available_memory_bytes:
        raise ResourceLimitError("INSUFFICIENT_AVAILABLE_MEMORY")
    if shutil.disk_usage(output.parent).free < cfg.minimum_free_disk_bytes:
        raise ResourceLimitError("INSUFFICIENT_FREE_DISK")
    if len(dates) * cfg.symbols_per_batch > 250000:
        raise ConfigurationError("Reduce symbols_per_batch to respect the dense interpreter limit")
    symbols = sorted(bars.symbol.unique())
    research_dates, members, market = dates, None, None
    input_frames = {"bars": bars}
    names = [r.factor_id for r in refs]
    if builder is not None:
        for name, frame in (("benchmark", benchmark), ("membership", membership),
                            ("status", status), ("listings", listings)):
            if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
                raise SchemaError("Unique input columns required: " + name)
            input_frames[name] = frame
        _keys(membership, dates, "membership")
        _keys(status, dates, "status")
        if not {"symbol", "listing_date"}.issubset(listings) or listings.symbol.duplicated().any():
            raise SchemaError("Listings require unique symbols and listing_date")
        forbidden = set(names) | {"raw_" + n for n in names} | {"entry_date", "exit_date"} | (set(MARKET_FEATURE_COLUMNS) - {"date"})
        if any(n in forbidden or n.startswith(("EXCESS_RET", "STOCK_FWD_RET", "BENCHMARK_FWD_RET")) for n in bars):
            raise SchemaError("Bars contain protected factor/label/market output fields")
        if membership.empty:
            raise SchemaError("PIT membership calendar must not be empty")
        membership_dates = pd.to_datetime(membership.date)
        research_dates = dates[(dates >= membership_dates.min()) & (dates <= membership_dates.max())]
        members = validate_daily_membership(membership, requested_dates=research_dates,
                                            expected_member_count=builder.options.expected_member_count)
        symbols = sorted(members.symbol.unique())
        if builder.options.include_market_regime:
            market = build_market_regime_features(benchmark)
            if len(research_dates.difference(pd.DatetimeIndex(market.date))):
                raise SchemaError("Benchmark market features missing research dates")
    binding = (builder._binding(dates) if builder else {
        "snapshot_hash": snapshot.snapshot_hash, "factors": [asdict(r) for r in refs],
        "data_context": asdict(context), "registration_status": "SOFTWARE_VALIDATED_ONLY",
        "calendar": [d.isoformat() for d in dates],
    })
    staging = output.with_name(output.name + ".pending-" + uuid.uuid4().hex)
    staging.mkdir()
    shards = staging / "shards"
    shards.mkdir()
    failed_task = None
    try:
        with _Guard(cfg, staging) as guard:
            input_hashes = {name: frame_hash(frame) for name, frame in input_frames.items()}

            def security_group(number, group):
                guard.check()
                source = bars.loc[bars.symbol.isin(group)].copy()
                source["date"] = pd.to_datetime(source.date)
                full = _full_bar_grid(source, dates, group) if builder else source
                if builder:
                    flags = build_filter_flags(full, status.loc[status.symbol.isin(group)],
                                               listings.loc[listings.symbol.isin(group)], dates,
                                               builder.options.filter_config)
                    eligible = members.loc[members.symbol.isin(group), ["date", "symbol"]].merge(
                        flags, on=["date", "symbol"], validate="one_to_one")
                    if eligible.eligible.isna().any():
                        raise SchemaError("Missing eligibility flags")
                    base = eligible.loc[eligible.eligible, ["date", "symbol"]].merge(
                        full, on=["date", "symbol"], validate="one_to_one")
                    labels = build_forward_excess_return_labels(
                        full, benchmark, holding_days=builder.options.holding_days,
                        entry_price=builder.options.entry_price, calendar=dates)
                else:
                    base = full[["date", "symbol"]].sort_values(["date", "symbol"]).reset_index(drop=True)
                parts = []
                for batch in _factor_batches(snapshot, refs, full, dates, cfg, guard, context):
                    parts.append(batch.set_index(["date", "symbol"]))
                factors = pd.concat(parts, axis=1).reset_index()[["date", "symbol"] + names]
                base = base.merge(factors, on=["date", "symbol"], validate="one_to_one")
                if builder:
                    base = base.merge(labels, on=["date", "symbol"], validate="one_to_one")
                    if market is not None:
                        base = base.merge(market.loc[:, list(MARKET_FEATURE_COLUMNS)], on="date", validate="many_to_one")
                base = base.sort_values(["date", "symbol"]).reset_index(drop=True)
                path = shards / f"security-{number:05d}.parquet"
                # Sorted date row groups allow predicate pushdown at merge time.
                base.to_parquet(path, index=False, row_group_size=max(1, cfg.dates_per_batch * len(group)))
                guard.check()
                return (path, len(factors), int(np.isfinite(factors[names].to_numpy(dtype=float)).sum()),
                        len(base))

            groups = [symbols[i:i + cfg.symbols_per_batch] for i in range(0, len(symbols), cfg.symbols_per_batch)]
            paths, factor_rows, valid_values, expected_rows = [], 0, 0, 0
            with ThreadPoolExecutor(max_workers=cfg.workers) as executor:
                # Bounded submission: at most workers tasks, no unbounded queued frames.
                for start in range(0, len(groups), cfg.workers):
                    guard.check()
                    futures = [executor.submit(security_group, i, groups[i])
                               for i in range(start, min(start + cfg.workers, len(groups)))]
                    for offset, future in enumerate(futures):
                        failed_task = {"security_group": start + offset}
                        path, count, valid, output_rows = future.result()
                        paths.append(path)
                        factor_rows += count
                        valid_values += valid
                        expected_rows += output_rows
            records, schema = [], None
            for start in range(0, len(research_dates), cfg.dates_per_batch):
                failed_task = {"date_batch": start}
                guard.check()
                local_dates = research_dates[start:start + cfg.dates_per_batch]
                parts = [pd.read_parquet(p, filters=[("date", ">=", local_dates[0]),
                                                     ("date", "<=", local_dates[-1])]) for p in paths]
                frame = pd.concat(parts, ignore_index=True).sort_values(["date", "symbol"]).reset_index(drop=True)
                if frame.empty:
                    continue
                if frame.duplicated(["date", "symbol"]).any():
                    raise SchemaError("Duplicate primary keys during cross-section merge")
                if builder:
                    opt = builder.options
                    frame = preprocess_cross_sections(frame, names, method=opt.winsorization,
                        mad_multiplier=opt.mad_multiplier, lower_quantile=opt.lower_quantile,
                        upper_quantile=opt.upper_quantile, minimum_count=opt.minimum_cross_section)
                current_schema = _schema(frame)
                if schema is not None and current_schema != schema:
                    raise SchemaError("Partition schema drift")
                schema = current_schema
                path = staging / f"date-{start:06d}.parquet"
                frame.to_parquet(path, index=False)
                records.append(_record(path, frame))
                guard.sample()
                guard.check()
            if not records:
                raise SchemaError("Built dataset is empty after universe filters")
            if sum(r["rows"] for r in records) != expected_rows:
                raise SchemaError("Date merge did not preserve all security-shard rows")
            for path in paths:
                path.unlink()
            shards.rmdir()
            manifest = seal({
                "format": "FACTORLAB_PARTITIONED_V1", "kind": "dataset" if builder else "factors",
                "binding": binding, "input_hashes": input_hashes,
                "execution_types": {r.factor_id: "STOCK_LOCAL_FULL_HISTORY" for r in refs},
                "research_interval": {"start": research_dates.min().date().isoformat(),
                                      "end": research_dates.max().date().isoformat()},
                "schema": schema, "partitions": records, "rows": sum(r["rows"] for r in records),
                "expected_rows": expected_rows,
                "counts": {"registered_definitions": len(snapshot.definitions),
                           "requested_candidates": len(refs), "resolved_definitions": len(resolved),
                           "unique_executed_definitions": len(unique), "successful_factors": len(refs),
                           "selected_factors": None, "factor_rows": factor_rows,
                           "total_factor_cells": factor_rows * len(refs), "valid_factor_values": valid_values},
                "execution": asdict(cfg), "failures": 0, "retries": 0,
            })
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            # Full disk read, hashes, schema, keys and inventory before publication.
            PartitionedDataset(staging).verify()
            guard.sample()
            guard.check()
            # os.rename on Windows fails if destination exists; no evidence overwrite.
            os.rename(staging, output)
        return PartitionedDataset(output)
    except BaseException as exc:
        diagnostic = {"status": "FAILED", "error_type": type(exc).__name__, "error": str(exc),
                      "task": failed_task, "retries": 0, "published": False}
        # Only this invocation's UUID directory can be removed.
        if staging.parent == output.parent and staging.name.startswith(output.name + ".pending-"):
            try:
                shutil.rmtree(staging)
                diagnostic["temporary_cleanup"] = "OK"
            except OSError as cleanup:
                diagnostic["temporary_cleanup"] = str(cleanup)
        output.with_name(output.name + ".failure-" + uuid.uuid4().hex + ".json").write_text(
            json.dumps(diagnostic, indent=2) + "\n", encoding="utf-8")
        raise
