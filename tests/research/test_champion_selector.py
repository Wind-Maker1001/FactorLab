from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.dataset.identity import file_hash
from factorlab.experiments import ExperimentTracker
from factorlab.research.automatic import expand_registry, run, validate_config, write_json
from factorlab.research.champion import (ChampionSelector, SelectionPolicy, objective,
                                        paired_evidence, risk_metrics)


def account(name, returns, policy):
    r = np.asarray(returns)
    return dict(candidate=dict(candidate_id=name), nav=pd.DataFrame(dict(
        date=pd.bdate_range("2020-01-01", periods=len(r)), strategy_return=r)),
        metrics=dict(score=objective(r, policy), net_return=float(np.prod(1+r)-1),
            annual_mean_net_return=float(r.mean()*252), max_drawdown=.01, daily_cvar=.01,
            max_participation=.001, capacity_cny=1000000., days=len(r),
            tail_observations=len(r)*.05))


def test_promotion_requires_paired_oos_advantage_and_cooldown():
    p = SelectionPolicy(resamples=2000)
    rng = np.random.default_rng(12)
    baseline = rng.normal(-.0001, .001, 260)
    incumbent = account("old", baseline, p)
    strong = account("strong", baseline+.001+rng.normal(0, .0001, 260), p)
    result = ChampionSelector(p).select(incumbent, [strong], family_size=8)
    assert result["promoted"] and result["selected"]["candidate_id"] == "strong"
    evidence = result["decisions"][0]["evidence"]
    assert evidence["confidence"] == pytest.approx(1-.05/8)
    assert evidence["hac_ci_lower"] > .0008
    assert evidence["score_ci_lower"] > p.min_score_improvement
    held = ChampionSelector(p).select(incumbent, [strong], family_size=8, cooldown_elapsed=False)
    assert held["selected"] == incumbent["candidate"]
    assert "promotion_cooldown" in held["decisions"][0]["reasons"]


def test_uncertain_degenerate_negative_or_risky_candidates_do_not_force_winner():
    p = SelectionPolicy(resamples=100)
    rng = np.random.default_rng(44)
    base = rng.normal(0, .001, 260)
    incumbent = account("old", base, p)
    noisy = account("noise", base+rng.normal(0, .003, 260), p)
    same = account("same", base, p)
    risky = account("risky", base+.002+rng.normal(0, .0001, 260), p)
    risky["metrics"].update(max_drawdown=.8, daily_cvar=.2, capacity_cny=None)
    result = ChampionSelector(p).select(incumbent, [noisy, same, risky], family_size=3)
    assert not result["promoted"] and result["selected"] == incumbent["candidate"]
    assert "oos_advantage_statistically_uncertain" in result["decisions"][1]["reasons"]
    assert "capacity_insufficient_or_unknown" in result["decisions"][2]["reasons"]
    short = account("short", base[:40]+.002, p)
    old_short = account("old", base[:40], p)
    held = ChampionSelector(p).select(old_short, [short], family_size=1)
    assert "insufficient_oos_or_tail_observations" in held["decisions"][0]["reasons"]


def test_paired_calendar_and_policy_validation():
    p = SelectionPolicy(resamples=100)
    c = account("a", np.arange(200)*.000001, p)
    other = c["nav"].iloc[1:].reset_index(drop=True)
    with pytest.raises(ValueError, match="calendars"):
        paired_evidence(c["nav"], other, p, 1)
    with pytest.raises(ValueError, match="family_size"):
        ChampionSelector(p).select(c, [], family_size=0)
    with pytest.raises(ValueError, match="unique"):
        ChampionSelector(p).select(c, [c], family_size=1)
    for invalid in (dict(objective="best"), dict(max_drawdown=float("nan")), dict(resamples=0),
                    dict(block_days=100), dict(seed=True), dict(participation_limit=2)):
        with pytest.raises(ValueError):
            SelectionPolicy(**invalid)


