"""Offline lifecycle tests for the paper-only Phase 4 CLI orchestration."""

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from qbot.research import cli
from qbot.research.config import (
    DataConfig,
    EvaluationConfig,
    ForwardConfig,
    ModelsConfig,
    PathsConfig,
    PortfolioConfig,
    ProjectConfig,
    ResearchConfig,
    RidgeConfig,
    UniverseConfig,
)
from qbot.research.data.schema import canonicalize_bars
from qbot.research.errors import FactorLabError
from qbot.research.forward import load_model_bundle, load_prediction_record
import qbot.research.forward.activation as activation_module
import qbot.research.forward.model_bundle as model_bundle_module


SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def _stable_git_state(monkeypatch):
    monkeypatch.setattr(cli, "git_commit", lambda root: "a" * 40)
    monkeypatch.setattr(
        cli,
        "git_worktree_state",
        lambda root: {"git_dirty": False, "git_diff_sha256": None},
    )
    monkeypatch.setattr(cli, "research_source_sha256", lambda root: "9" * 64)
    monkeypatch.setattr(
        cli,
        "_forward_now",
        lambda: datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        activation_module,
        "_utc_now",
        lambda: datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        model_bundle_module,
        "_utc_now",
        lambda: datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc),
    )


class _Run:
    def __init__(self, run_id="phase4-test", *, dirty=False):
        self.run_id = run_id
        self.payload = {
            "git_commit": "a" * 40,
            "git_dirty": dirty,
            "git_diff_sha256": "d" * 64 if dirty else None,
            "python_version": "3.9.25",
            "package_versions": {"numpy": "fixture"},
        }
        self.outputs = {}

    def add_output(self, name, value):
        self.outputs[name] = str(value)


def _config(tmp_path, *, end=date(2026, 9, 22)):
    config = ResearchConfig(
        project=ProjectConfig(name="factorlab-phase4-test"),
        data=DataConfig(
            start=date(2026, 8, 3),
            end=end,
            warmup_trading_days=120,
        ),
        universe=UniverseConfig(
            expected_member_count=3,
            min_listed_days=120,
            data_quality_window_days=60,
        ),
        factors=("REV1", "MOM20"),
        paths=PathsConfig(
            data_root=tmp_path / "data" / "research",
            results_root=tmp_path / "results",
        ),
        evaluation=EvaluationConfig(quantiles=2, min_cross_section=2),
        models=ModelsConfig(ridge=RidgeConfig(alphas=(0.5, 1.0))),
        forward=ForwardConfig(
            enabled=True,
            activation_date=date(2026, 9, 24),
            model_name="ridge",
            model_version="ridge-frozen-v1",
            training_asof=date(2026, 9, 22),
            parameter_candidate_index=0,
        ),
        portfolio=PortfolioConfig(top_k=2, rebalance_days=5),
    )
    object.__setattr__(config, "repo_root", Path(tmp_path))
    object.__setattr__(config, "config_path", Path(tmp_path) / "phase4.yaml")
    config.validate()
    return config


def _dataset():
    rows = []
    symbols = ("000001.SZ", "000002.SZ", "600000.SH")
    for day_number, day in enumerate(pd.to_datetime(["2026-09-14", "2026-09-15"])):
        for symbol_number, symbol in enumerate(symbols):
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "REV1": float(symbol_number + day_number),
                    "MOM20": float(2 * symbol_number - day_number),
                    "entry_date": day + pd.Timedelta(days=1),
                    "exit_date": day + pd.Timedelta(days=6),
                    "EXCESS_RET5": 0.01 * symbol_number - 0.003 * day_number,
                }
            )
    for symbol_number, symbol in enumerate(symbols):
        rows.append(
            {
                "date": pd.Timestamp("2026-09-24"),
                "symbol": symbol,
                "REV1": float(symbol_number) - 0.5,
                "MOM20": 1.0 - float(symbol_number),
                "entry_date": pd.NaT,
                "exit_date": pd.NaT,
                "EXCESS_RET5": np.nan,
            }
        )
    return pd.DataFrame(rows)


def _benchmark():
    dates = pd.bdate_range("2026-08-03", "2026-09-24")
    values = np.arange(len(dates), dtype=float)
    return canonicalize_bars(
        pd.DataFrame(
            {
                "date": dates,
                "symbol": "000905.SH",
                "open": 5000.0 + values,
                "high": 5002.0 + values,
                "low": 4998.0 + values,
                "close": 5001.0 + values,
                "volume": 1_000_000.0,
                "amount": 5_000_000_000.0,
                "turnover": np.nan,
            }
        )
    )


