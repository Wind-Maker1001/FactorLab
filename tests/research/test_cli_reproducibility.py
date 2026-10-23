"""Offline lifecycle tests for CLI orchestration and reproducibility manifests."""

from datetime import date
import json
from pathlib import Path
import random

import numpy as np
import pandas as pd
import pytest

from factorlab import cli
from factorlab.config import (
    DataConfig,
    PathsConfig,
    ProjectConfig,
    ResearchConfig,
    UniverseConfig,
)
from factorlab.data.schema import canonicalize_bars
from factorlab.errors import (
    ConfigurationError,
    PointInTimeUniverseRequiredError,
    SecurityStatusDataError,
)
from factorlab.reproducibility import (
    RunManifest,
    set_random_seed,
    validate_provenance,
)


def _config(tmp_path, expected_members=2):
    config = ResearchConfig(
        project=ProjectConfig(name="factorlab-test"),
        data=DataConfig(
            start=date(2024, 1, 2),
            end=date(2024, 1, 3),
        ),
        universe=UniverseConfig(expected_member_count=expected_members),
        factors=("REV1",),
        paths=PathsConfig(
            data_root=tmp_path / "data" / "research",
            results_root=tmp_path / "results",
        ),
    )
    object.__setattr__(config, "repo_root", tmp_path)
    object.__setattr__(config, "config_path", tmp_path / "test.yaml")
    config.validate()
    return config


def _manifest_files(config):
    return sorted(config.paths.runs_dir.glob("*/manifest.json"))


def _benchmark_bars():
    return canonicalize_bars(
        pd.DataFrame(
            {
                "date": ["2024-01-02", "2024-01-03"],
                "symbol": "000905.SH",
                "open": [5000.0, 5010.0],
                "high": [5020.0, 5030.0],
                "low": [4990.0, 5000.0],
                "close": [5010.0, 5020.0],
                "volume": [1000.0, 1100.0],
                "amount": [1_000_000.0, 1_100_000.0],
                "turnover": [np.nan, np.nan],
            }
        )
    )


def _benchmark_bars_with_warmup():
    dates = pd.bdate_range(end="2024-01-03", periods=132)
    values = np.arange(len(dates), dtype=float)
    return canonicalize_bars(
        pd.DataFrame(
            {
                "date": dates,
                "symbol": "000905.SH",
                "open": 5000.0 + values,
                "high": 5001.0 + values,
                "low": 4999.0 + values,
                "close": 5000.5 + values,
                "volume": 1000.0,
                "amount": 1_000_000.0,
                "turnover": np.nan,
            }
        )
    )