@pytest.fixture
def study(tmp_path):
    source = Path(__file__).resolve().parents[2] / "examples/research/champion_strategies.py"
    strategy = tmp_path / "strategies.py"
    strategy.write_bytes(source.read_bytes())
    dates = pd.bdate_range("2021-01-01", periods=230)
    rng = np.random.default_rng(91)
    close = 30*np.cumprod(1+.0009+.004*rng.standard_normal(len(dates)))
    opening = np.r_[30., close[:-1]]
    bars = pd.DataFrame(dict(date=dates, symbol="601857.SH", raw_open=opening,
        raw_high=np.maximum(close, opening)*1.002, raw_low=np.minimum(close, opening)*.998,
        raw_close=close, adj_close=close, volume=5000000., trade_status=1, is_st=0))
    bars.to_parquet(tmp_path / "bars.parquet", index=False)
    config = dict(schema="FACTORLAB_AUTO_RESEARCH_V1", study_id="paired-test",
        data=dict(bars="bars.parquet", sha256=file_hash(tmp_path / "bars.parquet"), source_kind="SYNTHETIC"),
        windows={n: [str(dates[a].date()), str(dates[b].date())]
                 for n, a, b in (("train", 30, 89), ("validation", 90, 129), ("test", 130, 229))},
        common=dict(symbol="601857.SH", initial_cash=100000., max_weight=1., board_lot=100,
            funding_bps=0, execution="a_share_daily_long_only_next_open_t1",
            fee_rules=[dict(effective_from="2020-01-01", slippage_bps=5.)]),
        policy=dict(resamples=100, min_oos_days=60, block_days=5, min_tail_observations=3),
        strategies=[dict(strategy_id=cls.lower(), file="strategies.py", **{"class": cls},
            sha256=file_hash(strategy), parameters={}, grid=grid)
            for cls, grid in (("Trend", {"window": [5, 10]}), ("TrainMean", {"multiplier": [1.]}))],
        champion=dict(strategy_id="cash", strategy={"file": "strategies.py", "class": "Cash", "sha256": file_hash(strategy)}, parameters={}))
    path = tmp_path / "config.json"
    write_json(path, config)
    return path, config, bars


def execute(path, name="first"):
    root = path.parent
    return run(path, output=root / name, store=root / (name+"-tracker"), state_path=root / (name+"-state.json"), command=["pytest"])


def test_multistrategy_tracker_state_and_reproduction(study):
    path, config, _ = study
    report = execute(path)
    assert len(report["candidates"]) == 4
    assert report["financial_admission"] == "FORMAL_ADMISSION_BLOCKED"
    tracker = ExperimentTracker(path.parent / "first-tracker")
    runs = tracker.query(status="SUCCEEDED")
    assert len(runs) >= 4
    for record in runs:
        assert tracker.reproduction(record["id"], verify_artifacts=True)
        assert record["payload"]["run"]["validation"]["train_only_fit"] == config["windows"]["train"]
    frozen = json.loads((path.parent / "first/frozen-search.json").read_text())
    assert len({c["strategy_id"] for c in frozen["finalists"]}) == len(frozen["finalists"])
    with pytest.raises(ValueError, match="already consumed"):
        run(path, output=path.parent / "repeat", store=tracker.root, state_path=path.parent / "first-state.json")
    again = execute(path.parent / "first/replay-config.json", "reproduce")
    assert report["selected"]["candidate_id"] == again["selected"]["candidate_id"]
    assert report["selected"]["code_version"] == again["selected"]["code_version"]
    assert report["decisions"] == again["decisions"]


