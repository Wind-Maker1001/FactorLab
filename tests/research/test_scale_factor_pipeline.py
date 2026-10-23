"""Engineering benchmark regression; all prices/members/status are artificial."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket

import pandas as pd
import pytest

from examples.research import scale_factor_pipeline as example
from factorlab.config import PathsConfig, load_config
from factorlab.data.schema import validate_bars
from factorlab.errors import ConfigurationError


@pytest.fixture
def cli(tmp_path, monkeypatch):
    original = load_config("configs/research/csi500_5y.yaml")
    config = replace(original, paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"))
    object.__setattr__(config, "repo_root", original.repo_root)
    object.__setattr__(config, "config_path", original.config_path)
    monkeypatch.setattr(example, "load_config", lambda path: config)
    def forbidden(*args, **kwargs):
        raise AssertionError("synthetic engineering benchmark must not connect to network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return config


def latest(config):
    return json.loads(sorted(config.paths.runs_dir.glob("*/manifest.json"))[-1].read_text(encoding="utf-8"))


def execute(extra=()):
    return example.main(["--stocks", "20", "--research-days", "20", "--warmup-days", "130", *extra])


def test_generator_seed_schema_and_explicit_artificial_dimensions():
    left, right = example.synthetic_inputs(20, 20, 130, 42), example.synthetic_inputs(20, 20, 130, 42)
    for a, b in zip(left[:5], right[:5]):
        pd.testing.assert_frame_equal(a, b, check_exact=True)
    pd.testing.assert_index_equal(left[-1], right[-1])
    bars, benchmark, membership, status, listings, dates = left
    assert len(bars) == len(status) == 3000 and len(membership) == 400
    assert len(benchmark) == len(dates) == 150 and len(listings) == 20
    assert membership["date"].min() == dates[130] and membership["is_member"].all()
    assert validate_bars(bars) is None
    assert validate_bars(benchmark) is None
    assert not bars.equals(example.synthetic_inputs(20, 20, 130, 43)[0])


def test_actual_engines_full_dimensions_future_mutation_label_tail_and_artifact_hashes(cli):
    assert execute() == 0
    m = latest(cli)
    assert m["status"] == "success" and m["synthetic_only"] is True
    assert m["point_in_time_universe_verified"] is m["research_conclusions_permitted"] is m["forward_evidence"] is False
    assert m["network_requests"] == 0
    measured = m["measurements"]
    assert measured["dataset_rows"] == 400 and measured["input_rows"] == 3000
    assert measured["mature_label_rows"] == 280 and measured["daily_ic_rows"] == 280
    assert measured["lookahead_prefix_rows_checked"] == 220 and measured["lookahead_prefix_exact_match"] is True
    assert measured["past_labels_affected_by_future_mutation"] == 100
    assert measured["process_peak_rss_measured"] is False
    assert m["synthetic_date_range"]["calendar_is_simulated_not_exchange_calendar"] is True
    assert all(value >= 0 for value in m["timings_seconds"].values())
    for value in m["output_artifacts"].values():
        assert value["sha256"] == hashlib.sha256(Path(value["path"]).read_bytes()).hexdigest()
    data = pd.read_parquet(m["outputs"]["synthetic_dataset"])
    assert len(data) == 400 and not data.duplicated(["date", "symbol"]).any()
    assert not cli.paths.data_root.exists() and not cli.paths.universe_dir.exists()


def test_regeneration_preserves_prior_run_and_same_seed_dataset(cli):
    assert execute() == 0
    old = latest(cli)
    raw = {value["path"]: Path(value["path"]).read_bytes() for value in old["output_artifacts"].values()}
    first = pd.read_parquet(old["outputs"]["synthetic_dataset"])
    assert execute() == 0
    new = latest(cli)
    assert new["run_id"] != old["run_id"]
    pd.testing.assert_frame_equal(first, pd.read_parquet(new["outputs"]["synthetic_dataset"]), check_exact=True)
    assert all(Path(path).read_bytes() == content for path, content in raw.items())


@pytest.mark.parametrize("extra", [("--stocks", "19"), ("--stocks", "1001"), ("--research-days", "1"),
                                   ("--research-days", "2001"), ("--warmup-days", "60"), ("--warmup-days", "501"),
                                   ("--stocks", "1000", "--research-days", "2000")],
                         ids=["small-cross-section", "too-many-stocks", "too-few-days", "too-many-days", "short-warmup",
                              "long-warmup", "large-grid"])
def test_bounds_fail_before_allocating_inputs(cli, extra, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid benchmark dimensions must not allocate inputs")
    monkeypatch.setattr(example, "synthetic_inputs", forbidden)
    assert execute(extra) == 1
    m = latest(cli)
    assert m["status"] == "failed" and "ConfigurationError" in m["error"] and not m["outputs"]


def test_engine_failure_records_terminal_manifest_without_formal_outputs(cli, monkeypatch):
    def failed(*args, **kwargs):
        raise ConfigurationError("artificial failed measurement")
    monkeypatch.setattr(example.DatasetBuilder, "build", failed)
    assert execute() == 1
    assert latest(cli)["status"] == "failed" and not cli.paths.processed_dir.exists()


def test_output_collision_does_not_replace_existing_directory(cli, monkeypatch):
    real = example.RunManifest
    def manifest(*args, **kwargs):
        m = real(*args, **kwargs)
        path = cli.paths.results_root / "diagnostics" / "scale-factor-pipeline" / m.run_id / "synthetic-inputs"
        path.mkdir(parents=True)
        (path / "sentinel").write_text("preserve", encoding="utf-8")
        return m
    monkeypatch.setattr(example, "RunManifest", manifest)
    assert execute() == 1 and latest(cli)["status"] == "failed"
    assert next(cli.paths.results_root.rglob("sentinel")).read_text(encoding="utf-8") == "preserve"
