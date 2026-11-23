from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.dataset.identity import file_hash, json_hash
from factorlab.experiments import ExperimentTracker
from factorlab.research.automatic import expand_registry, read_json, replay, run, validate_config, write_json
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


@pytest.fixture
def rolling_study(study):
    path, config, bars = study
    opening_price = bars.raw_open.iloc[0]
    returns = .0015 + .0002 * np.sin(np.arange(len(bars)))
    close = opening_price * np.cumprod(1 + returns)
    opening = np.r_[opening_price, close[:-1]]
    bars = bars.assign(raw_open=opening, raw_high=np.maximum(close, opening) * 1.002,
                       raw_low=np.minimum(close, opening) * .998, raw_close=close, adj_close=close)
    bars_path = path.parent / "bars.parquet"
    bars.to_parquet(bars_path, index=False)
    config["data"]["sha256"] = file_hash(bars_path)
    config["strategies"] = [config["strategies"][-1]]
    config["policy"]["resamples"] = 400

    strategy = dict(config["champion"]["strategy"], file=str(path.parent / "strategies.py"), parameters={})
    identity = dict(strategy_id="cash", code_version=strategy["sha256"], class_name="Cash", parameters={})
    champion = dict(identity, candidate_id="c-" + json_hash(identity)[:24], strategy=strategy)
    prior_selection = path.parent / "prior-selection.json"
    write_json(prior_selection, dict(version=5, selected=champion, promoted=True))
    previous_end = config["windows"]["validation"][1]
    prior = dict(schema="FACTORLAB_CHAMPION_STATE_V1", champion=champion, version=5,
                 last_test_end=previous_end, last_promotion_end=previous_end,
                 common_hash=json_hash(config["common"]), status="COMPLETED", reserved_study="prior-study",
                 decision=dict(path=str(prior_selection), sha256=file_hash(prior_selection)))
    write_json(path.parent / "first-state.json", prior)
    config["champion"] = dict(strategy_id="unused_initial_champion",
                              strategy=dict(config["champion"]["strategy"], **{"class": "Trend"}),
                              parameters={})
    write_json(path, config)
    return path, config, prior


def assert_same_selection(original, audited, original_state, audit_state):
    for key in ("candidate_id", "code_version"):
        assert original["selected"][key] == audited["selected"][key]
    for key in ("promoted", "version", "reason", "decisions"):
        assert original[key] == audited[key]
    for key in ("last_promotion_end", "last_test_end", "version", "status"):
        assert original_state[key] == audit_state[key]


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
        recorded_run = record["payload"]["run"]
        assert recorded_run["validation"]["train_only_fit"] == config["windows"]["train"]
        assert "pre-run-state.json" in recorded_run["artifacts"]
    frozen = json.loads((path.parent / "first/frozen-search.json").read_text())
    assert len({c["strategy_id"] for c in frozen["finalists"]}) == len(frozen["finalists"])
    with pytest.raises(ValueError, match="already consumed"):
        run(path, output=path.parent / "repeat", store=tracker.root, state_path=path.parent / "first-state.json")
    origin = path.parent / "first"
    assert read_json(origin / "pre-run-state.json")["state"] is None
    audit = path.parent / "reproduce"
    again = replay(origin, audit_dir=audit)
    assert_same_selection(report, again, read_json(path.parent / "first-state.json"),
                          read_json(audit / "champion.json"))


@pytest.mark.parametrize(("cooldown_days", "resamples", "reasons", "version"), [
    pytest.param(365, 400, ["promotion_cooldown"], 5, id="cooldown-holds-v5"),
    pytest.param(30, 400, [], 6, id="elapsed-cooldown-promotes-to-v6"),
    pytest.param(30, 100, ["insufficient_bootstrap_tail_resolution"], 5, id="insufficient-statistics-holds-v5"),
])
def test_rolling_replay_preserves_selection(rolling_study, cooldown_days, resamples, reasons, version):
    path, config, prior = rolling_study
    config["policy"].update(cooldown_days=cooldown_days, resamples=resamples)
    write_json(path, config)
    original = execute(path)
    origin = path.parent / "first"
    snapshot = read_json(origin / "pre-run-state.json")["state"]
    archived_strategy = dict(prior["champion"]["strategy"], file="sources/" + prior["champion"]["code_version"] + ".py")
    assert snapshot == dict(prior, champion=dict(prior["champion"], strategy=archived_strategy))

    audit_dir = path.parent / "audit"
    audited = replay(origin, audit_dir=audit_dir)
    original_state = read_json(path.parent / "first-state.json")
    assert_same_selection(original, audited, original_state, read_json(audit_dir / "champion.json"))
    assert original["version"] == version
    assert original["decisions"][0]["reasons"] == reasons
    assert original["promoted"] is (version == 6)
    if version == 6:
        assert original["selected"]["strategy_id"] == "trainmean"
        assert original_state["last_promotion_end"] == config["windows"]["test"][1]
    else:
        assert original["selected"] == prior["champion"]
        assert original_state["last_promotion_end"] == prior["last_promotion_end"]


