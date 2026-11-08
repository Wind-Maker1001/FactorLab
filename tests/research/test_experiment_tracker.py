from copy import deepcopy
import json

import pytest

from factorlab.experiments import ExperimentTracker
from factorlab.experiments.adapters import walk_forward_metadata


@pytest.fixture
def definition():
    return dict(model="ridge", parameters={"alpha": 1}, factors=[dict(factor_id="REV1", version="1", definition_hash="a" * 64)], dataset={"sha256": "b" * 64, "source": "synthetic"}, label={"name": "EXCESS_RET5"}, windows={"train": ["2020-01-01", "2020-04-30"], "validation": ["2020-05-01", "2020-06-30"], "test": ["2020-07-01", "2020-08-31"]}, walk_forward={"purge_days": 5}, costs={"mode": "NOT_APPLICABLE_PREDICTION"})


def add(tracker, name="r1", experiment="e1", **kwargs):
    options = dict(code={"commit": "c" * 40, "tree": "d" * 40}, environment={"python": "3.13"}, command=["python", "research.py"], status="SUCCEEDED", qualification="SYNTHETIC", prediction_metrics={"rank_ic": None}, validation={"sample_hash": "e" * 64, "evaluation": {"minimum_cross_section": 4}})
    options.update(kwargs)
    return tracker.append_run(experiment, name, **options)


def test_create_query_read_and_repeat(tmp_path, definition):
    tracker = ExperimentTracker(tmp_path)
    tracker.create_experiment("e1", definition)
    first = add(tracker)
    add(tracker, "r2")
    assert tracker.get_run("r1") == first
    assert len(tracker.query(model="ridge", factor="REV1", dataset=definition["dataset"], status="SUCCEEDED", qualification="SYNTHETIC", windows={"test": definition["windows"]["test"]})) == 2
    assert tracker.query(model="other") == []
    with pytest.raises(FileExistsError):
        tracker.create_experiment("e1", definition)
    with pytest.raises(FileExistsError):
        add(tracker)
    assert tracker.get_run("r1") == first
    assert tracker.compare(["r1", "r2"])["comparable"]


