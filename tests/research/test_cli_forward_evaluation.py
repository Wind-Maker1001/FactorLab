"""Offline orchestration tests; identities, signals and bars are synthetic."""

from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.usefixtures("synthetic_cli_admission")

from factorlab import cli
from factorlab.config import (
    DataConfig, EvaluationConfig, ForwardConfig, PathsConfig,
    PortfolioConfig, ProjectConfig, ResearchConfig, UniverseConfig,
)
from factorlab.data.schema import canonicalize_bars
from factorlab.errors import FactorLabError
from factorlab.forward.signal_recorder import SignalRecorder, finalize_signal_record
from factorlab.reports.forward import (
    ForwardEvaluationReportError, finalize_forward_evaluation_report,
    load_forward_evaluation_report,
)
from factorlab.reproducibility import RunManifest


DAY = date(2026, 9, 24)
AS_OF = date(2026, 10, 9)
SOURCE = "9" * 64
MODEL = "a" * 64
CONTRACT = "b" * 64
ACTIVATION_SHA = "c" * 64
SYMBOLS = ("000001.SZ", "000002.SZ", "600000.SH")


class OfflineForwardProvider:
    def cache_source_version(self, symbol, is_index):
        return "synthetic-index-hfq-v1" if is_index else "synthetic-stock-hfq-v1"

    def get_daily_bars(self, *args, **kwargs):
        raise AssertionError("Forward evaluation must not request data")

    get_index_bars = get_daily_bars


def _config(tmp_path):
    config = ResearchConfig(
        project=ProjectConfig(name="synthetic-forward-evaluation-test"),
        data=DataConfig(start=date(2026, 8, 3), end=AS_OF),
        factors=("REV1", "MOM20"),
        universe=UniverseConfig(expected_member_count=3),
        evaluation=EvaluationConfig(quantiles=2, min_cross_section=2),
        portfolio=PortfolioConfig(top_k=2),
        paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"),
        forward=ForwardConfig(
            enabled=True, activation_date=DAY, training_asof=date(2026, 9, 22),
            model_version="synthetic-frozen-ridge-v1",
        ),
    )
    object.__setattr__(config, "repo_root", tmp_path)
    object.__setattr__(config, "config_path", tmp_path / "synthetic.yaml")
    config.validate()
    return config


def _bars(symbol, sessions):
    opens = 100.0 + np.arange(len(sessions), dtype=float)
    return canonicalize_bars(pd.DataFrame({
        "date": sessions, "symbol": symbol, "open": opens,
        "high": opens + 2.0, "low": opens - 1.0, "close": opens + 1.0,
        "volume": 10000.0, "amount": opens * 10000.0, "turnover": 0.01,
        "adj_open": opens * (2.0 if symbol != "000905.SH" else 1.0),
        "adj_high": (opens + 2.0) * (2.0 if symbol != "000905.SH" else 1.0),
        "adj_low": (opens - 1.0) * (2.0 if symbol != "000905.SH" else 1.0),
        "adj_close": (opens + 1.0) * (2.0 if symbol != "000905.SH" else 1.0),
    }))


