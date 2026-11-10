"""Trusted local daily strategies producing close intents, never account mutations."""
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from typing import Protocol, get_type_hints

import numpy as np
import pandas as pd

from factorlab.backtest.fees import FeeSchedule
from factorlab.dataset.identity import file_hash
from factorlab.errors import ConfigurationError, SchemaError
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.strategies.dual_ma import DualMAConfig, daily_dates, prepare_inputs
from factorlab.strategies.technical_research import _evaluate_targets


class StrategyProtocol(Protocol):
    required_fields: tuple[str, ...]
    warmup_sessions: int

    def generate_targets(self, bars: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.DataFrame:
        """Return one target per supplied session, or just the current session."""
        ...


def load_strategy(config_path):
    """Load an explicitly selected trusted local file, not a security sandbox."""
    path = Path(config_path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or set(config) != {"strategy", "research"}:
        raise ConfigurationError("config requires strategy and research objects")
    spec = config["strategy"]
    if not isinstance(spec, dict) or set(spec) - {"file", "class", "parameters", "sha256"} or not {"file", "class", "parameters"} <= set(spec):
        raise ConfigurationError("strategy requires file, class, parameters; optional sha256")
    if not isinstance(spec["file"], str) or not spec["file"] or "://" in spec["file"]:
        raise ConfigurationError("strategy file must be an explicit local Python path")
    source = (path.parent / spec["file"]).resolve()
    if source.suffix != ".py" or not source.is_file():
        raise ConfigurationError("strategy requires an existing local .py file")
    if not isinstance(spec["class"], str) or not spec["class"].isidentifier():
        raise ConfigurationError("strategy class must be an identifier")
    if not isinstance(spec["parameters"], dict):
        raise ConfigurationError("strategy parameters must be an object")
    digest = file_hash(source)
    if "sha256" in spec and spec["sha256"] != digest:
        raise ConfigurationError("strategy code identity changed")
    module_spec = importlib.util.spec_from_file_location("factorlab_user_" + digest, source)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    try:
        module_spec.loader.exec_module(module)
        cls = getattr(module, spec["class"])
        if not inspect.isclass(cls):
            raise ConfigurationError("strategy class must be a class")
        signature = inspect.signature(cls)
        bound = signature.bind(**spec["parameters"])
        bound.apply_defaults()
        hints = get_type_hints(cls.__init__)
        for name, parameter in signature.parameters.items():
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                raise ConfigurationError("strategy constructors cannot use variadic parameters")
            expected = hints.get(name)
            value = bound.arguments[name]
            if expected not in (int, float, bool, str) or (type(value) is not expected and not (expected is float and type(value) is int)):
                raise ConfigurationError("parameter " + name + " requires an explicit int/float/bool/str type")
        json.dumps(bound.arguments, allow_nan=False)
        parameters = dict(bound.arguments)
        strategy = cls(**parameters)
    except Exception as exc:
        raise ConfigurationError(f"custom strategy {source}:{spec['class']} loading failed: {exc}") from exc
    warmup = getattr(strategy, "warmup_sessions", None)
    fields = getattr(strategy, "required_fields", None)
    if type(warmup) is not int or warmup < 2:
        raise ConfigurationError("strategy warmup_sessions must be an integer >= 2")
    if not isinstance(fields, tuple) or not fields or not all(isinstance(field, str) for field in fields):
        raise ConfigurationError("strategy required_fields must be a nonempty tuple of field names")
    if not callable(getattr(strategy, "generate_targets", None)):
        raise ConfigurationError("strategy requires generate_targets")
    research = config["research"]
    allowed = {"symbol", "start", "end", "initial_cash", "max_weight", "board_lot"}
    if not isinstance(research, dict) or set(research) - allowed or not {"symbol", "start", "end"} <= set(research):
        raise ConfigurationError("invalid research fields; symbol/start/end required")
    validated = DualMAConfig(**research, short_window=1, long_window=max(2, warmup))
    normalized = {"strategy": {"file": str(source), "class": spec["class"], "parameters": parameters, "sha256": digest},
                  "research": {key: getattr(validated, key) for key in sorted(allowed)}}
    return strategy, validated, normalized


def run_custom_strategy(bars, calendar, config_path, *, fee_schedule=None, source_kind="REAL_CANDIDATE"):
    """SOFTWARE_ONLY research. Each invocation receives history through its close only."""
    strategy, config, normalized = load_strategy(config_path)
    frame, research = prepare_inputs(bars, calendar, config)
    if not set(strategy.required_fields) <= set(frame):
        raise SchemaError("bars missing strategy required_fields")
    ratio = frame.adj_close / frame.raw_close
    if not np.allclose(ratio, ratio.iloc[0], rtol=1e-10, atol=1e-12):
        raise SchemaError("variable adjustment ratio requires qualified execution ledger")
    rows = []
    for day in research:
        columns = list(dict.fromkeys(("date", "symbol", *strategy.required_fields)))
        history = frame.loc[frame.date <= day, columns].copy()
        history_calendar = pd.DatetimeIndex(history.date)
        try:
            targets = strategy.generate_targets(history, history_calendar.copy())
        except Exception as exc:
            raise SchemaError(f"custom strategy {normalized['strategy']['class']} failed at {day.date()}: {exc}") from exc
        if not isinstance(targets, pd.DataFrame) or not {"date", "symbol", "target_weight"} <= set(targets):
            raise SchemaError("strategy targets require date, symbol, target_weight")
        dates = daily_dates(targets.date, "strategy targets")
        if len(dates.difference(history_calendar)) or not targets.symbol.eq(config.symbol).all():
            raise SchemaError("strategy targets must match the supplied calendar and security")
        weights = targets.target_weight
        if (not pd.api.types.is_numeric_dtype(weights) or pd.api.types.is_bool_dtype(weights)
                or pd.api.types.is_complex_dtype(weights)
                or (weights.notna() & (~np.isfinite(weights) | weights.lt(0) | weights.gt(1))).any()):
            raise SchemaError("target_weight must be NaN (hold) or numeric within [0, 1]; shorting unsupported")
        current = targets.loc[dates == day, ["date", "symbol", "target_weight"]]
        if len(current) != 1:
            raise SchemaError("strategy must explicitly output the current session; no interpolation")
        rows.append(current)
    signals = pd.concat(rows, ignore_index=True)
    fees = fee_schedule or FeeSchedule.a_share_default()
    result = _evaluate_targets(signals, frame, research, config, fees, source_kind)
    result.update(config=normalized, input_bars=frame, calendar=pd.DatetimeIndex(calendar), fees=fees)
    return result


def save_custom_run(result, output, *, provenance, command):
    """Save replay inputs and immutable tracker evidence to a new directory."""
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "config.json").write_text(json.dumps(result["config"], indent=2, allow_nan=False) + "\n", encoding="utf-8")
    result["input_bars"].to_parquet(root / "bars.parquet", index=False)
    pd.DataFrame({"date": result["calendar"]}).to_parquet(root / "calendar.parquet", index=False)
    result["signals"].to_csv(root / "signals.csv", index=False)
    for name in ("strategy", "benchmark"):
        for key in ("nav", "trades", "rejections"):
            result[name][key].to_csv(root / f"{name}_{key}.csv", index=False)
    report = {name: value.to_dict() for name, value in result["evaluations"].items()}
    (root / "evaluation.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    from factorlab.strategies.technical_research import _fee_identity
    (root / "fees.json").write_text(json.dumps(_fee_identity(result["fees"]), indent=2) + "\n", encoding="utf-8")
    research = result["config"]["research"]
    config = DualMAConfig(**research)
    tracker = ExperimentTracker(root / "tracker")
    # Existing tracker helper preserves the shared evaluation/cost semantics.
    artifacts = {path.name: {"path": str(path), "sha256": file_hash(path)} for path in root.iterdir() if path.is_file()}
    from factorlab.strategies.technical_research import SAFETY, _fee_identity
    tracker.create_experiment("custom", dict(model=result["config"]["strategy"]["class"],
        parameters=result["config"], factors=[], dataset={"bars_hash": result["dataset_id"]},
        label={"name": "daily_equity_return"}, windows={"train": None, "validation": None,
        "test": [config.start, config.end]}, walk_forward={"mode": "fixed_predeclared_parameters_no_fit"},
        costs={"rules": _fee_identity(result["fees"])}))
    tracker.append_run("custom", "run", **provenance, command=command, status="SUCCEEDED",
        qualification=result["evaluations"]["strategy"].context.source_kind, artifacts=artifacts,
        portfolio_metrics={name + ":" + key: value.value for name, evaluation in result["evaluations"].items()
                           for key, value in evaluation.metrics.items()},
        validation={"safety": SAFETY, "scope": "DAILY / SINGLE_SECURITY / LONG_ONLY / NEXT_OPEN",
                    "strategy_code": result["config"]["strategy"], "sample_hash": result["dataset_id"]})
    return root


def replay_custom_run(output):
    """Recompute saved data/config/costs; refuse changed strategy source."""
    from factorlab.backtest.fees import FeeRule
    root = Path(output)
    fees = FeeSchedule([FeeRule(**rule) for rule in json.loads((root / "fees.json").read_text(encoding="utf-8"))])
    context = json.loads((root / "evaluation.json").read_text(encoding="utf-8"))["strategy"]["context"]
    return run_custom_strategy(pd.read_parquet(root / "bars.parquet"),
        pd.DatetimeIndex(pd.read_parquet(root / "calendar.parquet").date), root / "config.json",
        fee_schedule=fees, source_kind=context["source_kind"])