def test_identity_and_artifacts(tmp_path, definition):
    tracker = ExperimentTracker(tmp_path / "store")
    tracker.create_experiment("e1", definition)
    add(tracker)
    for kwargs in ({"expected_code": {}}, {"expected_dataset": {}}):
        with pytest.raises(ValueError):
            tracker.reproduction("r1", **kwargs)
    record = tracker.get_experiment("e1")
    record["payload"]["parameters"]["alpha"] = 2
    (tracker.root / "experiments/e1.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="checksum"):
        tracker.get_run("r1")


@pytest.mark.parametrize("key,value", [("windows", {"train": None, "validation": None, "test": ["2021-01-01", "2021-02-01"]}), ("dataset", {"sha256": "different"}), ("label", {"name": "OTHER"}), ("costs", {"bps": 10})])
def test_incomparable(tmp_path, definition, key, value):
    tracker = ExperimentTracker(tmp_path)
    tracker.create_experiment("e1", definition)
    other = deepcopy(definition)
    other[key] = value
    tracker.create_experiment("e2", other)
    add(tracker)
    add(tracker, "r2", "e2")
    with pytest.raises(ValueError, match="Incomparable"):
        tracker.compare(["r1", "r2"])


def test_unknown_and_missing(tmp_path, definition):
    tracker = ExperimentTracker(tmp_path)
    tracker.create_experiment("e1", definition)
    add(tracker, status="NOT_RUN", prediction_metrics={}, qualification="UNKNOWN")
    run = tracker.get_run("r1")["payload"]["run"]
    assert run["prediction_metrics"] == {} and run["portfolio_metrics"] == {}
    assert run["test_status"] == "NOT_RUN"
    with pytest.raises(ValueError):
        tracker.compare(["r1", "r1"])
    with pytest.raises(ValueError):
        add(tracker, "bad", status="NOT_RUN")


@pytest.mark.parametrize("kwargs", [{"qualification": "VERIFIED"}, {"status": "VERIFIED"}, {"prediction_metrics": {"ic": float("nan")}}, {"prediction_metrics": {"ic": True}}])
def test_invalid_runs(tmp_path, definition, kwargs):
    tracker = ExperimentTracker(tmp_path)
    tracker.create_experiment("e1", definition)
    with pytest.raises(ValueError):
        add(tracker, **kwargs)
    assert tracker.query() == []


def test_rebuild_and_bad_input(tmp_path, definition):
    tracker = ExperimentTracker(tmp_path)
    tracker.create_experiment("e1", definition)
    add(tracker)
    tracker.index.unlink()
    tracker = ExperimentTracker(tmp_path)
    assert tracker.query() == []
    assert tracker.rebuild_index() == 2
    assert len(tracker.query()) == 1
    (tmp_path / "runs/bad.json").write_text("{}")
    with pytest.raises(ValueError):
        tracker.rebuild_index()
    assert len(tracker.query()) == 1
    with pytest.raises(ValueError):
        tracker.create_experiment("../escape", definition)


def test_actual_walk_forward_adapter(tmp_path):
    from tests.research.test_walk_forward_runner_phase2 import _dataset, _splitter
    from factorlab.validation.walk_forward import WalkForwardRunner
    dates, data = _dataset(end="2020-09-30")
    result = WalkForwardRunner(_splitter(), model_name="ridge", feature_names=["REV1", "MOM20"], ridge_alphas=[1.0], top_k=2, minimum_cross_section=4).run(data, calendar=dates)
    metadata = walk_forward_metadata(result, top_k=2, minimum_cross_section=4)
    assert "prediction_rank_ic_mean" in metadata["prediction_metrics"]
    assert len(metadata["validation"]["folds"]) == len(result.fold_summary)
    assert len(metadata["validation"]["sample_hash"]) == 64
    assert "portfolio_metrics" not in metadata
    from dataclasses import replace
    alternate = result.predictions.copy()
    alternate["model_name"] = "other"
    alternate["selected_alpha"] = 2.0
    alternate["selected_parameters"] = '{"alpha":2}'
    second = walk_forward_metadata(replace(result, predictions=alternate), top_k=2, minimum_cross_section=4)
    assert second["validation"]["sample_hash"] == metadata["validation"]["sample_hash"]


def test_artifact_and_sample_mismatch(tmp_path, definition):
    from factorlab.dataset.identity import file_hash
    tracker = ExperimentTracker(tmp_path / "store")
    tracker.create_experiment("e1", definition)
    artifact = tmp_path / "report.txt"
    artifact.write_text("result")
    add(tracker, artifacts={"report": {"path": str(artifact), "sha256": file_hash(artifact)}})
    tracker.reproduction("r1", verify_artifacts=True)
    artifact.write_text("changed")
    with pytest.raises(ValueError, match="Artifact"):
        tracker.reproduction("r1", verify_artifacts=True)
    add(tracker, "r2", validation={"sample_hash": "different", "evaluation": {"minimum_cross_section": 4}})
    with pytest.raises(ValueError):
        tracker.compare(["r1", "r2"])


def test_provenance(tmp_path):
    import subprocess
    from factorlab.experiments.provenance import capture_provenance
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
    receipt = capture_provenance(tmp_path)
    assert len(receipt["code"]["commit"]) == 40
    assert len(receipt["code"]["tree"]) == 40
    assert receipt["code"]["git_dirty"] is False
    assert receipt["environment"]["python_version"].startswith("3.13")


def test_index_failure_preserves_record(tmp_path, definition, monkeypatch):
    tracker = ExperimentTracker(tmp_path)
    tracker.create_experiment("e1", definition)
    original = tracker._index
    def fail(*args):
        raise OSError("index unavailable")
    monkeypatch.setattr(tracker, "_index", fail)
    with pytest.raises(OSError):
        add(tracker)
    assert tracker.get_run("r1")["id"] == "r1"
    monkeypatch.setattr(tracker, "_index", original)
    assert tracker.rebuild_index() == 2
    assert len(tracker.query()) == 1