def _activate(config):
    run = _Run("activate")
    cli.command_activate_forward(
        config,
        run,
        date(2026, 9, 24),
    )
    return run


def test_runtime_config_pins_data_end_without_changing_forward_contract(tmp_path):
    config = _config(tmp_path)
    original_contract = cli.forward_contract_sha256(config)

    runtime = cli._forward_runtime_config(config, date(2026, 9, 24))

    assert runtime.data.end == date(2026, 9, 24)
    assert config.data.end == date(2026, 9, 22)
    assert runtime.repo_root == config.repo_root
    assert runtime.config_path == config.config_path
    assert cli.forward_contract_sha256(runtime) == original_contract


def test_main_serializes_forward_date_and_uses_runtime_end(tmp_path, monkeypatch):
    config = _config(tmp_path)
    seen = {}
    monkeypatch.setattr(cli, "load_config", lambda path: config)

    def capture(runtime, run, signal_date, **kwargs):
        seen["end"] = runtime.data.end
        seen["date"] = signal_date
        fake = tmp_path / "signals" / "2026-09-24" / "v000001" / "signals.csv"
        run.add_output("forward_signals", fake)

    monkeypatch.setattr(cli, "command_forward_signal", capture)
    monkeypatch.setattr(
        cli,
        "finalize_signal_record",
        lambda directory, manifest: SimpleNamespace(
            completion_path=Path(directory) / "completion"
        ),
    )

    assert (
        cli.main(
            [
                "forward-signal",
                "--config",
                "unused.yaml",
                "--date",
                "2026-09-24",
            ]
        )
        == 0
    )
    assert seen == {"end": date(2026, 9, 24), "date": date(2026, 9, 24)}
    manifests = list(config.paths.runs_dir.glob("*/manifest.json"))
    assert len(manifests) == 1
    assert '"date": "2026-09-24"' in manifests[0].read_text(encoding="utf-8")


def test_activate_forward_creates_advance_only_receipt_and_requires_clean_run(tmp_path):
    config = _config(tmp_path)
    run = _activate(config)

    assert Path(run.outputs["forward_activation"]).is_file()
    assert run.payload["activation_id"].startswith("2026-09-24-")
    assert len(run.payload["activation_manifest_sha256"]) == 64

    dirty_config = _config(tmp_path / "dirty")
    with pytest.raises(FactorLabError, match="clean committed worktree"):
        cli.command_activate_forward(
            dirty_config,
            _Run("dirty", dirty=True),
            date(2026, 9, 24),
        )


