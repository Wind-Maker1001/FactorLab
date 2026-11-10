"""Local synthetic inputs for exercising both existing research engines."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.data.research_input import prepare
from factorlab.dataset.identity import file_hash

ROOT = Path(__file__).resolve().parents[2]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return path


def research_inputs(root):
    source, raw, research = (root / name for name in ("source", "raw", "research"))
    source.mkdir(parents=True)
    raw.mkdir()
    dates = pd.bdate_range("2017-09-01", "2018-04-30")
    close = 12 + np.sin(np.arange(len(dates)) / 5) - np.arange(len(dates)) / 150
    frame = pd.DataFrame(dict(date=dates, symbol="601857.SH", raw_open=close + .01,
        raw_close=close, raw_high=close + .1, raw_low=close - .1,
        volume=1000000, trade_status=1, is_st=0, adj_close=close))
    frame = pd.concat([frame, frame.assign(symbol='601398.SH')], ignore_index=True)
    frame.to_parquet(source / "bars.parquet", index=False)
    write_json(source / "manifest.json", dict(schema="P5_R03_CSMAR_PRIVATE_V1",
        bars_sha256=file_hash(source / "bars.parquet"), references=[]))
    prepare_config = write_json(root / "prepare.json", dict(
        source_manifest_sha256=file_hash(source / "manifest.json"),
        symbols=["601857.SH", "601398.SH"], start="2017-09-01", end="2018-04-30"))
    prepare(prepare_config, source, raw, research)
    strategy = root / "strategy.py"
    strategy.write_text((ROOT / "examples/custom_strategy/ma_strategy.py").read_text(encoding="utf-8"), encoding="utf-8")
    return dict(source=source, research=research, strategy=strategy,
        source_hash=file_hash(source / "manifest.json"), manifest_hash=file_hash(research / "manifest.json"))
