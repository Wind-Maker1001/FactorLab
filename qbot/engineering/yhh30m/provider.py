"""FactorLab engineering adapter. It cannot satisfy MarketDataProvider."""

from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from qbot.research.forward.integrity import assert_no_link_components
from .contract import SCHEMA, IntradayDataError
from .validation import artifact, checked_partition, load_catalog


class IntradayParquetProvider:
    frequency = "VENDOR_30M_LABELS"
    financial_research_eligible = False

    def __init__(self, root, *, purpose):
        if purpose not in {"engineering", "exploratory"}:
            raise IntradayDataError("MINUTE_OBSERVATIONS_NOT_FORMALLY_RESEARCH_ELIGIBLE")
        self.root = assert_no_link_components(Path(root))
        self.catalog = load_catalog(self.root)

    def _path(self, relative):
        return artifact(self.root, relative)

    def verify(self):
        """Full output replay, with exactly the same catalog binding as reads."""
        from .storage import verify_partition
        for part in self.catalog["partitions"]:
            receipt, parquet = checked_partition(self.root, self.catalog, part)
            parquet.close()
            verify_partition(self._path(part["receipt"]).parent, receipt)
        return {"status": "VERIFIED", "rows": self.catalog["rows"],
                "partitions": len(self.catalog["partitions"]), "formal_admission": False}

    def iter_batches(self, *, start=None, end=None, symbols=None, columns=None, batch_size=65536):
        if batch_size < 1:
            raise IntradayDataError("POSITIVE_BATCH_SIZE_REQUIRED")
        symbols = tuple(symbols) if symbols is not None else None
        columns = tuple(columns) if columns is not None else None
        for partition in self.catalog["partitions"]:
            if (start and max(partition["dates"]) < start) or (end and min(partition["dates"]) > end):
                continue
            _, parquet = checked_partition(self.root, self.catalog, partition)
            for batch in parquet.iter_batches(batch_size=batch_size):
                table = pa.Table.from_batches([batch], schema=SCHEMA)
                mask = pa.array([True] * len(table))
                if start:
                    mask = pc.and_(mask, pc.greater_equal(table["trade_date"], pa.scalar(date.fromisoformat(start))))
                if end:
                    mask = pc.and_(mask, pc.less_equal(table["trade_date"], pa.scalar(date.fromisoformat(end))))
                if symbols is not None:
                    mask = pc.and_(mask, pc.is_in(table["symbol"], value_set=pa.array(list(symbols), type=pa.string())))
                table = table.filter(mask)
                if columns is not None:
                    table = table.select(columns)
                yield from table.to_batches(max_chunksize=batch_size)

    def daily_bars(self, *args, **kwargs):
        raise IntradayDataError("DAILY_AGGREGATION_DISABLED_UNKNOWN_INTERVAL_AND_UNIT_SEMANTICS")