def test_run_manifest_records_lifecycle_outputs_failures_and_seed(tmp_path, monkeypatch):
    config = _config(tmp_path)
    universe_dir = config.paths.universe_dir
    universe_dir.mkdir(parents=True)
    (universe_dir / "manifest.json").write_text(
        json.dumps({"sha256": "universe-fixture-sha"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "factorlab.reproducibility.git_commit", lambda root: "abc123"
    )
    monkeypatch.setattr(
        "factorlab.reproducibility.package_versions",
        lambda: {"numpy": "fixture"},
    )
    monkeypatch.setattr(
        "factorlab.reproducibility.git_worktree_state",
        lambda root: {"git_dirty": True, "git_diff_sha256": "d" * 64},
    )

    run = RunManifest(config, "unit-test", {"flag": "value"})
    running = json.loads(run.path.read_text(encoding="utf-8"))
    assert running["status"] == "running"
    # A user's project Git identity must not impersonate the executing package.
    from factorlab._identity import software_identity
    assert running["git_commit"] == software_identity()["git_commit"]
    assert running["project_git_commit"] == "abc123"
    assert running["git_dirty"] is True
    assert running["git_diff_sha256"] == "d" * 64
    assert running["package_versions"] == {"numpy": "fixture"}
    assert running["universe_version"] == "universe-fixture-sha"
    assert running["factor_list"] == ["REV1"]
    assert running["data_start"] == "2024-01-02"
    assert running["data_end"] == "2024-01-03"

    run.add_output("artifact", tmp_path / "artifact.parquet")
    run.add_failures("symbols", {"600000.SH": "fixture failure"})
    run.finish("success")
    completed = json.loads(run.path.read_text(encoding="utf-8"))
    assert completed["status"] == "success"
    assert completed["completed_at"] is not None
    assert completed["outputs"]["artifact"].endswith("artifact.parquet")
    assert completed["failures"]["symbols"]["600000.SH"] == "fixture failure"
    assert not list(run.path.parent.glob("*.tmp-*"))

    set_random_seed(19)
    first = (random.random(), np.random.random())
    set_random_seed(19)
    assert first == (random.random(), np.random.random())


def _membership():
    research_dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    return pd.DataFrame(
        [
            {"date": day, "symbol": symbol, "is_member": True}
            for day in research_dates
            for symbol in ("000001.SZ", "600000.SH")
        ]
    )


def _complete_security_data(calendar):
    dates = pd.DatetimeIndex(calendar)
    symbols = ("000001.SZ", "600000.SH")
    status = pd.DataFrame(
        [
            {
                "date": day,
                "symbol": symbol,
                "trade_status": 1,
                "is_st": False,
            }
            for day in dates
            for symbol in symbols
        ]
    )
    listings = pd.DataFrame(
        {
            "symbol": symbols,
            "listing_date": ["1991-04-03", "1999-11-10"],
        }
    )
    return status, listings


def test_validate_provenance_rejects_missing_or_unknown_values():
    complete = {
        "git_commit": "a" * 40,
        "git_dirty": False,
        "git_diff_sha256": None,
        "python_version": "3.9.25",
        "package_versions": {"numpy": "fixture", "akshare": None},
    }
    assert validate_provenance(complete) == complete

    missing = dict(complete)
    missing.pop("package_versions")
    with pytest.raises(ValueError, match="missing fields: package_versions"):
        validate_provenance(missing)

    unknown = dict(complete)
    unknown["git_dirty"] = None
    with pytest.raises(ValueError, match="git_dirty is unknown"):
        validate_provenance(unknown)

    malformed_commit = dict(complete, git_commit="abc123")
    with pytest.raises(ValueError, match="git_commit is unknown"):
        validate_provenance(malformed_commit)

    dirty_without_hash = dict(complete, git_dirty=True)
    with pytest.raises(ValueError, match="git_diff_sha256 is unknown"):
        validate_provenance(dirty_without_hash)


def test_audit_cli_success_creates_completed_manifest(tmp_path, monkeypatch):
    config = _config(tmp_path)
    docs = tmp_path / "docs" / "research"
    docs.mkdir(parents=True)
    (docs / "ARCHITECTURE_AUDIT.md").write_text("audit", encoding="utf-8")
    (docs / "KNOWN_LEGACY_ISSUES.md").write_text("known", encoding="utf-8")
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        "factorlab.reproducibility.git_commit", lambda root: "audit-sha"
    )
    monkeypatch.setattr(
        "factorlab.reproducibility.package_versions", lambda: {}
    )

    assert cli.main(["audit", "--config", "unused.yaml"]) == 0
    manifests = _manifest_files(config)
    assert len(manifests) == 1
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["command"] == "audit"
    assert payload["status"] == "success"
    assert payload["completed_at"] is not None
    assert payload["architecture_audit"]["protocol"] == "FACTORLAB_NAMESPACE_MIGRATION_V1"
    assert payload["architecture_audit"]["source_sha256"]
    assert payload["architecture_audit"]["resource_sha256"]
    assert payload["formal_admission"] == "FORMAL_ADMISSION_BLOCKED"


def test_audit_cli_failure_after_config_is_persisted(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        "factorlab.reproducibility.package_versions", lambda: {}
    )
    monkeypatch.setattr("factorlab._identity.package_root", lambda: tmp_path / "missing-package")

    assert cli.main(["audit", "--config", "unused.yaml"]) == 1
    manifests = _manifest_files(config)
    assert len(manifests) == 1
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["completed_at"] is not None
    assert "Installed software audit resources missing" in payload["error"]


def test_config_load_failure_returns_two_before_manifest_location_is_known(
    tmp_path, monkeypatch
):
    def fail(_path):
        raise ConfigurationError("fixture configuration failure")

    monkeypatch.setattr(cli, "load_config", fail)
    assert cli.main(["audit", "--config", "invalid.yaml"]) == 2
    assert not (tmp_path / "results" / "runs").exists()


def test_manifest_initialisation_failure_is_a_clean_cli_error(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda path: config)

    def fail_manifest(*args, **kwargs):
        raise OSError("fixture manifest failure")

    monkeypatch.setattr(cli, "RunManifest", fail_manifest)
    assert cli.main(["audit", "--config", "unused.yaml"]) == 2


def test_manifest_finalisation_failure_returns_clean_nonzero_status(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    docs = tmp_path / "docs" / "research"
    docs.mkdir(parents=True)
    (docs / "ARCHITECTURE_AUDIT.md").write_text("audit", encoding="utf-8")
    (docs / "KNOWN_LEGACY_ISSUES.md").write_text("known", encoding="utf-8")
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    real_manifest = cli.RunManifest

    class BrokenFinaliseManifest(real_manifest):
        def finish(self, status="success", error=None):
            raise OSError("fixture finalisation failure")

    monkeypatch.setattr(cli, "RunManifest", BrokenFinaliseManifest)

    assert cli.main(["audit", "--config", "unused.yaml"]) == 1


def test_import_universe_cli_orchestrates_without_network(tmp_path, monkeypatch):
    config = _config(tmp_path)
    object.__setattr__(config.universe, "provider", "local")
    seen = {}

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        cli, "_load_benchmark_with_warmup", lambda current: _benchmark_bars()
    )
    monkeypatch.setattr(
        "factorlab.reproducibility.package_versions", lambda: {}
    )

    def fake_import(
        input_path, output_dir, calendar, expected_member_count, index_symbol
    ):
        seen["input_path"] = input_path
        seen["output_dir"] = output_dir
        seen["calendar"] = calendar
        seen["expected_member_count"] = expected_member_count
        seen["index_symbol"] = index_symbol
        return {
            "sha256": "imported-universe-sha",
            "trading_dates": len(calendar),
            "input_layout": "inclusive-intervals",
            "source_file_sha256": "a" * 64,
        }

    monkeypatch.setattr(cli, "import_point_in_time_universe", fake_import)
    assert (
        cli.main(
            [
                "import-universe",
                "--config",
                "unused.yaml",
                "--input",
                "membership.csv",
            ]
        )
        == 0
    )

    assert seen["input_path"] == (config.repo_root / "membership.csv").resolve()
    assert seen["output_dir"] == config.paths.universe_dir
    assert seen["expected_member_count"] == 2
    assert seen["index_symbol"] == config.universe.index_symbol
    assert seen["calendar"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    payload = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["universe_version"] == "imported-universe-sha"
    assert payload["universe_input_layout"] == "inclusive-intervals"
    assert payload["universe_source_file_sha256"] == "a" * 64
    assert payload["outputs"]["universe"].endswith("csi500_daily.parquet")
    assert payload["outputs"]["universe_intervals"].endswith(
        "csi500_intervals.parquet"
    )
    assert payload["outputs"]["universe_manifest"].endswith("manifest.json")


def test_import_universe_requires_explicit_local_provider(tmp_path):
    config = _config(tmp_path)

    with pytest.raises(
        PointInTimeUniverseRequiredError,
        match="POINT-IN-TIME UNIVERSE REQUIRED.*provider=local",
    ):
        cli.command_import_universe(config, object(), "membership.csv")


def test_import_security_data_cli_orchestrates_without_baostock(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    object.__setattr__(config.universe, "status_provider", "local")
    full_benchmark = _benchmark_bars_with_warmup()
    membership = _membership()
    seen = {}

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        cli, "_load_benchmark_with_warmup", lambda current: full_benchmark
    )
    monkeypatch.setattr(
        cli, "_load_verified_membership", lambda current, calendar: membership
    )
    monkeypatch.setattr(
        "factorlab.reproducibility.package_versions", lambda: {}
    )

    def fake_import(
        status_path,
        listings_path,
        *,
        output_dir,
        calendar,
        membership,
        universe_sha256,
    ):
        seen.update(
            {
                "status_path": status_path,
                "listings_path": listings_path,
                "output_dir": output_dir,
                "calendar": calendar,
                "membership": membership,
                "universe_sha256": universe_sha256,
            }
        )
        return {
            "security_data_version": "c" * 64,
            "source": "local-import",
            "source_version": "user-security-data-import-v1",
            "status_sha256": "d" * 64,
            "listings_sha256": "e" * 64,
            "input_sha256": {"status": "f" * 64, "listings": "0" * 64},
            "input_files": {
                "status": str(status_path.resolve()),
                "listings": str(listings_path.resolve()),
            },
            "status_rows": 264,
            "symbols": 2,
        }

    class ExplodingBaoStock:
        def __init__(self, **kwargs):
            raise AssertionError("import-security-data must not construct BaoStock")

    monkeypatch.setattr(cli, "import_security_data", fake_import)
    monkeypatch.setattr(cli, "BaoStockPointInTimeProvider", ExplodingBaoStock)

    assert (
        cli.main(
            [
                "import-security-data",
                "--config",
                "unused.yaml",
                "--status",
                "status.csv",
                "--listings",
                "listings.csv",
            ]
        )
        == 0
    )

    assert seen["status_path"] == (config.repo_root / "status.csv").resolve()
    assert seen["listings_path"] == (config.repo_root / "listings.csv").resolve()
    assert seen["output_dir"] == config.paths.universe_dir
    assert seen["calendar"].equals(pd.DatetimeIndex(full_benchmark["date"]))
    assert seen["universe_sha256"] == cli.dataframe_sha256(membership)
    payload = json.loads(_manifest_files(config)[0].read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["security_data_version"] == "c" * 64
    assert payload["security_status_sha256"] == "d" * 64
    assert payload["listing_dates_sha256"] == "e" * 64
    assert payload["outputs"]["security_data_manifest"].endswith(
        "security_data_manifest.json"
    )
    assert json.loads(
        (config.paths.universe_dir / "status_failures.json").read_text(
            encoding="utf-8"
        )
    ) == {
        "symbols": [],
        "error": None,
        "security_data_version": "c" * 64,
    }


def test_import_security_data_requires_local_status_provider(tmp_path):
    config = _config(tmp_path)

    with pytest.raises(
        SecurityStatusDataError,
        match="SECURITY STATUS DATA REQUIRED.*status_provider=local",
    ):
        cli.command_import_security_data(
            config,
            object(),
            "status.csv",
            "listings.csv",
        )


def test_benchmark_warmup_selects_exact_prior_sessions(tmp_path):
    config = _config(tmp_path)
    selected = cli._select_benchmark_with_warmup(
        config, _benchmark_bars_with_warmup()
    )
    start = pd.Timestamp(config.data.start)
    assert len(selected[selected["date"] < start]) == 130
    assert selected[selected["date"] >= start]["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]


def test_benchmark_warmup_rejects_missing_configured_start(tmp_path):
    config = _config(tmp_path)
    bars = _benchmark_bars_with_warmup()
    bars = bars.loc[bars["date"] != pd.Timestamp(config.data.start)]

    with pytest.raises(
        cli.FactorLabError,
        match=r"do not contain configured data\.start 2024-01-02",
    ):
        cli._select_benchmark_with_warmup(config, bars)


def test_benchmark_warmup_rejects_truncated_configured_end(tmp_path):
    config = _config(tmp_path)
    bars = _benchmark_bars_with_warmup()
    bars = bars.loc[bars["date"] < pd.Timestamp(config.data.end)]

    with pytest.raises(
        cli.FactorLabError,
        match=r"do not contain configured data\.end 2024-01-03",
    ):
        cli._select_benchmark_with_warmup(config, bars)


def test_download_reuses_valid_local_import_instead_of_recollecting_pit(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    object.__setattr__(config.universe, "provider", "local")
    config.paths.universe_dir.mkdir(parents=True)
    (config.paths.universe_dir / "manifest.json").write_text(
        json.dumps({"source": "local-import", "sha256": "local-sha"}),
        encoding="utf-8",
    )
    full_benchmark = _benchmark_bars_with_warmup()
    research_dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    membership = pd.DataFrame(
        [
            {"date": day, "symbol": symbol, "is_member": True}
            for day in research_dates
            for symbol in ("000001.SZ", "600000.SH")
        ]
    )
    calls = {"stock": [], "status": None}

    class FakeCache:
        def data_path(self, symbol):
            return tmp_path / (symbol + ".parquet")

        def update_from_provider(self, provider, symbol, start, end, is_index=False):
            calls["stock"].append((symbol, pd.Timestamp(start), pd.Timestamp(end)))

    class FakePit:
        def __init__(self, **kwargs):
            calls["provider_options"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get_security_history(self, symbols, start, end):
            calls["status"] = (tuple(symbols), pd.Timestamp(start), pd.Timestamp(end))
            status, listings = _complete_security_data(full_benchmark["date"])
            return status, listings, []

    class FakeRun:
        def __init__(self):
            self.payload = {}

        def add_output(self, *args):
            return None

        def add_failures(self, *args):
            return None

    monkeypatch.setattr(cli, "_provider", lambda current: object())
    monkeypatch.setattr(
        cli, "_download_benchmark_with_warmup", lambda current, provider: full_benchmark
    )
    monkeypatch.setattr(cli, "_benchmark_cache", lambda current: FakeCache())
    monkeypatch.setattr(cli, "_stock_cache", lambda current: FakeCache())
    monkeypatch.setattr(
        cli, "_load_verified_membership", lambda current, calendar: membership
    )
    monkeypatch.setattr(cli, "BaoStockPointInTimeProvider", FakePit)
    monkeypatch.setattr(
        cli,
        "collect_point_in_time_universe",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("local PIT must not be overwritten")
        ),
    )
    monkeypatch.setattr(cli, "_atomic_parquet", lambda *args: None)
    monkeypatch.setattr(cli, "_atomic_json", lambda *args: None)

    run = FakeRun()
    cli.command_download(config, run)

    full_start = full_benchmark["date"].min()
    full_end = full_benchmark["date"].max()
    assert run.payload["universe_version"] == cli.dataframe_sha256(membership)
    assert len(run.payload["security_data_version"]) == 64
    assert calls["provider_options"] == {
        "retry_count": config.network.max_retries,
        "backoff_seconds": config.network.backoff_seconds,
        "request_interval": config.network.request_interval_seconds,
    }
    assert calls["status"][1:] == (full_start, full_end)
    assert len(calls["stock"]) == 2
    assert all(call[1:] == (full_start, full_end) for call in calls["stock"])


def test_download_baostock_does_not_reuse_or_publish_unverified_membership(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    config.paths.universe_dir.mkdir(parents=True)
    (config.paths.universe_dir / "manifest.json").write_text(
        json.dumps({"source": "local-import", "sha256": "stale-local-sha"}),
        encoding="utf-8",
    )
    full_benchmark = _benchmark_bars_with_warmup()
    research_dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    membership = pd.DataFrame(
        [
            {"date": day, "symbol": symbol, "is_member": True}
            for day in research_dates
            for symbol in ("000001.SZ", "600000.SH")
        ]
    )
    calls = {"collect": 0, "collect_retries": []}

    class FakeCache:
        def data_path(self, symbol):
            return tmp_path / (symbol + ".parquet")

        def update_from_provider(self, provider, symbol, start, end, is_index=False):
            return None

    class FakePit:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get_security_history(self, symbols, start, end):
            status, listings = _complete_security_data(full_benchmark["date"])
            return status, listings, []

    class FakeRun:
        def __init__(self):
            self.payload = {}

        def add_output(self, *args):
            return None

        def add_failures(self, *args):
            return None

    def fake_collect(*args, **kwargs):
        calls["collect"] += 1
        calls["collect_retries"].append(kwargs["retries"])
        return {"source": "FakePit", "sha256": "baostock-sha"}

    monkeypatch.setattr(cli, "_provider", lambda current: object())
    monkeypatch.setattr(
        cli, "_download_benchmark_with_warmup", lambda current, provider: full_benchmark
    )
    monkeypatch.setattr(cli, "_benchmark_cache", lambda current: FakeCache())
    monkeypatch.setattr(cli, "_stock_cache", lambda current: FakeCache())
    monkeypatch.setattr(
        cli, "_load_verified_membership", lambda current, calendar: membership
    )
    monkeypatch.setattr(cli, "BaoStockPointInTimeProvider", FakePit)
    monkeypatch.setattr(cli, "collect_point_in_time_universe", fake_collect)
    monkeypatch.setattr(cli, "_atomic_parquet", lambda *args: None)
    monkeypatch.setattr(cli, "_atomic_json", lambda *args: None)

    run = FakeRun()
    with pytest.raises(
        PointInTimeUniverseRequiredError,
        match="EFFECTIVE MEMBERSHIP SEMANTICS UNVERIFIED",
    ):
        cli.command_download(config, run)

    assert calls["collect"] == 0
    assert calls["collect_retries"] == []
    assert "universe_version" not in run.payload
    manifest = json.loads(
        (config.paths.universe_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest == {"source": "local-import", "sha256": "stale-local-sha"}


def test_download_local_membership_and_status_never_constructs_baostock(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    object.__setattr__(config.universe, "provider", "local")
    object.__setattr__(config.universe, "status_provider", "local")
    config.paths.universe_dir.mkdir(parents=True)
    (config.paths.universe_dir / "manifest.json").write_text(
        json.dumps({"source": "local-import", "sha256": "local-sha"}),
        encoding="utf-8",
    )
    full_benchmark = _benchmark_bars_with_warmup()
    membership = _membership()
    status, listings = _complete_security_data(full_benchmark["date"])
    security_manifest = cli.publish_security_data(
        status,
        listings,
        output_dir=config.paths.universe_dir,
        calendar=pd.DatetimeIndex(full_benchmark["date"]),
        membership=membership,
        universe_sha256=cli.dataframe_sha256(membership),
        source="local-import",
        source_version="user-security-data-import-v1",
        input_hashes={"status": "a" * 64, "listings": "b" * 64},
        input_files={
            "status": str((tmp_path / "status.csv").resolve()),
            "listings": str((tmp_path / "listings.csv").resolve()),
        },
    )
    stock_calls = []

    class FakeCache:
        def data_path(self, symbol):
            return tmp_path / (symbol + ".parquet")

        def update_from_provider(self, provider, symbol, start, end, is_index=False):
            stock_calls.append(symbol)

    class ExplodingBaoStock:
        def __init__(self, **kwargs):
            raise AssertionError("local+local must not construct BaoStock")

    class FakeRun:
        def __init__(self):
            self.payload = {}

        def add_output(self, *args):
            return None

        def add_failures(self, *args):
            return None

    monkeypatch.setattr(cli, "_provider", lambda current: object())
    monkeypatch.setattr(
        cli, "_download_benchmark_with_warmup", lambda current, provider: full_benchmark
    )
    monkeypatch.setattr(cli, "_benchmark_cache", lambda current: FakeCache())
    monkeypatch.setattr(cli, "_stock_cache", lambda current: FakeCache())
    monkeypatch.setattr(
        cli, "_load_verified_membership", lambda current, calendar: membership
    )
    monkeypatch.setattr(cli, "BaoStockPointInTimeProvider", ExplodingBaoStock)
    monkeypatch.setattr(
        cli,
        "collect_point_in_time_universe",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("local PIT must not be overwritten")
        ),
    )

    run = FakeRun()
    cli.command_download(config, run)

    assert stock_calls == ["000001.SZ", "600000.SH"]
    assert run.payload["security_data_version"] == security_manifest[
        "security_data_version"
    ]
    status_diagnostics = json.loads(
        (config.paths.universe_dir / "status_failures.json").read_text(
            encoding="utf-8"
        )
    )
    assert status_diagnostics == {
        "symbols": [],
        "error": None,
        "security_data_version": security_manifest["security_data_version"],
    }


def test_partial_baostock_status_refresh_does_not_replace_complete_bundle(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    object.__setattr__(config.universe, "provider", "local")
    full_benchmark = _benchmark_bars_with_warmup()
    old_membership = _membership()
    old_status, old_listings = _complete_security_data(full_benchmark["date"])
    cli.publish_security_data(
        old_status,
        old_listings,
        output_dir=config.paths.universe_dir,
        calendar=pd.DatetimeIndex(full_benchmark["date"]),
        membership=old_membership,
        universe_sha256=cli.dataframe_sha256(old_membership),
        source="BaoStockPointInTimeProvider",
        source_version="baostock-security-history-v1",
    )
    protected_paths = [
        config.paths.universe_dir / "security_status.parquet",
        config.paths.universe_dir / "listing_dates.parquet",
        config.paths.universe_dir / "security_data_manifest.json",
    ]
    before = {path.name: path.read_bytes() for path in protected_paths}
    current_membership = old_membership.copy()
    current_membership.loc[
        current_membership["symbol"].eq("000001.SZ"), "symbol"
    ] = "000002.SZ"
    pit_source = tmp_path / "synthetic-pit.csv"
    current_membership.to_csv(pit_source, index=False, date_format="%Y-%m-%d")
    cli.import_point_in_time_universe(
        pit_source, config.paths.universe_dir,
        pd.DatetimeIndex(current_membership["date"].drop_duplicates()),
        config.universe.expected_member_count, config.universe.index_symbol,
    )

    class FakeCache:
        def data_path(self, symbol):
            return tmp_path / (symbol + ".parquet")

        def update_from_provider(self, provider, symbol, start, end, is_index=False):
            return None

    class FakePit:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get_security_history(self, symbols, start, end):
            partial_status = old_status.loc[
                old_status["symbol"].eq("600000.SH")
            ].reset_index(drop=True)
            partial_listings = old_listings.loc[
                old_listings["symbol"].eq("600000.SH")
            ].reset_index(drop=True)
            return partial_status, partial_listings, ["000002.SZ"]

    class FakeRun:
        def __init__(self):
            self.run_id = "20240101T000000-download-data"
            self.payload = {}
            self.failures = {}

        def add_output(self, *args):
            return None

        def add_failures(self, name, values):
            self.failures[name] = values

    monkeypatch.setattr(cli, "_provider", lambda current: object())
    monkeypatch.setattr(
        cli, "_download_benchmark_with_warmup", lambda current, provider: full_benchmark
    )
    monkeypatch.setattr(cli, "_benchmark_cache", lambda current: FakeCache())
    monkeypatch.setattr(cli, "_stock_cache", lambda current: FakeCache())
    monkeypatch.setattr(
        cli,
        "_load_verified_membership",
        lambda current, calendar: current_membership,
    )
    monkeypatch.setattr(cli, "BaoStockPointInTimeProvider", FakePit)
    monkeypatch.setattr(
        cli,
        "collect_point_in_time_universe",
        lambda *args, **kwargs: {"sha256": "f" * 64},
    )

    run = FakeRun()
    with pytest.raises(
        SecurityStatusDataError, match="complete bundle not published"
    ):
        cli.command_download(config, run)

    after = {path.name: path.read_bytes() for path in protected_paths}
    assert after == before
    assert (
        config.paths.universe_dir
        / "security_status.partial.20240101T000000-download-data.parquet"
    ).exists()
    assert not (
        config.paths.universe_dir / "security_status.partial.parquet"
    ).exists()
    assert run.failures["security_status"]["symbols"] == ["000002.SZ"]
