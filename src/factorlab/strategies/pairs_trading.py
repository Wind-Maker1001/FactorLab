"""Offline, time-isolated single-pair research over expanding folds."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date

import numpy as np
import pandas as pd

from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.backtest.pairs import BorrowTerms, PairAccount
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation
from factorlab.strategies.pairs_statistics import PairModel, SelectionConfig, select_pairs
from factorlab.validation.splitter import ExpandingWalkForwardSplitter


@dataclass(frozen=True)
class PairTradingConfig:
    z_window: int = 30
    entry_z: float = 2.0
    exit_z: float = .5
    max_holding_sessions: int = 10
    max_loss_fraction: float = .03
    gross_fraction: float = .5
    max_gross_leverage: float = 1.0
    diagnostic_window: int = 120
    diagnostic_interval: int = 20
    failure_pvalue: float = .10
    initial_cash: float = 100_000.0
    margin_rate: float = .5
    commission_bps: float = 3.0
    minimum_commission: float = 0.0

    def __post_init__(self):
        for key in ("z_window", "max_holding_sessions", "diagnostic_window", "diagnostic_interval"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < (20 if key == "diagnostic_window" else 2):
                raise ValueError(key + " has invalid session count")
        for key in ("entry_z", "exit_z", "max_loss_fraction", "gross_fraction", "max_gross_leverage",
                    "failure_pvalue", "initial_cash", "margin_rate", "commission_bps", "minimum_commission"):
            value = getattr(self, key)
            if isinstance(value, bool) or not np.isfinite(value) or value < 0:
                raise ValueError(key + " must be finite and nonnegative")
        if not (0 <= self.exit_z < self.entry_z and 0 < self.max_loss_fraction < 1
                and 0 < self.gross_fraction <= self.max_gross_leverage and 0 < self.failure_pvalue <= 1
                and self.initial_cash > 0):
            raise ValueError("invalid thresholds, capital or exposure settings")

    def fee_schedule(self):
        return FeeSchedule([FeeRule(date(1900, 1, 1), commission_bps=self.commission_bps,
                                    minimum_commission_cny=self.minimum_commission, slippage_bps=0)])


@dataclass
class PairSimulation:
    nav: pd.DataFrame
    trades: pd.DataFrame
    positions: pd.DataFrame
    failures: pd.DataFrame
    signals: pd.DataFrame


def mean_reversion_signal(zscore: float, direction: int, config: PairTradingConfig):
    """Direction +1 buys spread; exits consume a decision and cannot re-enter."""
    if not np.isfinite(zscore):
        return "hold", direction
    if direction:
        if abs(zscore) <= config.exit_z or direction * zscore >= 0:
            return "exit", 0
        return "hold", direction
    if zscore >= config.entry_z:
        return "enter", -1
    if zscore <= -config.entry_z:
        return "enter", 1
    return "hold", 0


def _validate_panel(opens, closes):
    for panel in (opens, closes):
        if not isinstance(panel.index, pd.DatetimeIndex) or panel.index.has_duplicates or panel.index.hasnans or panel.index.tz is not None or not panel.index.is_monotonic_increasing:
            raise ValueError("panels need ordered unique timezone-free dates")
        if not panel.index.equals(panel.index.normalize()) or panel.columns.has_duplicates:
            raise ValueError("normalized sessions and unique securities required")
        if np.isinf(panel.to_numpy(dtype=float)).any() or (panel <= 0).any().any():
            raise ValueError("prices must be positive and finite when present")
    if not opens.index.equals(closes.index) or not opens.columns.equals(closes.columns):
        raise ValueError("open/close panels must have identical calendars and symbols; no filling")


def simulate_pair(opens: pd.DataFrame, closes: pd.DataFrame, dates: pd.DatetimeIndex,
                  model: PairModel, config: PairTradingConfig, borrow: dict[str, BorrowTerms],
                  *, fill_scenarios: dict | None = None) -> PairSimulation:
    """Decide after close, execute only at the following supplied session open.

    Raw-price beta maps to share quantities; log beta maps to entry-price dollar
    elasticity. Integer quantities then stay fixed until exit. Window-end exit
    is scheduled at the penultimate close, not retroactively at the last close.
    """
    try:
        from statsmodels.tsa.stattools import coint
    except ModuleNotFoundError as exc:
        if exc.name != "statsmodels":
            raise
        raise ModuleNotFoundError(
            'install factorlab[pairs] for cointegration research: pip install "factorlab[pairs]"',
            name="statsmodels",
        ) from exc

    _validate_panel(opens, closes)
    dates = pd.DatetimeIndex(dates)
    if len(dates) < 2 or not dates.is_monotonic_increasing or dates.has_duplicates or not dates.isin(closes.index).all():
        raise ValueError("simulation requires at least two ordered panel dates")
    locations = closes.index.get_indexer(dates)
    if not np.all(np.diff(locations) == 1):
        raise ValueError("execution calendar may not skip panel sessions")
    if pd.Timestamp(model.train_end) >= dates[0]:
        raise ValueError("model must be trained strictly before execution window")
    symbols = [model.y_symbol, model.x_symbol]
    if opens.loc[dates, symbols].isna().any().any() or closes.loc[dates, symbols].isna().any().any():
        raise ValueError("execution and mark prices must exist; missing data is not forward-filled")
    account = PairAccount(config.initial_cash, config.fee_schedule(), borrow,
                          margin_rate=config.margin_rate, max_gross_leverage=config.max_gross_leverage)
    zscores = model.zscore(closes.loc[:dates[-1]], config.z_window)
    spread = model.spread(closes.loc[:dates[-1]])
    rolling = spread.shift(1).rolling(config.z_window)
    means, stds = rolling.mean(), rolling.std(ddof=1)
    pending = None
    direction = 0
    opened_at = None
    entry_equity = None
    forced_exit = False
    coint_valid = True
    latest_pvalue = model.coint_pvalue
    nav, positions, signals = [], [], []
    previous_equity = config.initial_cash
    fills = fill_scenarios or {}
    for i, day in enumerate(dates):
        open_prices = opens.loc[day, symbols].to_dict()
        close_prices = closes.loc[day, symbols].to_dict()
        daily_borrow = 0.0
        if i:
            daily_borrow = account.accrue_borrow(closes.loc[dates[i - 1], symbols], (day - dates[i - 1]).days)
        cost_before = account.transaction_cost
        executed = False
        if pending is not None:
            action, next_direction, decided_on, reason = pending
            day_fills = fills.get(day.strftime("%Y-%m-%d"), {})
            if action == "enter":
                beta = model.hedge_ratio
                if model.price_transform == "log":
                    beta *= open_prices[model.y_symbol] / open_prices[model.x_symbol]
                budget = account.values(open_prices)["equity"] * config.gross_fraction
                y_qty = int(budget / (open_prices[model.y_symbol] + beta * open_prices[model.x_symbol]))
                x_qty = int(y_qty * beta)
                if y_qty > 0 and x_qty > 0:
                    entry_equity = account.values(open_prices)["equity"]
                    complete = account.open_pair(day, {model.y_symbol: next_direction * y_qty,
                                                       model.x_symbol: -next_direction * x_qty},
                                                 open_prices, decision_date=decided_on, fills=day_fills)
                    if not account.flat:
                        direction, opened_at = next_direction, i
                        forced_exit = not complete
                else:
                    account.failures.append(dict(date=day, symbol="PAIR", action="enter",
                                                 requested=0, filled=0, reason="SIZE_BELOW_ONE_SHARE"))
            else:
                account.close_pair(day, open_prices, decision_date=decided_on, reason=reason, fills=day_fills)
                forced_exit = not account.flat
                if account.flat:
                    direction, opened_at, entry_equity = 0, None, None
            pending = None
            executed = True
        state = account.values(close_prices)
        if i % config.diagnostic_interval == 0:
            history = closes.loc[:day, symbols].tail(config.diagnostic_window)
            if len(history) == config.diagnostic_window and not history.isna().any().any():
                values = np.log(history) if model.price_transform == "log" else history
                if (values.max() - values.min() > 0).all():
                    _, latest_pvalue, _ = coint(values.iloc[:, 0], values.iloc[:, 1], trend="c", autolag="aic")
                    coint_valid = bool(np.isfinite(latest_pvalue) and latest_pvalue <= config.failure_pvalue)
                else:
                    coint_valid = False
            else:
                coint_valid = False
                latest_pvalue = float("nan")
        zscore = float(zscores.loc[day])
        reason = ""
        action = "hold"
        target_direction = direction
        if not account.flat:
            if forced_exit:
                reason = "leg_failure"
            elif state["equity"] <= 0:
                reason = "insolvency"
            elif state["available_cash"] < 0:
                reason = "margin_call"
            elif state["gross_exposure"] > config.max_gross_leverage * state["equity"]:
                reason = "gross_limit"
            elif state["equity"] <= entry_equity * (1 - config.max_loss_fraction):
                reason = "max_loss"
            elif not coint_valid:
                reason = "cointegration_failure"
            elif i - opened_at + 1 >= config.max_holding_sessions:
                reason = "max_holding"
            elif i == len(dates) - 2:
                reason = "window_end"
            if reason:
                action, target_direction = "exit", 0
            elif not executed:
                action, target_direction = mean_reversion_signal(zscore, direction, config)
                if action == "exit":
                    reason = "mean_reversion"
        elif not executed and coint_valid and i < len(dates) - 2:
            action, target_direction = mean_reversion_signal(zscore, 0, config)
            if action == "enter":
                reason = "zscore_entry"
        if action != "hold" and i < len(dates) - 1:
            pending = action, target_direction, day, reason
        daily_cost = account.transaction_cost - cost_before
        nav.append(dict(date=day, **state, strategy_nav=state["equity"] / config.initial_cash,
                        strategy_return=state["equity"] / previous_equity - 1,
                        benchmark_nav=1.0, benchmark_return=0.0, transaction_cost=daily_cost,
                        borrow_cost=daily_borrow, holdings=len(account.longs.symbols()) + len(account.shorts.symbols())))
        positions.extend(account.positions(day, close_prices))
        signals.append(dict(date=day, spread=float(spread.loc[day]), spread_mean=float(means.loc[day]),
                            spread_std=float(stds.loc[day]), zscore=zscore, hedge_ratio=model.hedge_ratio,
                            action=action, direction=direction, reason=reason,
                            coint_pvalue=float(latest_pvalue), coint_valid=coint_valid,
                            unresolved_exposure=forced_exit))
        previous_equity = state["equity"]
        if previous_equity <= 0 and i < len(dates) - 1:
            raise ValueError("insolvent software account: no unsecured financing is available")
    trade_columns = ["execution_date", "decision_date", "symbol", "action", "shares", "price", "trade_notional", "transaction_cost", "reason"]
    trades = pd.DataFrame(account.trades, columns=trade_columns).astype(
        {"shares": "int64", "price": float, "trade_notional": float, "transaction_cost": float})
    return PairSimulation(pd.DataFrame(nav), trades,
                          pd.DataFrame(positions, columns=["date", "symbol", "side", "shares", "signed_shares", "market_value"]),
                          pd.DataFrame(account.failures, columns=["date", "symbol", "action", "requested", "filled", "reason"]),
                          pd.DataFrame(signals))


def read_pair_panels(hub, source, symbols, *, start, end, knowledge_as_of=None, close_field="raw_close"):
    """Use DataHub canonical bars without joining future observations into gaps."""
    opens, closes, receipts = {}, {}, []
    for symbol in symbols:
        result = hub.read(source, "daily_bars", symbol=symbol, start=start, end=end, knowledge_as_of=knowledge_as_of)
        frame = result.data.set_index("date")
        opens[symbol], closes[symbol] = frame["raw_open"], frame[close_field]
        receipts.append(result)
    return pd.DataFrame(opens).sort_index(), pd.DataFrame(closes).sort_index(), receipts


@dataclass
class PairResearchResult:
    nav: pd.DataFrame
    selections: pd.DataFrame
    folds: pd.DataFrame
    trades: pd.DataFrame
    positions: pd.DataFrame
    failures: pd.DataFrame
    signals: pd.DataFrame
    validation: pd.DataFrame
    evaluation: object
    metadata: dict


def run_pair_research(opens, closes, *, selection: SelectionConfig, strategy: PairTradingConfig,
                      splitter: ExpandingWalkForwardSplitter, borrow: dict[str, BorrowTerms],
                      entry_grid=(1.5, 2.0, 2.5), source_kind="SYNTHETIC", fill_scenarios=None,
                      statistics_only=False):
    """Choose one pair by training EG p-value; choose entry threshold on validation.

    Validation objective is terminal net equity, ties follow ascending threshold.
    Validation windows start independently funded and flat. OOS windows cannot
    overlap; settled equity funds the next fold without cash injection. Unresolved
    final positions remain marked and stop later folds.
    """
    _validate_panel(opens, closes)
    if statistics_only:
        from factorlab.strategies.pairs_research import statistical_research
        return statistical_research(closes, selection=selection, strategy=strategy,
                                    splitter=splitter, entry_grid=entry_grid)
    if source_kind not in {"SYNTHETIC", "REAL_CANDIDATE"}:
        raise ValueError("invalid source_kind")
    grid = tuple(sorted(set(entry_grid)))
    if not grid:
        raise ValueError("entry_grid must not be empty")
    for threshold in grid:
        replace(strategy, entry_z=threshold)
    if splitter.step_months < splitter.test_months:
        raise ValueError("overlapping OOS folds cannot be chained")
    horizon = strategy.max_holding_sessions + 2
    calendar = closes.index
    labels = pd.DataFrame(dict(date=calendar, entry_date=pd.Series(calendar).shift(-1),
                               exit_date=pd.Series(calendar).shift(-horizon)))
    chunks = {key: [] for key in ("nav", "trades", "positions", "failures", "signals")}
    selections, fold_rows, validation_rows = [], [], []
    running_cash = strategy.initial_cash
    for fold in splitter.split(labels, calendar=calendar):
        cfg = replace(selection, train_start=fold.train_dates[0].isoformat(), train_end=fold.train_dates[-1].isoformat())
        models, diagnostics = select_pairs(closes, cfg)
        diagnostics.insert(0, "fold", fold.fold)
        selections.append(diagnostics)
        model = models[0] if models else None
        chosen = strategy
        if model is not None:
            candidates = []
            for threshold in grid:
                candidate = replace(strategy, entry_z=threshold)
                simulated = simulate_pair(opens, closes, fold.validation_dates, model, candidate, borrow)
                equity = float(simulated.nav.equity.iloc[-1])
                unresolved = float(simulated.nav.gross_exposure.iloc[-1]) != 0
                validation_rows.append(dict(fold=fold.fold, entry_z=threshold, terminal_equity=equity,
                                            eligible=not unresolved))
                if not unresolved:
                    candidates.append((equity, -threshold, candidate))
            if not candidates:
                raise ValueError("all validation configurations end with unresolved exposure")
            chosen = max(candidates, key=lambda item: item[:2])[2]
            simulated = simulate_pair(opens, closes, fold.test_dates, model, replace(chosen, initial_cash=running_cash),
                                      borrow, fill_scenarios=fill_scenarios)
        else:
            simulated = PairSimulation(pd.DataFrame(dict(date=fold.test_dates, equity=running_cash,
                cash=running_cash, restricted_short_proceeds=0., margin=0., available_cash=running_cash,
                long_value=0., short_liability=0., net_exposure=0., gross_exposure=0., leverage=0., long_pnl=0., short_pnl=0.,
                cumulative_transaction_cost=0., cumulative_borrow_cost=0., strategy_nav=1., strategy_return=0.,
                benchmark_nav=1., benchmark_return=0., transaction_cost=0., borrow_cost=0., holdings=0)),
                pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        unresolved = simulated.nav.gross_exposure.iloc[-1] != 0
        for key in chunks:
            frame = getattr(simulated, key).copy()
            frame["fold"] = fold.fold
            chunks[key].append(frame)
        fold_rows.append(dict(fold=fold.fold, initial_cash=running_cash, train_start=cfg.train_start, train_end=cfg.train_end,
                              validation_start=fold.validation_dates[0], validation_end=fold.validation_dates[-1],
                              test_start=fold.test_dates[0], test_end=fold.test_dates[-1],
                              entry_z=chosen.entry_z, selected_pair=None if model is None else model.y_symbol + "/" + model.x_symbol,
                              model=None if model is None else asdict(model),
                              purged_sessions=len(fold.purged_dates), embargoed_sessions=len(fold.embargoed_dates),
                              train_overlap_rows_removed=fold.audit.train_overlap_rows_removed,
                              validation_overlap_rows_removed=fold.audit.validation_overlap_rows_removed,
                              unresolved_final_exposure=bool(unresolved)))
        running_cash = float(simulated.nav.equity.iloc[-1])
        if unresolved:
            break
    if not fold_rows:
        raise ValueError("calendar does not contain a complete walk-forward fold")
    combined = {key: pd.concat(frames, ignore_index=True) for key, frames in chunks.items()}
    nav = combined["nav"]
    for column in ("long_pnl", "short_pnl", "cumulative_transaction_cost", "cumulative_borrow_cost"):
        offsets = nav.groupby("fold", sort=False)[column].last().shift(fill_value=0).cumsum()
        nav[column] += nav.fold.map(offsets)
    nav["strategy_nav"] = (1 + nav.strategy_return).cumprod()
    context = EvaluationContext(dataset_id="pairs-offline", universe_id="/".join(closes.columns),
        label="pair_pnl", label_id="marked-equity", benchmark_id="non-interest-bearing-cash",
        calendar=tuple(nav.date.dt.strftime("%Y-%m-%d")), holding_days=strategy.max_holding_sessions,
        source_kind=source_kind)
    trades = combined["trades"]
    if "trade_notional" not in trades:
        trades = pd.DataFrame(columns=["execution_date", "trade_notional", "transaction_cost"])
        trades = trades.astype({"trade_notional": float, "transaction_cost": float})
    evaluation = ResearchEvaluation(context).portfolio("pairs-oos", nav, trades=trades)
    metadata = dict(evidence=["NO LIVE MONEY", "PIT_UNVERIFIED", "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED"],
                    result_kind="SYNTHETIC / SOFTWARE_ONLY" if source_kind == "SYNTHETIC" else "REAL_CANDIDATE / SOFTWARE_ONLY",
                    actual_tradability="UNKNOWN", borrow_evidence="ASSUMED_NOT_HISTORICAL",
                    fold_continuation="BLOCKED_UNRESOLVED_EXPOSURE" if unresolved else "COMPLETED",
                    parameters_frozen="OLS fixed per fold; lagged rolling Z-score; trailing EG diagnostics only",
                    borrow_assumptions={key: asdict(value) for key, value in borrow.items()})
    return PairResearchResult(nav, pd.concat(selections, ignore_index=True), pd.DataFrame(fold_rows), trades,
        combined["positions"], combined["failures"], combined["signals"], pd.DataFrame(validation_rows), evaluation, metadata)
