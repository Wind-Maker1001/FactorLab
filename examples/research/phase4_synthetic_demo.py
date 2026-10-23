"""Deterministic, offline Phase-4 forward-workflow demonstration.

This example exercises software integration only.  It uses generated data,
fixed simulated clocks, and synthetic provenance identities; none of its
scores or selected symbols are research findings.

Run from the repository root::

    python -m examples.research.phase4_synthetic_demo \
        --output results/examples/phase4_synthetic

The program calls only the low-level :mod:`factorlab.forward` APIs.  It
does not invoke the research CLI, a PIT store, a data vendor, a broker, or the
network.  To preserve any prior artifacts, an existing non-empty output
directory is rejected and is never deleted or cleaned automatically.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from factorlab.forward import (
    SignalRecord,
    SignalRecorder,
    classify_forward_timing,
    finalize_signal_record,
    fit_final_model,
    load_forward_activation,
    load_model_bundle,
    load_prediction_record,
    load_signal_record,
    register_forward_activation,
    save_model_bundle,
)


WARNING = "SYNTHETIC SOFTWARE DEMO ONLY - NOT A RESEARCH RESULT"
FEATURE_NAMES = ("VALUE_SYNTH", "MOMENTUM_SYNTH")
TARGET = "SYNTHETIC_EXCESS_RETURN"
MODEL_NAME = "ridge"
MODEL_VERSION = "synthetic-ridge-v1"
TOP_K = 3
TRAINING_ASOF = date(2024, 1, 8)
# Far-future synthetic dates keep the public advance-only activation API
# honest without exposing or monkeypatching its internal registration clock.
ACTIVATION_DATE = date(2099, 1, 10)
REISSUE_REASON = "Synthetic replay demonstrating an explained next-day reissue"
SHANGHAI = ZoneInfo("Asia/Shanghai")
ON_TIME_GENERATED_AT = datetime(2099, 1, 10, 15, 31, tzinfo=SHANGHAI)
LATE_GENERATED_AT = datetime(2099, 1, 11, 9, 0, tzinfo=SHANGHAI)
RIDGE_PARAMETERS = {
    "alpha": 0.75,
    "fit_intercept": True,
    "max_iter": 5_000,
    "tolerance": 0.0001,
}


def build_synthetic_inputs(seed: int = 42) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Return generated training rows and one generated scoring cross-section.

    The final two training dates deliberately have label exit dates after
    ``TRAINING_ASOF`` and extreme values.  ``fit_final_model`` must exclude
    those rows before fitting any Ridge preprocessing or coefficients.
    """

    generator = np.random.default_rng(seed)
    symbols = tuple("%06d.SZ" % number for number in range(1, 7))
    label_windows = (
        ("2023-12-27", "2023-12-29"),
        ("2023-12-28", "2024-01-02"),
        ("2023-12-29", "2024-01-03"),
        ("2024-01-02", "2024-01-04"),
        ("2024-01-03", "2024-01-05"),
        ("2024-01-04", "2024-01-08"),
        # These labels are not mature at the frozen training cutoff.
        ("2024-01-05", "2024-01-09"),
        ("2024-01-08", "2024-01-10"),
    )
    rows = []
    for date_number, (signal_day, exit_day) in enumerate(label_windows):
        immature = date.fromisoformat(exit_day) > TRAINING_ASOF
        for symbol_number, symbol in enumerate(symbols):
            cross_section = (symbol_number - 2.5) / 2.5
            value = cross_section + 0.025 * date_number
            momentum = np.cos(0.55 * symbol_number + 0.3 * date_number)
            target = (
                0.035 * value
                + 0.018 * momentum
                + generator.normal(0.0, 0.0005)
            )
            if immature:
                # If leakage filtering failed, these values would dominate the fit.
                value += 10_000.0
                momentum -= 10_000.0
                target += 1_000.0
            rows.append(
                {
                    "date": pd.Timestamp(signal_day),
                    "exit_date": pd.Timestamp(exit_day),
                    "symbol": symbol,
                    "VALUE_SYNTH": value,
                    "MOMENTUM_SYNTH": momentum,
                    "SYNTHETIC_EXCESS_RETURN": target,
                }
            )

    scoring_rows = []
    for symbol_number, symbol in enumerate(symbols):
        scoring_rows.append(
            {
                "date": pd.Timestamp(ACTIVATION_DATE),
                "symbol": symbol,
                "VALUE_SYNTH": (symbol_number - 2.5) / 2.5 + 0.2,
                "MOMENTUM_SYNTH": np.cos(0.55 * symbol_number + 2.7),
            }
        )
    return pd.DataFrame(rows), pd.DataFrame(scoring_rows)


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _frame_sha256(frame: pd.DataFrame) -> str:
    content = frame.to_csv(
        index=False,
        date_format="%Y-%m-%d",
        float_format="%.17g",
        lineterminator="\n",
    ).encode("utf-8")
    return _sha256_bytes(content)


