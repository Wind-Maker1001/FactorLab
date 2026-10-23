"""Source-scoped unit conversion backed by explicit, inspectable evidence."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Tuple

import numpy as np
import pandas as pd

from factorlab.data.corporate_actions.validation import decimal
from factorlab.data.evidence import SourceArtifact, date, fail, require_text, timestamp


@dataclass(frozen=True)
class UnitContract:
    symbol: str
    start: str
    end: str
    source_version: str
    price_currency: str
    price_multiplier: Decimal
    volume_unit: str
    amount_currency: str
    amount_multiplier: Decimal
    available_at: str
    artifact: SourceArtifact
    synthetic_only: bool = False

    def validate(self) -> None:
        self.artifact.read()
        require_text(self.source_version, "unit source version")
        if date(self.start) > date(self.end):
            fail("invalid unit scope")
        if self.price_currency != "CNY" or self.amount_currency != "CNY":
            fail("independently specified CNY units required")
        if self.volume_unit not in {"SHARES", "LOTS_100_SHARES"}:
            fail("volume physical unit unverified")
        decimal(self.price_multiplier, "proved price multiplier", positive=True)
        decimal(self.amount_multiplier, "proved amount multiplier", positive=True)
        timestamp(self.available_at)
        if not isinstance(self.synthetic_only, bool):
            fail("explicit unit synthetic boundary required")


def convert_units(raw: pd.DataFrame, contracts: Tuple[UnitContract, ...],
                  *, source_version: str, signal_as_of) -> pd.DataFrame:
    """Only unit conversion; original staging remains unchanged."""
    fields = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]
    if not set(fields).issubset(raw) or raw.duplicated(["date", "symbol"]).any():
        fail("explicit unique RAW fields required")
    for c in contracts:
        c.validate()
    result = raw[fields].copy()
    for idx, row in result.iterrows():
        matches = [c for c in contracts if c.symbol == row.symbol and date(c.start) <= row.date <= date(c.end)
                   and c.source_version == source_version]
        if len(matches) != 1:
            fail("unit scope/version missing or ambiguous")
        c = matches[0]
        if row.date not in signal_as_of or timestamp(c.available_at) > timestamp(signal_as_of[row.date]):
            fail("unit evidence unavailable at signal")
        for name in fields[2:]:
            value = row[name]
            if isinstance(value, (bool, np.bool_)) or (pd.notna(value) and (not isinstance(value, (int, float, np.number)) or not np.isfinite(value))):
                fail("raw unit field is not finite numeric")
        for name in ("open", "high", "low", "close"):
            result.at[idx, name] = row[name] * float(c.price_multiplier)
        result.at[idx, "volume"] = row.volume * (100 if c.volume_unit == "LOTS_100_SHARES" else 1)
        result.at[idx, "amount"] = row.amount * float(c.amount_multiplier)
    volume = result.volume.dropna().to_numpy(dtype=float)
    if (volume < 0).any() or not np.equal(volume, np.floor(volume)).all():
        fail("converted share volume must be nonnegative integral shares")
    if np.isinf(result[fields[2:]].to_numpy(dtype=float)).any():
        fail("unit conversion overflow")
    return result
