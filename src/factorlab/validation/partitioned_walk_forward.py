"""Verified fold-local DataFrames for the unchanged research training pipeline."""

from contextlib import contextmanager
import threading
import time

import pandas as pd

from factorlab.dataset.partitioned import PartitionedDataset, tree_rss
from factorlab.errors import ConfigurationError, SchemaError


@contextmanager
def measure_rss():
    """Sample process-tree RSS every 10ms, including both scope boundaries."""
    sample = {"peak_rss_bytes": tree_rss()}
    stop = threading.Event()

    def monitor():
        while not stop.wait(0.01):
            sample["peak_rss_bytes"] = max(sample["peak_rss_bytes"], tree_rss())

    thread = threading.Thread(target=monitor, daemon=True)
    started = time.perf_counter()
    thread.start()
    try:
        yield sample
    finally:
        stop.set()
        thread.join()
        sample["peak_rss_bytes"] = max(sample["peak_rss_bytes"], tree_rss())
        sample["elapsed_seconds"] = time.perf_counter() - started


class PartitionedWalkForwardSource:
    def __init__(self, dataset, runner, entry_date_col, exit_date_col):
        if not isinstance(dataset, PartitionedDataset) or dataset.manifest["kind"] != "dataset":
            raise ConfigurationError("Partitioned research dataset required")
        if runner.date_col != "date" or runner.symbol_col != "symbol":
            raise ConfigurationError("Partitioned datasets use date/symbol primary keys")
        schema_columns = [name for name, _ in dataset.manifest["schema"]]
        runner._validate_required_columns(schema_columns)
        runner._validate_audit_columns(schema_columns, entry_date_col, exit_date_col)
        self.dataset, self.runner = dataset, runner
        self.measurements = []
        columns = list(dict.fromkeys((runner.date_col, runner.symbol_col, runner.target_col,
                                      entry_date_col, exit_date_col)))
        if runner.regime_col is not None:
            columns.append(runner.regime_col)
        before = dict(dataset.read_stats)
        with measure_rss() as measurement:
            self.metadata = dataset.read_metadata(columns)
        self.measurements.append(dict(phase="planning", fold=None, **measurement,
            **{k: v - before[k] for k, v in dataset.read_stats.items()}))

    def fold_frames(self, metadata, folds):
        keys = [self.runner.date_col, self.runner.symbol_col]
        for fold in folds:
            before = dict(self.dataset.read_stats)
            with measure_rss() as measurement:
                dates = fold.train_dates.union(fold.validation_dates).union(fold.test_dates)
                window = self.runner._validate_data(self.dataset.read_dates(dates))
                index = pd.MultiIndex.from_frame(window[keys])
                parts = []
                for positions in fold.as_indices():
                    wanted = pd.MultiIndex.from_frame(metadata.iloc[positions][keys])
                    local = index.get_indexer(wanted)
                    if (local < 0).any():
                        raise SchemaError("Fold keys missing from partition window")
                    part = window.iloc[local].reset_index(drop=True)
                    expected = metadata.iloc[positions].reset_index(drop=True)
                    if not part.loc[:, expected.columns].equals(expected):
                        raise SchemaError("Partition metadata changed after planning")
                    parts.append(part)
                del window, index
                yield fold, *parts
            self.measurements.append(dict(phase="fold", fold=fold.fold, **measurement,
                train_rows=len(parts[0]), train_labeled_rows=int(parts[0][self.runner.target_col].notna().sum()),
                validation_rows=len(parts[1]), test_rows=len(parts[2]),
                **{k: v - before[k] for k, v in self.dataset.read_stats.items()}))
            del parts, part