def _prepare_empty_output_directory(destination: Path) -> None:
    """Create ``destination`` or accept it only when it is a real empty dir."""

    if os.path.lexists(str(destination)):
        if destination.is_symlink() or not destination.is_dir():
            raise FileExistsError(
                "output path already exists and is not a local directory: %s"
                % destination
            )
        if any(destination.iterdir()):
            raise FileExistsError(
                "output directory is not empty; refusing to delete or overwrite: %s"
                % destination
            )
        return
    destination.mkdir(parents=True, exist_ok=False)


def _write_new_bytes(destination: Path, payload: bytes) -> None:
    """Write a new root artifact without an overwrite code path."""

    with destination.open("xb") as stream:
        stream.write(payload)


def _write_warning_files(destination: Path) -> None:
    _write_new_bytes(
        destination / "SYNTHETIC_SOFTWARE_DEMO_ONLY.txt",
        (
            WARNING
            + "\n\nAll dates, features, labels, identities, scores, and symbols "
            "in this directory are generated fixtures.\nNo network, real market "
            "data, PIT store, broker, or research CLI was used.\n"
        ).encode("utf-8"),
    )
    _write_new_bytes(
        destination / "README.md",
        (
            "# %s\n\n" % WARNING
            + "This directory is an offline integration fixture for the Phase-4 "
            "forward APIs. It demonstrates advance activation, mature-label-only "
            "Ridge freezing, same-day Top-K publication, and an explained next-day "
            "late reissue. It is not evidence about any market, factor, security, "
            "model, return, or trading strategy.\n"
        ).encode("utf-8"),
    )


def _full_predictions(bundle, scoring: pd.DataFrame) -> pd.DataFrame:
    scored = scoring[["symbol"]].copy()
    scored["score"] = bundle.predict(scoring)
    selected = scored.sort_values(
        ["score", "symbol"],
        ascending=[False, True],
        kind="mergesort",
    )
    selected = selected.reset_index(drop=True)
    result = selected[["symbol", "score"]].copy()
    result["rank"] = np.arange(1, len(result) + 1, dtype=np.int64)
    return result


def _verify_signal_record(record: SignalRecord) -> SignalRecord:
    verified = load_signal_record(record.directory)
    identity = (
        verified.signal_date,
        verified.version,
        verified.sha256,
        verified.manifest_sha256,
        verified.row_count,
    )
    expected = (
        record.signal_date,
        record.version,
        record.sha256,
        record.manifest_sha256,
        record.row_count,
    )
    if identity != expected:
        raise RuntimeError("load_signal_record returned a different artifact identity")
    return verified


