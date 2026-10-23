"""Integrated acceptance, generic-math replay, leakage and financial isolation."""

from dataclasses import replace
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import shutil

import numpy as np
import pandas as pd
import pytest

from qbot.research.exploratory.factor_discovery.contracts import (
    DiscoveryConfig,
    DiscoveryError,
)
from qbot.research.exploratory.factor_discovery.engine import ExpressionEngine
from qbot.research.exploratory.factor_discovery.evaluation import (
    development_frame,
    evaluate_discovery,
)
from qbot.research.exploratory.factor_discovery.generator import generate_candidates
from qbot.research.exploratory.factor_discovery.private_inputs import (
    load_private_inputs,
)
from qbot.research.exploratory.factor_discovery.publication import (
    PUBLIC_FILES,
    publish_aggregates,
)
from qbot.research.exploratory.factor_discovery.receipts import (
    assert_discovery_only,
    verify_receipt,
    write_receipt,
)
from qbot.research.exploratory.factor_discovery.redundancy import diagnose_redundancy
from qbot.research.exploratory.factor_discovery.runner import run_frames, run_synthetic
from qbot.research.exploratory.factor_discovery.synthetic import make_synthetic_data
from qbot.research.factors.registry import FACTOR_REGISTRY, get_factor
from qbot.research.labels.forward_return import build_forward_excess_return_labels
from qbot.research.metrics.factor import evaluate_factors


@pytest.fixture(scope="module")
def integrated(tmp_path_factory):
    root = tmp_path_factory.mktemp("factor-discovery")
    return root, run_synthetic(output_root=root / "runs")


def test_actual_40_record_full_loop_and_artifacts_verified(integrated):
    _, run = integrated
    assert run.receipt["status"] == "SOFTWARE_MVP_SUCCESS"
    counts = run.receipt["counts"]
    assert (
        counts["total_records"] == 40
        and counts["structurally_unique"] == counts["successful"] == 39
    )
    assert (
        counts["failed"] == counts["rejected"] == 0 and counts["exact_duplicates"] == 1
    )
    assert run.receipt["input"]["classification"] == "SYNTHETIC_TEST_ONLY"
    assert run.receipt["real_data_status"] == "NOT_RUN"
    assert len(run.evaluation.summary) == 39
    assert run.evaluation.summary["RankIC Valid Dates"].ge(60).all()
    assert set(run.evaluation.quantile_returns["quantile"]) == {
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "Q5",
        "Q5-Q1",
    }
    assert (
        run.candidates["reason_counts"]
        .apply(json.loads)
        .map(lambda item: item.get("OUTPUT_MISSING", 0))
        .sum()
        > 0
    )
    assert set(
        verify_receipt(run.destination / "receipt.json")["artifacts"]
    ) == PUBLIC_FILES | {"redundancy_daily.csv"}
    for record in run.candidates.loc[run.candidates["status"].eq("SUCCESS")].to_dict(
        "records"
    ):
        assert (
            len(record["expression_sha256"]) == 64
            and record["estimated_cell_operations"] > 0
        )


def test_actual_repeat_same_hashes_normalized_asts_and_numerical_outputs(integrated):
    root, first = integrated
    second = run_synthetic(output_root=root / "repeated")
    for key in (
        "normalized_expression_set_sha256",
        "development_factor_values_sha256",
        "daily_ic_sha256",
        "quantile_returns_sha256",
        "evaluation_summary_sha256",
    ):
        assert first.receipt[key] == second.receipt[key]
    pd.testing.assert_frame_equal(first.factors, second.factors, check_exact=True)
    pd.testing.assert_frame_equal(first.redundancy, second.redundancy, check_exact=True)
    pd.testing.assert_series_equal(
        first.candidates["normalized_ast"], second.candidates["normalized_ast"]
    )


