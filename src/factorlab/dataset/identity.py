"""Software/data consistency receipts, never financial certification.

The digest is an integrity checksum, not a signature. Retain its value outside
the artifact bundle when checking against a previously trusted research run.
"""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.errors import SchemaError


IDENTITY_ATTR = "dataset_registry_v2"


def json_hash(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def frame_hash(frame):
    """Exact values/schema/column order; independent of input row/index order."""
    if not frame.columns.is_unique:
        raise SchemaError("Identity inputs require unique columns")
    schema = [(str(name), str(dtype)) for name, dtype in frame.dtypes.items()]
    canonical = frame.copy()
    # Parquet preserves missingness, not the sign/payload bits of IEEE NaNs.
    for name in canonical.select_dtypes(include="floating"):
        values = canonical[name].to_numpy(copy=True)
        values[np.isnan(values)] = np.nan
        canonical[name] = values
    rows = pd.util.hash_pandas_object(canonical, index=False).to_numpy(dtype="<u8")
    rows.sort()
    return hashlib.sha256(bytes.fromhex(json_hash(schema)) + rows.tobytes()).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def calendar_identity(calendar, calendar_id):
    return {
        "id": calendar_id,
        "sessions": len(calendar),
        "sha256": json_hash([date.isoformat() for date in calendar]),
        "start": calendar[0].date().isoformat(),
        "end": calendar[-1].date().isoformat(),
    }


def binding(snapshot, refs, context, data_version, calendar, calendar_id, options):
    # Dependency definitions are included as well as the ordered output pins.
    return {
        "schema": "FACTORLAB_DATASET_REGISTRY_V2_1",
        "snapshot_hash": snapshot.snapshot_hash,
        "factors": [asdict(ref) for ref in refs],
        "resolved_definitions": [asdict(d) for d in snapshot.resolve(refs)],
        "data_context": asdict(context),
        "data_version": data_version,
        "calendar": calendar_identity(calendar, calendar_id) if calendar is not None else None,
        "options": json.loads(json.dumps(asdict(options))),
        "timing": {
            "factor_availability": "SESSION_CLOSE",
            "earliest_decision": "NEXT_SESSION_OPEN",
            "entry_price": "next_open",
            "label_price_basis": "HFQ",
            "window_policy": "EXPLICIT_CALENDAR_NO_FILL",
        },
        "registration_status": "SOFTWARE_VALIDATED_ONLY",
        "financial_status": "FINANCIAL_ADMISSION_BLOCKED",
    }


def seal(identity):
    return {**identity, "identity_hash": json_hash(identity)}


def check_identity(identity):
    if not isinstance(identity, dict):
        raise SchemaError("V2 dataset identity sidecar required")
    payload = {k: v for k, v in identity.items() if k != "identity_hash"}
    if identity.get("identity_hash") != json_hash(payload):
        raise SchemaError("Dataset identity checksum mismatch")
    return payload


def verify_dataset_artifacts(processed_path, features_dir, *, expected_binding,
                             expected_identity_hash=None, expected_input_hashes=None):
    """Read all three artifacts, checking pins, external calendar and checksums.

    Call through DatasetBuilder.read_artifacts to derive expected_binding from
    the explicitly selected snapshot, refs, context, versions and calendar.
    """
    destination, features = Path(processed_path), Path(features_dir)
    try:
        identity = json.loads(destination.with_suffix(".identity.json").read_text(encoding="utf-8"))
        payload = check_identity(identity)
        if expected_identity_hash is not None and identity["identity_hash"] != expected_identity_hash:
            raise SchemaError("Unexpected dataset identity hash")
        if any(payload.get(k) != v for k, v in expected_binding.items()):
            raise SchemaError("Dataset snapshot/factor/context/calendar/options identity mismatch")
        if expected_input_hashes is not None and payload.get("input_hashes") != expected_input_hashes:
            raise SchemaError("Dataset input identity mismatch")
        paths = {
            "dataset": destination,
            "raw_features": features / "raw_factors.parquet",
            "processed_features": features / "processed_factors.parquet",
        }
        if set(payload["artifacts"]) != set(paths):
            raise SchemaError("Dataset artifact inventory mismatch")
        for name, path in paths.items():
            if file_hash(path) != payload["artifacts"][name]:
                raise SchemaError("Dataset artifact checksum mismatch: " + name)
        dataset = pd.read_parquet(destination)
        if frame_hash(dataset) != payload["dataset_content_hash"]:
            raise SchemaError("Dataset content checksum mismatch")
        names = [ref["factor_id"] for ref in payload["factors"]]
        for name, columns in (
            ("raw_features", ["raw_" + n for n in names]),
            ("processed_features", names),
        ):
            if frame_hash(pd.read_parquet(paths[name])) != frame_hash(dataset[["date", "symbol"] + columns]):
                raise SchemaError("Dataset feature content mismatch: " + name)
        dataset.attrs[IDENTITY_ATTR] = identity
        return dataset
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SchemaError("Invalid/missing V2 dataset sidecar or artifacts: " + str(exc)) from exc
