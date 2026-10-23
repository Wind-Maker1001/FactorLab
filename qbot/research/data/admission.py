"""One fail-closed financial admission boundary for every formal consumer.

No synthetic/exploratory override exists here. Those workflows use their
separate runners; software orchestration tests explicitly mock this boundary
without manufacturing a positive financial qualification.
"""

from dataclasses import asdict
import hashlib
import re

import pandas as pd

from qbot.research.data.market_qualification import load_qualification
from qbot.research.data.evidence import table_sha256
from qbot.research.data.providers.base import MarketQualificationEvidence
from qbot.research.data.tdx import read_snapshot, safe_path
from qbot.research.errors import FactorLabError, ProviderError


PREFIX = "MARKET DATA QUALIFICATION REQUIRED: "


def _reject(message):
    raise ProviderError(PREFIX + message)


def _same_rows(actual, expected, keys, index, label):
    if (not isinstance(actual, pd.DataFrame) or actual.empty
            or not set(expected.columns).issubset(actual.columns)
            or actual.duplicated(keys).any()):
        _reject(label + " rows/schema are incomplete or duplicated")
    if len(index.difference(pd.MultiIndex.from_frame(actual[keys]))):
        _reject(label + " requested rows are missing")
    if len(keys) == 1:
        index = pd.Index(index.get_level_values(0), name=keys[0])
    left = actual.set_index(keys).reindex(index)[list(expected.set_index(keys).columns)]
    right = expected.set_index(keys).reindex(index)
    try:
        pd.testing.assert_frame_equal(left, right, check_exact=True)
    except AssertionError as exc:
        raise ProviderError(PREFIX + label + " differ from qualified source rows") from exc


def _bind_scoped_rows(actual, expected, index, dates, symbols, label):
    _same_rows(actual, expected, ["date", "symbol"], index, label)
    scoped = actual.loc[actual.date.isin(dates) & actual.symbol.isin(symbols), ["date", "symbol"]]
    if len(pd.MultiIndex.from_frame(scoped).difference(index)):
        _reject(label + " contain rows outside qualified key scope")