def test_development_outputs_invariant_to_post_development_prices_and_labels(
    integrated,
):
    _, baseline = integrated
    bars, benchmark, dates = make_synthetic_data()
    labels = build_forward_excess_return_labels(bars, benchmark, calendar=dates)
    altered = bars.copy()
    after = altered["date"] > pd.Timestamp(DiscoveryConfig().development_end)
    for column in altered:
        if column not in ("date", "symbol", "adj_factor"):
            altered.loc[after, column] *= 3
    labels.loc[
        labels["date"] > pd.Timestamp(DiscoveryConfig().development_end), "EXCESS_RET5"
    ] = 9999
    # Labels that cross the boundary may change too: maturity mask must remove them.
    labels.loc[
        labels["exit_date"] > pd.Timestamp(DiscoveryConfig().development_end),
        "EXCESS_RET5",
    ] = -8888
    local = altered.loc[~after]
    engine = ExpressionEngine(local, dates[dates <= DiscoveryConfig().development_end])
    frame = engine.panel[["date", "symbol"]].copy()
    for record in generate_candidates().records:
        if record["status"] == "GENERATED":
            data = local[["date", "symbol"]].copy()
            data[record["candidate_id"]] = engine.compute(record["compiled"]).values
            frame = frame.merge(
                data, on=["date", "symbol"], how="left", validate="one_to_one"
            )
    observed = evaluate_discovery(frame, labels, dates)
    pd.testing.assert_frame_equal(
        observed.daily_ic, baseline.evaluation.daily_ic, check_exact=True
    )
    pd.testing.assert_frame_equal(
        observed.quantile_returns,
        baseline.evaluation.quantile_returns,
        check_exact=True,
    )


def test_generic_factor_math_exactly_reused_not_a_second_ic_formula(integrated):
    _, run = integrated
    names = run.evaluation.quality["candidate_id"].tolist()
    frame = run.evaluation.processed
    dates = pd.DatetimeIndex(frame["date"].drop_duplicates().sort_values())
    replay = evaluate_factors(frame, names, calendar=dates)
    pd.testing.assert_frame_equal(
        replay.daily_ic, run.evaluation.daily_ic, check_exact=True
    )
    pd.testing.assert_frame_equal(
        replay.quantile_returns, run.evaluation.quantile_returns, check_exact=True
    )


def test_signal_quantiles_ignore_missing_future_labels_and_validate_mature_offsets():
    dates = pd.bdate_range("2023-07-03", periods=12)
    factors = pd.DataFrame(
        {
            "date": np.repeat(dates, 20),
            "symbol": ["%06d.SZ" % (i + 1) for i in range(20)] * 12,
            "FD_001": np.tile(np.arange(20, dtype=float), 12),
        }
    )
    labels = factors[["date", "symbol"]].copy()
    labels["entry_date"] = labels["date"].map(pd.Series(dates, index=dates).shift(-1))
    labels["exit_date"] = labels["date"].map(pd.Series(dates, index=dates).shift(-6))
    labels["EXCESS_RET5"] = factors["FD_001"] / 100
    config = replace(
        DiscoveryConfig(),
        development_end=str(dates[-1].date()),
        minimum_evaluation_dates=1,
    )
    complete = evaluate_discovery(factors, labels, dates, config)
    labels.loc[labels["symbol"].eq("000001.SZ"), "EXCESS_RET5"] = np.nan
    missing = evaluate_discovery(factors, labels, dates, config)
    assert complete.quantile_returns["observations"].equals(
        missing.quantile_returns["observations"]
    )
    selected = missing.quantile_returns.loc[
        (missing.quantile_returns["date"] == dates[0])
        & missing.quantile_returns["quantile"].eq("Q1")
    ].iloc[0]
    assert selected["observations"] == 4 and selected["labeled_observations"] == 3
    labels.loc[0, "entry_date"] = dates[2]
    frame, _ = development_frame(factors, labels, dates, config)
    assert pd.isna(frame.loc[0, "EXCESS_RET5"])
    assert frame.loc[frame["date"] > dates[-7], "EXCESS_RET5"].isna().all()


