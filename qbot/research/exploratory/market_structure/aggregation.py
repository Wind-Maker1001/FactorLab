"""Closed-window OHLCV and named cash classification; no imputation."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .contracts import ContractError, FLOW_DEFINITION, real_number
from .store import validate_calendar


@dataclass
class AggregationResult:
    frame: pd.DataFrame
    records: tuple


def aggregate_intraday(
    store,
    sessions,
    request,
    *,
    include_minutes=True,
    include_flow=True,
    include_prices=True,
    include_amounts=True,
):
    sessions = validate_calendar(sessions)
    anchors = [
        (a, b)
        for s in sessions
        if s.session_id == request.session_id
        for a, b in s.segments
        if a <= request.observation_start < request.observation_end <= b
    ]
    if len(anchors) != 1 or (request.observation_start - anchors[0][0]) % pd.Timedelta(
        minutes=request.frequency
    ):
        raise ContractError("window not aligned/contained in explicit session")
    if include_flow and (not include_minutes or not include_amounts):
        raise ContractError("cash classification requires minute observations")
    if request.observation_end > request.as_of or not include_minutes:
        records = ()
        missing_reason = (
            "WINDOW_NOT_CLOSED"
            if request.observation_end > request.as_of
            else "INPUT_NOT_REQUESTED"
        )
    else:
        records = store.snapshot(
            "minute", request.observation_end, request.as_of, request.observation_start
        )
        missing_reason = "MISSING_OR_NOT_YET_AVAILABLE_MINUTES"
    records = tuple(r for r in records if r.payload["security_id"] in request.universe)
    versions = {r.definition_version for r in records} if include_flow else set()
    definition_ids = (
        {
            r.payload["flow_definition"]
            for r in records
            if isinstance(r.payload.get("flow_definition"), str)
        }
        if include_flow
        else set()
    )
    definitions = (
        tuple(
            r
            for r in store.snapshot(
                "source_definition", request.observation_end, request.as_of
            )
            if r.definition_version in versions
            and r.payload.get("definition_id") in definition_ids
        )
        if include_flow
        else ()
    )
    definition_groups = {}
    for record in definitions:
        if record.payload.get("definition_id") == FLOW_DEFINITION:
            definition_groups.setdefault(record.definition_version, []).append(
                record.payload
            )
    approved = {
        version
        for version, group in definition_groups.items()
        if all(
            p.get("unit") == "CNY"
            and p.get("currency") == "CNY"
            and p.get("vendor") == "SYNTHETIC_TEST_ONLY"
            for p in group
        )
    }
    by_security = {s: [] for s in request.universe}
    for record in records:
        security = record.payload["security_id"]
        if security in by_security:
            by_security[security].append(record)
    expected = pd.date_range(
        request.observation_start, periods=request.frequency, freq="min", tz="UTC"
    )
    rows, consumed = [], list(definitions)
    for security in request.universe:
        selected = sorted(by_security[security], key=lambda r: r.valid_from)
        row = dict(
            security_id=security,
            open=np.nan,
            high=np.nan,
            low=np.nan,
            close=np.nan,
            volume=np.nan,
            amount=np.nan,
            return_value=np.nan,
            limit_state="UNKNOWN",
            buy=np.nan,
            sell=np.nan,
            unclassified=np.nan,
            unclassified_amount_ratio=np.nan,
            reason=missing_reason,
            amount_reason=missing_reason,
            flow_reason=missing_reason,
            input_minute_coverage=len(selected) / request.frequency,
        )
        consumed.extend(selected)
        starts = pd.DatetimeIndex([r.valid_from for r in selected])
        if starts.has_duplicates:
            raise ContractError("duplicate minute observation")
        if (
            len(selected) != request.frequency
            or not starts.equals(expected)
            or any(
                r.observation_end != r.valid_from + pd.Timedelta(minutes=1)
                for r in selected
            )
        ):
            rows.append(row)
            continue
        payloads = [r.payload for r in selected]
        if include_amounts:
            try:
                amounts = [real_number(p["amount"], "amount", True) for p in payloads]
                if any(p.get("unit") != "CNY" for p in payloads):
                    raise ContractError("amount unit mismatch")
                row.update(
                    amount=real_number(sum(amounts), "window amount", True),
                    amount_reason="OK",
                )
            except (KeyError, ContractError):
                row["amount_reason"] = "INVALID_AMOUNT_INPUT"
        else:
            row["amount_reason"] = "INPUT_NOT_REQUESTED"
        if include_prices:
            try:
                numeric = {
                    k: [real_number(p[k], k, True) for p in payloads]
                    for k in ("open", "high", "low", "close", "volume")
                }
                if any(
                    p.get("unit") != "CNY" or p.get("price_basis") != "SYNTHETIC_RAW"
                    for p in payloads
                ):
                    raise ContractError("bar unit/price basis mismatch")
                for i in range(len(selected)):
                    if (
                        numeric["low"][i] <= 0
                        or numeric["low"][i]
                        > min(numeric["open"][i], numeric["close"][i])
                        or numeric["high"][i]
                        < max(numeric["open"][i], numeric["close"][i])
                    ):
                        raise ContractError("invalid OHLC relationship")
                row.update(
                    open=numeric["open"][0],
                    high=max(numeric["high"]),
                    low=min(numeric["low"]),
                    close=numeric["close"][-1],
                    volume=real_number(sum(numeric["volume"]), "window volume", True),
                    reason="OK",
                )
                row["return_value"] = row["close"] / row["open"] - 1
            except (KeyError, ContractError):
                row["reason"] = "INVALID_BAR_INPUT"
        else:
            row["reason"] = "INPUT_NOT_REQUESTED"
        last = payloads[-1]
        if last.get("limit_definition") == "SYNTHETIC_LIMIT_STATE_V1" and last.get(
            "limit_state"
        ) in ("UP", "DOWN", "NONE"):
            row["limit_state"] = last["limit_state"]
        if not include_flow:
            row["flow_reason"] = "INPUT_NOT_REQUESTED"
            rows.append(row)
            continue
        if row["amount_reason"] != "OK":
            row["flow_reason"] = row["amount_reason"]
            rows.append(row)
            continue
        try:
            versions = {
                (r.definition_version, p.get("flow_definition"), p.get("flow_unit"))
                for r, p in zip(selected, payloads)
            }
        except TypeError:
            row["flow_reason"] = "BLOCKED_SOURCE_DEFINITION"
            rows.append(row)
            continue
        if len(versions) != 1:
            row["flow_reason"] = "MIXED_SOURCE_DEFINITION"
        elif (
            selected[0].definition_version not in approved
            or payloads[0].get("flow_definition") != FLOW_DEFINITION
            or payloads[0].get("flow_unit") != "CNY"
        ):
            row["flow_reason"] = "BLOCKED_SOURCE_DEFINITION"
        else:
            try:
                cash = {
                    k: [real_number(p[k], k, True) for p in payloads]
                    for k in ("buy", "sell", "unclassified")
                }
                if any(
                    not np.isclose(
                        cash["buy"][i] + cash["sell"][i] + cash["unclassified"][i],
                        amounts[i],
                        rtol=1e-12,
                        atol=1e-8,
                    )
                    for i in range(len(selected))
                ):
                    raise ContractError("cash partition does not conserve amount")
                row.update({k: sum(v) for k, v in cash.items()})
                if row["amount"] == 0:
                    row["flow_reason"] = "ZERO_DENOMINATOR"
                else:
                    row["unclassified_amount_ratio"] = (
                        row["unclassified"] / row["amount"]
                    )
                    row["flow_reason"] = (
                        "OK"
                        if row["unclassified"] == 0
                        else "INCOMPLETE_CLASSIFICATION"
                    )
            except (KeyError, ContractError):
                row["flow_reason"] = "INVALID_CASH_PARTITION"
        rows.append(row)
    return AggregationResult(pd.DataFrame(rows), tuple(consumed))
