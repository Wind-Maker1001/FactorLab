"""Explicit 18-column assembly after identity/unit/adjustment checks.

The result is a representation, not a production qualification certificate.
"""

from dataclasses import dataclass
from typing import Mapping, Tuple

import numpy as np
import pandas as pd

from factorlab.data.adjustment.contracts import AdjustmentResult
from factorlab.data.evidence import fail
from factorlab.data.providers.local import _strict_bars, _validate_pair
from factorlab.data.schema import CANONICAL_BAR_COLUMNS
from factorlab.data.units import UnitContract, convert_units
from factorlab.universe.identity import IdentitySnapshot


@dataclass(frozen=True)
class MarketTables:
    stocks: pd.DataFrame
    benchmark: pd.DataFrame
    synthetic_only: bool


def assemble_market_data(raw: pd.DataFrame, adjusted: Mapping[str, AdjustmentResult],
                         turnover: pd.DataFrame, identity: IdentitySnapshot,
                         units: Tuple[UnitContract, ...], *, source_version: str,
                         signal_as_of: Mapping, benchmark: pd.DataFrame,
                         benchmark_symbol: str = "000905.SH") -> MarketTables:
    converted = convert_units(raw, units, source_version=source_version, signal_as_of=signal_as_of)
    resolved = identity.resolve(converted[["date", "symbol"]], signal_as_of)
    if set(adjusted) != set(resolved.security_id):
        fail("adjusted identity set differs from resolved raw identities")
    if set(turnover.columns) != {"date", "symbol", "turnover"} or turnover.duplicated(["date", "symbol"]).any():
        fail("explicit unique historical turnover table required")
    frame = converted.merge(resolved, on=["date", "symbol"], validate="one_to_one")
    blocks = []
    for security_id, result in sorted(adjusted.items()):
        keys = frame.loc[frame.security_id.eq(security_id), ["date", "symbol"]].reset_index(drop=True)
        if not result.bars[["date", "symbol"]].reset_index(drop=True).equals(keys):
            fail("adjusted keys must cover exactly the resolved raw rows")
        if result.evidence["anchor"]["security_id"] != security_id:
            fail("adjustment anchor identity differs")
        blocks.append(result.bars)
    frame = frame.merge(pd.concat(blocks, ignore_index=True), on=["date", "symbol"], validate="one_to_one")
    required = pd.MultiIndex.from_frame(frame[["date", "symbol"]])
    supplied = pd.MultiIndex.from_frame(turnover[["date", "symbol"]])
    if len(required.difference(supplied)) or len(supplied.difference(required)):
        fail("turnover keys must match raw keys exactly")
    frame = frame.merge(turnover, on=["date", "symbol"], validate="one_to_one")
    if frame.loc[frame.close.notna(), "turnover"].isna().any():
        fail("trading rows require known turnover; missing is not zero")
    for field in ("open", "high", "low", "close"):
        frame["raw_" + field] = frame[field]
    frame["adj_factor"] = frame.adj_close / frame.raw_close
    # Missing adjusted fields were checked before calling canonicalization.
    stocks = _strict_bars(frame[list(CANONICAL_BAR_COLUMNS)], is_index=False, benchmark_symbol=benchmark_symbol)
    index = _strict_bars(benchmark, is_index=True, benchmark_symbol=benchmark_symbol)
    _validate_pair(stocks, index)
    return MarketTables(stocks, index, identity.synthetic_only or any(c.synthetic_only for c in units)
                        or any(r.synthetic_only for r in adjusted.values()))