def test_original14_redundancy_independent_daily_pearson_recalculation(integrated):
    _, run = integrated
    bars, _, dates = make_synthetic_data()
    local = bars.loc[bars["date"] <= DiscoveryConfig().development_end]
    panel = ExpressionEngine(
        local, dates[dates <= DiscoveryConfig().development_end]
    ).panel
    combined = run.factors.copy()
    for name in FACTOR_REGISTRY:
        combined[name] = get_factor(name).compute(panel).to_numpy()
    development = combined.loc[
        combined["date"].between(
            pd.Timestamp(DiscoveryConfig().development_start),
            pd.Timestamp(DiscoveryConfig().development_end),
        )
    ]
    per_day = [cross for _, cross in development.groupby("date")]
    originals = run.redundancy.loc[run.redundancy["comparison"].eq("ORIGINAL14")]
    assert len(originals) == 39 * 14
    for observed in originals.to_dict("records"):
        correlations, coverages = [], []
        for cross in per_day:
            x = cross[observed["left"]].to_numpy(dtype=float)
            y = cross[observed["right"]].to_numpy(dtype=float)
            paired = np.isfinite(x) & np.isfinite(y)
            coverages.append(float(paired.mean()))
            if paired.sum() >= 20:
                x, y = x[paired], y[paired]
                x = x / max(float(np.abs(x).max()), 1e-300)
                y = y / max(float(np.abs(y).max()), 1e-300)
                if np.ptp(x) > 0 and np.ptp(y) > 0:
                    value = float(np.corrcoef(x, y)[0, 1])
                    if np.isfinite(value):
                        correlations.append(value)
        assert observed["valid_dates"] == len(correlations)
        assert observed["mean_pearson"] == pytest.approx(
            np.mean(correlations), abs=1e-7
        )
        assert observed["mean_abs_pearson"] == pytest.approx(
            np.mean(np.abs(correlations)), abs=1e-7
        )
        assert observed["mean_pair_coverage"] == pytest.approx(np.mean(coverages))


def test_redundancy_statuses_and_insufficient_evidence_not_noncorrelation():
    dates = pd.bdate_range("2023-07-03", periods=61)
    x = np.tile(np.arange(20, dtype=float), 61)
    frame = pd.DataFrame(
        {
            "date": np.repeat(dates, 20),
            "symbol": np.tile(np.arange(20), 61),
            "A": x,
            "B": x,
            "C": -x,
            "D": np.sin(x),
        }
    )
    summary, _ = diagnose_redundancy(frame, ["A"], ["B", "C", "D"])
    assert summary.set_index("right")["status"].to_dict() == {
        "B": "NUMERIC_DUPLICATE",
        "C": "HIGHLY_REDUNDANT",
        "D": "NON_REDUNDANT",
    }
    short, _ = diagnose_redundancy(frame.loc[frame["date"].eq(dates[0])], ["A"], ["B"])
    assert short.iloc[0]["status"] == "UNDETERMINED"
    frame.loc[np.tile(np.arange(20) >= 15, 61), "B"] = np.nan
    missing, _ = diagnose_redundancy(frame, ["A"], ["B"])
    assert missing.iloc[0]["status"] == "UNDETERMINED"
    with pytest.raises(DiscoveryError, match="BUDGET_EXCEEDED"):
        diagnose_redundancy(
            frame,
            ["A"],
            ["B", "C"],
            replace(DiscoveryConfig(), max_diagnostic_records=1),
        )


def test_missing_and_tampered_private_data_statuses_never_synthetic_success(tmp_path):
    assert load_private_inputs()[0] == "NOT_RUN"
    assert load_private_inputs(tmp_path / "missing.json")[0] == "NOT_RUN"
    manifest, receipt = tmp_path / "manifest.json", tmp_path / "receipt.json"
    manifest.write_text("{}")
    assert load_private_inputs(manifest)[0] == "BLOCKED"
    receipt.write_text("{}")
    status, loaded, reason = load_private_inputs(manifest, receipt)
    assert status == "BLOCKED" and loaded is None and "byte hash" in reason
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "qbot.research.exploratory.factor_discovery",
            "--mode",
            "private",
            "--output-root",
            str(tmp_path / "not-run"),
        ],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0
    state = json.loads(process.stdout)
    assert state["status"] == state["real_data_status"] == "NOT_RUN"
    verify_receipt(Path(state["destination"]) / "receipt.json")


def test_receipt_aggregate_publication_and_tamper_detection(integrated, tmp_path):
    _, run = integrated
    public = tmp_path / "public-v1"
    receipt = publish_aggregates(run.destination / "receipt.json", public)
    assert set(item.name for item in public.iterdir()) == PUBLIC_FILES | {
        "receipt.json"
    }
    assert receipt["public_boundary"].startswith("EXPRESSIONS_AND_AGGREGATES")
    for csv in public.glob("*.csv"):
        assert "symbol" not in pd.read_csv(csv, nrows=0)
    (public / "candidates.csv").write_text("changed", encoding="utf-8")
    with pytest.raises(DiscoveryError, match="hash/path"):
        verify_receipt(public / "receipt.json")


