"""Fold-local reads retain the dense pipeline's exact training semantics."""

from dataclasses import replace
import json

import pandas as pd
import pytest

from examples.research.dataset_registry_v2_demo import synthetic_inputs
from examples.research.scalable_factor_computation import configured
from factorlab.dataset.identity import seal
from factorlab.dataset.partitioned import ChunkedConfig, PartitionedDataset
from factorlab.errors import ConfigurationError, SchemaError
from factorlab.validation import (
    ExpandingWalkForwardSplitter, FactorSelectionConfig, TrainFactorSelector, WalkForwardRunner,
)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    dates, *inputs = synthetic_inputs(180, 8)
    builder = configured(8, 14)
    builder.options = replace(builder.options,
        filter_config=replace(builder.options.filter_config, min_listed_days=1))
    dataset = builder.build_chunked(*inputs, dates,
        output_dir=tmp_path_factory.mktemp("p303") / "dataset",
        config=ChunkedConfig(dates_per_batch=11))
    interval = dataset.manifest["research_interval"]
    return builder, dates[(dates >= interval["start"]) & (dates <= interval["end"])], dataset


def make_runner(builder, model, dynamic):
    selector = TrainFactorSelector(builder.snapshot, builder.factor_refs,
        FactorSelectionConfig(k=3, minimum_cross_section=4, minimum_dates=5,
            minimum_observations=20, minimum_abs_rank_ic=0,
            minimum_direction_consistency=0, minimum_coverage=0.2,
            maximum_missing_ratio=0.8, minimum_pair_coverage=0.2)) if dynamic else None
    options = {
        "factor_score": ({"dynamic_factor_score_weighting": "equal"} if dynamic else
                         {"factor_weights": dict.fromkeys(builder.factor_names, 1.0)}),
        "ridge": {"ridge_alphas": (0.1, 1.0)},
        "lightgbm": {"lightgbm_candidates": [{"n_estimators": 5, "min_child_samples": 4}]},
        "xgboost": {"xgboost_candidates": [{"n_estimators": 5, "max_depth": 2}]},
    }[model]
    return WalkForwardRunner(
        ExpandingWalkForwardSplitter(initial_train_months=1, validation_months=1,
            test_months=1, step_months=2, purge_days=2, embargo_days=2),
        model_name=model, feature_names=builder.factor_names, factor_selector=selector,
        top_k=2, minimum_cross_section=4, **options)


def assert_results_equal(a, b):
    for name in ("predictions", "fold_summary", "validation_metrics", "coefficients",
                 "feature_importance", "diagnostic_predictions", "diagnostic_daily",
                 "diagnostic_summary", "diagnostic_audit", "factor_quality", "factor_redundancy"):
        pd.testing.assert_frame_equal(getattr(a, name), getattr(b, name), check_exact=True)
    # Layout receipts and content receipts intentionally describe different objects.
    excluded = ["dataset_identity_hash", "model_prediction_identity"]
    pd.testing.assert_frame_equal(a.factor_selection.drop(columns=excluded, errors="ignore"),
                                 b.factor_selection.drop(columns=excluded, errors="ignore"),
                                 check_exact=True)


@pytest.mark.parametrize("model", ["factor_score", "ridge", "lightgbm", "xgboost"])
@pytest.mark.parametrize("dynamic", [False, True])
def test_four_models_exact_inputs_and_outputs(bundle, monkeypatch, model, dynamic):
    builder, dates, dataset = bundle
    runner = make_runner(builder, model, dynamic)
    stages = []
    original_fit = WalkForwardRunner._fit_and_select
    original_predict_frame = WalkForwardRunner._prediction_frame

    def fit(self, fold, train, validation, **kwargs):
        stages.append(("fit", fold, train.reset_index(drop=True).copy(),
                       validation.reset_index(drop=True).copy()))
        return original_fit(self, fold, train, validation, **kwargs)

    def predict_frame(self, frame, *args, **kwargs):
        stages.append(("prediction", frame.reset_index(drop=True).copy()))
        return original_predict_frame(self, frame, *args, **kwargs)

    monkeypatch.setattr(WalkForwardRunner, "_fit_and_select", fit)
    monkeypatch.setattr(WalkForwardRunner, "_prediction_frame", predict_frame)
    dense = runner.run(dataset.to_frame(), calendar=dates)
    expected = list(stages)
    stages.clear()

    def forbidden(*args, **kwargs):
        pytest.fail("Partitioned runner must not materialize the full dataset")

    monkeypatch.setattr(PartitionedDataset, "to_frame", forbidden)
    actual = runner.run_partitioned(dataset, calendar=dates)
    assert len(stages) == len(expected)
    for left, right in zip(expected, stages):
        for a, b in zip(left, right):
            if isinstance(a, pd.DataFrame):
                pd.testing.assert_frame_equal(a, b, check_exact=True)
            else:
                assert a == b
    assert_results_equal(dense, actual)
    assert len(actual.resource_usage.query("phase == 'fold'")) == len(actual.fold_summary)
    assert (actual.resource_usage.peak_rss_bytes > 0).all()
    assert (actual.fold_summary.train_max_exit_date < actual.fold_summary.audit_validation_first_date).all()