@pytest.fixture
def context(tmp_path, monkeypatch):
    config = _config(tmp_path)
    provider = OfflineForwardProvider()
    activation = SimpleNamespace(
        activation_date=DAY, activation_id="2026-09-24-" + CONTRACT[:16],
        manifest_sha256=ACTIVATION_SHA,
    )
    provenance = {"git_commit": "f" * 40, "git_dirty": False,
                  "git_diff_sha256": None, "python_version": "3.9.25",
                  "package_versions": {"numpy": "synthetic-test"}}
    bundle = SimpleNamespace(metadata={"content_sha256": MODEL})
    monkeypatch.setattr(cli, "_registered_forward_context", lambda *args: (provenance, CONTRACT, activation, SOURCE))
    monkeypatch.setattr(cli, "_load_registered_forward_bundle", lambda *args: bundle)
    monkeypatch.setattr(cli, "_assert_current_forward_worktree", lambda *args: None)
    monkeypatch.setattr(cli, "research_source_sha256", lambda *args: SOURCE)
    monkeypatch.setattr(cli, "_forward_now", lambda: datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(cli, "_provider", lambda *args: provider)
    monkeypatch.setattr(cli, "_run_forward_child", lambda *args, **kwargs: pytest.fail("No data update/rebuild is allowed"))
    sessions = pd.bdate_range("2026-08-03", "2026-10-09")
    for symbol in (config.data.benchmark, *SYMBOLS):
        is_index = symbol == config.data.benchmark
        cache = cli._benchmark_cache(config) if is_index else cli._stock_cache(config)
        cache.update(symbol, _bars(symbol, sessions), sessions[0], sessions[-1],
                     provider.__class__.__name__, provider.cache_source_version(symbol, is_index))
    predictions = pd.DataFrame({"symbol": SYMBOLS, "score": [0.8, 0.2, -0.3], "rank": [1, 2, 3]})
    signals = predictions.head(2).copy()
    signals["target_weight"] = 0.5
    record = SignalRecorder(config.paths.forward_dir / "signals", activation_date=DAY).record(
        signals, predictions=predictions, cross_section_size=3,
        signal_date=DAY, data_asof=DAY, generated_at="2026-09-24T16:10:00+08:00",
        model_name="ridge", model_version=config.forward.model_version,
        model_bundle_sha256=MODEL, dataset_sha256="d" * 64,
        git_commit="e" * 40, run_id="synthetic-original-producer", universe_version="8" * 64,
        activation_id=activation.activation_id, activation_manifest_sha256=ACTIVATION_SHA,
        research_contract_sha256=CONTRACT, source_sha256=SOURCE, timeliness="ON_TIME",
    )
    producer = tmp_path / "synthetic-producer.json"
    producer.write_text(json.dumps({
        "run_id": "synthetic-original-producer", "command": "forward-signal", "status": "success",
        "completed_at": "2026-09-24T08:15:00+00:00", "signal_sha256": record.sha256,
        "signal_manifest_sha256": record.manifest_sha256, "signal_rows": record.row_count,
        "prediction_sha256": record.predictions_sha256, "prediction_rows": record.prediction_row_count,
        "outputs": {"forward_signals": str(record.signals_path),
                    "forward_signal_manifest": str(record.manifest_path),
                    "forward_predictions": str(record.predictions_path)},
    }), encoding="utf-8")
    finalize_signal_record(record.directory, producer)
    return config, record


def test_cli_evaluation_uses_only_archived_predictions_and_pinned_bars(context):
    config, original = context
    before = {path.relative_to(original.directory): path.read_bytes()
              for path in original.directory.rglob("*") if path.is_file()}
    run = RunManifest(config, "evaluate-forward", {"synthetic": True})
    cli.command_evaluate_forward(config, run, AS_OF)
    directory = Path(run.payload["outputs"]["forward_evaluation_report"])
    with pytest.raises(ForwardEvaluationReportError, match="orphan"):
        load_forward_evaluation_report(directory)
    run.finish("success")
    report = finalize_forward_evaluation_report(directory, run.path)
    loaded = load_forward_evaluation_report(directory)
    assert report.manifest_sha256 == loaded.manifest_sha256
    linked = pd.read_parquet(directory / "forward_predictions.parquet")
    assert linked["symbol"].tolist() == list(SYMBOLS)
    assert linked["score"].tolist() == [0.8, 0.2, -0.3]
    assert linked["rank"].tolist() == [1, 2, 3]
    assert linked["label_status"].eq("MATURE").all()
    assert run.payload["forward_evaluation"]["missing_signal_dates"]
    assert run.payload["forward_evaluation"]["version_policy"] == "FIRST_ON_TIME"
    assert set(run.payload["forward_evaluation"]["bar_data_provenance"]["stocks"]) == set(SYMBOLS)
    after = {path.relative_to(original.directory): path.read_bytes()
             for path in original.directory.rglob("*") if path.is_file()}
    assert before == after


@pytest.mark.parametrize("as_of", [date(2026, 10, 10), datetime(2026, 10, 9), True, "2026-10-09"])
def test_evaluation_rejects_future_and_noncanonical_cutoffs(context, as_of):
    config, _ = context
    run = RunManifest(config, "evaluate-forward", {"synthetic": True})
    with pytest.raises(FactorLabError, match="canonical|future"):
        cli.command_evaluate_forward(config, run, as_of)
    assert not (config.paths.forward_dir / "evaluations").exists()


def test_evaluation_rejects_same_day_before_close_and_naive_clock(context, monkeypatch):
    config, _ = context
    run = RunManifest(config, "evaluate-forward", {"synthetic": True})
    monkeypatch.setattr(cli, "_forward_now", lambda: datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc))
    with pytest.raises(FactorLabError, match="15:30"):
        cli.command_evaluate_forward(config, run, AS_OF)
    monkeypatch.setattr(cli, "_forward_now", lambda: datetime(2026, 10, 9, 18, 0))
    with pytest.raises(FactorLabError, match="timezone-aware"):
        cli.command_evaluate_forward(config, run, AS_OF)


@pytest.mark.parametrize("start,end", [(date(2026, 9, 23), AS_OF), (AS_OF, DAY), (DAY, date(2026, 10, 10))])
def test_evaluation_range_cannot_cross_activation_or_cutoff(context, start, end):
    config, _ = context
    with pytest.raises(FactorLabError, match="range"):
        cli.command_evaluate_forward(config, RunManifest(config, "evaluate-forward"), AS_OF, start=start, end=end)