def assert_formal_market_admission(
    provider, *, membership, calendar, bars, benchmark, research_start,
    research_end, status=None, listings=None, require_execution=False,
    required_symbols=None,
):
    """Replay evidence and bind the exact requested financial inputs.

    ``required_symbols`` narrows *realised-price evaluation* to the recorded
    prediction securities; PIT membership and the declared research interval
    are still checked independently. Dataset/model consumers use all members.
    Returned receipts describe this observation, never authorize later inputs.
    Even callers supplying already pinned bar snapshots must invoke this again.
    """
    method = getattr(provider, "get_research_qualification", None)
    evidence = method() if callable(method) else None
    if evidence is None:
        _reject("research requires replayable v2 qualification; provider has no qualification package; schema-v1 bundles are diagnostic only")
    if type(evidence) is not MarketQualificationEvidence:
        _reject("typed replayable qualification evidence required; approval flags are not evidence")
    if (not isinstance(evidence.manifest_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence.manifest_sha256)
            or not isinstance(evidence.source_version, str) or not evidence.source_version.strip()):
        _reject("pinned qualification source identity is incomplete")
    if type(require_execution) is not bool:
        _reject("explicit execution purpose required")
    try:
        path = safe_path(evidence.manifest)
        _, digest = read_snapshot(path, path.parent)
        if digest != evidence.manifest_sha256:
            _reject("qualification manifest identity mismatch")
        report, inputs, keys, qualified_calendar, replay_digest = load_qualification(
            path, stocks=evidence.stocks, benchmark=evidence.benchmark,
        )
        if digest != replay_digest:
            _reject("qualification changed during admission")
    except (FactorLabError, ValueError, TypeError, KeyError, OSError) as exc:
        raise ProviderError(PREFIX + "qualification replay failed: " + str(exc)) from exc
    if report.source_version != evidence.source_version or inputs.source_version != evidence.source_version:
        _reject("qualification source identity mismatch")
    dates = pd.DatetimeIndex(calendar)
    start, end = pd.Timestamp(research_start), pd.Timestamp(research_end)
    if (dates.empty or dates.tz is not None or dates.has_duplicates
            or not dates.is_monotonic_increasing or not dates.equals(dates.normalize())
            or pd.isna(start) or pd.isna(end) or start.tzinfo is not None or end.tzinfo is not None
            or start != start.normalize() or end != end.normalize() or start > end
            or start not in dates or end not in dates or len(dates.difference(qualified_calendar))):
        _reject("research calendar exceeds qualified scope or is invalid")
    if (not inputs.research_start or not inputs.research_end
            or start < pd.Timestamp(inputs.research_start) or end > pd.Timestamp(inputs.research_end)):
        _reject("declared research interval exceeds financial qualification")
    research_dates = dates[(dates >= start) & (dates <= end)]
    if (not isinstance(membership, pd.DataFrame)
            or not {"date", "symbol", "is_member"}.issubset(membership)
            or membership.empty or membership.duplicated(["date", "symbol"]).any()
            or not pd.DatetimeIndex(sorted(membership.date.unique())).equals(research_dates)):
        _reject("actual PIT membership does not cover the declared research interval")
    expected_members = inputs.membership.loc[
        inputs.membership.date.isin(research_dates), ["date", "symbol", "is_member"]
    ].sort_values(["date", "symbol"]).reset_index(drop=True)
    actual_members = membership[["date", "symbol", "is_member"]].sort_values(
        ["date", "symbol"]
    ).reset_index(drop=True)
    if not actual_members.equals(expected_members):
        _reject("actual PIT members differ from qualified effective membership")
    symbols = sorted(set(membership.symbol)) if required_symbols is None else sorted(set(required_symbols))
    if not symbols or not set(symbols).issubset(set(membership.symbol)):
        _reject("requested securities are outside qualified PIT membership")
    scoped_keys = keys.loc[keys.date.isin(dates) & keys.symbol.isin(symbols), ["date", "symbol"]]
    index = pd.MultiIndex.from_frame(scoped_keys)
    needed_pit = membership.loc[membership.symbol.isin(symbols), ["date", "symbol"]]
    if index.empty or len(pd.MultiIndex.from_frame(needed_pit).difference(index)):
        _reject("qualification key coverage is incomplete")
    _bind_scoped_rows(bars, evidence.stocks, index, dates, symbols, "cached prices")
    benchmark_index = pd.MultiIndex.from_product(
        [dates, evidence.benchmark.symbol.unique()], names=["date", "symbol"]
    )
    _same_rows(benchmark, evidence.benchmark, ["date", "symbol"], benchmark_index, "benchmark")
    if (status is None) != (listings is None):
        _reject("security status and listing inputs must be bound together")
    if status is not None:
        status_keys = inputs.status.loc[
            inputs.status.date.isin(dates) & inputs.status.symbol.isin(symbols), ["date", "symbol"]
        ]
        _bind_scoped_rows(status, inputs.status, pd.MultiIndex.from_frame(status_keys),
                          dates, symbols, "actual security status")
        listing_index = pd.MultiIndex.from_arrays([symbols], names=["symbol"])
        _same_rows(listings, inputs.listings, ["symbol"], listing_index, "actual listing dates")
    required_stages = {"RAW", "ADJUSTMENT", "STATUS", "PIT", "SOURCE_BINDING", "FORMAL"}
    if (report.synthetic_only is not False or report.formal_admission is not True
            or report.issues or set(report.stages) != required_stages
            or any(report.stages[name] != "PASS" for name in required_stages)):
        _reject("qualification is SOFTWARE_ONLY, incomplete or lacks formal qualification")
    if require_execution and report.execution_admission is not True:
        _reject("corporate-action execution ledger/protocol remains unqualified")
    # Reobserve the pinned manifest after all comparisons; the package loader
    # already checks every source/table artifact and replays financial semantics.
    if read_snapshot(path, path.parent)[1] != digest:
        _reject("qualification changed during admission")
    return {
        "schema": "FACTORLAB_FORMAL_MARKET_ADMISSION_V1",
        "qualification_manifest_sha256": digest,
        "source_version": report.source_version,
        "research_start": start.date().isoformat(), "research_end": end.date().isoformat(),
        "calendar_sha256": hashlib.sha256(dates.values.tobytes()).hexdigest(),
        "stocks_request_sha256": table_sha256(bars.loc[bars.date.isin(dates) & bars.symbol.isin(symbols)]),
        "benchmark_request_sha256": table_sha256(benchmark.loc[benchmark.date.isin(dates)]),
        "membership_sha256": table_sha256(actual_members),
        "security_status_sha256": table_sha256(status) if status is not None else None,
        "listings_sha256": table_sha256(listings) if listings is not None else None,
        "required_symbols": symbols, "qualification_report": asdict(report),
        "formal_admission": True, "execution_admission": report.execution_admission,
        "no_live_money": True,
    }