def test_discovery_receipt_rejected_by_actual_formal_forward_and_live_gates(
    integrated, tmp_path
):
    from qbot.research.data.market_qualification import load_qualification
    from qbot.research.errors import DataQualityError
    from qbot.research.forward.model_bundle import (
        BundleIntegrityError,
        load_model_bundle,
    )
    from qbot.research.forward.signal_recorder import (
        SignalValidationError,
        load_signal_record,
    )
    from qbot.research.config.loader import load_config
    from qbot.research.errors import ConfigurationError

    _, run = integrated
    bars, benchmark, _ = make_synthetic_data()
    with pytest.raises(DataQualityError):
        load_qualification(
            run.destination / "receipt.json", stocks=bars, benchmark=benchmark
        )
    with pytest.raises(BundleIntegrityError):
        load_model_bundle(run.destination)
    with pytest.raises(SignalValidationError):
        load_signal_record(run.destination)
    for purpose in ("FORMAL", "FORWARD", "LIVE", "LIVE_TRADING"):
        with pytest.raises(DiscoveryError):
            assert_discovery_only(run.receipt, purpose=purpose)
    with pytest.raises(ConfigurationError):
        load_config(run.destination / "receipt.json")
    # Even a validly hashed Forward-like container rejects the new schema.
    content = json.dumps(run.receipt).encode("utf-8")
    (tmp_path / "manifest.json").write_bytes(content)
    (tmp_path / "manifest.sha256").write_text(hashlib.sha256(content).hexdigest())
    (tmp_path / "signals.csv").write_text("date,symbol\n")
    with pytest.raises(SignalValidationError, match="schema"):
        load_signal_record(tmp_path, require_completion=False)


def test_old_factor_golden_configs_and_historical_artifacts_unchanged():
    repo = Path(__file__).resolve().parents[2]
    paths = [
        "qbot/research/factors",
        "tests/research/reference",
        "configs/research",
        "qbot/research/forward",
        "qbot/engine",
        "docs/research/evidence/exploratory-factor-v1",
        "docs/research/evidence/exploratory-factor-v2",
        "docs/research/evidence/exploratory-statistical-audit-v3",
    ]
    process = subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            "48ea4dd8c1876c1515f3172c9bb2e812f20ebe82",
            "--",
            *paths,
        ],
        cwd=repo,
        capture_output=True,
    )
    assert process.returncode == 0, process.stdout.decode("utf-8", errors="replace")
    assert len(FACTOR_REGISTRY) == 14


def test_rejected_templates_and_budget_failure_have_truthful_completion_receipts(
    tmp_path,
):
    rejected = run_synthetic(
        replace(DiscoveryConfig(), max_depth=1), output_root=tmp_path / "rejected"
    )
    assert rejected.receipt["status"] == "FAILED"
    assert rejected.receipt["counts"]["rejected"] == 40
    assert rejected.receipt["counts"]["successful"] == 0
    budget = run_synthetic(
        replace(DiscoveryConfig(), max_diagnostic_records=1),
        output_root=tmp_path / "budget",
    )
    assert budget.receipt["status"] == "FAILED"
    assert "BUDGET_EXCEEDED" in budget.receipt["completion_reason"]
    verify_receipt(budget.destination / "receipt.json")


@pytest.mark.parametrize(
    "column", ["symbol", "members", "membership_sha256", "unknown_membership_digest"]
)
def test_publication_rejects_private_columns_even_with_valid_rehashed_receipt(
    integrated, tmp_path, column
):
    _, run = integrated
    forged = tmp_path / "forged"
    shutil.copytree(run.destination, forged)
    table = pd.read_csv(forged / "quantile_returns.csv")
    table[column] = "private-test-only"
    table.to_csv(forged / "quantile_returns.csv", index=False)
    payload = {
        key: value
        for key, value in run.receipt.items()
        if key not in ("receipt_sha256", "artifacts")
    }
    write_receipt(forged, payload)
    verify_receipt(forged / "receipt.json")
    with pytest.raises(DiscoveryError, match="security-level"):
        publish_aggregates(forged / "receipt.json", tmp_path / "public")


