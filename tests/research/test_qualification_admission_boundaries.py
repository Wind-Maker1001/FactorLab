"""Offline adversarial declarations; none of these bytes are financial evidence."""

from dataclasses import replace
from pathlib import Path
import struct

import pandas as pd
import pytest

from factorlab.data.adjustment.engine import build_hfq
from factorlab.data.assemble import assemble_market_data
from factorlab.data.corporate_actions import select_events
from factorlab.data.evidence import archive_bytes, json_bytes, snapshot_file, table_sha256
from factorlab.data.market_qualification import load_qualification, publish_qualification, qualify_market_data
from tests.research.market_data_fixture import cutoffs, event, qualification_inputs


def window(inputs, dates):
    return replace(inputs, research_start=str(dates[0].date()), research_end=str(dates[-1].date()))


def declarations_only(tmp_path):
    """Exercise the old local review contract, without pretending it is an audit."""
    i, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    i = window(i, dates)
    reference = archive_bytes(b"SYNTHETIC REGRESSION ONLY; NOT A REFERENCE\n", tmp_path / "sources",
                              "SYNTHETIC_REFERENCE", "synthetic-regression-reference",
                              "2024-12-31T00:00:00+08:00")
    spec = i.adjustments["fixture-security"]
    spec = replace(spec, view=replace(spec.view, snapshot=replace(spec.view.snapshot, synthetic_only=False)),
                   anchor=replace(spec.anchor, synthetic_only=False),
                   contract=replace(spec.contract, synthetic_only=False), reference_source=reference)
    i = replace(i, synthetic_only=False, identity=replace(i.identity, synthetic_only=False),
                units=(replace(i.units[0], synthetic_only=False),), adjustments={"fixture-security": spec},
                benchmark_source=i.raw_sources[0])
    roles = {"RAW": i.raw_sources, "PIT": (i.pit_source,), "STATUS": (i.status_source,),
             "CALENDAR": (i.calendar_source,), "IDENTITY": i.identity.artifacts,
             "UNITS": (i.units[0].artifact,), "ADJUSTMENT": spec.view.snapshot.artifacts,
             "REFERENCE": (reference,), "BENCHMARK": (i.benchmark_source,)}
    reviews = []
    for role, artifacts in roles.items():
        value = dict(schema="FACTORLAB_INDEPENDENT_SOURCE_REVIEW_V1", role=role,
                     reviewer="SYNTHETIC_REGRESSION_DECLARATION_NOT_AN_AUDITOR",
                     reviewed_at="2024-12-30T00:00:00+08:00", bound_sha256=[a.sha256 for a in artifacts],
                     upstream_origin="SYNTHETIC ONLY", time_basis="SYNTHETIC ONLY",
                     coverage_basis="SYNTHETIC ONLY", effective_date_basis="SYNTHETIC ONLY",
                     history_kind="CONTEMPORANEOUS", scope_start="2024-01-01", scope_end="2024-12-31")
        reviews.append(archive_bytes(json_bytes(value), tmp_path / "reviews", "SYNTHETIC_LOCAL_DECLARATION",
                                     "synthetic-regression-" + role, "2024-12-31T00:00:00+08:00"))
    return replace(i, source_reviews=tuple(reviews)), keys, dates, stocks, benchmark


def reassemble(i, benchmark):
    spec = i.adjustments["fixture-security"]
    result = build_hfq(spec.raw, spec.view, method=spec.method, anchor=spec.anchor, signal_as_of=spec.signal_as_of)
    return assemble_market_data(i.raw, {"fixture-security": result}, i.turnover, i.identity, i.units,
                                source_version=i.source_version, signal_as_of=i.signal_as_of,
                                benchmark=benchmark).stocks


@pytest.mark.parametrize("role", ["RAW", "PIT", "STATUS", "CALENDAR", "IDENTITY", "UNITS",
                                  "ADJUSTMENT", "REFERENCE", "BENCHMARK"])