def test_exact_dates_masks_types_and_intersecting_partitions(bundle, monkeypatch):
    _, dates, dataset = bundle
    dense = dataset.to_frame()
    requested = pd.DatetimeIndex(dense.date.unique()[8:28:2])
    reads = []
    original = pd.read_parquet

    def read(path, *args, **kwargs):
        reads.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", read)
    actual = dataset.read_dates(requested)
    pd.testing.assert_frame_equal(actual, dense.loc[dense.date.isin(requested)].reset_index(drop=True),
                                  check_exact=True)
    expected = [r["file"] for r in dataset.manifest["partitions"]
                if ((requested >= pd.Timestamp(r["date_start"])) &
                    (requested <= pd.Timestamp(r["date_end"]))).any()]
    assert reads == expected
    assert actual.attrs == {"partitioned_source_identity": dataset.identity_hash}
    with pytest.raises(ConfigurationError):
        dataset.read_dates(requested[::-1])
    with pytest.raises(SchemaError, match="absent"):
        dataset.read_dates(pd.DatetimeIndex([dates[-1] + pd.Timedelta(days=50)]))


@pytest.mark.parametrize("damage", ["missing", "corrupt", "identity", "overlap"])
def test_changed_partition_rejected(bundle, tmp_path, damage):
    import shutil
    _, _, dataset = bundle
    path = tmp_path / "dataset"
    shutil.copytree(dataset.path, path)
    loaded = PartitionedDataset(path, expected_identity_hash=dataset.identity_hash)
    record = loaded.manifest["partitions"][0]
    if damage == "missing":
        (path / record["file"]).unlink()
    elif damage == "corrupt":
        (path / record["file"]).write_bytes(b"corrupted")
    else:
        manifest = json.loads((path / "manifest.json").read_text())
        if damage == "overlap":
            manifest["partitions"][1]["date_start"] = record["date_start"]
        else:
            manifest["binding"]["data_version"] = "changed"
        (path / "manifest.json").write_text(json.dumps(seal({
            k: v for k, v in manifest.items() if k != "identity_hash"})))
    with pytest.raises(SchemaError):
        loaded.read_dates(pd.DatetimeIndex([pd.Timestamp(record["date_start"])]))


def test_future_values_do_not_change_historical_training(bundle, tmp_path):
    import shutil
    from factorlab.dataset.partitioned import _record

    builder, dates, dataset = bundle
    runner = make_runner(builder, "ridge", True)
    baseline = runner.run_partitioned(dataset, calendar=dates)
    cutoff = baseline.fold_summary.iloc[0].test_end
    path = tmp_path / "future"
    shutil.copytree(dataset.path, path)
    manifest = json.loads((path / "manifest.json").read_text())
    for number, record in enumerate(manifest["partitions"]):
        if pd.Timestamp(record["date_end"]) > cutoff:
            part_path = path / record["file"]
            part = pd.read_parquet(part_path)
            future = part.date > cutoff
            part.loc[future, list(builder.factor_names)] *= -10
            part.loc[future, "EXCESS_RET5"] *= -20
            part.to_parquet(part_path, index=False)
            manifest["partitions"][number] = _record(part_path, part)
    (path / "manifest.json").write_text(json.dumps(seal({
        k: v for k, v in manifest.items() if k != "identity_hash"})))
    updated = runner.run_partitioned(PartitionedDataset(path), calendar=dates)
    first = baseline.fold_summary.iloc[0].fold
    pd.testing.assert_frame_equal(baseline.predictions.query("fold == @first"),
                                  updated.predictions.query("fold == @first"), check_exact=True)
    columns = ["selection_hash", "selected_features", "directions", "fit_source_sha256"]
    pd.testing.assert_frame_equal(baseline.factor_selection.loc[[0], columns],
                                  updated.factor_selection.loc[[0], columns], check_exact=True)


def rewrite_bundle(dataset, path, transform):
    import shutil
    from factorlab.dataset.partitioned import _record, _schema

    shutil.copytree(dataset.path, path)
    manifest = json.loads((path / "manifest.json").read_text())
    for number, record in enumerate(manifest["partitions"]):
        part_path = path / record["file"]
        part = transform(pd.read_parquet(part_path))
        part.to_parquet(part_path, index=False)
        manifest["partitions"][number] = _record(part_path, part)
        manifest["schema"] = _schema(part)
    (path / "manifest.json").write_text(json.dumps(seal({
        k: v for k, v in manifest.items() if k != "identity_hash"})))
    return PartitionedDataset(path)


