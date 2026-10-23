"""SYNTHETIC_TEST_ONLY adversarial checks, never real financial evidence.

Every generated price below is an offline software fixture. The real research
entry point must reject these datasets even when their hashes are correct.
"""

from copy import deepcopy
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.data.market_qualification import load_qualification
from factorlab.data.providers.local import LocalParquetProvider
from factorlab.errors import DataQualityError, ProviderError
from factorlab.exploratory.contract import (
    ExploratoryContractError,
    REQUIRED_MARKERS,
    assert_exploratory_only,
    load_dataset,
    manifest_version,
    sha256_file,
    validate_dataset_frames,
    validate_manifest,
)
from factorlab.factors.registry import FACTOR_REGISTRY, compute_factors
from factorlab.forward.model_bundle import BundleIntegrityError, load_model_bundle
from factorlab.forward.signal_recorder import (
    SignalValidationError,
    load_signal_record,
)
from factorlab.labels.forward_return import build_forward_excess_return_labels


def synthetic_frames(size=20, periods=165):
    """Business dates are synthetic sessions, not a historical exchange calendar."""
    rng = np.random.default_rng(20261014)
    dates = pd.bdate_range("2025-01-02", periods=periods)
    symbols = ["%06d.SZ" % (n + 1) for n in range(size)]

    def one(symbol, number, benchmark=False):
        increments = rng.normal(0.0002, 0.012, periods)
        close = (5000 if benchmark else 10 + number) * np.exp(np.cumsum(increments))
        opening = close * (1 + rng.normal(0, 0.004, periods))
        high = np.maximum(opening, close) * 1.03
        low = np.minimum(opening, close) * 0.97
        factor = 1.0 if benchmark else 2.0 + number / 10.0
        volume = rng.integers(1_000_000, 2_000_000, periods).astype(float)
        frame = pd.DataFrame(
            {
                "date": dates,
                "symbol": symbol,
                "open": opening,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": volume * close,
                "turnover": np.nan if benchmark else rng.uniform(0.005, 0.035, periods),
                "adj_factor": factor,
            }
        )
        for field in ("open", "high", "low", "close"):
            frame["raw_" + field] = frame[field]
            frame["adj_" + field] = frame[field] * factor
        return frame

    bars = pd.concat([one(s, n) for n, s in enumerate(symbols)], ignore_index=True)
    bars = bars.sort_values(["date", "symbol"]).reset_index(drop=True)
    benchmark = one("000905.SH", 0, benchmark=True)
    sample = {
        "symbols": symbols,
        "pit": False,
        "classification": "NON_PIT_CONVENIENCE",
        "selection_method": "SYNTHETIC_TEST_ONLY sequential fake symbols",
        "selection_date": "2026-10-14",
        "frozen_at": "2026-10-14T10:00:00+08:00",
    }
    manifest = {
        "schema": "EXPLORATORY_DATASET_V1",
        "purpose": "EXPLORATORY",
        "qualification": "UNVERIFIED",
        "formal_admission": False,
        "historical_status": "HISTORICALLY_RECONSTRUCTED",
        "synthetic_only": True,
        "markers": list(REQUIRED_MARKERS),
        "vendor": "SYNTHETIC_TEST_ONLY",
        "source_version": "synthetic-test-v1",
        "retrieved_at": "2026-10-14T11:00:00+08:00",
        "research_start": str(dates[130].date()),
        "research_end": str(dates[-1].date()),
        "warmup_min_sessions": 130,
        "benchmark_basis": "CSI500_PRICE_INDEX_UNADJUSTED",
        "units": {
            "price": "CNY",
            "volume": "SHARES",
            "amount": "CNY",
            "turnover": "RATIO",
        },
        "price_basis": {"canonical": "RAW", "raw": "RAW", "adj": "HFQ"},
    }
    return bars, benchmark, dates, manifest, sample


