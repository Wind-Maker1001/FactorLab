"""Reproduce bounded CSMAR historical pair statistics without a PnL account."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys

import numpy as np
import pandas as pd

from factorlab.data.hub import DataHub
from factorlab.data.hub.contracts import QueryResult, SourceMetadata, UnsupportedQueryError
from factorlab.dataset.identity import file_hash, frame_hash
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.strategies.pairs_statistics import SelectionConfig
from factorlab.strategies.pairs_trading import PairTradingConfig, read_pair_panels, run_pair_research
from factorlab.validation.splitter import ExpandingWalkForwardSplitter


BLOCK = "BLOCKED_CORPORATE_ACTION_AND_SHORT_LIABILITY_SEMANTICS"


class PreparedCsmarAdapter:
    """Read the private, native-replayed snapshot, not arbitrary renamed prices."""
    capabilities = frozenset({"daily_bars"})

    def __init__(self, root):
        root = Path(root)
        self.manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if self.manifest["schema"] != "P5_R03_CSMAR_PRIVATE_V1":
            raise ValueError("unsupported prepared snapshot")
        if file_hash(root / "bars.parquet") != self.manifest["bars_sha256"]:
            raise ValueError("private price snapshot checksum mismatch")
        self.bars = pd.read_parquet(root / "bars.parquet")
        if self.bars.duplicated(["date", "symbol"]).any():
            raise ValueError("duplicate native price keys")
        if not set(self.bars.symbol).issubset(self.manifest["symbols"]):
            raise ValueError("snapshot symbol scope mismatch")
        self.metadata = SourceMetadata(source_id="CSMAR_TRDNEW_Dalyr_CANDIDATE",
            data_version=self.manifest["bars_sha256"], calendar_kind="OBSERVED_SECURITY_SESSIONS_ONLY",
            coverage="NO_FILL; HISTORICAL_IDENTITY_AND_AVAILABILITY_UNVERIFIED",
            field_semantics={"adj_close": {"unit": "CNY/share", "meaning": self.manifest["price_semantics"]},
                             "raw_open": {"unit": "CNY/share", "meaning": "unadjusted observed open"}},
            provenance={"references": self.manifest["references"]})

    def read(self, kind, *, symbol, security_id, start, end, knowledge_as_of):
        if knowledge_as_of is not None:
            raise UnsupportedQueryError("historical availability UNKNOWN")
        frame = self.bars.loc[self.bars.symbol.eq(symbol) & self.bars.date.between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        frame = frame.sort_values("date")
        frame.loc[frame.trade_status.ne(1), ["adj_close", "raw_open", "raw_close"]] = np.nan
        return QueryResult(frame, self.metadata)


def _combine(results, name):
    frames = []
    for pair_id, result in results:
        frame = getattr(result, name).copy()
        frame.insert(0, "pair_id", pair_id)
        if not frame.empty:
            frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["pair_id"])


def execute(config, *, candidate, output):
    try:
        import statsmodels
    except ModuleNotFoundError as exc:
        if exc.name != "statsmodels":
            raise
        raise ModuleNotFoundError(
            'install factorlab[pairs] for cointegration research: pip install "factorlab[pairs]"',
            name="statsmodels",
        ) from exc
    if config["execution"] != BLOCK or config["price_field"] != "adj_close":
        raise ValueError("this research entry requires comparable-close statistics with execution blocked")
    pairs = config["pairs"]
    keys = [tuple(sorted(pair["symbols"])) for pair in pairs]
    if not 1 <= len(keys) <= 30 or len(set(keys)) != len(keys) or any(len(set(k)) != 2 for k in keys):
        raise ValueError("require 1..30 distinct explicit two-security candidates")
    adapter = PreparedCsmarAdapter(candidate)
    symbols = sorted({s for key in keys for s in key})
    if not set(symbols).issubset(adapter.manifest["symbols"]):
        raise ValueError("configured symbols not in native replay scope")
    if config["start"] < adapter.manifest["start"] or config["end"] > adapter.manifest["end"]:
        raise ValueError("configured dates outside native replay scope")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "config.json").write_text(json.dumps(config, indent=2, allow_nan=False), encoding="utf-8")
    hub = DataHub({"csmar": adapter})
    opens, closes, _ = read_pair_panels(hub, "csmar", symbols, start=config["start"], end=config["end"], close_field=config["price_field"])
    coverage = []
    for symbol in symbols:
        present = closes[symbol].dropna()
        raw = adapter.bars.loc[adapter.bars.symbol.eq(symbol)]
        coverage.append(dict(symbol=symbol, observed_rows=len(raw), valid_signal_rows=len(present),
                             missing_signal_sessions=int(closes[symbol].isna().sum()),
                             suspended_or_filled_rows=int(raw.trade_status.ne(1).sum()),
                             first_valid=None if present.empty else present.index[0].isoformat(),
                             last_valid=None if present.empty else present.index[-1].isoformat(),
                             listing_boundary="OBSERVED_ONLY_NOT_CERTIFIED_LISTING_DATE"))
    pd.DataFrame(coverage).to_csv(output / "coverage.csv", index=False)
    strategy = PairTradingConfig(**config["strategy"])
    selection = SelectionConfig(train_end=config["start"], **config["selection"])
    results = []
    for y, x in keys:
        result = run_pair_research(opens[[y, x]], closes[[y, x]],
            selection=selection, strategy=strategy,
            splitter=ExpandingWalkForwardSplitter(**config["splitter"]), borrow={},
            entry_grid=config["entry_grid"], source_kind="REAL_CANDIDATE", statistics_only=True)
        results.append((y + "__" + x, result))
    tables = {name: _combine(results, name) for name in
              ("selections", "folds", "diagnostics", "signals", "episodes", "validation")}
    selections = tables["selections"]
    selections["bonferroni_accepted"] = selections.accepted & selections.coint_pvalue.le(selection.coint_pvalue / len(keys))
    selections["high_correlation_rejected"] = (~selections.accepted & selections.price_correlation.ge(config["correlation_control_threshold"]))
    selections["high_correlation_eg_rejected"] = (selections.high_correlation_rejected &
        selections.coint_pvalue.gt(selection.coint_pvalue))
    summary = []
    for pair_id, result in results:
        selected = selections.loc[selections.pair_id.eq(pair_id)]
        for threshold in config["entry_grid"]:
            events = result.episodes
            events = events.loc[events.entry_z.eq(threshold)] if len(events) else events
            eligible = events.loc[events.accepted] if len(events) else events
            complete = eligible.loc[~eligible.censored] if len(eligible) else eligible
            recovered = complete.loc[complete.recovered] if len(complete) else complete
            controls = events.loc[~events.accepted & ~events.censored] if len(events) else events
            summary.append(dict(pair_id=pair_id, entry_z=threshold, folds=len(selected),
                accepted_folds=int(selected.accepted.sum()), bonferroni_folds=int(selected.bonferroni_accepted.sum()),
                high_correlation_rejected_folds=int(selected.high_correlation_rejected.sum()),
                high_correlation_eg_rejected_folds=int(selected.high_correlation_eg_rejected.sum()),
                eligible_oos_episodes=len(eligible), complete_oos_episodes=len(complete),
                censored_oos_episodes=len(eligible)-len(complete), recovered_episodes=len(recovered),
                rejected_control_complete_episodes=len(controls),
                rejected_control_recovery_fraction=None if not len(controls) else float(controls.recovered.mean()),
                recovery_fraction=None if not len(complete) else len(recovered)/len(complete),
                recovery_sessions_median=None if not len(recovered) else float(recovered.elapsed_sessions.median()),
                recovery_sessions_p90=None if not len(recovered) else float(recovered.elapsed_sessions.quantile(.9)),
                pnl_status=BLOCK))
    tables["summary"] = pd.DataFrame(summary)
    for name, frame in tables.items():
        if name == "folds":
            frame.drop(columns=["model"]).to_csv(output / (name + ".csv"), index=False)
            (output / "models.json").write_text(frame[["pair_id", "fold", "accepted", "model"]].to_json(orient="records", indent=2), encoding="utf-8")
        else:
            frame.to_csv(output / (name + ".csv"), index=False)
    identity = dict(source="CSMAR_TRDNEW_Dalyr", snapshot_sha256=adapter.manifest["bars_sha256"],
                    config_sha256=file_hash(output / "config.json"), panel_hash=frame_hash(closes.reset_index()),
                    sources=[dict(alias="CSMAR_DALYR_" + str(i), sha256=ref["source_sha256"],
                                  member=ref["member"], member_sha256=ref["member_sha256"],
                                  dictionary_scope=ref["profile_sha256"], parser_sha256=ref["parser_sha256"])
                             for i, ref in enumerate(adapter.manifest["references"])],
                    python=platform.python_version(), pandas=pd.__version__, numpy=np.__version__, statsmodels=statsmodels.__version__,
                    implementation={p.name: file_hash(p) for p in [Path(__file__), Path(__file__).with_name("pairs_research.py"),
                        Path(__file__).with_name("pairs_statistics.py"), Path(__file__).with_name("pairs_trading.py")]})
    code = {key: subprocess.check_output(["git", "rev-parse", ref], text=True).strip()
            for key, ref in (("commit", "HEAD"), ("tree", "HEAD^{tree}"))}
    code["dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    identity["code"] = code
    unavailable = selections.reason.isin(["insufficient completeness", "insufficient observations", "insufficient overlap"])
    status = "P5_R03_BLOCKED_DATA" if unavailable.all() else "P5_R03_REAL_STATISTICAL_EVIDENCE"
    metadata = dict(status=status, HISTORICAL_TRADABILITY="UNKNOWN",
                    BORROW_ASSUMPTIONS="SYNTHETIC", execution=BLOCK, pnl_metrics=None,
                    validation_selection="Fixed entry_z; validation diagnostic only; no profit optimization",
                    calendar="Union of configured native observed sessions; no exchange-calendar certification",
                    evidence=["NO LIVE MONEY", "PIT_UNVERIFIED", "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED"],
                    multiple_testing=config["multiple_testing"],
                    not_computable={"returns_drawdown_sharpe_cost_borrow_sensitivity": BLOCK,
                                    "fills_rejections_margin_borrow_limits": "NOT_RUN_NO_EXECUTION_ACCOUNT"})
    for name, value in (("identity", identity), ("metadata", metadata)):
        (output / (name + ".json")).write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    tracker = ExperimentTracker(output / "tracker")
    folds = tables["folds"]
    windows = {phase: [str(folds[phase + "_start"].min()), str(folds[phase + "_end"].max())]
               for phase in ("train", "validation", "test")}
    tracker.create_experiment("p5-r03", dict(model="OLS-Engle-Granger-frozen-spread", parameters=config,
        factors=[], dataset=identity, label={"name": "10-session causal rolling-Z recovery, not PnL"},
        windows=windows, walk_forward=config["splitter"], costs={"status": BLOCK}))
    tracker.append_run("p5-r03", "real-statistics", code=code,
        environment={k: identity[k] for k in ("python", "pandas", "numpy", "statsmodels")},
        command=[sys.executable, "-m", "factorlab.strategies.pairs_real_cli", "--config", "config.json",
                 "--candidate", "PRIVATE_CSMAR_SNAPSHOT", "--output", "NEW_OUTPUT_DIRECTORY"],
        status="SUCCEEDED", qualification="REAL_CANDIDATE", portfolio_metrics={},
        prediction_metrics={"accepted_pair_folds": int(selections.accepted.sum())}, validation=metadata,
        artifacts={p.name: {"path": str(p.resolve()), "sha256": file_hash(p)} for p in output.iterdir() if p.is_file()})
    return tables, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "candidate", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    tables, metadata = execute(json.loads(args.config.read_text(encoding="utf-8")), candidate=args.candidate, output=args.output)
    print(json.dumps(dict(**metadata, candidate_pairs=len(tables["summary"].pair_id.unique()),
                          accepted_pair_folds=int(tables["selections"].accepted.sum())), indent=2))


if __name__ == "__main__":
    main()