def test_unchanged_source_bytes_cannot_certify_changed_normalized_facts(tmp_path, role):
    i, keys, dates, stocks, benchmark = declarations_only(tmp_path)
    original = {a.path: a.read() for a in (*i.raw_sources, i.pit_source, i.status_source,
                i.calendar_source, *i.identity.artifacts, *(u.artifact for u in i.units),
                *i.adjustments["fixture-security"].view.snapshot.artifacts,
                i.adjustments["fixture-security"].reference_source, i.benchmark_source)}
    spec = i.adjustments["fixture-security"]
    if role == "RAW":
        raw = i.raw.copy()
        raw["volume"] *= 2
        i = replace(i, raw=raw, adjustments={"fixture-security": replace(spec, raw=raw)})
    elif role == "PIT":
        i = replace(i, membership=i.membership.iloc[1:].reset_index(drop=True))
    elif role == "STATUS":
        i = replace(i, status=i.status.assign(is_st=True))
    elif role == "CALENDAR":
        extra = dates[-1] + pd.Timedelta(days=1)
        dates = dates.append(pd.DatetimeIndex([extra]))
        last = benchmark.iloc[[-1]].assign(date=extra)
        benchmark = pd.concat([benchmark, last], ignore_index=True)
        i = replace(i, calendar_reference=pd.DataFrame({"date": dates}))
    elif role == "IDENTITY":
        identity = replace(i.identity, intervals=(replace(i.identity.intervals[0], listing_date="1992-04-03"),))
        i = replace(i, identity=identity, listings=i.listings.assign(listing_date=pd.Timestamp("1992-04-03")))
    elif role == "UNITS":
        unit = replace(i.units[0], amount_multiplier=i.units[0].amount_multiplier * 2)
        i = replace(i, units=(unit,), adjustments={"fixture-security": replace(spec, raw=spec.raw.assign(amount=spec.raw.amount * 2))})
    elif role in {"ADJUSTMENT", "REFERENCE"}:
        if role == "ADJUSTMENT":
            snapshot = replace(spec.view.snapshot, events=(event(i.raw_sources[0], synthetic_only=False),))
            view = select_events(snapshot, effective_through=spec.view.effective_through,
                                 knowledge_as_of=spec.view.knowledge_as_of)
            spec = replace(spec, view=view)
        else:
            raw = i.raw.copy()
            raw[["open", "high", "low", "close"]] += 1
            i = replace(i, raw=raw)
            spec = replace(spec, raw=raw)
        generated = build_hfq(spec.raw, spec.view, method=spec.method, anchor=spec.anchor, signal_as_of=spec.signal_as_of)
        # Self-rehashing a changed reference was sufficient for the old gate.
        spec = replace(spec, reference=generated.bars,
                       contract=replace(spec.contract, reference_sha256=table_sha256(generated.bars)))
        i = replace(i, adjustments={"fixture-security": spec})
    else:
        benchmark = benchmark.copy()
        price_columns = [c for c in benchmark if c.endswith(("open", "high", "low", "close"))]
        benchmark[price_columns] += 1
    stocks = reassemble(i, benchmark)
    report = qualify_market_data(i, keys, dates, stocks=stocks, benchmark=benchmark)
    assert {path: Path(path).read_bytes() for path in original} == original
    assert not report.formal_admission
    assert not report.execution_admission
    assert any("SOURCE_BINDING/" + role in issue for issue in report.issues)


@pytest.mark.parametrize("missing", [0, -1, 3])
def test_pit_requires_both_boundaries_and_internal_days(tmp_path, missing):
    i, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    i = window(i, dates)
    i = replace(i, membership=i.membership.loc[i.membership.date.ne(dates[missing])].reset_index(drop=True))
    report = qualify_market_data(i, keys, dates, stocks=stocks, benchmark=benchmark)
    assert report.stages["PIT"] == "INCOMPLETE"
    assert any("coverage mismatch" in issue for issue in report.issues)


@pytest.mark.parametrize("missing", [None, 3, -1])
def test_warmup_is_separate_from_the_declared_research_window(tmp_path, missing):
    i, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    i = window(i, dates[3:])
    i = replace(i, membership=i.membership.loc[i.membership.date.isin(dates[3:])].reset_index(drop=True))
    if missing is not None:
        i = replace(i, membership=i.membership.loc[i.membership.date.ne(dates[missing])].reset_index(drop=True))
    report = qualify_market_data(i, keys, dates, stocks=stocks, benchmark=benchmark)
    assert report.stages["PIT"] == ("PASS" if missing is None else "INCOMPLETE")
    assert report.stages["STATUS"] == "PASS"  # Warmup status/RAW remain in the key scope.


def test_pit_window_must_not_be_inferred_when_not_declared(tmp_path):
    i, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    i = window(i, dates)
    i = replace(i, research_start="", research_end="")
    report = qualify_market_data(i, keys, dates, stocks=stocks, benchmark=benchmark)
    assert report.stages["PIT"] == "INCOMPLETE"
    assert any("research window" in issue for issue in report.issues)


def tdx_bytes(i, tmp_path):
    path = tmp_path / "sh600000.day"
    path.write_bytes(b"".join(struct.pack("<IIIIIfII", int(row.date.strftime("%Y%m%d")),
                     int(row.open * 100), int(row.high * 100), int(row.low * 100),
                     int(row.close * 100), row.amount, int(row.volume), 0) for row in i.raw.itertuples()))
    return snapshot_file(path, "TDX_DAY_BYTES_V1", "2024-12-31T00:00:00+08:00")


def test_tdx_raw_is_reparsed_even_when_products_are_consistently_rehashed(tmp_path):
    i, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    i = window(i, dates)
    artifact = tdx_bytes(i, tmp_path)
    i = replace(i, raw_sources=(artifact,))
    baseline = qualify_market_data(i, keys, dates, stocks=stocks, benchmark=benchmark)
    assert baseline.stages["RAW"] == "PASS"
    content = artifact.read()
    raw = i.raw.copy()
    raw["volume"] *= 2
    spec = replace(i.adjustments["fixture-security"], raw=raw)
    i = replace(i, raw=raw, adjustments={"fixture-security": spec})
    report = qualify_market_data(i, keys, dates, stocks=reassemble(i, benchmark), benchmark=benchmark)
    assert artifact.read() == content
    assert report.stages["ADJUSTMENT"] == "PASS"
    assert report.stages["RAW"] == "INCOMPLETE"
    assert any("reparsed source" in issue for issue in report.issues)