@pytest.fixture
def synthetic_dataset(tmp_path):
    bars, benchmark, calendar, manifest, sample = synthetic_frames()
    bars.to_parquet(tmp_path / "bars.parquet", index=False)
    benchmark.to_parquet(tmp_path / "benchmark.parquet", index=False)
    pd.DataFrame({"date": calendar}).to_csv(tmp_path / "calendar.csv", index=False)
    (tmp_path / "sample.json").write_text(json.dumps(sample), encoding="utf-8")
    (tmp_path / "source.bin").write_bytes(
        b"SYNTHETIC_TEST_ONLY; NEVER FINANCIAL EVIDENCE\n"
    )
    manifest["files"] = {}
    for name, filename in (
        ("bars", "bars.parquet"),
        ("benchmark", "benchmark.parquet"),
        ("calendar", "calendar.csv"),
        ("sample", "sample.json"),
    ):
        manifest["files"][name] = {
            "path": filename,
            "sha256": sha256_file(tmp_path / filename),
            "source_version": manifest["source_version"],
        }
    manifest["sources"] = [
        {
            "path": "source.bin",
            "sha256": sha256_file(tmp_path / "source.bin"),
            "source_version": manifest["source_version"],
        }
    ]
    manifest["data_version"] = manifest_version(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, bars, benchmark, calendar, manifest, sample


def test_software_fixture_is_explicit_and_never_accepted_as_real(synthetic_dataset):
    path, bars, benchmark, calendar, manifest, sample = synthetic_dataset
    assert manifest["vendor"] == "SYNTHETIC_TEST_ONLY"
    with pytest.raises(ExploratoryContractError, match="SYNTHETIC_TEST_ONLY"):
        load_dataset(path)
    observed, observed_benchmark, observed_calendar, metadata = load_dataset(
        path, allow_synthetic=True
    )
    pd.testing.assert_frame_equal(observed, bars)
    pd.testing.assert_frame_equal(observed_benchmark, benchmark)
    pd.testing.assert_index_equal(observed_calendar, calendar, check_names=False)
    assert metadata["sample"] == sample
    assert observed.attrs["formal_admission"] is False
    assert observed.attrs["qualification"] == "UNVERIFIED"


@pytest.mark.parametrize(
    "change",
    ["duplicate", "missing_raw", "missing_adj", "partial_ohlc", "missing_turnover"],
)
def test_missing_and_duplicate_price_inputs_fail_explicitly(synthetic_dataset, change):
    _, bars, benchmark, calendar, manifest, sample = synthetic_dataset
    if change == "duplicate":
        bars = pd.concat([bars, bars.iloc[[0]]], ignore_index=True)
    elif change == "missing_raw":
        bars = bars.drop(columns="raw_open")
    elif change == "missing_adj":
        bars = bars.drop(columns="adj_close")
    elif change == "missing_turnover":
        bars = bars.drop(columns="turnover")
    else:
        bars.loc[0, "adj_open"] = np.nan
    with pytest.raises(ExploratoryContractError):
        validate_dataset_frames(bars, benchmark, calendar, manifest, sample)


def test_whole_missing_session_stays_missing_and_is_not_repaired(synthetic_dataset):
    _, bars, benchmark, calendar, manifest, sample = synthetic_dataset
    columns = [c for c in bars if c not in ("date", "symbol")]
    bars.loc[0, columns] = np.nan
    validate_dataset_frames(bars, benchmark, calendar, manifest, sample)
    assert bars.loc[0, columns].isna().all()


@pytest.mark.parametrize(
    "change",
    ["wrong_symbol", "equity_hfq_index", "wrong_declared_basis", "duplicate_date"],
)
def test_price_index_cannot_be_aliased_to_an_equity_hfq_series(
    synthetic_dataset, change
):
    _, bars, benchmark, calendar, manifest, sample = synthetic_dataset
    if change == "wrong_declared_basis":
        manifest = deepcopy(manifest)
        manifest["benchmark_basis"] = "EQUITY_HFQ"
        manifest["data_version"] = manifest_version(manifest)
        with pytest.raises(
            ExploratoryContractError, match="unadjusted CSI500 price index"
        ):
            validate_manifest(manifest, allow_synthetic=True)
        return
    if change == "wrong_symbol":
        benchmark = benchmark.assign(symbol="000300.SH")
    elif change == "equity_hfq_index":
        benchmark = benchmark.copy()
        for field in ("open", "high", "low", "close"):
            benchmark["adj_" + field] *= 2
        benchmark["adj_factor"] = 2.0
    else:
        benchmark = pd.concat([benchmark, benchmark.iloc[[-1]]], ignore_index=True)
    with pytest.raises(ExploratoryContractError):
        validate_dataset_frames(bars, benchmark, calendar, manifest, sample)


@pytest.mark.parametrize(
    "change",
    [
        "canonical_is_hfq",
        "wrong_factor",
        "volume_lots",
        "amount_wan",
        "fractional_shares",
        "turnover_percent",
    ],
)
def test_raw_hfq_and_unit_confusion_is_rejected(synthetic_dataset, change):
    _, bars, benchmark, calendar, manifest, sample = synthetic_dataset
    if change == "canonical_is_hfq":
        for field in ("open", "high", "low", "close"):
            bars[field] = bars["adj_" + field]
    elif change == "wrong_factor":
        bars["adj_factor"] *= 2
    elif change == "volume_lots":
        bars["volume"] /= 100
    elif change == "amount_wan":
        bars["amount"] /= 10000
    elif change == "fractional_shares":
        bars["volume"] += 0.5
    else:
        bars["turnover"] = 25.0
    with pytest.raises(ExploratoryContractError):
        validate_dataset_frames(bars, benchmark, calendar, manifest, sample)


@pytest.mark.parametrize(
    "field,bad_value",
    [("volume", "LOTS"), ("amount", "WAN_CNY"), ("turnover", "PERCENT")],
)
def test_misdeclared_units_fail_even_when_manifest_rehashed(
    synthetic_dataset, field, bad_value
):
    manifest = deepcopy(synthetic_dataset[4])
    manifest["units"][field] = bad_value
    manifest["data_version"] = manifest_version(manifest)
    with pytest.raises(ExploratoryContractError, match="units"):
        validate_manifest(manifest, allow_synthetic=True)


@pytest.mark.parametrize(
    "change",
    [
        "outside_calendar",
        "time_of_day",
        "future_price",
        "duplicate_calendar",
        "short_warmup",
    ],
)
def test_calendar_and_acquisition_dates_are_not_silently_normalized(
    synthetic_dataset, change
):
    _, bars, benchmark, calendar, manifest, sample = synthetic_dataset
    if change == "outside_calendar":
        bars.loc[0, "date"] = calendar[0] - pd.Timedelta(days=1)
    elif change == "time_of_day":
        bars.loc[0, "date"] += pd.Timedelta(hours=1)
    elif change == "future_price":
        manifest = dict(
            manifest, retrieved_at=str(calendar[135].date()) + "T11:00:00+08:00"
        )
        sample = dict(
            sample,
            selection_date=str(calendar[0].date()),
            frozen_at=str(calendar[0].date()) + "T10:00:00+08:00",
        )
    elif change == "duplicate_calendar":
        calendar = calendar.append(calendar[-1:])
    else:
        calendar = calendar[1:]
    with pytest.raises(ExploratoryContractError):
        validate_dataset_frames(bars, benchmark, calendar, manifest, sample)


@pytest.mark.parametrize(
    "change",
    ["claims_pit", "drops_bias_disclosure", "frozen_after_fetch", "unfrozen_member"],
)
def test_convenience_selection_must_be_frozen_and_non_pit(synthetic_dataset, change):
    _, bars, benchmark, calendar, manifest, sample = synthetic_dataset
    sample = deepcopy(sample)
    if change == "claims_pit":
        sample["pit"] = True
    elif change == "drops_bias_disclosure":
        sample["classification"] = "CSI500_PIT"
    elif change == "frozen_after_fetch":
        sample["frozen_at"] = "2026-10-14T12:00:00+08:00"
    else:
        sample["symbols"] = sample["symbols"][1:]
    with pytest.raises(ExploratoryContractError):
        validate_dataset_frames(bars, benchmark, calendar, manifest, sample)


@pytest.mark.parametrize("purpose", ["FORMAL", "FORWARD", "LIVE", "LIVE_TRADING"])
def test_exploratory_artifacts_cannot_be_routed_to_other_purposes(
    synthetic_dataset, purpose
):
    with pytest.raises(ExploratoryContractError, match="FORMAL, FORWARD, or LIVE"):
        assert_exploratory_only(synthetic_dataset[4], purpose=purpose)


@pytest.mark.parametrize(
    "field,value",
    [
        ("formal_admission", True),
        ("qualification", "VERIFIED"),
        ("historical_status", "CONTEMPORANEOUS"),
        ("purpose", "FORMAL"),
    ],
)
def test_rehash_cannot_upgrade_reconstructed_history_to_formal(
    synthetic_dataset, field, value
):
    manifest = deepcopy(synthetic_dataset[4])
    manifest[field] = value
    manifest["data_version"] = manifest_version(manifest)
    with pytest.raises(ExploratoryContractError):
        validate_manifest(manifest, allow_synthetic=True)


@pytest.mark.parametrize("role", ["bars", "benchmark", "calendar", "sample", "source"])
def test_source_and_product_hash_mismatches_fail_before_analysis(
    synthetic_dataset, role
):
    path, _, _, _, manifest, _ = synthetic_dataset
    item = manifest["sources"][0] if role == "source" else manifest["files"][role]
    artifact = path.parent / item["path"]
    artifact.write_bytes(artifact.read_bytes() + b"TAMPERED")
    with pytest.raises(ExploratoryContractError, match="hash mismatch"):
        load_dataset(path, allow_synthetic=True)


@pytest.mark.parametrize("role", ["bars", "benchmark", "calendar", "sample", "source"])
def test_mixed_source_version_fails_even_when_semantic_hash_is_updated(
    synthetic_dataset, role
):
    manifest = deepcopy(synthetic_dataset[4])
    item = manifest["sources"][0] if role == "source" else manifest["files"][role]
    item["source_version"] = "different-vendor-or-history-v2"
    manifest["data_version"] = manifest_version(manifest)
    with pytest.raises(ExploratoryContractError, match="source version mismatch"):
        validate_manifest(manifest, allow_synthetic=True)


def test_changed_source_declaration_requires_new_dataset_identity(synthetic_dataset):
    manifest = deepcopy(synthetic_dataset[4])
    manifest["vendor"] = "SYNTHETIC_TEST_ONLY renamed source"
    with pytest.raises(ExploratoryContractError, match="semantic hash"):
        validate_manifest(manifest, allow_synthetic=True)


def test_existing_formal_readers_reject_exploratory_dataset(synthetic_dataset):
    path, bars, benchmark, _, _, _ = synthetic_dataset
    with pytest.raises(DataQualityError, match="qualification manifest fields/schema"):
        load_qualification(path, stocks=bars, benchmark=benchmark)
    with pytest.raises(ProviderError):
        LocalParquetProvider(path.parent).assert_research_admission(
            bars[["date", "symbol"]].assign(is_member=True),
            synthetic_dataset[3],
            bars,
        )


def test_existing_forward_readers_reject_exploratory_dataset(synthetic_dataset):
    path = synthetic_dataset[0]
    with pytest.raises(
        BundleIntegrityError, match="exactly model.pkl and metadata.json"
    ):
        load_model_bundle(path.parent)
    with pytest.raises(SignalValidationError, match="completion receipt"):
        load_signal_record(path.parent)


def test_correct_forward_like_layout_cannot_turn_exploratory_metadata_into_signal(
    synthetic_dataset, tmp_path
):
    root = tmp_path / "fake-forward"
    root.mkdir()
    content = json.dumps(synthetic_dataset[4]).encode("utf-8")
    (root / "manifest.json").write_bytes(content)
    (root / "manifest.sha256").write_text(
        hashlib.sha256(content).hexdigest(), encoding="ascii"
    )
    (root / "signals.csv").write_text("date,symbol\n", encoding="utf-8")
    with pytest.raises(SignalValidationError, match="schema"):
        load_signal_record(root, require_completion=False)


def test_label_alignment_uses_actual_session_offsets_and_never_fills_tail(
    synthetic_dataset,
):
    _, bars, benchmark, calendar, _, _ = synthetic_dataset
    labels = build_forward_excess_return_labels(bars, benchmark, calendar=calendar)
    signal = calendar[130]
    symbol = bars.symbol.iloc[0]
    row = labels.loc[labels.date.eq(signal) & labels.symbol.eq(symbol)].iloc[0]
    stock = bars.loc[bars.symbol.eq(symbol)].set_index("date")
    index = benchmark.set_index("date")
    expected = (
        stock.at[calendar[136], "adj_open"] / stock.at[calendar[131], "adj_open"]
        - index.at[calendar[136], "adj_open"] / index.at[calendar[131], "adj_open"]
    )
    assert row.entry_date == calendar[131]
    assert row.exit_date == calendar[136]
    assert row.EXCESS_RET5 == pytest.approx(expected)
    assert labels.loc[labels.date.isin(calendar[-6:]), "EXCESS_RET5"].isna().all()
    # Removing t+1 must not shift entry to the next observed row.
    missing = bars.loc[~(bars.symbol.eq(symbol) & bars.date.eq(calendar[131]))]
    missing_labels = build_forward_excess_return_labels(
        missing, benchmark, calendar=calendar
    )
    selected = missing_labels.loc[
        missing_labels.date.eq(signal) & missing_labels.symbol.eq(symbol)
    ]
    assert selected.EXCESS_RET5.isna().all()


def test_all_fourteen_factors_are_invariant_to_future_input_mutation(synthetic_dataset):
    _, bars, _, calendar, _, _ = synthetic_dataset
    names = list(FACTOR_REGISTRY)
    baseline = compute_factors(bars, names)
    changed = bars.copy()
    future = changed.date.gt(calendar[140])
    for column in changed.columns.difference(["date", "symbol"]):
        changed.loc[future, column] *= 7.0
    observed = compute_factors(changed, names)
    before = baseline.loc[baseline.date.le(calendar[140]), ["date", "symbol"] + names]
    after = observed.loc[observed.date.le(calendar[140]), ["date", "symbol"] + names]
    pd.testing.assert_frame_equal(before, after, check_exact=True)


def test_exploratory_quantile_boundaries_exclude_missing_future_labels():
    from factorlab.exploratory.runner import (
        FACTOR_NAMES,
        evaluate_exploratory_factors,
    )

    calendar = pd.DatetimeIndex(["2025-09-12"])
    size = 24
    frame = pd.DataFrame(
        {
            "date": calendar[0],
            "symbol": ["%06d.SZ" % (n + 1) for n in range(size)],
            "EXCESS_RET5": list(np.arange(20, dtype=float)) + [np.nan] * 4,
            "MARKET_REGIME": "SIDEWAYS",
        }
    )
    for name in FACTOR_NAMES:
        frame[name] = np.arange(size, dtype=float)
    result = evaluate_exploratory_factors(frame, calendar)
    quintiles = result.quantile_returns.loc[result.quantile_returns.scope.eq("ALL")]
    for name in FACTOR_NAMES:
        selected = quintiles.loc[quintiles.factor.eq(name)].set_index("quantile")
        assert selected.at["Q1", "future_return"] == pytest.approx(1.5)
        assert selected.at["Q5", "future_return"] == pytest.approx(17.5)
        buckets = selected.loc[["Q1", "Q2", "Q3", "Q4", "Q5"]]
        assert buckets["observations"].sum() == 20
        assert buckets["labeled_observations"].sum() == 20


def test_insufficient_cross_section_has_no_fabricated_ic_or_quintiles():
    from factorlab.exploratory.runner import (
        FACTOR_NAMES,
        evaluate_exploratory_factors,
    )

    calendar = pd.DatetimeIndex(["2025-09-12"])
    frame = pd.DataFrame(
        {
            "date": calendar[0],
            "symbol": ["%06d.SZ" % (n + 1) for n in range(19)],
            "EXCESS_RET5": np.arange(19) / 100.0,
            "MARKET_REGIME": "SIDEWAYS",
        }
    )
    for name in FACTOR_NAMES:
        frame[name] = np.arange(19, dtype=float)
    result = evaluate_exploratory_factors(frame, calendar)
    assert result.daily_ic[["IC", "RankIC"]].isna().all().all()
    assert result.daily_ic.observations.eq(19).all()
    assert result.quantile_returns.empty


def test_real_runner_rejects_synthetic_fixture_and_marks_blocked(
    synthetic_dataset, tmp_path
):
    from factorlab.exploratory.runner import run_research

    result = run_research(synthetic_dataset[0], output_root=tmp_path / "exploratory")
    assert result.status == "BLOCKED"
    assert any("SYNTHETIC_TEST_ONLY" in reason for reason in result.reasons)


def test_runner_factors_regimes_and_sample_do_not_depend_on_future_rows():
    from factorlab.exploratory.runner import FACTOR_NAMES, analyze_frames

    bars, benchmark, calendar, manifest, sample = synthetic_frames(size=21)
    manifest["sample"] = sample
    original = analyze_frames(bars, benchmark, calendar, manifest)
    changed = bars.copy()
    future = changed.date.gt(calendar[144])
    for field in changed.columns.difference(["date", "symbol"]):
        changed.loc[future, field] *= 7
    # Future delisting-like absent rows cannot retroactively remove a security.
    changed = changed.loc[~(future & changed.symbol.eq(sample["symbols"][0]))]
    changed_benchmark = benchmark.copy()
    changed_benchmark.loc[changed_benchmark.date.gt(calendar[144]), "adj_close"] *= 7
    observed = analyze_frames(changed, changed_benchmark, calendar, manifest)
    assert (
        original.quality["analyzed_security_count"]
        == observed.quality["analyzed_security_count"]
        == 21
    )
    assert observed.security_coverage.eligible_by_pre_window_only.all()
    columns = (
        ["date", "symbol", "MARKET_REGIME"]
        + list(FACTOR_NAMES)
        + ["raw_" + name for name in FACTOR_NAMES]
    )
    before = original.frame.loc[original.frame.date.le(calendar[144]), columns]
    after = observed.frame.loc[observed.frame.date.le(calendar[144]), columns]
    pd.testing.assert_frame_equal(before, after, check_exact=True)


def test_runner_reindexes_missing_session_without_skipping_calendar_offsets():
    from factorlab.exploratory.runner import analyze_frames

    bars, benchmark, calendar, manifest, sample = synthetic_frames(size=21)
    manifest["sample"] = sample
    symbol = sample["symbols"][0]
    missing = bars.loc[~(bars.symbol.eq(symbol) & bars.date.eq(calendar[131]))]
    result = analyze_frames(missing, benchmark, calendar, manifest)
    absent = result.frame.loc[
        result.frame.symbol.eq(symbol) & result.frame.date.eq(calendar[131])
    ].iloc[0]
    assert pd.isna(absent.adj_close)
    assert pd.isna(absent.raw_REV1)
    next_session = result.frame.loc[
        result.frame.symbol.eq(symbol) & result.frame.date.eq(calendar[132])
    ].iloc[0]
    assert pd.isna(next_session.raw_REV1)
    assert result.quality["no_price_imputation"] is True
    assert result.quality["analyzed_security_count"] == 21


@pytest.mark.parametrize(
    "missing", ["one_warmup_security", "all_turnover", "benchmark_warmup"]
)
def test_required_real_inputs_cannot_be_replaced_by_later_window_history(missing):
    from factorlab.exploratory.runner import analyze_frames

    bars, benchmark, calendar, manifest, sample = synthetic_frames()
    manifest["sample"] = sample
    if missing == "one_warmup_security":
        bars = bars.loc[
            ~(bars.symbol.eq(sample["symbols"][0]) & bars.date.eq(calendar[0]))
        ]
    elif missing == "all_turnover":
        bars["turnover"] = np.nan
    else:
        benchmark.loc[benchmark.date.lt(calendar[130]), ["adj_open", "adj_close"]] = (
            np.nan
        )
    with pytest.raises(ExploratoryContractError, match="pre-window"):
        analyze_frames(bars, benchmark, calendar, manifest)


def test_label_eligibility_adapter_keeps_temporal_diagnostics_on_full_calendar():
    from factorlab.exploratory.runner import (
        FACTOR_NAMES,
        evaluate_exploratory_factors,
    )

    calendar = pd.bdate_range("2025-09-12", periods=3)
    frame = pd.DataFrame(
        {
            "date": np.repeat(calendar, 25),
            "symbol": np.tile(["%06d.SZ" % (n + 1) for n in range(25)], 3),
            "EXCESS_RET5": np.arange(75, dtype=float) / 100,
            "MARKET_REGIME": np.repeat(["BULL", "BEAR", "BULL"], 25),
        }
    )
    rng = np.random.default_rng(123)
    for name in FACTOR_NAMES:
        frame[name] = rng.normal(size=75)
    complete = evaluate_exploratory_factors(frame, calendar)
    missing = frame.copy()
    missing.loc[missing.date.eq(calendar[1]), "EXCESS_RET5"] = np.nan
    observed = evaluate_exploratory_factors(missing, calendar)
    pd.testing.assert_frame_equal(
        observed.autocorrelation, complete.autocorrelation, check_exact=True
    )
    pd.testing.assert_frame_equal(
        observed.turnover, complete.turnover, check_exact=True
    )
    pd.testing.assert_frame_equal(
        observed.correlation, complete.correlation, check_exact=True
    )
    assert observed.quantile_returns.loc[
        observed.quantile_returns.date.eq(calendar[1])
    ].empty


@pytest.mark.parametrize(
    "tamper",
    [
        "derived_bytes",
        "source_version",
        "new_unhashed_file",
        "formal_flag",
        "forward_flag",
        "live_flag",
    ],
)
def test_receipt_hash_and_exploratory_boundaries_survive_reader_checks(
    synthetic_dataset, tmp_path, tamper
):
    from factorlab.exploratory.runner import run_research, verify_receipt

    result = run_research(synthetic_dataset[0], output_root=tmp_path / "exploratory")
    receipt_path = result.output_dir / "receipt.json"
    payload = verify_receipt(receipt_path)
    assert payload["status"] == "BLOCKED"
    assert payload["formal_admission"] is False
    assert payload["forward_allowed"] is False
    assert payload["live_allowed"] is False
    if tamper == "derived_bytes":
        relative = next(iter(payload["artifacts"]))
        artifact = result.output_dir / relative
        artifact.write_bytes(artifact.read_bytes() + b"TAMPERED")
    elif tamper == "new_unhashed_file":
        (result.output_dir / "unexpected.csv").write_text(
            "unhashed artifact", encoding="utf-8"
        )
    else:
        field = {
            "formal_flag": "formal_admission",
            "forward_flag": "forward_allowed",
            "live_flag": "live_allowed",
        }.get(tamper)
        if field:
            payload[field] = True
            # A self-consistent new hash cannot authorize another purpose.
            digest = hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, allow_nan=False
                ).encode("utf-8")
            ).hexdigest()
            payload["receipt_sha256"] = digest
        else:
            payload["source_version"] = "SYNTHETIC_TEST_ONLY changed source"
        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ExploratoryContractError):
        verify_receipt(receipt_path)


