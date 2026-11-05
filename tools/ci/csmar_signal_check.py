"""Run inside an isolated PR10 + CSMAR integration checkout. Signals only."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
import pandas as pd

from factorlab.data.evidence import json_bytes, sha256, table_sha256
from factorlab.strategies.dual_ma import DualMAConfig, moving_average_signals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.candidate / "manifest.json").read_bytes())
    for name, digest in manifest["files"].items():
        if sha256((args.candidate / name).read_bytes()) != digest:
            raise ValueError("candidate artifact changed: " + name)
    config = DualMAConfig(symbol=manifest["coverage"]["symbol"],
                          start=manifest["coverage"]["start"], end=manifest["coverage"]["end"])
    bars = pd.read_parquet(args.candidate / "market/bars.parquet")
    dates = pd.DatetimeIndex(pd.read_parquet(args.candidate / "calendar/observed_sessions.parquet").date)
    signals = moving_average_signals(bars, dates, config)
    # Independent arithmetic for every selected output, using only each prefix.
    for row in signals.itertuples(index=False):
        prefix = bars.loc[bars.date <= row.date, "adj_close"].to_numpy()
        np.testing.assert_allclose(row.ma_short, sum(prefix[-5:]) / 5, rtol=1e-12)
        np.testing.assert_allclose(row.ma_long, sum(prefix[-20:]) / 20, rtol=1e-12)
    cut = signals.date.iloc[len(signals) // 2]
    changed = bars.copy()
    changed.loc[changed.date > cut, "adj_close"] *= 1.7
    perturbed = moving_average_signals(changed, dates, config)
    pd.testing.assert_frame_equal(signals.loc[signals.date <= cut], perturbed.loc[perturbed.date <= cut], check_exact=True)
    next_dates = dict(zip(dates[:-1], dates[1:]))
    signals["next_observed_open_date"] = signals.date.map(next_dates)
    # The last close has no execution session inside the requested range.
    signals.loc[signals.next_observed_open_date > pd.Timestamp(config.end), "next_observed_open_date"] = pd.NaT
    args.output.mkdir(parents=True, exist_ok=False)
    signals.to_parquet(args.output / "signals.parquet", index=False)
    result = dict(schema="CSMAR_DUAL_MA_SIGNAL_DIAGNOSTIC_V1", signal_status="PASS_OBSERVED_INPUT_SCOPE_ONLY",
                  code_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                  manifest_sha256=sha256((args.candidate / "manifest.json").read_bytes()),
                  signals_sha256=table_sha256(signals), records=len(signals),
                  counts=signals.signal.value_counts().to_dict(), future_perturbation="PASS",
                  moving_average_arithmetic="PASS", next_open_mapping="OBSERVED_SECURITY_SESSIONS_ONLY",
                  calendar="OFFICIAL_CALENDAR_UNVERIFIED", pit="PIT_UNVERIFIED", hfq="HFQ_UNVERIFIED",
                  formal_backtest="BLOCKED / NOT_RUN", no_live_money=True)
    (args.output / "result.json").write_bytes(json_bytes(result))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