@pytest.mark.parametrize("change", ["value", "order", "missing_row", "extra_column", "duplicate_source", "mixed_source"])
def test_tdx_binding_compares_all_rows_fields_and_sources(tmp_path, change):
    i, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    artifact = tdx_bytes(i, tmp_path)
    i = replace(i, raw_sources=(artifact,))
    if change == "value":
        raw = i.raw.copy()
        raw.loc[0, "close"] += 1e-10  # No financial tolerance in source binding.
        i = replace(i, raw=raw)
    elif change == "order":
        i = replace(i, raw=i.raw.iloc[::-1].reset_index(drop=True))
    elif change == "missing_row":
        i = replace(i, raw=i.raw.iloc[1:].reset_index(drop=True))
    elif change == "extra_column":
        i = replace(i, raw=i.raw.assign(unsupported_fact=True))
    elif change == "duplicate_source":
        i = replace(i, raw_sources=(artifact, artifact))
    else:
        i = replace(i, raw_sources=(artifact, i.status_source))
    report = qualify_market_data(i, keys, dates, stocks=stocks, benchmark=benchmark)
    assert report.stages["RAW"] == "INCOMPLETE"
    assert not report.formal_admission


@pytest.mark.parametrize("change", ["truncated", "invalid_date", "duplicate_date"])
def test_rehashing_corrupt_tdx_bytes_does_not_make_them_parseable(tmp_path, change):
    i, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    artifact = tdx_bytes(i, tmp_path)
    content = artifact.read()
    if change == "truncated":
        content += b"x"
    elif change == "invalid_date":
        content = struct.pack("<I", 20241301) + content[4:]
    else:
        content = content[:32] + content[:32] + content[64:]
    Path(artifact.path).write_bytes(content)
    artifact = snapshot_file(Path(artifact.path), artifact.source_id, artifact.acquired_at)
    report = qualify_market_data(replace(i, raw_sources=(artifact,)), keys, dates, stocks=stocks, benchmark=benchmark)
    assert report.stages["RAW"] == "INCOMPLETE"


def test_tdx_replay_reads_packaged_bytes_without_reopening_original_locator(tmp_path):
    i, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    artifact = tdx_bytes(i, tmp_path)
    i = replace(i, raw_sources=(artifact,))
    path = publish_qualification(i, keys, dates, stocks=stocks, benchmark=benchmark, output_root=tmp_path / "package")
    Path(artifact.path).write_bytes(b"original file changed after publication")
    report = load_qualification(path, stocks=stocks, benchmark=benchmark)[0]
    assert report.stages["RAW"] == "PASS"
    assert report.stages["SOURCE_BINDING"] == "SOFTWARE_ONLY"
    assert not report.formal_admission


@pytest.mark.parametrize("start,end", [("2024-01-01", "2024-01-11"), ("2024-01-02", "2024-01-12"),
                                     ("2024-01-11", "2024-01-02"), ("2024-01-06", "2024-01-11")])
def test_independent_window_requires_calendar_boundaries(tmp_path, start, end):
    i, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    report = qualify_market_data(replace(i, research_start=start, research_end=end), keys, dates,
                                 stocks=stocks, benchmark=benchmark)
    assert report.stages["PIT"] == "INCOMPLETE"
    assert any("research window" in issue for issue in report.issues)


def test_warmup_members_cannot_expand_the_formal_window(tmp_path):
    i, keys, dates, stocks, benchmark = qualification_inputs(tmp_path)
    report = qualify_market_data(window(i, dates[3:]), keys, dates, stocks=stocks, benchmark=benchmark)
    assert report.stages["PIT"] == "INCOMPLETE"
    assert any("coverage mismatch" in issue for issue in report.issues)


def test_real_declaration_cannot_lower_csi500_member_count(tmp_path):
    i, keys, dates, stocks, benchmark = declarations_only(tmp_path)
    report = qualify_market_data(i, keys, dates, stocks=stocks, benchmark=benchmark)
    assert report.stages["PIT"] == "INCOMPLETE"
    assert any("exactly 500" in issue for issue in report.issues)


def test_complete_500_member_window_is_checked_on_every_day(tmp_path):
    i, _, dates, stocks, benchmark = qualification_inputs(tmp_path)
    keys = pd.MultiIndex.from_product([dates, ["%06d.SH" % (600000 + n) for n in range(500)]],
                                     names=["date", "symbol"]).to_frame(index=False)
    i = replace(i, membership=keys.assign(is_member=True), expected_member_count=500)
    report = qualify_market_data(i, keys, dates, stocks=stocks, benchmark=benchmark)
    assert report.stages["PIT"] == "PASS"
    incomplete = i.membership.drop(index=len(i.membership) - 1)
    report = qualify_market_data(replace(i, membership=incomplete), keys, dates, stocks=stocks, benchmark=benchmark)
    assert report.stages["PIT"] == "INCOMPLETE"
    assert any("expected 500" in issue for issue in report.issues)
