"""SYNTHETIC_TEST_ONLY statistical contracts, never empirical evidence."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qbot.research.exploratory.contract import ExploratoryContractError, sha256_file
from qbot.research.exploratory.statistical_audit.statistics import (
    bootstrap_summary,
    contrast,
    hac_fit,
    holm,
    infer_panel,
    moving_block_indices,
    regime_design,
    run_count,
    sample_means,
)
from qbot.research.exploratory.statistical_audit.quintiles import compare_quintiles
from qbot.research.exploratory.statistical_audit.inputs import load_frozen
from qbot.research.exploratory.statistical_audit.delivery import (
    MARKERS,
    PUBLIC_FILES,
    identity,
    publish_audit,
    verify_audit,
    write_receipt,
)
from qbot.research.exploratory.statistical_audit.__main__ import run
from qbot.research.metrics.factor import evaluate_factors


def test_hac_against_independent_bartlett_loop_and_missing_calendar_positions():
    y = np.sin(np.arange(100) / 4) + np.arange(100) / 120
    y[[10, 11, 55]] = np.nan
    x = np.ones((100, 1))
    beta, covariance = hac_fit(y, x, 5)
    valid = np.isfinite(y)
    n = valid.sum()
    residual = np.where(valid, y - np.nanmean(y), 0)
    meat = sum(residual[t] ** 2 for t in range(100))
    for lag in range(1, 6):
        meat += (
            2
            * (1 - lag / 6)
            * sum(residual[t] * residual[t - lag] for t in range(lag, 100))
        )
    assert beta[0] == pytest.approx(np.nanmean(y))
    assert covariance[0, 0] == pytest.approx(meat / n**2 * n / (n - 1), abs=1e-14)
    compact = hac_fit(y[valid], np.ones((n, 1)), 5)[1][0, 0]
    assert abs(covariance[0, 0] - compact) > 1e-5


def test_temporal_dependence_changes_uncertainty_and_is_not_row_iid():
    rng = np.random.default_rng(17)
    y = np.zeros(500)
    for i in range(1, len(y)):
        y[i] = 0.85 * y[i - 1] + rng.normal()
    iid = np.std(y, ddof=1) / np.sqrt(len(y))
    result = contrast(hac_fit(y, np.ones((500, 1)), 20), [1])
    assert result["se"] > 2 * iid
    bootstrap = bootstrap_summary(
        sample_means(y, moving_block_indices(500)), np.mean(y)
    )
    assert bootstrap["se"] > 2 * iid


def test_blocks_are_adjacent_actual_positions_reproducible_and_keep_switches():
    indices = moving_block_indices(242)
    assert indices.shape == (2000, 242)
    np.testing.assert_array_equal(indices, moving_block_indices(242))
    for start in range(0, 242, 20):
        assert (np.diff(indices[:, start : min(start + 20, 242)], axis=1) == 1).all()
    assert indices.min() == 0 and indices.max() == 241
    regimes = np.array(["BULL", "BEAR", "SIDEWAYS"] * 81)[:242]
    drawn = regimes[indices]
    assert (drawn[:, 1] != drawn[:, 0]).all()
    y = np.ones(242)
    y[-6:] = np.nan
    np.testing.assert_allclose(sample_means(y, indices), 1)


@pytest.mark.parametrize(
    "block,repeats", [(0, 2000), (243, 2000), (20, 1), (1.5, 2000)]
)
def test_invalid_block_parameters_rejected(block, repeats):
    with pytest.raises(ExploratoryContractError):
        moving_block_indices(242, block=block, repeats=repeats)


def test_holm_fixed_family_and_unavailable_tests_stay_in_denominator():
    np.testing.assert_allclose(holm([0.01, 0.04, 0.03]), [0.03, 0.06, 0.06])
    result = holm([0.01, np.nan, 0.04])
    assert result[0] == pytest.approx(0.03)
    assert result[2] == pytest.approx(0.08)
    assert np.isnan(result[1])


def test_centered_null_bootstrap_p_is_not_a_sign_test_and_counts_bad_draws():
    draws = np.r_[np.linspace(0.1, 0.3, 1900), np.full(100, np.nan)]
    r = bootstrap_summary(draws, 0.2)
    assert r["p_two_sided"] == pytest.approx(1 / 1901)
    assert r["valid_repeats"] == 1900
    r = bootstrap_summary(np.r_[np.ones(1899), np.full(101, np.nan)], 1)
    assert np.isnan(r["p_two_sided"])
    assert r["numerical_status"] == "UNAVAILABLE"


def test_regime_contrasts_include_covariance_on_full_time_axis():
    regimes = np.array((["BULL"] * 10 + ["SIDEWAYS"] * 5 + ["BEAR"] * 10) * 5)
    x = regime_design(regimes)
    y = 0.2 * x[:, 0] - 0.1 * x[:, 1] + np.sin(np.arange(len(x)) / 4) / 8
    y[12] = np.nan
    beta, covariance = hac_fit(y, x, 20)
    result = contrast((beta, covariance), [1, -1, 0])
    assert result["estimate"] == pytest.approx(
        np.nanmean(y[regimes == "BULL"]) - np.nanmean(y[regimes == "BEAR"])
    )
    assert result["se"] ** 2 == pytest.approx(
        covariance[0, 0] + covariance[1, 1] - 2 * covariance[0, 1]
    )
    assert abs(covariance[0, 1]) > 1e-6
    assert run_count(regimes == "BULL") == 5
    with pytest.raises(ExploratoryContractError):
        regime_design(["BULL", "UNKNOWN"])


def test_small_sideways_and_degenerate_series_never_receive_support():
    dates = pd.bdate_range("2025-01-01", periods=242)
    regimes = pd.Series(["BULL"] * 128 + ["SIDEWAYS"] * 45 + ["BEAR"] * 69, index=dates)
    y = np.sin(np.arange(242)) / 10 + np.where(regimes == "SIDEWAYS", 0.9, 0)
    y[-6:] = np.nan
    panel = pd.DataFrame({"F": y}, index=dates)
    desc, means, states, comparisons = infer_panel(panel, regimes, ["F"])
    assert desc.valid_dates.iloc[0] == 236
    sideways = states[states.state == "SIDEWAYS"]
    assert sideways.valid_dates.eq(45).all() and sideways.sample_insufficient.all()
    limited = comparisons.comparison.str.contains("SIDEWAYS")
    assert comparisons.loc[limited, "sample_insufficient"].all()
    assert not comparisons.loc[limited, "conditional_support"].any()
    assert np.isnan(
        contrast(hac_fit(np.ones(80), np.ones((80, 1)), 5), [1])["p_two_sided"]
    )
    assert np.isnan(bootstrap_summary(np.ones(2000), 1)["p_two_sided"])
    assert hac_fit(np.arange(30), regime_design(["BULL"] * 30), 5) is None


def quintile_fixture(missing=True):
    f = pd.DataFrame(
        {
            "date": pd.Timestamp("2025-09-12"),
            "symbol": ["s%02d" % i for i in range(25)],
            "F": np.arange(25, dtype=float),
            "EXCESS_RET5": np.arange(25, dtype=float) / 100,
        }
    )
    if missing:
        f.loc[[0, 9, 22], "EXCESS_RET5"] = np.nan
    return f


def test_label_missingness_changes_membership_but_not_formal_math():
    f = quintile_fixture()
    original = f.copy(deep=True)
    daily, spread, monthly, members = compare_quintiles(f, ["F"])
    pd.testing.assert_frame_equal(f, original)
    assert daily.A_members.sum() == 22 and daily.B_members.sum() == 25
    assert daily.B_observable_members.sum() == daily.A_observable_members.sum() == 22
    assert daily.observable_members_changed.sum() > 0
    assert daily.label_missing_rate.eq(3 / 25).all()
    assert (
        not members.loc[members.method.eq("A")].symbol.isin(["s00", "s09", "s22"]).any()
    )
    assert len(members.loc[members.method.eq("B")]) == 25
    # Independent public Golden function: diagnostic B agrees; A differs.
    core = evaluate_factors(
        f, ["F"], minimum_cross_section=20, calendar=pd.DatetimeIndex(f.date.unique())
    ).quantile_returns
    actual = daily.sort_values("quantile").B_mean.to_numpy()
    expected = (
        core.loc[core["quantile"].ne("Q5-Q1")]
        .sort_values("quantile")
        .future_return.to_numpy()
    )
    np.testing.assert_allclose(actual, expected)
    assert spread.B_minus_A_spread.abs().iloc[0] > 0
    assert monthly.paired_dates.eq(1).all()


def test_no_missing_labels_means_identical_A_B_and_ties_keep_original_order():
    f = quintile_fixture(missing=False)
    f["F"] = 1.0
    daily, spread, _, members = compare_quintiles(f, ["F"])
    assert daily.observable_members_changed.eq(0).all()
    assert daily.B_minus_A_mean.eq(0).all()
    assert spread.B_minus_A_spread.eq(0).all()
    assert (
        members.loc[
            members.method.eq("B") & members["quantile"].eq("Q1"), "symbol"
        ].tolist()
        == f.symbol.iloc[:5].tolist()
    )


def test_unmatured_tail_and_insufficient_A_keep_NA_with_B_members():
    f = quintile_fixture()
    f["EXCESS_RET5"] = np.nan
    daily, spread, _, members = compare_quintiles(f, ["F"])
    assert not daily.A_formed.any() and daily.B_formed.all()
    assert daily.A_members.eq(0).all() and daily.B_members.sum() == 25
    assert daily[["A_mean", "B_mean", "B_minus_A_mean"]].isna().all().all()
    assert spread.A_Q5_minus_Q1.isna().all() and spread.B_Q5_minus_Q1.isna().all()
    assert not members.label_observable.any()
    f.loc[:18, "EXCESS_RET5"] = 1
    daily, _, _, _ = compare_quintiles(f, ["F"])
    assert not daily.A_formed.any() and daily.B_observable_members.sum() == 19


def test_missing_private_input_fails_without_fallback(tmp_path):
    with pytest.raises(ExploratoryContractError, match="FileNotFoundError"):
        load_frozen(tmp_path / "absent.json", tmp_path / "run", tmp_path / "public")
    status, output = run(
        tmp_path / "absent.json",
        tmp_path / "run",
        tmp_path / "public",
        tmp_path / "exploratory",
    )
    assert status == "BLOCKED"
    r = verify_audit(output / "receipt.json")
    assert r["data_version"] is None
    assert set(r["artifacts"]) == {"report.md"}


def private_package(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    (root / "report.md").write_text(" · ".join(MARKERS), encoding="utf-8")
    (root / "private_quintile_membership.csv").write_text(
        "PRIVATE STOCK-LEVEL ONLY\n", encoding="utf-8"
    )
    write_receipt(
        root,
        "BLOCKED",
        {"verification_complete": False},
        {},
        ["SOFTWARE_TEST_ONLY fixture"],
    )
    return root


def test_publication_never_copies_private_members_and_refuses_overwrite(tmp_path):
    root = private_package(tmp_path)
    dest = publish_audit(root, tmp_path / "public")
    assert not (dest / "private_quintile_membership.csv").exists()
    assert verify_audit(dest / "receipt.json", public=True)[
        "private_audit_receipt_sha256"
    ] == sha256_file(root / "receipt.json")
    with pytest.raises(ExploratoryContractError, match="immutable"):
        publish_audit(root, dest)


def test_artifact_and_receipt_hash_mismatches_fail_closed(tmp_path):
    root = private_package(tmp_path)
    (root / "report.md").write_text("TAMPERED", encoding="utf-8")
    with pytest.raises(ExploratoryContractError, match="artifact hash"):
        verify_audit(root / "receipt.json")
    r = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    r["parameters"]["block_sessions"] = 1
    (root / "receipt.json").write_text(json.dumps(r), encoding="utf-8")
    with pytest.raises(ExploratoryContractError, match="semantic hash"):
        verify_audit(root / "receipt.json")


@pytest.mark.parametrize(
    "unsafe", ["formal_admission", "forward_allowed", "live_allowed"]
)
def test_audit_cannot_be_promoted_even_with_recomputed_semantic_hash(tmp_path, unsafe):
    root = private_package(tmp_path)
    r = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    r[unsafe] = True
    r["receipt_sha256"] = identity(r)
    (root / "receipt.json").write_text(json.dumps(r), encoding="utf-8")
    with pytest.raises(ExploratoryContractError):
        verify_audit(root / "receipt.json")


def test_existing_formal_and_forward_readers_reject_audit_receipt(tmp_path):
    from qbot.research.data.market_qualification import load_qualification
    from qbot.research.errors import DataQualityError
    from qbot.research.forward.signal_recorder import (
        load_signal_record,
        SignalValidationError,
    )

    root = private_package(tmp_path)
    with pytest.raises(DataQualityError):
        load_qualification(
            root / "receipt.json", stocks=pd.DataFrame(), benchmark=pd.DataFrame()
        )
    content = (root / "receipt.json").read_bytes()
    forward = tmp_path / "fake-forward"
    forward.mkdir()
    (forward / "manifest.json").write_bytes(content)
    import hashlib

    (forward / "manifest.sha256").write_text(
        hashlib.sha256(content).hexdigest(), encoding="ascii"
    )
    (forward / "signals.csv").write_text("date,symbol\n", encoding="utf-8")
    with pytest.raises(SignalValidationError, match="schema"):
        load_signal_record(forward, require_completion=False)


def test_public_allowlist_has_no_raw_security_factor_or_label_files():
    assert "private_quintile_membership.csv" not in PUBLIC_FILES
    assert not any(name.endswith(".parquet") for name in PUBLIC_FILES)


def test_public_quintile_diagnostic_omits_enumerable_bucket_commitments():
    daily, _, _, private = compare_quintiles(quintile_fixture(), ["F"])
    assert not any("hash" in name or "sha256" in name for name in daily)
    assert "symbol" not in daily and "symbol" in private
    assert len(private) > 0


@pytest.mark.parametrize("private_column", ["A_member_sha256", "B_member_sha256"])
def test_old_bucket_hash_column_is_rejected_even_with_valid_artifact_hash(
    tmp_path, private_column
):
    from qbot.research.exploratory.statistical_audit.schemas import CSV_COLUMNS

    root = private_package(tmp_path)
    columns = CSV_COLUMNS["quintile_daily.csv"] + [private_column]
    pd.DataFrame(columns=columns).to_csv(root / "quintile_daily.csv", index=False)
    write_receipt(root, "BLOCKED", {}, {}, ["SOFTWARE_TEST_ONLY"])
    with pytest.raises(ExploratoryContractError, match="CSV columns"):
        publish_audit(root, tmp_path / "public")
    assert not (tmp_path / "public").exists()


def test_public_tables_are_invariant_to_private_symbol_renaming():
    source = quintile_fixture()
    renamed = source.copy()
    renamed["symbol"] = ["PRIVATE_ALTERNATIVE_%d" % n for n in range(len(source))]
    before = compare_quintiles(source, ["F"])
    after = compare_quintiles(renamed, ["F"])
    for original, observed in zip(before[:3], after[:3]):
        pd.testing.assert_frame_equal(original, observed, check_exact=True)
    assert not before[3].symbol.equals(after[3].symbol)


def test_private_membership_file_tampering_fails_whole_file_commitment(tmp_path):
    root = private_package(tmp_path)
    membership = root / "private_quintile_membership.csv"
    membership.write_bytes(membership.read_bytes() + b"TAMPERED")
    with pytest.raises(ExploratoryContractError, match="artifact hash"):
        verify_audit(root / "receipt.json")


def test_code_version_failure_produces_blocked_not_an_unhandled_exception(
    tmp_path, monkeypatch
):
    import subprocess
    from qbot.research.exploratory.statistical_audit import __main__ as audit_main

    def fail():
        raise subprocess.CalledProcessError(1, ["git", "merge-base"])

    monkeypatch.setattr(audit_main, "code_evidence", fail)
    status, output = run(
        tmp_path / "absent.json", tmp_path, tmp_path, tmp_path / "exploratory"
    )
    assert status == "BLOCKED"
    r = verify_audit(output / "receipt.json")
    assert r["code_and_environment"] == {"verification_complete": False}


def test_rehashed_incomplete_success_is_rejected(tmp_path):
    root = private_package(tmp_path)
    r = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    r["status"] = "SUCCESS"
    r["receipt_sha256"] = identity(r)
    (root / "receipt.json").write_text(json.dumps(r), encoding="utf-8")
    with pytest.raises(ExploratoryContractError, match="complete verified"):
        verify_audit(root / "receipt.json")


def test_raw_security_columns_cannot_hide_in_public_csv_filename(tmp_path):
    root = private_package(tmp_path)
    (root / "quintile_daily.csv").write_text(
        "symbol,close,EXCESS_RET5\ns01,10,0.1\n", encoding="utf-8"
    )
    write_receipt(root, "BLOCKED", {}, {}, ["SOFTWARE_TEST_ONLY"])
    with pytest.raises(ExploratoryContractError, match="CSV columns"):
        publish_audit(root, tmp_path / "public")


@pytest.mark.parametrize(
    "failure", ["version", "receipt_binding", "code_environment", "source_code"]
)
def test_frozen_lineage_failure_is_rejected_before_replay(
    tmp_path, monkeypatch, failure
):
    from copy import deepcopy
    from qbot.research.exploratory.statistical_audit import inputs
    from qbot.research.exploratory.statistical_audit import __main__ as audit_main

    private = tmp_path / "private"
    private.mkdir()
    (private / "receipt.json").write_text("SOFTWARE_TEST_ONLY", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text("# SOFTWARE_TEST_ONLY\n", encoding="utf-8")
    code = {"source_sha256": {"source.py": sha256_file(source)}}
    meta = {"data_version": inputs.DATA_VERSION, "files": {}}
    r = {
        "status": "SUCCESS",
        "data_version": inputs.DATA_VERSION,
        "code_and_environment": deepcopy(code),
        "data_files": {},
    }
    pub = deepcopy(r)
    pub["private_run_receipt_sha256"] = sha256_file(private / "receipt.json")
    if failure == "version":
        meta["data_version"] = "incorrect"
    elif failure == "receipt_binding":
        pub["private_run_receipt_sha256"] = "0" * 64
    elif failure == "code_environment":
        pub["code_and_environment"]["python"] = "incorrect"
    else:
        r["code_and_environment"]["source_sha256"]["source.py"] = "0" * 64
        pub["code_and_environment"] = deepcopy(r["code_and_environment"])
    monkeypatch.setattr(inputs, "ROOT", tmp_path)
    monkeypatch.setattr(
        inputs, "load_dataset", lambda *a, **k: (None, None, None, meta)
    )
    monkeypatch.setattr(inputs, "verify_receipt", lambda *a: r)
    monkeypatch.setattr(inputs, "verify_publication", lambda *a: pub)

    def forbidden(*a, **k):
        raise AssertionError("replay must not execute after invalid frozen lineage")

    monkeypatch.setattr(inputs, "analyze_frames", forbidden)
    with pytest.raises(ExploratoryContractError):
        inputs.load_frozen(
            tmp_path / "manifest.json", private, tmp_path / "publication"
        )
    monkeypatch.setattr(
        audit_main, "code_evidence", lambda: {"software_test_only": True}
    )
    status, dest = audit_main.run(
        tmp_path / "manifest.json",
        private,
        tmp_path / "publication",
        tmp_path / "exploratory",
    )
    assert status == "BLOCKED"
    assert verify_audit(dest / "receipt.json")["data_version"] is None