def test_freeze_and_signal_lifecycle_is_explicit_and_append_only(tmp_path, monkeypatch):
    training_config = _config(tmp_path)
    _activate(training_config)
    dataset = _dataset()
    benchmark = _benchmark()
    monkeypatch.setattr(cli, "_load_benchmark_with_warmup", lambda config: benchmark)
    monkeypatch.setattr(
        cli,
        "_load_verified_dataset",
        lambda config, calendar, run=None, **kwargs: (
            dataset.copy(),
            "b" * 64,
            "c" * 64,
        ),
    )

    freeze_run = _Run("freeze")
    cli.command_freeze_forward_model(training_config, freeze_run)
    bundle_path = Path(freeze_run.outputs["forward_model_bundle"])
    bundle = load_model_bundle(bundle_path)
    assert bundle.metadata["training_asof"] == "2026-09-22"
    assert bundle.metadata["max_label_exit_date"] <= "2026-09-22"
    assert bundle.metadata["training_rows"] == 6
    assert bundle.metadata["source_sha256"] == "9" * 64
    assert bundle.metadata["frozen_at"].endswith("Z")

    signal_config = cli._forward_runtime_config(
        training_config, date(2026, 9, 24)
    )
    signal_run = _Run("signal-on-time")
    cli.command_forward_signal(
        signal_config,
        signal_run,
        date(2026, 9, 24),
        generated_at=datetime(2026, 9, 24, 16, 0, tzinfo=SHANGHAI),
        refresh_data=False,
    )

    first_path = Path(signal_run.outputs["forward_signals"])
    assert first_path.is_file()
    first_bytes = first_path.read_bytes()
    first = pd.read_csv(first_path, dtype={"symbol": str})
    assert len(first) == 2
    assert first["rank"].tolist() == [1, 2]
    assert first["target_weight"].tolist() == [0.5, 0.5]
    assert first["timeliness"].unique().tolist() == ["ON_TIME"]
    assert signal_run.payload["model_bundle_sha256"] == bundle.metadata[
        "content_sha256"
    ]
    predictions_path = Path(signal_run.outputs["forward_predictions"])
    assert predictions_path.parent == first_path.parent
    assert signal_run.payload["prediction_rows"] == 3
    assert len(signal_run.payload["prediction_sha256"]) == 64
    predictions = load_prediction_record(first_path.parent, require_completion=False)
    assert len(predictions) == 3
    assert predictions["rank"].tolist() == [1, 2, 3]
    pd.testing.assert_frame_equal(
        predictions[["symbol", "score", "rank"]].head(2).reset_index(drop=True),
        first[["symbol", "score", "rank"]],
    )
    prediction_bytes = predictions_path.read_bytes()

    late_run = _Run("signal-late")
    cli.command_forward_signal(
        signal_config,
        late_run,
        date(2026, 9, 24),
        generated_at=datetime(2026, 9, 25, 9, 0, tzinfo=SHANGHAI),
        reissue_reason="approved data correction",
        refresh_data=False,
    )
    late_path = Path(late_run.outputs["forward_signals"])
    assert late_path.parent.name == "v000002"
    assert first_path.read_bytes() == first_bytes
    assert predictions_path.read_bytes() == prediction_bytes
    late = pd.read_csv(late_path, dtype={"symbol": str})
    assert late["timeliness"].unique().tolist() == ["LATE_REISSUE"]
    assert late["reissue_reason"].unique().tolist() == [
        "approved data correction"
    ]

    complete_dataset = dataset.copy()
    dataset = dataset.drop(
        dataset.loc[
            dataset["date"].eq(pd.Timestamp("2026-09-24"))
        ].index[0]
    )
    with pytest.raises(FactorLabError, match="eligible cross-section"):
        cli.command_forward_signal(
            signal_config,
            _Run("signal-undercovered"),
            date(2026, 9, 24),
            generated_at=datetime(2026, 9, 24, 16, 0, tzinfo=SHANGHAI),
            refresh_data=False,
        )
    dataset = complete_dataset

    def invalid_prediction(self, features):
        # The non-finite stock would otherwise be silently removed by Top-K,
        # leaving two finite stocks and falsely claiming full coverage.
        return pd.Series([2.0, 1.0, invalid_score], index=features.index)

    monkeypatch.setattr(type(bundle), "predict", invalid_prediction)
    for invalid_score in (np.inf, -np.inf, np.nan):
        with pytest.raises(FactorLabError, match="model predictions are non-finite"):
            cli.command_forward_signal(
                signal_config,
                _Run("signal-nonfinite-score"),
                date(2026, 9, 24),
                generated_at=datetime(2026, 9, 24, 16, 0, tzinfo=SHANGHAI),
                refresh_data=False,
            )
    dataset.loc[
        dataset["date"].eq(pd.Timestamp("2026-09-24")), "REV1"
    ] = np.nan
    with pytest.raises(FactorLabError, match="factors are incomplete"):
        cli.command_forward_signal(
            signal_config,
            _Run("signal-incomplete"),
            date(2026, 9, 24),
            generated_at=datetime(2026, 9, 24, 16, 0, tzinfo=SHANGHAI),
            refresh_data=False,
        )
    versions = sorted(path.name for path in first_path.parent.parent.iterdir())
    assert versions == ["v000001", "v000002"]


def test_freeze_requires_dataset_end_exactly_at_training_cutoff(tmp_path):
    config = _config(tmp_path, end=date(2026, 9, 21))
    _activate(config)

    with pytest.raises(FactorLabError, match="data.end to equal"):
        cli.command_freeze_forward_model(config, _Run("freeze-wrong-end"))


def test_freeze_is_rejected_on_or_after_activation_date(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _activate(config)
    monkeypatch.setattr(
        cli,
        "_forward_now",
        lambda: datetime(2026, 9, 24, 0, 0, tzinfo=SHANGHAI),
    )
    with pytest.raises(FactorLabError, match="strictly before"):
        cli.command_freeze_forward_model(config, _Run("late-freeze"))


def test_forward_download_child_fails_closed_on_partial_failures(tmp_path, monkeypatch):
    instances = []

    class ChildRun:
        def __init__(self, config, command, arguments):
            self.run_id = "child-download"
            self.payload = {"failures": {}, "outputs": {}}
            self.status = "running"
            instances.append(self)

        def finish(self, status, error=None):
            self.status = status
            self.error = error

    def partial_download(config, run):
        run.payload["failures"] = {
            "market_data": {"600000.SH": "network error"},
            "security_status": {},
        }

    monkeypatch.setattr(cli, "RunManifest", ChildRun)
    with pytest.raises(FactorLabError, match=r"incomplete \(market_data\)"):
        cli._run_forward_child(
            _config(tmp_path),
            _Run("parent"),
            "download-data",
            partial_download,
            require_no_failures=True,
        )
    assert instances[0].status == "failed"