def test_snapshot_coverage_is_order_independent_but_never_refilled():
    snapshot = SimpleNamespace(metadata={"covered_ranges": [
        {"start": "2026-09-28", "end": "2026-10-09"},
        {"start": "2026-09-24", "end": "2026-09-27"},
    ]})
    cli._snapshot_covers(snapshot, DAY, AS_OF, SYMBOLS[0])
    snapshot.metadata["covered_ranges"][1]["end"] = "2026-09-26"
    with pytest.raises(FactorLabError, match="coverage is incomplete"):
        cli._snapshot_covers(snapshot, DAY, AS_OF, SYMBOLS[0])
    with pytest.raises(FactorLabError, match="no verified cache"):
        cli._snapshot_covers(SimpleNamespace(metadata={}), DAY, AS_OF, SYMBOLS[0])


def test_evaluation_respects_producer_workflow_lock(context):
    config, _ = context
    lock = config.paths.data_root / ".locks" / "forward-workflow.lock"
    with cli.workflow_lock(lock):
        with pytest.raises(FactorLabError, match="lock is already held"):
            cli.command_evaluate_forward(config, RunManifest(config, "evaluate-forward"), AS_OF)
    assert not (config.paths.forward_dir / "evaluations").exists()


def test_evaluation_rejects_unobserved_benchmark_cutoff(context, monkeypatch):
    config, _ = context
    snapshot = cli._load_benchmark_snapshot(config)
    trimmed = SimpleNamespace(
        metadata=snapshot.metadata,
        frame=snapshot.frame.loc[snapshot.frame["date"].lt(pd.Timestamp(AS_OF))],
    )
    monkeypatch.setattr(cli, "_load_benchmark_snapshot", lambda *args: trimmed)
    with pytest.raises(FactorLabError, match="not an observed benchmark session"):
        cli.command_evaluate_forward(config, RunManifest(config, "evaluate-forward"), AS_OF)
    assert not (config.paths.forward_dir / "evaluations").exists()


def test_evaluation_requires_cached_coverage_for_every_recorded_symbol(context, monkeypatch):
    config, _ = context
    cache = cli._stock_cache(config)
    original_loader = cache.load_snapshot

    def truncated_loader(symbol, **kwargs):
        snapshot = original_loader(symbol, **kwargs)
        if symbol == SYMBOLS[-1]:
            return SimpleNamespace(metadata={"covered_ranges": [
                {"start": "2026-08-03", "end": "2026-10-08"},
            ]}, frame=snapshot.frame)
        return snapshot

    monkeypatch.setattr(cache, "load_snapshot", truncated_loader)
    monkeypatch.setattr(cli, "_stock_cache", lambda *args: cache)
    with pytest.raises(FactorLabError, match="coverage is incomplete"):
        cli.command_evaluate_forward(config, RunManifest(config, "evaluate-forward"), AS_OF)
    assert not (config.paths.forward_dir / "evaluations").exists()


def test_source_change_during_report_creation_leaves_uncompleted_orphan(context, monkeypatch):
    config, _ = context
    hashes = iter([SOURCE, "0" * 64])
    monkeypatch.setattr(cli, "research_source_sha256", lambda *args: next(hashes))
    run = RunManifest(config, "evaluate-forward", {"synthetic": True})
    with pytest.raises(FactorLabError, match="report publication"):
        cli.command_evaluate_forward(config, run, AS_OF)
    directory = config.paths.forward_dir / "evaluations" / run.run_id
    assert directory.is_dir()
    with pytest.raises(ForwardEvaluationReportError, match="orphan"):
        load_forward_evaluation_report(directory)


def test_main_records_dates_and_finalizes_only_successful_evaluation(context, monkeypatch):
    config, _ = context
    monkeypatch.setattr(cli, "load_config", lambda *args: config)
    assert cli.main(["evaluate-forward", "--config", "synthetic.yaml", "--as-of", "2026-10-09",
                     "--start", "2026-09-24", "--end", "2026-09-24"]) == 0
    manifests = list(config.paths.runs_dir.glob("*/manifest.json"))
    assert len(manifests) == 1
    run = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert run["status"] == "success"
    assert run["arguments"]["as_of"] == "2026-10-09"
    assert run["data_end"] == "2026-10-09"
    report = load_forward_evaluation_report(Path(run["outputs"]["forward_evaluation_report"]))
    assert report.manifest["as_of"] == "2026-10-09"


def test_main_marks_failed_report_finalization_without_formal_receipt(context, monkeypatch):
    config, _ = context
    monkeypatch.setattr(cli, "load_config", lambda *args: config)
    import factorlab.reports.forward as report_module
    monkeypatch.setattr(report_module, "finalize_forward_evaluation_report",
                        lambda *args: (_ for _ in ()).throw(OSError("synthetic completion disk failure")))
    assert cli.main(["evaluate-forward", "--config", "synthetic.yaml", "--as-of", "2026-10-09"]) == 1
    path = next(config.paths.runs_dir.glob("*/manifest.json"))
    run = json.loads(path.read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    with pytest.raises(ForwardEvaluationReportError, match="orphan"):
        load_forward_evaluation_report(Path(run["outputs"]["forward_evaluation_report"]))