def test_per_security_maturity_masks_and_missing_windows(bundle, tmp_path, monkeypatch):
    builder, dates, dataset = bundle
    runner = make_runner(builder, "ridge", True)
    dense = dataset.to_frame()
    fold = next(runner.splitter.split(dense, calendar=dates))
    train_date, validation_date = fold.train_dates[-2], fold.validation_dates[-2]

    def change(part):
        # Other securities on these same signal dates retain mature labels.
        symbol = "000001.SZ"
        part.loc[(part.date == train_date) & (part.symbol == symbol), "exit_date"] = fold.audit.validation_first_date
        part.loc[(part.date == validation_date) & (part.symbol == symbol), "exit_date"] = fold.audit.test_first_date
        part.loc[(part.date == fold.train_dates[2]) & (part.symbol == symbol), "entry_date"] = pd.NaT
        return part

    modified = rewrite_bundle(dataset, tmp_path / "maturity", change)
    expected = runner.run(modified.to_frame(), calendar=dates)
    seen = []
    original = TrainFactorSelector.select

    def select(self, train, **kwargs):
        seen.append(train[["date", "symbol"]].reset_index(drop=True))
        return original(self, train, **kwargs)

    monkeypatch.setattr(TrainFactorSelector, "select", select)
    actual = runner.run_partitioned(modified, calendar=dates)
    assert_results_equal(expected, actual)
    first = seen[0]
    assert not ((first.date == train_date) & (first.symbol == "000001.SZ")).any()
    assert (first.date == train_date).any()
    assert actual.fold_summary.iloc[0].incomplete_label_rows_removed > 0


def test_mutation_after_planning_rejected_before_fit(bundle, tmp_path, monkeypatch):
    from factorlab.validation.partitioned_walk_forward import PartitionedWalkForwardSource

    builder, dates, dataset = bundle
    modified = rewrite_bundle(dataset, tmp_path / "mutable", lambda part: part)
    original = PartitionedWalkForwardSource.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        record = self.dataset.manifest["partitions"][0]
        (self.dataset.path / record["file"]).write_bytes(b"changed after planning")

    def forbidden(*args, **kwargs):
        pytest.fail("Corrupt fold must not reach model fitting")

    monkeypatch.setattr(PartitionedWalkForwardSource, "__init__", initialize)
    monkeypatch.setattr(WalkForwardRunner, "_fit_and_select", forbidden)
    with pytest.raises(SchemaError, match="changed"):
        make_runner(builder, "ridge", False).run_partitioned(modified, calendar=dates)


def test_previous_fold_frames_released_before_next_read(bundle, monkeypatch):
    import weakref
    from factorlab.validation.partitioned_walk_forward import PartitionedWalkForwardSource

    builder, dates, dataset = bundle
    runner = make_runner(builder, "ridge", False)
    source = PartitionedWalkForwardSource(dataset, runner, "entry_date", "exit_date")
    folds = list(runner.splitter.split(source.metadata, calendar=dates))
    frames = source.fold_frames(source.metadata, folds)
    first = next(frames)
    references = [weakref.ref(frame) for frame in first[1:]]
    del first
    original = dataset.read_dates

    def read(dates):
        assert all(ref() is None for ref in references)
        return original(dates)

    monkeypatch.setattr(dataset, "read_dates", read)
    try:
        next(frames)
    finally:
        frames.close()


@pytest.mark.parametrize("entry,exit", [("date", "exit_date"), ("missing", "exit_date"),
                                       ("entry_date", "entry_date"), (None, "exit_date")])
def test_runtime_column_errors_match_dense(bundle, entry, exit):
    builder, dates, dataset = bundle
    runner = make_runner(builder, "ridge", False)
    dense = dataset.to_frame()
    with pytest.raises(ValueError) as expected:
        runner.run(dense, calendar=dates, entry_date_col=entry, exit_date_col=exit)
    with pytest.raises(type(expected.value), match=str(expected.value)):
        runner.run_partitioned(dataset, calendar=dates, entry_date_col=entry, exit_date_col=exit)


def test_custom_audit_column_names(bundle, tmp_path):
    builder, dates, dataset = bundle
    renamed = rewrite_bundle(dataset, tmp_path / "renamed", lambda part:
        part.rename(columns={"entry_date": "label_entry", "exit_date": "label_exit"}))
    runner = make_runner(builder, "ridge", True)
    options = dict(calendar=dates, entry_date_col="label_entry", exit_date_col="label_exit")
    assert_results_equal(runner.run(renamed.to_frame(), **options),
                         runner.run_partitioned(renamed, **options))


def test_candidate_pins_checked(bundle):
    builder, dates, dataset = bundle
    runner = make_runner(builder, "ridge", True)
    runner.factor_selector = replace(runner.factor_selector,
                                     factor_refs=builder.factor_refs[:-1])
    with pytest.raises(ValueError, match="candidate pins"):
        runner.run_partitioned(dataset, calendar=dates)