def _finalize_synthetic_signal(
    record: SignalRecord,
    destination: Path,
    completed_at: datetime,
) -> SignalRecord:
    """Create a truthful synthetic producer run and append its success receipt."""

    run_path = destination / "synthetic_runs" / record.directory.parent.name
    run_path = run_path / record.version / "manifest.json"
    run_path.parent.mkdir(parents=True, exist_ok=False)
    signal_manifest = json.loads(record.manifest_path.read_text(encoding="utf-8"))
    payload = {
        "run_id": signal_manifest["run_id"],
        "command": "forward-signal",
        "status": "success",
        "completed_at": completed_at.isoformat(),
        "signal_sha256": record.sha256,
        "signal_manifest_sha256": record.manifest_sha256,
        "signal_rows": record.row_count,
        "prediction_sha256": record.predictions_sha256,
        "prediction_rows": record.prediction_row_count,
        "outputs": {
            "forward_signals": str(record.signals_path),
            "forward_signal_manifest": str(record.manifest_path),
            "forward_predictions": str(record.predictions_path),
        },
    }
    _write_new_bytes(run_path, _canonical_json_bytes(payload))
    return finalize_signal_record(record.directory, run_path)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def run_demo(output_dir: Path, seed: int = 42) -> Dict[str, object]:
    """Run the complete synthetic Phase-4 lifecycle using local low-level APIs."""

    # Forward integrity helpers intentionally return absolute paths without
    # following links.  Normalize the demo root the same way so relative CLI
    # output paths remain comparable in the final manifest.
    destination = Path(os.path.abspath(os.fspath(output_dir)))
    _prepare_empty_output_directory(destination)
    # Mark the directory before producing any forward artifact, so even a
    # partially failed run cannot be mistaken for research output.
    _write_warning_files(destination)

    training, scoring = build_synthetic_inputs(seed=seed)
    training_sha256 = _frame_sha256(training)
    scoring_sha256 = _frame_sha256(scoring)
    symbols = tuple(sorted(scoring["symbol"].tolist()))
    universe_sha256 = _sha256_bytes(("\n".join(symbols) + "\n").encode("ascii"))
    # This is a deterministic synthetic identity accepted by the low-level
    # provenance field; it is explicitly not the repository's Git commit.
    synthetic_git_identity = hashlib.sha1(
        b"phase4-synthetic-software-demo-no-real-git-commit"
    ).hexdigest()

    contract_payload = {
        "warning": WARNING,
        "artifact_type": "phase4-synthetic-forward-contract",
        "synthetic": True,
        "live_trading": False,
        "activation_date": ACTIVATION_DATE.isoformat(),
        "training_asof": TRAINING_ASOF.isoformat(),
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "target": TARGET,
        "parameters": RIDGE_PARAMETERS,
        "seed": seed,
        "top_k": TOP_K,
        "source_dataset_sha256": training_sha256,
        "universe_version": universe_sha256,
    }
    source_sha256 = _sha256_bytes(
        b"phase4-synthetic-example-source-contract-v1"
    )
    contract_payload["source_sha256"] = source_sha256
    contract_content = _canonical_json_bytes(contract_payload)
    contract_sha256 = _sha256_bytes(contract_content)
    _write_new_bytes(destination / "synthetic_contract.json", contract_content)

    forward_root = destination / "forward"
    activation = register_forward_activation(
        forward_root,
        activation_date=ACTIVATION_DATE,
        contract_sha256=contract_sha256,
        source_sha256=source_sha256,
        git_commit=synthetic_git_identity,
        config_path="synthetic_contract.json",
        timezone_name="Asia/Shanghai",
    )
    verified_activation = load_forward_activation(
        forward_root,
        activation.activation_id,
        expected_contract_sha256=contract_sha256,
        expected_source_sha256=source_sha256,
    )

    fitted = fit_final_model(
        training,
        model_name=MODEL_NAME,
        params=RIDGE_PARAMETERS,
        feature_names=FEATURE_NAMES,
        target=TARGET,
        training_asof=TRAINING_ASOF,
        seed=seed,
    )
    saved_bundle = save_model_bundle(
        fitted,
        forward_root / "models",
        model_version=MODEL_VERSION,
        source_dataset_sha256=training_sha256,
        source_sha256=source_sha256,
        universe_version=universe_sha256,
        research_contract_sha256=contract_sha256,
        git_commit=synthetic_git_identity,
    )
    bundle = load_model_bundle(
        saved_bundle.path,
        expected_model_name=MODEL_NAME,
        expected_feature_names=FEATURE_NAMES,
        expected_source_sha256=source_sha256,
    )
    if (
        bundle.metadata["research_contract_sha256"] != contract_sha256
        or bundle.metadata["model_version"] != MODEL_VERSION
    ):
        raise RuntimeError("loaded Ridge bundle does not match the frozen contract")

    predictions = _full_predictions(bundle, scoring)
    signals = predictions.head(TOP_K).copy()
    signals["target_weight"] = 1.0 / len(signals)
    on_time = classify_forward_timing(
        ACTIVATION_DATE,
        activation_date=verified_activation.activation_date,
        generated_at=ON_TIME_GENERATED_AT,
        timezone_name="Asia/Shanghai",
    )
    late = classify_forward_timing(
        ACTIVATION_DATE,
        activation_date=verified_activation.activation_date,
        generated_at=LATE_GENERATED_AT,
        timezone_name="Asia/Shanghai",
        reissue_reason=REISSUE_REASON,
    )

    recorder = SignalRecorder(
        forward_root / "signals",
        activation_date=verified_activation.activation_date,
    )
    common_record_options = {
        "predictions": predictions,
        "cross_section_size": len(scoring),
        "signal_date": ACTIVATION_DATE,
        "data_asof": ACTIVATION_DATE,
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "model_bundle_sha256": str(bundle.metadata["content_sha256"]),
        "dataset_sha256": scoring_sha256,
        "git_commit": synthetic_git_identity,
        "universe_version": universe_sha256,
        "activation_id": verified_activation.activation_id,
        "activation_manifest_sha256": verified_activation.manifest_sha256,
        "research_contract_sha256": contract_sha256,
        "source_sha256": source_sha256,
    }
    on_time_record = recorder.record(
        signals,
        generated_at=on_time.generated_at,
        run_id="synthetic-on-time-run",
        timeliness=on_time.timeliness,
        reissue_reason=on_time.reissue_reason,
        **common_record_options,
    )
    late_record = recorder.record(
        signals,
        generated_at=late.generated_at,
        run_id="synthetic-next-day-reissue-run",
        timeliness=late.timeliness,
        reissue_reason=late.reissue_reason,
        **common_record_options,
    )
    _finalize_synthetic_signal(
        on_time_record, destination, ON_TIME_GENERATED_AT
    )
    _finalize_synthetic_signal(
        late_record, destination, LATE_GENERATED_AT
    )
    verified_on_time = _verify_signal_record(on_time_record)
    verified_late = _verify_signal_record(late_record)
    for verified in (verified_on_time, verified_late):
        stored_predictions = load_prediction_record(verified.directory)
        pd.testing.assert_frame_equal(
            stored_predictions[["symbol", "score", "rank"]],
            predictions,
            check_exact=True,
        )

    mature_rows = int(
        (
            pd.to_datetime(training["date"]).le(pd.Timestamp(TRAINING_ASOF))
            & pd.to_datetime(training["exit_date"]).le(pd.Timestamp(TRAINING_ASOF))
            & np.isfinite(training[TARGET].astype(float))
        ).sum()
    )
    if fitted.training_rows != mature_rows:
        raise RuntimeError("Ridge freeze did not use the expected mature-label rows")

    manifest: Dict[str, object] = {
        "warning": WARNING,
        "artifact_type": "phase4-synthetic-software-demo",
        "synthetic": True,
        "software_demo_only": True,
        "research_result": False,
        "network_used": False,
        "cli_used": False,
        "pit_used": False,
        "real_data_used": False,
        "live_trading": False,
        "seed": seed,
        "top_k": TOP_K,
        "timeline": {
            "registered_at": verified_activation.registered_at.isoformat(),
            "training_asof": TRAINING_ASOF.isoformat(),
            "activation_date": ACTIVATION_DATE.isoformat(),
            "on_time_generated_at": on_time.generated_at.isoformat(),
            "late_reissue_generated_at": late.generated_at.isoformat(),
        },
        "activation": {
            "activation_id": verified_activation.activation_id,
            "contract_sha256": contract_sha256,
            "manifest_sha256": verified_activation.manifest_sha256,
            "directory": _relative(verified_activation.path, destination),
            "registered_in_advance": (
                verified_activation.registered_at.astimezone(SHANGHAI).date()
                < verified_activation.activation_date
            ),
            "load_verified": True,
        },
        "ridge_model": {
            "model_name": bundle.model_name,
            "model_version": bundle.model_version,
            "feature_names": list(bundle.feature_names),
            "parameters": dict(bundle.metadata["parameters"]),
            "source_rows": int(len(training)),
            "mature_training_rows": fitted.training_rows,
            "excluded_immature_rows": int(len(training) - fitted.training_rows),
            "training_asof": fitted.training_asof,
            "max_label_exit_date": fitted.max_label_exit_date,
            "bundle_sha256": str(bundle.metadata["content_sha256"]),
            "directory": _relative(bundle.path, destination),
            "load_verified": True,
        },
        "signals": {
            "selected_symbols": signals["symbol"].tolist(),
            "on_time": {
                "signal_date": on_time.signal_date.isoformat(),
                "timeliness": on_time.timeliness,
                "reissue_reason": on_time.reissue_reason,
                "version": verified_on_time.version,
                "rows": verified_on_time.row_count,
                "sha256": verified_on_time.sha256,
                "manifest_sha256": verified_on_time.manifest_sha256,
                "directory": _relative(verified_on_time.directory, destination),
                "load_signal_record_verified": True,
                "prediction_rows": verified_on_time.prediction_row_count,
                "prediction_sha256": verified_on_time.predictions_sha256,
                "load_prediction_record_verified": True,
            },
            "late_reissue": {
                "signal_date": late.signal_date.isoformat(),
                "timeliness": late.timeliness,
                "reissue_reason": late.reissue_reason,
                "version": verified_late.version,
                "rows": verified_late.row_count,
                "sha256": verified_late.sha256,
                "manifest_sha256": verified_late.manifest_sha256,
                "directory": _relative(verified_late.directory, destination),
                "load_signal_record_verified": True,
                "prediction_rows": verified_late.prediction_row_count,
                "prediction_sha256": verified_late.predictions_sha256,
                "load_prediction_record_verified": True,
            },
        },
    }
    _write_new_bytes(destination / "manifest.json", _canonical_json_bytes(manifest))
    return manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=WARNING)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/examples/phase4_synthetic"),
    )
    parser.add_argument("--seed", type=int, default=42)
    arguments = parser.parse_args(argv)
    manifest = run_demo(arguments.output, seed=arguments.seed)
    print(WARNING)
    print(
        "Recorded %s and %s under %s"
        % (
            manifest["signals"]["on_time"]["timeliness"],
            manifest["signals"]["late_reissue"]["timeliness"],
            arguments.output,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
