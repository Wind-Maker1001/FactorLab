"""Source-scoped unit conversion backed by explicit, inspectable evidence."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Tuple

import numpy as np
import pandas as pd

from factorlab.data.corporate_actions.validation import decimal
from factorlab.data.evidence import SourceArtifact, date, fail, require_text, timestamp


def bigquant_field_semantics(kind):
    """Current vendor claims do not establish units of a historical archive.

    Evidence: saved 2026-11-09 responses in validation/p4-d02, S08/S11.
    Neither physical types nor sample magnitudes establish economic units.
    """
    if kind not in {'bars', 'benchmark'}:
        return {}
    tables = ({'open': ('cn_stock_index_bar1d', 'open')} if kind == 'benchmark' else {
        **{prefix + name: (table, name)
           for prefix, table in [('raw_', 'cn_stock_real_bar1d'), ('adj_', 'cn_stock_bar1d')]
           for name in ('open', 'high', 'low', 'close')},
        **{name: ('cn_stock_real_bar1d', native)
           for name, native in [('volume', 'volume'), ('amount', 'amount'), ('turnover', 'turn')]},
    })
    claims = {'volume': 'shares', 'amount': 'yuan; currency unspecified', 'turn': 'fraction'}
    return {name: dict(native_field=table + '.' + native,
        vendor_documented_unit=claims.get(native, 'UNKNOWN'),
        normalized_unit='UNKNOWN_VENDOR_CONTRACT', multiplier=None,
        evidence_level='VENDOR_DOCUMENTED_CURRENT' if native in claims else 'UNKNOWN',
        historical_applicability='UNKNOWN',
        evidence=('docs/validation/p4-d02/sources/'
                  + ('S11' if table == 'cn_stock_bar1d' else 'S08') + '/response.bin'
                  if kind == 'bars' else None),
        vendor_price_semantics=('UNADJUSTED' if name.startswith('raw_') else
            'ADJUSTED_BY_CUMULATIVE_FACTOR; HFQ_UNVERIFIED' if name.startswith('adj_') else None),
        transformation='rename_only' if name != native else 'identity',
        minimum_evidence='Version-bound native field dictionary with economic unit, scale and historical applicability')
        for name, (table, native) in tables.items()}


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