def test_synthetic_cannot_be_relabelled_as_successful_real_evaluation(tmp_path):
    bars, benchmark, dates = make_synthetic_data(stock_count=2, sessions=20)
    with pytest.raises(DiscoveryError, match="cannot assert real"):
        run_frames(
            bars,
            benchmark,
            dates,
            evidence={"classification": "SYNTHETIC_TEST_ONLY", "synthetic_only": True},
            real_data_status="SUCCESS",
            output_root=tmp_path,
        )


def test_failed_calculation_cannot_retain_successful_private_input_status(
    tmp_path, monkeypatch
):
    # A trusted-adapter metadata stub exercises failure-state disclosure only.
    # It is not an input qualification or an empirical evidence fixture.
    bars, benchmark, dates = make_synthetic_data(stock_count=2, sessions=20)
    labels = build_forward_excess_return_labels(bars, benchmark, calendar=dates)
    from qbot.research.exploratory.factor_discovery import private_inputs

    evidence = {
        "classification": "EXPLORATORY / UNVERIFIED / NON_PIT_CONVENIENCE",
        "synthetic_only": False,
        "negative_test_only": True,
    }
    universe = sorted(bars.symbol.unique())
    monkeypatch.setattr(
        private_inputs,
        "load_private_inputs",
        lambda *args: (
            "SUCCESS",
            (bars, benchmark, dates, labels, evidence, universe),
            "adapter stub only",
        ),
    )
    config = replace(
        DiscoveryConfig(),
        development_start=str(dates[0].date()),
        development_end=str(dates[-1].date()),
        max_diagnostic_records=1,
    )
    run = run_frames(
        bars,
        benchmark,
        dates,
        config,
        labels=labels,
        evidence=evidence,
        universe=universe,
        real_data_status="SUCCESS",
        real_data_reason="adapter stub only",
        output_root=tmp_path,
    )
    assert run.receipt["status"] == "FAILED"
    assert run.receipt["real_data_status"] == "BLOCKED"
    assert "BUDGET_EXCEEDED" in run.receipt["real_data_reason"]
    assert "FAILED" in (run.destination / "summary.md").read_text(encoding="utf-8")


def test_self_rehashed_receipt_cannot_redefine_approved_historical_labels(
    tmp_path, monkeypatch
):
    from qbot.research.exploratory.factor_discovery import private_inputs

    manifest, previous = tmp_path / "manifest.json", tmp_path / "receipt.json"
    manifest.write_text("SYNTHETIC_NEGATIVE_TEST_ONLY")
    # A caller may recompute its own receipt hashes, but cannot change the
    # separate, immutable approved receipt byte anchor.
    previous.write_text(
        json.dumps(
            {
                "schema": "EXPLORATORY_RUN_V1",
                "artifacts": {"derived_factor_labels.parquet": "0" * 64},
            }
        )
    )
    actual_hash = private_inputs.file_sha256
    monkeypatch.setattr(
        private_inputs,
        "file_sha256",
        lambda path: (
            private_inputs.MANIFEST_SHA256
            if Path(path) == manifest
            else actual_hash(path)
        ),
    )

    def forbidden_load(*args, **kwargs):
        pytest.fail("unanchored private receipt should fail before any dataset read")

    monkeypatch.setattr(private_inputs, "load_dataset", forbidden_load)
    status, loaded, reason = private_inputs.load_private_inputs(manifest, previous)
    assert status == "BLOCKED" and loaded is None
    assert "approved v2 receipt byte hash" in reason


def test_redundancy_preserves_near_constant_variation_and_ignores_unpaired_extreme():
    random = np.random.RandomState(11)
    x, y = random.normal(size=20), random.normal(size=20)
    dates = pd.bdate_range("2023-07-03", periods=61)
    frame = pd.DataFrame(
        {
            "date": np.repeat(dates, 21),
            "symbol": np.tile(np.arange(21), 61),
            "A": np.tile(np.r_[1 + 1e-10 * x, 1e300], 61),
            "B": np.tile(np.r_[1 + 1e-10 * y, np.nan], 61),
        }
    )
    observed, _ = diagnose_redundancy(frame, ["A"], ["B"])
    expected = np.corrcoef(1 + 1e-10 * x, 1 + 1e-10 * y)[0, 1]
    assert observed.iloc[0]["mean_pearson"] == pytest.approx(expected, abs=1e-8)
    assert observed.iloc[0]["status"] == "NON_REDUNDANT"
    assert observed.iloc[0]["valid_dates"] == 61


