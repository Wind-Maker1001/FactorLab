"""Bounded per-batch conversion, with exact per-day audit reconciliation."""

from collections import Counter
import hashlib
import json
import math

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv

from .contract import (EXPECTED_LABELS, FLAGS, MAPPING, NUMERIC_COLUMNS, RAW_COLUMNS,
                       SCHEMA, IntradayDataError)
from .source import open_source


def logical_update(digest, table):
    """Batch-boundary-independent fingerprint; file SHA remains authoritative."""
    frame = table.to_pandas()
    values = pd.util.hash_pandas_object(frame, index=False, categorize=False).to_numpy(dtype="<u8")
    digest.update(values.tobytes())


def portable(value):
    if isinstance(value, dict):
        return {k: portable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [portable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (float, np.floating)) and not math.isfinite(value):
        return None if math.isnan(value) else str(value)
    if isinstance(value, np.generic):
        return value.item()
    return value


class DayValidator:
    def __init__(self, item):
        self.item = item
        self.rows = 0
        self.seen = set()
        self.symbol_rows = Counter()
        self.labels = Counter()
        self.counts = Counter()
        self.missing = Counter()
        self.flag_counts = Counter()
        self.samples = []
        self.sample_rows = set(np.random.default_rng(int(item["content_sha256"][:16], 16)).choice(
            int(item["rows"]), size=min(5, int(item["rows"])), replace=False).tolist())
        self.max_batch_rows = 0

    def convert(self, batch):
        if tuple(batch.schema.names) != RAW_COLUMNS:
            raise IntradayDataError("RAW_11_COLUMN_SCHEMA_MISMATCH")
        raw = batch.to_pandas()
        frame = raw.rename(columns=MAPPING).copy()
        n = len(frame)
        self.max_batch_rows = max(n, self.max_batch_rows)
        vendor = frame.symbol.copy()
        symbols = vendor.str.strip().str.upper()
        if not symbols.str.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)").fillna(False).all():
            raise IntradayDataError("INVALID_VENDOR_SYMBOL_NO_SUFFIX_INFERENCE")
        if not frame.trade_date.str.fullmatch(r"\d{8}").fillna(False).all():
            raise IntradayDataError("INVALID_TRADE_DATE")
        if not frame.timestamp_local.str.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}").fillna(False).all():
            raise IntradayDataError("INVALID_TIMESTAMP_FORMAT")
        dates = pd.to_datetime(frame.trade_date, format="%Y%m%d", errors="raise")
        times = pd.to_datetime(frame.timestamp_local, format="%Y-%m-%d %H:%M:%S", errors="raise")
        expected = pd.Timestamp(self.item["date"])
        if not dates.eq(expected).all() or not times.dt.normalize().eq(expected).all():
            raise IntradayDataError("FILENAME_DATE_TIMESTAMP_MISMATCH")
        keys = list(zip(symbols, times.astype("int64")))
        if len(set(keys)) != n or any(k in self.seen for k in keys):
            raise IntradayDataError("DUPLICATE_PRIMARY_KEY")
        self.seen.update(keys)
        self.symbol_rows.update(symbols.tolist())
        labels = frame.timestamp_local.str.slice(11)
        self.labels.update(labels.tolist())
        values = frame[list(NUMERIC_COLUMNS)]
        prices = values.iloc[:, :4]
        masks = {
            "missing_numeric": values.isna().any(axis=1),
            "nonfinite_numeric": ~np.isfinite(values.to_numpy()).all(axis=1),
            "nonpositive_price": prices.le(0).any(axis=1),
            "invalid_ohlc": ((frame.high < prices[["open", "close", "low"]].max(axis=1)) |
                             (frame.low > prices[["open", "close", "high"]].min(axis=1))),
            "negative_volume": frame.volume_vendor.lt(0),
            "negative_amount": frame.amount_vendor.lt(0),
            "unrecognized_time_label": ~labels.isin(EXPECTED_LABELS),
            "outside_regular_session": ~((labels.between("09:30:00", "11:30:00")) |
                                          labels.between("13:00:00", "15:00:00")),
        }
        flags = np.zeros(n, dtype=np.uint16)
        for name, mask in masks.items():
            flags[np.asarray(mask, dtype=bool)] |= FLAGS[name]
            self.flag_counts[name] += int(mask.sum())
        self.counts.update({
            "null_cells": int(frame.isna().sum().sum()),
            "nonfinite_numeric_cells": int((~np.isfinite(values.to_numpy())).sum()),
            "nonpositive_price_rows": int(masks["nonpositive_price"].sum()),
            "ohlc_logic_rows": int(masks["invalid_ohlc"].sum()),
            "negative_volume_rows": int(masks["negative_volume"].sum()),
            "negative_amount_rows": int(masks["negative_amount"].sum()),
            "zero_volume_rows": int(frame.volume_vendor.eq(0).sum()),
            "zero_amount_rows": int(frame.amount_vendor.eq(0).sum()),
            "outside_nine_label_rows": int(masks["unrecognized_time_label"].sum()),
            "nonpositive_factor_rows": int(frame.adjustment_factor_vendor.le(0).sum()),
        })
        self.missing.update({c: int(frame[c].isna().sum()) for c in frame})
        frame["symbol"], frame["vendor_symbol"] = symbols, vendor
        frame["trade_date"], frame["timestamp_local"] = dates, times
        frame["source_id"] = self.item["content_sha256"]
        frame["source_row"] = np.arange(self.rows + 1, self.rows + n + 1, dtype=np.int32)
        frame["quality_flags"] = flags
        table = pa.Table.from_pandas(frame, schema=SCHEMA, preserve_index=False).replace_schema_metadata(SCHEMA.metadata)
        for index in sorted(self.sample_rows):
            if self.rows <= index < self.rows + n:
                offset = index - self.rows
                self.samples.append({"source_row": index + 1,
                                     "raw": portable(raw.iloc[offset].to_dict()),
                                     "normalized": portable(table.slice(offset, 1).to_pylist()[0])})
        self.rows += n
        return table

    def finish(self, digest):
        if digest != self.item["content_sha256"] or self.rows != int(self.item["rows"]):
            raise IntradayDataError("SOURCE_HASH_OR_ROW_COUNT_MISMATCH: " + self.item["date"])
        for key, value in self.counts.items():
            if value != int(self.item[key]):
                raise IntradayDataError("PRIOR_AUDIT_CHECK_MISMATCH: " + key)
        if (len(self.symbol_rows) != int(self.item["securities"]) or
                dict(Counter(self.symbol_rows.values())) != {int(k): v for k, v in json.loads(self.item["bars_histogram"]).items()} or
                set(self.labels) != set(self.item["time_labels"].split("|"))):
            raise IntradayDataError("PRIOR_AUDIT_SYMBOL_OR_TIME_COVERAGE_MISMATCH")
        return dict(date=self.item["date"], container=self.item["container"], locator=self.item["locator"],
                    source_id=digest, rows=self.rows, securities=len(self.symbol_rows),
                    checks=dict(self.counts), missing=dict(self.missing), flags=dict(self.flag_counts),
                    time_labels=dict(sorted(self.labels.items())), samples=self.samples,
                    max_batch_rows=self.max_batch_rows, primary_key_unique=True)


def convert_source(source, item, block_size, consumer):
    validator = DayValidator(item)
    types = {c: pa.string() if i < 4 else pa.float64() for i, c in enumerate(RAW_COLUMNS)}
    with open_source(source, item) as handle:
        reader = pacsv.open_csv(handle, read_options=pacsv.ReadOptions(block_size=block_size, use_threads=False),
                                convert_options=pacsv.ConvertOptions(column_types=types, strings_can_be_null=True))
        for batch in reader:
            consumer(validator.convert(batch))
        # Consume any buffered tail, including bytes after the final parsed row.
        if handle.read(1):
            raise IntradayDataError("UNCONSUMED_SOURCE_BYTES")
        result = validator.finish(handle.digest.hexdigest())
    return result
