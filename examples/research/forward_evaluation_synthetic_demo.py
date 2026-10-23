"""Offline synthetic demonstration of immutable mature-forward evaluation.

Run ``python -m examples.research.forward_evaluation_synthetic_demo --output
results/examples/forward-evaluation-20261009 --seed 42``. This calls production
storage, evidence selection, labels, metrics and reports, but NOT the research
CLI, a model, data provider, PIT store, broker or network. All 2099 clocks,
weekday calendars, market observations and signal identities are artificial.
This is software verification, not historical or real forward performance.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from factorlab.config.models import (
    DataConfig, PathsConfig, ProjectConfig, ResearchConfig, UniverseConfig,
)
from factorlab.data.schema import canonicalize_bars
from factorlab.forward.evaluation import evaluate_forward_predictions
from factorlab.forward.evidence import FIRST_ON_TIME, select_forward_prediction_records
from factorlab.forward.integrity import assert_no_link_components
from factorlab.forward.signal_recorder import SignalRecorder, finalize_signal_record
from factorlab.reports.forward import (
    finalize_forward_evaluation_report, load_forward_evaluation_report,
    write_forward_evaluation_report,
)
from factorlab.reproducibility import RunManifest, set_random_seed


LOGGER = logging.getLogger(__name__)
WARNING = "SYNTHETIC SOFTWARE DEMO ONLY - NOT A RESEARCH RESULT"
SYMBOLS = tuple("%06d.SZ" % number for number in range(1, 7))
SIGNAL_DATES = (date(2099, 1, 13), date(2099, 1, 15), date(2099, 1, 21))
AS_OF = date(2099, 1, 22)
SHANGHAI = ZoneInfo("Asia/Shanghai")
FLAGS = {
    "synthetic": True, "software_demo_only": True, "research_result": False,
    "point_in_time_universe_verified": False, "network_used": False,
    "live_trading": False, "research_cli_used": False,
    "signal_timestamps_simulated": True, "real_market_data_used": False,
}
# These are deliberately invented, format-valid identities. They do not name
# a real activation, source tree, model bundle, dataset, PIT version or Git SHA.
SYNTHETIC_IDENTITY = {
    "activation_id": "2099-01-13-eeeeeeeeeeeeeeee",
    "activation_manifest_sha256": "f" * 64,
    "research_contract_sha256": "e" * 64, "source_sha256": "1" * 64,
    "model_name": "ridge", "model_version": "synthetic-unfitted-ranking-v1",
    "model_bundle_sha256": "a" * 64,
}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _prepare_output(path: Path) -> Path:
    # Unlike resolve(), this rejects a link/reparse component before following
    # it, including a link in a parent or an otherwise empty linked directory.
    destination = assert_no_link_components(path)
    if os.path.lexists(str(destination)):
        if not destination.is_dir() or any(destination.iterdir()):
            raise FileExistsError("Refusing a non-empty or non-directory output: %s" % destination)
    else:
        destination.mkdir(parents=True, exist_ok=False)
    return destination


def _synthetic_config(destination: Path, seed: int) -> ResearchConfig:
    config = ResearchConfig(
        project=ProjectConfig(name="synthetic-forward-evaluation-software-demo"),
        data=DataConfig(start=date(2098, 10, 1), end=AS_OF),
        universe=UniverseConfig(provider="local", status_provider="local"),
        factors=("REV1",), seed=seed, live_trading=False,
        paths=PathsConfig(data_root=destination / "unused-data-root",
                          results_root=destination / "runtime"),
    )
    object.__setattr__(config, "repo_root", Path(__file__).resolve().parents[2])
    config.validate()
    return config


def build_synthetic_bars(seed: int = 42):
    """Generate positive OHLC and one absent entry bar on a weekday calendar."""
    dates = pd.bdate_range("2098-10-01", AS_OF.isoformat())
    steps = np.arange(len(dates), dtype=float)
    generator = np.random.default_rng(seed)

    def bars(symbol, base, slope, adjustment):
        opened = base + slope * steps
        close = opened * 1.001
        return canonicalize_bars(pd.DataFrame({
            "date": dates, "symbol": symbol,
            "open": opened, "high": opened * 1.01, "low": opened * 0.99, "close": close,
            "adj_open": opened * adjustment, "adj_high": opened * 1.01 * adjustment,
            "adj_low": opened * 0.99 * adjustment, "adj_close": close * adjustment,
            "volume": 10000.0, "amount": opened * 10000.0, "turnover": 0.01,
        }))

    stocks = pd.concat([
        bars(symbol, 10.0 + number, 0.002 + number * 0.005 + generator.uniform(0, 0.0001),
             1.1 + number * 0.01)
        for number, symbol in enumerate(SYMBOLS)
    ], ignore_index=True)
    stocks = stocks.loc[~((stocks["symbol"] == SYMBOLS[0])
                         & (stocks["date"] == pd.Timestamp("2099-01-14")))].reset_index(drop=True)
    return stocks, bars("000905.SH", 1000.0, 0.1, 1.0)


def _persist_frame(path: Path, frame: pd.DataFrame) -> str:
    buffer = BytesIO()
    frame.to_parquet(buffer, index=False, engine="pyarrow")
    content = buffer.getvalue()
    _write_new(path, content)
    return _sha(content)


def _publish_record(config, destination, recorder, *, day, generated_at, reverse=False, late=False):
    producer = RunManifest(config, "forward-signal", arguments={
        **FLAGS, "simulated_signal_date": day.isoformat(),
        "simulated_generated_at": generated_at.isoformat(),
    })
    producer.payload.update(FLAGS)
    producer.payload["identity_mode"] = "invented-signal-identities-not-real-git-source-model-or-PIT"
    symbols = SYMBOLS[::-1] if reverse else SYMBOLS
    scores = np.arange(6, 0, -1, dtype=float) + (100.0 if reverse else 1000.0 if late else 0.0)
    predictions = pd.DataFrame({"symbol": symbols, "score": scores,
                                "rank": np.arange(1, 7, dtype=np.int64)})
    signals = predictions.head(3).copy()
    signals["target_weight"] = 1 / 3
    try:
        record = recorder.record(
            signals, predictions=predictions, cross_section_size=6,
            signal_date=day, data_asof=day, generated_at=generated_at,
            **SYNTHETIC_IDENTITY, dataset_sha256="b" * 64,
            git_commit="c" * 40, run_id=producer.run_id, universe_version="d" * 64,
            timeliness="LATE_REISSUE" if late else "ON_TIME",
            reissue_reason="Artificial next-day reissue; not a contemporaneous prediction" if late else None,
        )
        producer.payload.update({
            "signal_sha256": record.sha256, "signal_manifest_sha256": record.manifest_sha256,
            "signal_rows": record.row_count, "prediction_sha256": record.predictions_sha256,
            "prediction_rows": record.prediction_row_count,
        })
        producer.add_output("forward_signals", record.signals_path)
        producer.add_output("forward_signal_manifest", record.manifest_path)
        producer.add_output("forward_predictions", record.predictions_path)
        producer.finish("success")
        return finalize_signal_record(record.directory, producer.path)
    except Exception as exc:
        # Preserve any orphan record as evidence of failure, never remove it or
        # silently create a better version. A failed output cannot be rerun.
        if producer.payload["status"] != "success":
            producer.finish("failed", error="%s: %s" % (type(exc).__name__, exc))
        raise


def _file_snapshot(root: Path) -> Dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def run_demo(output: Path, *, seed: int = 42) -> dict:
    """Run isolated production APIs on explicitly artificial input evidence."""
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2 ** 32:
        raise ValueError("seed must be an integer in [0, 2**32)")
    destination = _prepare_output(Path(output))
    _write_new(destination / "SYNTHETIC_SOFTWARE_DEMO_ONLY.txt", (
        WARNING + "\n\nAll 2099 timestamps, weekdays, prices, scores, labels and identity hashes "
        "are artificial. Canonical symbol spellings do not establish real index membership.\n"
        "No true Forward activation, research CLI, PIT store, network, model fit or broker is used.\n"
        "RunManifest records the real computer environment; signal clocks and identity hashes "
        "are explicitly simulated and do not prove contemporary real-market publication.\n"
    ).encode("utf-8"))
    _write_new(destination / "README.md", (
        "# " + WARNING + "\n\nThis offline integration demonstration selects the first simulated "
        "ON_TIME version before associating labels. Later ON_TIME and LATE_REISSUE variants "
        "cannot improve the selected result. Missing prices do not promote another stock. "
        "The generated weekday calendar is not an official 2099 exchange calendar.\n"
    ).encode("utf-8"))
    LOGGER.warning(WARNING)
    config = _synthetic_config(destination, seed)
    set_random_seed(seed)
    stock_bars, benchmark_bars = build_synthetic_bars(seed)
    input_hashes = {
        "stock_bars.parquet": _persist_frame(destination / "stock_bars.parquet", stock_bars),
        "benchmark_bars.parquet": _persist_frame(destination / "benchmark_bars.parquet", benchmark_bars),
    }
    recorder = SignalRecorder(destination / "synthetic-signals", activation_date=SIGNAL_DATES[0])
    records = [
        _publish_record(config, destination, recorder, day=SIGNAL_DATES[0],
                        generated_at=datetime(2099, 1, 13, 15, 31, tzinfo=SHANGHAI)),
        _publish_record(config, destination, recorder, day=SIGNAL_DATES[0], reverse=True,
                        generated_at=datetime(2099, 1, 13, 15, 32, tzinfo=SHANGHAI)),
        _publish_record(config, destination, recorder, day=SIGNAL_DATES[0], late=True,
                        generated_at=datetime(2099, 1, 14, 15, 31, tzinfo=SHANGHAI)),
    ]
    records.extend(_publish_record(config, destination, recorder, day=day,
                                  generated_at=datetime.combine(day, datetime.min.time()).replace(
                                      hour=15, minute=31, tzinfo=SHANGHAI))
                   for day in SIGNAL_DATES[1:])
    original_files = _file_snapshot(recorder.root)
    evidence = select_forward_prediction_records(
        recorder.root, start=SIGNAL_DATES[0], end=SIGNAL_DATES[-1],
        **SYNTHETIC_IDENTITY, version_policy=FIRST_ON_TIME,
    )
    evaluation = evaluate_forward_predictions(
        evidence.predictions, stock_bars, benchmark_bars, as_of=AS_OF,
        holding_days=5, top_k=3, minimum_cross_section=3, include_regime=True,
    )
    run = RunManifest(config, "evaluate-forward", arguments={
        **FLAGS, "as_of": AS_OF.isoformat(), "version_policy": FIRST_ON_TIME,
    })
    run.payload.update(FLAGS)
    run.payload.update({"as_of": AS_OF.isoformat(), "version_policy": FIRST_ON_TIME,
                        "synthetic_input_sha256": input_hashes,
                        "signal_identity_mode": "invented-not-real-git-source-model-or-PIT"})
    try:
        report = write_forward_evaluation_report(
            evaluation, evidence, destination / "evaluations" / run.run_id, run_id=run.run_id,
            provenance={**FLAGS, "as_of": AS_OF.isoformat(), "version_policy": FIRST_ON_TIME,
                        "calendar_source": "generated-weekdays-not-official-exchange-sessions",
                        "synthetic_identity": SYNTHETIC_IDENTITY,
                        "synthetic_input_sha256": input_hashes},
        )
        run.payload.update({"forward_evaluation_manifest_sha256": report.manifest_sha256,
                            "forward_evaluation_artifact_sha256": report.artifact_sha256})
        run.add_output("forward_evaluation_report", report.directory)
        run.finish("success")
        report = finalize_forward_evaluation_report(report.directory, run.path)
        verified = load_forward_evaluation_report(report.directory)
        if verified.manifest_sha256 != report.manifest_sha256:
            raise RuntimeError("Completed report changed identity during verification")
    except Exception as exc:
        try:
            run.finish("failed", error="%s: %s" % (type(exc).__name__, exc))
        except Exception:
            LOGGER.exception("Cannot finalize failed synthetic evaluation run")
        raise
    unchanged = _file_snapshot(recorder.root) == original_files
    if not unchanged:
        raise RuntimeError("Synthetic original signal evidence changed during evaluation")
    status_counts = {name: int(evaluation.predictions.label_status.eq(name).sum())
                     for name in ("MATURE", "MATURE_MISSING_PRICE", "IMMATURE")}
    result = {
        "warning": WARNING, **FLAGS, "seed": seed, "as_of": evaluation.as_of,
        "calendar_source": "generated-weekdays-not-official-exchange-sessions",
        "synthetic_identity": SYNTHETIC_IDENTITY,
        "signal_git_commit_is_synthetic": True, "run_git_commit_is_actual_environment": True,
        "published_records": len(records), "matched_records": len(evidence.selected),
        "prediction_rows": len(evaluation.predictions), "label_status_counts": status_counts,
        "version_policy": FIRST_ON_TIME,
        "selected": evidence.selected.to_dict("records"),
        "excluded": evidence.excluded.to_dict("records"),
        "signals_root": str(recorder.root), "original_signal_bytes_unchanged": unchanged,
        "original_signal_sha256": {name: _sha(content) for name, content in original_files.items()},
        "synthetic_input_sha256": input_hashes,
        "evaluation_run_id": run.run_id, "evaluation_run_manifest": str(run.path),
        "evaluation_report": str(report.directory),
        "evaluation_manifest_sha256": report.manifest_sha256,
        "evaluation_artifact_sha256": report.artifact_sha256,
        "report_completion_verified": True,
    }
    _write_new(destination / "manifest.json", _json_bytes(result))
    LOGGER.info("Selected %d/%d simulated records; labelled %d predictions: %s; report=%s",
                len(evidence.selected), len(records), len(evaluation.predictions), status_counts, report.directory)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=WARNING)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_demo(arguments.output, seed=arguments.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