@pytest.mark.parametrize(
    "changed", ["bars", "benchmark", "calendar", "labels", "universe", "evidence"]
)
def test_private_claim_must_bind_actual_values_to_independently_loaded_input(
    tmp_path, monkeypatch, changed
):
    from qbot.research.exploratory.factor_discovery import private_inputs

    bars, benchmark, calendar = make_synthetic_data(stock_count=2, sessions=20)
    labels = build_forward_excess_return_labels(bars, benchmark, calendar=calendar)
    evidence = {
        "classification": "EXPLORATORY / UNVERIFIED / NON_PIT_CONVENIENCE",
        "synthetic_only": False,
    }
    universe = sorted(bars.symbol.unique())
    # Only this isolated negative test replaces the independently anchored disk
    # loader; it never presents the fixture as financial evidence.
    monkeypatch.setattr(
        private_inputs,
        "load_private_inputs",
        lambda *args: (
            "SUCCESS",
            (bars, benchmark, calendar, labels, evidence, universe),
            "SYNTHETIC_NEGATIVE_TEST_ONLY",
        ),
    )
    actual = {
        "bars": bars.copy(),
        "benchmark": benchmark.copy(),
        "calendar": calendar.copy(),
        "labels": labels.copy(),
        "universe": list(universe),
        "evidence": dict(evidence),
    }
    if changed == "bars":
        actual[changed].loc[0, "adj_close"] += 1
    elif changed == "benchmark":
        column = next(c for c in benchmark if c != "date")
        actual[changed].loc[0, column] += 1
    elif changed == "calendar":
        actual[changed] = calendar[:-1]
    elif changed == "labels":
        actual[changed].loc[0, "EXCESS_RET5"] = 123
    elif changed == "universe":
        actual[changed][0] = "000003.SZ"
    else:
        actual[changed]["caller_supplied_private_identifier"] = "NEGATIVE_TEST_ONLY"
    with pytest.raises(DiscoveryError, match="private input binding mismatch"):
        run_frames(
            actual.pop("bars"),
            actual.pop("benchmark"),
            actual.pop("calendar"),
            **actual,
            real_data_status="SUCCESS",
            output_root=tmp_path / "untrusted"
        )
    assert not (tmp_path / "untrusted").exists()


@pytest.mark.parametrize(
    "changed",
    ["summary.md", "config.json", "factor", "scope", "date", "meaning", "receipt"],
)
def test_self_rehashed_contents_cannot_acquire_producer_publication_trust(
    integrated, tmp_path, changed
):
    _, run = integrated
    forged = tmp_path / "self-rehashed"
    shutil.copytree(run.destination, forged)
    payload = deepcopy(
        {
            key: value
            for key, value in run.receipt.items()
            if key not in ("receipt_sha256", "artifacts")
        }
    )
    if changed == "summary.md":
        (forged / changed).write_text("PRIVATE_SYNTHETIC_ATTACK_ONLY", encoding="utf-8")
    elif changed == "config.json":
        value = json.loads((forged / changed).read_text(encoding="utf-8"))
        value["private_identifier"] = "PRIVATE_SYNTHETIC_ATTACK_ONLY"
        (forged / changed).write_text(json.dumps(value), encoding="utf-8")
    elif changed == "receipt":
        payload["input"]["private_identifier"] = "PRIVATE_SYNTHETIC_ATTACK_ONLY"
    else:
        name = "candidates.csv" if changed == "meaning" else "daily_ic.csv"
        table = pd.read_csv(forged / name)
        table.loc[0, changed] = "PRIVATE_SYNTHETIC_ATTACK_ONLY"
        table.to_csv(forged / name, index=False)
    write_receipt(forged, payload)
    verify_receipt(forged / "receipt.json")
    with pytest.raises(DiscoveryError, match="runner provenance"):
        publish_aggregates(forged / "receipt.json", tmp_path / "published")
    assert not (tmp_path / "published").exists()
