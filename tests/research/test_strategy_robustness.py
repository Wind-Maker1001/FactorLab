"""Synthetic integration checks, not evidence of historical profitability."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from factorlab.errors import SchemaError
from factorlab.strategies.real_comparison import NAMES, compare
from factorlab.strategies.robustness import software_batch, summarize

ROOT = Path(__file__).parents[2]


def test_freeze_requires_prior_year_and_retains_all_coverage_eligible():
    from types import SimpleNamespace
    from factorlab.strategies.robustness_real import freeze
    dates = pd.bdate_range('2015-01-01', '2017-12-31')
    adapter = SimpleNamespace(bars=pd.DataFrame({'date': dates}), quality={'securities': [
        {'symbol': '601857.SH', 'intervals': [{'start': '2015-01-01', 'end': '2017-12-29'}]},
        {'symbol': '601398.SH', 'intervals': [{'start': '2016-01-01', 'end': '2017-12-29'}]}]})
    windows, excluded = freeze({'candidates': {'energy': ['601857.SH'], 'bank': ['601398.SH']},
        'years': [2015, 2016, 2017]}, adapter)
    assert {(w['symbol'], w['year']) for w in windows} == {('601857.SH', 2016), ('601857.SH', 2017), ('601398.SH', 2017)}
    assert len(excluded) == 3
    assert sum(w['reason'] == 'MISSING_PREVIOUS_YEAR_WARMUP' for w in excluded) == 2


def test_batch_independent_accounts_and_group_statistics():
    template = json.loads((ROOT / "configs/research/p5-r02-601857-2018.json").read_text())
    samples = []
    for year in (2017, 2018):
        dates = pd.bdate_range(f"{year - 1}-10-01", f"{year}-05-31")
        for symbol, industry, direction in (("601857.SH", "energy", 1), ("601398.SH", "bank", -1)):
            close = 10 + np.sin(np.arange(len(dates)) / 5) + direction * np.arange(len(dates)) / 150
            bars = pd.DataFrame(dict(date=dates, symbol=symbol, raw_open=close + .01,
                raw_close=close, raw_high=close + .1, raw_low=close - .1,
                volume=1000000, trade_status=True, is_st=False))
            samples.append(dict(symbol=symbol, industry=industry, year=year,
                start=f"{year}-01-01", end=f"{year}-05-31", warmup_start=str(dates[0].date()),
                bars=bars, calendar=dates))
    batch = software_batch(samples, template)
    rows, tables = batch["observations"], batch["tables"]
    assert batch["status"] == "SOFTWARE_ONLY"
    assert len(rows) == 24
    assert set(rows.strategy) == set(NAMES)
    for sample in samples:
        config = deepcopy(template)
        config["common"].update(symbol=sample["symbol"], start=sample["start"], end=sample["end"])
        config["warmup_start"] = sample["warmup_start"]
        direct = compare(sample["bars"], sample["calendar"], config, source_kind="SYNTHETIC")
        result = batch["results"][(sample["symbol"], sample["year"])]
        for name in NAMES:
            assert result["evaluations"][name].context.source_kind == "SYNTHETIC"
            assert result["summary"][name] == direct["summary"][name]
            nav = result["legs"][name]["nav"]
            row = rows.loc[(rows.symbol == sample["symbol"]) & (rows.year == sample["year"]) & (rows.strategy == name)].iloc[0]
            assert row.average_capital_exposure == pytest.approx((nav.market_value / nav.portfolio_value).mean())
    assert len(tables["annual"]) == 12
    assert len(tables["annual_industry"]) == 24
    for row in tables["security_proportions"].itertuples():
        security = rows.loc[rows.strategy.eq(row.strategy)].groupby("symbol").cumulative_return.mean()
        assert row.profitable_security_fraction == security.gt(0).mean()
    for row in tables["leave_one_security_out"].itertuples():
        remaining = rows.loc[rows.strategy.eq(row.strategy) & rows.symbol.ne(row.omitted_symbol)]
        assert row.mean_annual_return == pytest.approx(remaining.cumulative_return.mean())
    # Losing accounts remain in all relevant tables.
    assert rows.cumulative_return.lt(0).any()
    assert tables["overall"].accounts.eq(4).all()
    with pytest.raises(SchemaError, match="unique"):
        software_batch([samples[0], samples[0]], template)
    with pytest.raises(SchemaError, match="six"):
        summarize(rows.loc[rows.strategy.ne("rsi")])


def test_missing_input_cli_never_emits_empirical_metrics(tmp_path):
    output = tmp_path / "blocked"
    config = json.loads((ROOT / 'configs/research/p6-r01-strategy-robustness.json').read_text())
    config['input_status'] = 'WAITING_FOR_P6_D01_INPUT'
    pending = tmp_path / 'pending.json'
    pending.write_text(json.dumps(config))
    run = subprocess.run([sys.executable, "-I", str(ROOT / "tools/research/run_strategy_robustness.py"),
        "--config", str(pending),
        "--output", str(output)], capture_output=True, text=True)
    assert run.returncode == 2, run.stderr
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "P6_R01_BLOCKED_DATA"
    assert summary["input_status"] == "WAITING_FOR_P6_D01_INPUT"
    assert summary["observations"] == []
    assert summary["aggregates"] is None
    assert summary["real_accounts_run"] == 0
    assert not list(output.glob("*nav*"))