def test_interruption_reserves_oos_and_keeps_champion(study, monkeypatch):
    from factorlab.research import automatic
    path, config, _ = study
    original = automatic.run_custom_strategy

    def interrupt_test(*args, **kwargs):
        if json.loads(Path(args[2]).read_text())["research"]["start"] == config["windows"]["test"][0]:
            raise KeyboardInterrupt("simulated process interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(automatic, "run_custom_strategy", interrupt_test)
    with pytest.raises(KeyboardInterrupt):
        execute(path)
    state = json.loads((path.parent / "first-state.json").read_text())
    assert state["status"] == "TEST_RESERVED" and state["champion"]["strategy_id"] == "cash"
    with pytest.raises(ValueError, match="already consumed"):
        run(path, output=path.parent / "retry", store=path.parent / "first-tracker", state_path=path.parent / "first-state.json")


def test_fit_receives_only_training_market_fields(study):
    path, config, _ = study
    source = path.parent / "strategies.py"
    text = source.read_text()
    text = text.replace("self.threshold = float(bars.adj_close.mean())*self.multiplier",
        "assert str(calendar[-1].date()) == '"+config["windows"]["train"][1]+"'\n"
        "        assert set(bars.columns) == {'date', 'symbol', 'adj_close'}\n"
        "        self.threshold = float(bars.adj_close.mean())*self.multiplier")
    source.write_text(text)
    for entry in config["strategies"]:
        entry["sha256"] = file_hash(source)
    config["champion"]["strategy"]["sha256"] = file_hash(source)
    write_json(path, config)
    report = execute(path)
    fitted = [c for c in report["candidates"] if c["candidate"]["strategy_id"] == "trainmean"][0]
    assert fitted["stages"]["validation"]["status"] == "SUCCEEDED"


def test_test_perturbation_cannot_change_validation_shortlist(study):
    path, config, bars = study
    original = execute(path)
    first = json.loads((path.parent / "first/frozen-search.json").read_text())
    changed = bars.copy()
    mask = changed.date.ge(config["windows"]["test"][0])
    for name in ("raw_open", "raw_close", "raw_high", "raw_low", "adj_close"):
        changed.loc[mask, name] *= 1.2
    changed.to_parquet(path.parent / "bars.parquet", index=False)
    config["data"]["sha256"] = file_hash(path.parent / "bars.parquet")
    write_json(path, config)
    perturbed = execute(path, "perturbed")
    second = json.loads((path.parent / "perturbed/frozen-search.json").read_text())
    assert first["finalists"] == second["finalists"]
    for a, b in zip(original["candidates"], perturbed["candidates"]):
        if "validation" in a["stages"]:
            assert a["stages"]["validation"]["metrics"] == b["stages"]["validation"]["metrics"]


def test_all_unqualified_holds_exact_champion(study):
    path, config, _ = study
    config["common"]["max_weight"] = .1
    write_json(path, config)
    report = execute(path)
    assert report["promoted"] is False and report["selected"]["strategy_id"] == "cash"
    assert report["version"] == 0 and report["decisions"] == []
    assert all(c["status"] == "VALIDATION_REJECTED" for c in report["candidates"] if c["candidate"]["strategy_id"] != "cash")


def test_config_pinning_boundaries_funding_and_bounded_search(study):
    path, config, _ = study
    for change in ("windows", "funding", "execution"):
        modified = deepcopy(config)
        if change == "windows":
            modified["windows"]["validation"][0] = modified["windows"]["train"][1]
        elif change == "funding":
            modified["common"]["funding_bps"] = 1
        else:
            modified["common"]["execution"] = "instant"
        with pytest.raises(ValueError):
            validate_config(modified)
    with pytest.raises(ValueError, match="max_candidates"):
        expand_registry(config["strategies"], 1)
    (path.parent / "bars.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="identity changed"):
        execute(path)


def test_capacity_uses_signal_close_trailing_volume_and_origin_drawdown():
    p = SelectionPolicy(volume_lookback=2)
    dates = pd.bdate_range("2020-01-01", periods=5)
    bars = pd.DataFrame(dict(date=dates, raw_close=10., volume=[100., 100., 1e9, 1e9, 1e9]))
    result = dict(signals=pd.DataFrame(dict(date=[dates[1]], target_weight=[1.])), strategy=dict(
        nav=pd.DataFrame(dict(strategy_return=[-.1, .01, .01])),
        trades=pd.DataFrame(dict(signal_date=[dates[1]], executed_shares=[100.], transaction_cost=[1.]))))
    metrics = risk_metrics(result, bars, 1000., p)
    assert metrics["max_participation"] == 1.
    assert metrics["capacity_cny"] == 10.
    assert metrics["max_drawdown"] == pytest.approx(.1)
