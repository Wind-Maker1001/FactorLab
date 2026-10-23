"""PIT readers consume one committed generation despite interrupted refreshes."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from factorlab.cli import _load_verified_membership
from factorlab.errors import PointInTimeUniverseError
import factorlab.universe.csi500 as pit


@pytest.fixture
def bundle(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=3)
    output = tmp_path / "universe"
    old = pd.DataFrame({"date": dates, "symbol": "600000.SH", "is_member": True})
    new = old.assign(symbol="000001.SZ")
    config = SimpleNamespace(
        paths=SimpleNamespace(universe_dir=output),
        universe=SimpleNamespace(
            provider="local", expected_member_count=1, index_symbol="000905.SH"
        ),
    )

    def publish(frame):
        source = tmp_path / (frame["symbol"].iloc[0] + ".csv")
        frame.to_csv(source, index=False)
        return pit.import_point_in_time_universe(
            source, output, dates, expected_member_count=1
        )

    previous = publish(old)
    return dates, output, old, new, config, publish, previous


def test_new_import_binds_both_immutable_artifact_bytes(bundle):
    dates, output, old, _, config, _, manifest = bundle
    assert manifest["schema_version"] == 3
    for key in ("daily", "intervals"):
        filename = manifest[key + "_file"]
        assert Path(filename).name == filename
        assert manifest["generation_id"] in filename
        assert manifest[key + "_file_sha256"] == hashlib.sha256(
            (output / filename).read_bytes()
        ).hexdigest()
    pd.testing.assert_frame_equal(_load_verified_membership(config, dates), old)


def test_generation_collision_cannot_overwrite_committed_artifacts(bundle, monkeypatch):
    dates, output, old, new, config, publish, manifest = bundle
    previous_bytes = (output / manifest["daily_file"]).read_bytes()
    monkeypatch.setattr(pit.uuid, "uuid4", lambda: SimpleNamespace(hex=manifest["generation_id"]))
    with pytest.raises(PointInTimeUniverseError, match="generation already exists"):
        publish(new)
    assert (output / manifest["daily_file"]).read_bytes() == previous_bytes
    pd.testing.assert_frame_equal(_load_verified_membership(config, dates), old)


def test_generation_reader_rejects_symlink_escape(bundle, tmp_path):
    dates, output, _, _, config, _, manifest = bundle
    path = output / manifest["daily_file"]
    external = tmp_path / "outside.parquet"
    external.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(external)
    except OSError:
        # Windows without Developer Mode may deny actual symlink creation.
        pytest.skip("symlink creation is not available")
    with pytest.raises(PointInTimeUniverseError, match="path escapes its root"):
        _load_verified_membership(config, dates)


@pytest.mark.parametrize("failure", ["first-generation", "second-generation", "manifest"])
def test_failed_generation_or_manifest_write_leaves_previous_bundle_readable(
    bundle, monkeypatch, failure
):
    dates, output, old, new, config, publish, previous = bundle
    original_manifest = (output / "manifest.json").read_bytes()
    real_parquet = pit._atomic_parquet
    writes = 0

    def fail_generation(frame, path):
        nonlocal writes
        writes += 1
        if writes == {"first-generation": 1, "second-generation": 2}.get(failure):
            raise OSError("injected generation failure")
        return real_parquet(frame, path)

    def fail_manifest(payload, path):
        raise OSError("injected manifest failure")

    monkeypatch.setattr(pit, "_atomic_parquet", fail_generation)
    if failure == "manifest":
        monkeypatch.setattr(pit, "_atomic_json", fail_manifest)
    with pytest.raises(PointInTimeUniverseError, match="cannot publish PIT generation"):
        publish(new)
    assert (output / "manifest.json").read_bytes() == original_manifest
    assert (output / previous["daily_file"]).is_file()
    assert not (output / ".universe-write.lock").exists()
    pd.testing.assert_frame_equal(_load_verified_membership(config, dates), old)


@pytest.mark.parametrize("failed_alias_write", [3, 4])
def test_alias_failure_is_warning_after_successful_generation_commit(
    bundle, monkeypatch, caplog, failed_alias_write
):
    dates, output, _, new, config, publish, previous = bundle
    real_parquet = pit._atomic_parquet
    writes = 0

    def fail_alias(frame, path):
        nonlocal writes
        writes += 1
        if writes == failed_alias_write:
            raise OSError("injected convenience alias failure")
        return real_parquet(frame, path)

    monkeypatch.setattr(pit, "_atomic_parquet", fail_alias)
    current = publish(new)
    assert current["generation_id"] != previous["generation_id"]
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == current
    assert "not refreshed" in caplog.text
    pd.testing.assert_frame_equal(_load_verified_membership(config, dates), new)


def test_generation_reader_ignores_missing_or_tampered_convenience_aliases(bundle):
    dates, output, old, _, config, _, _ = bundle
    (output / pit.PIT_DAILY_ALIAS).unlink()
    (output / pit.PIT_INTERVALS_ALIAS).write_bytes(b"not parquet")
    pd.testing.assert_frame_equal(_load_verified_membership(config, dates), old)


@pytest.mark.parametrize("key", ["daily", "intervals"])
def test_generation_reader_rejects_tampered_artifact_bytes(bundle, key):
    dates, output, _, _, config, _, manifest = bundle
    artifact = output / manifest[key + "_file"]
    artifact.write_bytes(artifact.read_bytes() + b"tamper")
    with pytest.raises(PointInTimeUniverseError, match=key + "_file_sha256 mismatch"):
        _load_verified_membership(config, dates)


@pytest.mark.parametrize(
    "field,value",
    [
        ("daily_file", "../outside.parquet"),
        ("intervals_file", "C:/outside.parquet"),
        ("generation_id", "../outside"),
        ("daily_alias", "other.parquet"),
        ("intervals_alias", "other.parquet"),
        ("daily_file", "csi500_daily." + "f" * 32 + ".parquet"),
    ],
)
def test_generation_reader_requires_exact_safe_artifact_names(bundle, field, value):
    dates, output, _, _, config, _, manifest = bundle
    changed = {**manifest, field: value}
    (output / "manifest.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(PointInTimeUniverseError, match="unexpected generation artifact names"):
        _load_verified_membership(config, dates)


def test_reader_follows_one_manifest_snapshot_when_another_writer_commits(
    bundle, monkeypatch
):
    dates, output, old, new, config, publish, previous = bundle
    real_read_bytes = Path.read_bytes
    manifest_reads = 0

    def commit_after_manifest_read(path):
        nonlocal manifest_reads
        content = real_read_bytes(path)
        if path == output / "manifest.json":
            manifest_reads += 1
            if manifest_reads == 1:
                publish(new)
        return content

    monkeypatch.setattr(Path, "read_bytes", commit_after_manifest_read)
    pd.testing.assert_frame_equal(_load_verified_membership(config, dates), old)
    assert manifest_reads == 1
    current = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert current["generation_id"] != previous["generation_id"]
    pd.testing.assert_frame_equal(_load_verified_membership(config, dates), new)


@pytest.mark.parametrize("failure", ["second-generation", "manifest"])
def test_failed_first_upgrade_preserves_legacy_alias_bundle(bundle, monkeypatch, failure):
    dates, output, old, new, config, publish, previous = bundle
    legacy = {
        key: value
        for key, value in previous.items()
        if key not in {
            "generation_id", "daily_file", "intervals_file", "daily_alias",
            "intervals_alias", "daily_file_sha256", "intervals_file_sha256",
        }
    }
    legacy["schema_version"] = 2
    (output / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")
    original_aliases = {
        alias: (output / alias).read_bytes()
        for alias in (pit.PIT_DAILY_ALIAS, pit.PIT_INTERVALS_ALIAS)
    }
    real_parquet = pit._atomic_parquet
    writes = 0

    def fail_second(frame, path):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected upgrade failure")
        return real_parquet(frame, path)

    def fail_manifest(payload, path):
        raise OSError("injected upgrade manifest failure")

    if failure == "second-generation":
        monkeypatch.setattr(pit, "_atomic_parquet", fail_second)
    else:
        monkeypatch.setattr(pit, "_atomic_json", fail_manifest)
    with pytest.raises(PointInTimeUniverseError, match="cannot publish PIT generation"):
        publish(new)
    for alias, content in original_aliases.items():
        assert (output / alias).read_bytes() == content
    pd.testing.assert_frame_equal(_load_verified_membership(config, dates), old)


def test_collector_publishes_same_generation_contract_and_reuses_snapshots(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=2)

    class SyntheticSnapshotProvider:
        def __init__(self):
            self.calls = 0

        def get_constituents(self, index_symbol, date):
            self.calls += 1
            return pd.DataFrame({"date": [date], "symbol": ["600000.SH"], "is_member": [True]})

    provider = SyntheticSnapshotProvider()
    output = tmp_path / "collected"
    first = pit.collect_point_in_time_universe(
        provider, "000905.SH", dates, output,
        expected_member_count=1, request_interval_seconds=0,
    )
    config = SimpleNamespace(
        paths=SimpleNamespace(universe_dir=output),
        universe=SimpleNamespace(provider="baostock", expected_member_count=1, index_symbol="000905.SH"),
    )
    # Generic snapshot persistence is software evidence, not approval of an
    # unverified BaoStock source for research through the CLI.
    with pytest.raises(PointInTimeUniverseError, match="EFFECTIVE MEMBERSHIP SEMANTICS UNVERIFIED"):
        _load_verified_membership(config, dates)
    daily = pd.read_parquet(output / first["daily_file"])
    intervals = pd.read_parquet(output / first["intervals_file"])
    assert len(daily) == 2
    assert set(daily["date"]) == set(dates)
    assert daily["symbol"].eq("600000.SH").all()
    pd.testing.assert_frame_equal(intervals, pit.compress_membership_intervals(daily))
    for key in ("daily", "intervals"):
        assert first[key + "_file_sha256"] == hashlib.sha256(
            (output / first[key + "_file"]).read_bytes()
        ).hexdigest()
    second = pit.collect_point_in_time_universe(
        provider, "000905.SH", dates, output,
        expected_member_count=1, request_interval_seconds=0,
    )
    assert provider.calls == 2
    assert first["generation_id"] != second["generation_id"]
    assert first["sha256"] == second["sha256"]
    assert (output / first["daily_file"]).is_file()