def test_interruption_reserves_oos_and_keeps_champion(rolling_study, monkeypatch):
    from factorlab.research import automatic
    path, config, prior = rolling_study
    original = automatic.run_custom_strategy

    def interrupt_test(*args, **kwargs):
        if json.loads(Path(args[2]).read_text())["research"]["start"] == config["windows"]["test"][0]:
            raise KeyboardInterrupt("simulated process interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(automatic, "run_custom_strategy", interrupt_test)
    with pytest.raises(KeyboardInterrupt):
        execute(path)
    state = read_json(path.parent / "first-state.json")
    assert state == dict(prior, status="TEST_RESERVED", reserved_study=config["study_id"],
                         last_test_end=config["windows"]["test"][1])
    assert not (path.parent / "first/selection.json").exists()
    with pytest.raises(ValueError, match="already consumed"):
        run(path, output=path.parent / "retry", store=path.parent / "first-tracker", state_path=path.parent / "first-state.json")

    audit = path.parent / "interrupted-audit"
    with pytest.raises(KeyboardInterrupt):
        replay(path.parent / "first", audit_dir=audit)
    audit_state = read_json(audit / "champion.json")
    for key in ("status", "version", "last_promotion_end", "last_test_end"):
        assert audit_state[key] == state[key]
    for key in ("candidate_id", "code_version"):
        assert audit_state["champion"][key] == prior["champion"][key]
    assert audit_state["decision"] is None
    assert not (audit / "run/selection.json").exists()
    with pytest.raises(FileExistsError):
        replay(path.parent / "first", audit_dir=audit)


def test_replay_isolates_state_and_keeps_window_consumption(rolling_study):
    path, _, _ = rolling_study
    execute(path)
    root = path.parent
    origin = root / "first"
    original_state_path = root / "first-state.json"
    original_state_bytes = original_state_path.read_bytes()
    audit = root / "audit"
    replay(origin, audit_dir=audit)

    for existing in (audit, origin, root / "first-tracker", original_state_path):
        with pytest.raises(FileExistsError):
            replay(origin, audit_dir=existing)
    with pytest.raises(ValueError, match="already consumed"):
        execute(path)
    with pytest.raises(ValueError, match="invalid automatic research configuration"):
        run(origin / "replay-config.json", output=root / "repeat", store=root / "first-tracker",
            state_path=original_state_path)
    with pytest.raises(ValueError, match="deployment mandate/common comparison contract changed"):
        run(path, output=root / "repeat", store=root / "first-tracker", state_path=audit / "champion.json")

    assert original_state_path.read_bytes() == original_state_bytes
    assert (audit / "run/selection.json").is_file()
    assert ExperimentTracker(audit / "tracker").query(status="SUCCEEDED")


def test_replay_rejects_invalid_pre_run_snapshot(rolling_study):
    path, _, _ = rolling_study
    execute(path)
    origin = path.parent / "first"
    snapshot_path = origin / "pre-run-state.json"
    replay_config_path = origin / "replay-config.json"
    snapshot = read_json(snapshot_path)
    replay_config = read_json(replay_config_path)
    state = snapshot["state"]
    audit = path.parent / "audit"

    write_json(snapshot_path, dict(snapshot, state=None))
    with pytest.raises(ValueError, match="pre-run Champion state checksum mismatch"):
        replay(origin, audit_dir=audit)

    without_version = dict(state)
    del without_version["version"]
    wrong_champion = dict(state["champion"], candidate_id="c-" + "0" * 24)
    cases = [
        (dict(snapshot, schema="UNKNOWN"), "invalid pre-run Champion state snapshot schema"),
        (dict(snapshot, state=without_version), "invalid pre-run Champion state fields"),
        (dict(snapshot, common_hash="0" * 64), "does not match the replay research contract"),
        (dict(snapshot, state=dict(state, last_promotion_end=None)), "invalid pre-run Champion promotion continuity"),
        (dict(snapshot, state=dict(state, last_test_end="2025-01-01")), "already consumed"),
        (dict(snapshot, state=dict(state, champion=wrong_champion)), "pre-run Champion identity"),
    ]
    for index, (invalid_snapshot, reason) in enumerate(cases):
        write_json(snapshot_path, invalid_snapshot)
        write_json(replay_config_path, dict(replay_config, pre_run_state_sha256=json_hash(invalid_snapshot)))
        with pytest.raises(ValueError, match=reason):
            replay(origin, audit_dir=path.parent / f"invalid-audit-{index}")


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