@pytest.mark.parametrize(
    "role,broken_bytes",
    [
        ("bars", b"SYNTHETIC_TEST_ONLY; corrupt parquet bytes\n"),
        ("benchmark", b"SYNTHETIC_TEST_ONLY; corrupt parquet bytes\n"),
        ("calendar", b"SYNTHETIC_TEST_ONLY_other_column\n2025-01-02\n"),
        ("calendar", b""),
        ("sample", b'{"SYNTHETIC_TEST_ONLY":'),
        ("sample", b"\xffSYNTHETIC_TEST_ONLY invalid UTF8"),
        ("sample", b'["SYNTHETIC_TEST_ONLY"]'),
        ("sample", b"null"),
    ],
    ids=[
        "corrupt-bars-parquet",
        "corrupt-benchmark-parquet",
        "calendar-missing-date",
        "calendar-empty-csv",
        "sample-malformed-json",
        "sample-invalid-utf8",
        "sample-json-array",
        "sample-json-null",
    ],
)
def test_hash_consistent_unreadable_inputs_are_contract_failures(
    synthetic_dataset, role, broken_bytes
):
    """A matching digest authenticates bytes, never their parseability."""
    path, _, _, _, original, _ = synthetic_dataset
    manifest = deepcopy(original)
    item = manifest["files"][role]
    artifact = path.parent / item["path"]
    artifact.write_bytes(broken_bytes)
    item["sha256"] = sha256_file(artifact)
    manifest["data_version"] = manifest_version(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert manifest["data_version"] != original["data_version"]
    assert item["sha256"] != original["files"][role]["sha256"]
    validate_manifest(manifest, allow_synthetic=True)
    with pytest.raises(ExploratoryContractError) as failure:
        load_dataset(path, allow_synthetic=True)
    assert str(failure.value)


@pytest.mark.parametrize(
    "manifest_bytes",
    [
        b'{"SYNTHETIC_TEST_ONLY":',
        b"\xffSYNTHETIC_TEST_ONLY invalid UTF8",
        b'["SYNTHETIC_TEST_ONLY"]',
        b'{"SYNTHETIC_TEST_ONLY": NaN}',
    ],
    ids=["malformed-json", "invalid-utf8", "non-object-json", "nonfinite-json"],
)
def test_unreadable_manifest_has_contract_error_and_cli_blocked_report(
    tmp_path, capsys, manifest_bytes
):
    """Broken input produces a failure report with zero empirical results."""
    from factorlab.exploratory.__main__ import main
    from factorlab.exploratory.runner import verify_receipt

    path = tmp_path / "unreadable-manifest.json"
    path.write_bytes(manifest_bytes)
    with pytest.raises(ExploratoryContractError):
        load_dataset(path)
    code = main(
        ["run", "--manifest", str(path), "--output", str(tmp_path / "exploratory")]
    )
    assert code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED"
    assert result["reasons"]
    from pathlib import Path

    output = Path(result["output_dir"])
    receipt = verify_receipt(output / "receipt.json")
    assert receipt["status"] == "BLOCKED"
    assert receipt["input_metadata_verified"] is False
    assert receipt["data_version"] is None
    assert receipt["forward_allowed"] is False
    assert receipt["live_allowed"] is False
    assert not (output / "daily_ic.csv").exists()


def test_cli_turns_deserialization_contract_failure_into_blocked(
    tmp_path, monkeypatch, capsys
):
    """Mock only a reader failure; never admit generated data as real prices."""
    from pathlib import Path

    from factorlab.exploratory import runner
    from factorlab.exploratory.__main__ import main

    reason = "SYNTHETIC_TEST_ONLY mocked input reader failure: unreadable bars parquet"

    def unreadable(*args, **kwargs):
        raise ExploratoryContractError(reason)

    monkeypatch.setattr(runner, "load_dataset", unreadable)
    code = main(
        [
            "run",
            "--manifest",
            str(tmp_path / "missing-authorized-input.json"),
            "--output",
            str(tmp_path / "exploratory"),
        ]
    )
    assert code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED"
    assert result["reasons"] == [reason]
    output = Path(result["output_dir"])
    receipt = runner.verify_receipt(output / "receipt.json")
    assert receipt["status"] == "BLOCKED"
    assert receipt["input_metadata_verified"] is False
    assert receipt["data_version"] is None
    assert receipt["formal_admission"] is False
    assert receipt["forward_allowed"] is False
    assert receipt["live_allowed"] is False
    quality = json.loads((output / "data_quality.json").read_text(encoding="utf-8"))
    assert quality["empirical_results_computed"] is False
    assert not (output / "derived_factor_labels.parquet").exists()


def test_hash_consistent_nested_sample_symbols_fail_as_contract_error(
    synthetic_dataset,
):
    path, _, _, _, original, sample = synthetic_dataset
    manifest = deepcopy(original)
    sample = deepcopy(sample)
    sample["symbols"] = [["SYNTHETIC_TEST_ONLY"]]
    artifact = path.parent / manifest["files"]["sample"]["path"]
    artifact.write_text(json.dumps(sample), encoding="utf-8")
    manifest["files"]["sample"]["sha256"] = sha256_file(artifact)
    manifest["data_version"] = manifest_version(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ExploratoryContractError, match="structured dataset"):
        load_dataset(path, allow_synthetic=True)
