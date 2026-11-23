"""Predeclared, paired Champion/Challenger selection; no live activation."""
from dataclasses import asdict, dataclass
import math

import numpy as np
import pandas as pd

from factorlab.metrics.robustness import RobustnessOptions, mean_uncertainty
from factorlab.research.monte_carlo import _loss_tail


@dataclass(frozen=True)
class SelectionPolicy:
    # Default objective: annual mean net return minus risk_aversion * annual vol.
    objective: str = "risk_adjusted_net_return"
    risk_aversion: float = 1.0
    periods_per_year: int = 252
    max_drawdown: float = .20
    max_daily_cvar: float = .03
    cvar_confidence: float = .95
    participation_limit: float = .01
    min_capacity_cny: float = 100000.
    min_net_return: float = 0.
    min_score_improvement: float = .02
    min_annual_net_improvement: float = .01
    min_oos_days: int = 120
    min_tail_observations: int = 5
    min_positive_blocks: float = .6
    cooldown_days: int = 30
    confidence: float = .95
    block_days: int = 10
    resamples: int = 2000
    seed: int = 42
    max_candidates: int = 32
    max_finalists: int = 4
    volume_lookback: int = 20

    def __post_init__(self):
        if self.objective not in {"risk_adjusted_net_return", "net_return", "sharpe"}:
            raise ValueError("unknown objective")
        integers = {"periods_per_year": 1, "min_oos_days": 2, "min_tail_observations": 1,
                    "cooldown_days": 0, "block_days": 1, "resamples": 100,
                    "seed": 0, "max_candidates": 1, "max_finalists": 1, "volume_lookback": 2}
        for name, minimum in integers.items():
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(name + " has an invalid integer value")
        for name, value in asdict(self).items():
            if name == "objective" or name in integers:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(name + " must be finite and nonnegative")
        if (not 0 < self.confidence < 1 or not 0 < self.cvar_confidence < 1
                or not 0 < self.participation_limit <= 1 or self.max_drawdown > 1
                or self.min_positive_blocks > 1 or self.seed >= 2**32
                or self.min_oos_days < 2*self.block_days
                or self.max_finalists > self.max_candidates):
            raise ValueError("invalid selection probability, sample size or search bound")


def objective(returns, policy):
    r = np.asarray(returns, dtype=float)
    mean = float(r.mean()*policy.periods_per_year)
    vol = float(r.std(ddof=1)*math.sqrt(policy.periods_per_year))
    if policy.objective == "sharpe":
        return mean/vol if vol > 1e-12 else None
    if policy.objective == "net_return":
        return mean
    return mean - policy.risk_aversion*vol


def risk_metrics(result, bars, capital, policy):
    """Empirical daily loss CVaR, origin-inclusive drawdown, conservative capacity.

    Capacity uses intended entry weights AND executed orders against trailing
    volume known at signal close. It is an admission gate, not an impact model.
    """
    nav, trades = result["strategy"]["nav"], result["strategy"]["trades"]
    r = nav.strategy_return.to_numpy(dtype=float)
    if len(r) < 2 or not np.isfinite(r).all() or (r <= -1).any():
        raise ValueError("invalid complete net-return series")
    values = np.r_[1., np.cumprod(1+r)]
    _, cvar = _loss_tail(-r, policy.cvar_confidence)
    market = bars.set_index("date")
    volume = market.volume.rolling(policy.volume_lookback,
                                   min_periods=policy.volume_lookback).mean()
    signals = result["signals"]
    intended = signals.loc[signals.target_weight.gt(0)]
    demands = []
    # Avoid using the day's realised execution volume to grant capacity.
    for row in intended.itertuples():
        denominator = volume.get(row.date, np.nan)
        demand = capital * row.target_weight / market.loc[row.date, "raw_close"]
        demands.append(demand/denominator if denominator > 0 else math.inf)
    for row in trades.itertuples():
        denominator = volume.get(row.signal_date, np.nan)
        demands.append(row.executed_shares/denominator if denominator > 0 else math.inf)
    participation = max(demands, default=0.)
    capacity = capital*policy.participation_limit/participation if participation > 0 else None
    # No-order capacity is unknown, never infinite certified capacity.
    return dict(score=objective(r, policy), net_return=float(values[-1]-1),
                annual_mean_net_return=float(r.mean()*policy.periods_per_year),
                annual_volatility=float(r.std(ddof=1)*math.sqrt(policy.periods_per_year)),
                max_drawdown=float(-np.min(values/np.maximum.accumulate(values)-1)),
                daily_cvar=max(0., cvar), tail_observations=(1-policy.cvar_confidence)*len(r),
                capacity_cny=capacity if capacity is not None and math.isfinite(capacity) else None,
                max_participation=participation if math.isfinite(participation) else None,
                days=len(r), trade_count=len(trades),
                transaction_cost=float(trades.transaction_cost.sum()))


def qualification(metrics, policy):
    reasons = []
    for name, limit in (("max_drawdown", policy.max_drawdown), ("daily_cvar", policy.max_daily_cvar),
                        ("max_participation", policy.participation_limit)):
        if metrics[name] is None or metrics[name] > limit:
            reasons.append(name + "_breach_or_unknown")
    if metrics["capacity_cny"] is None or metrics["capacity_cny"] < policy.min_capacity_cny:
        reasons.append("capacity_insufficient_or_unknown")
    if metrics["score"] is None:
        reasons.append("objective_undefined")
    if metrics["net_return"] <= policy.min_net_return:
        reasons.append("net_return_below_floor")
    return reasons


def paired_evidence(challenger, champion, policy, family_size):
    """HAC and block-bootstrap mean differences on exactly aligned OOS days.

    Both two-sided intervals use Bonferroni confidence over the full searched
    family. This is conservative within a campaign, not lifetime error control.
    """
    if not challenger.date.equals(champion.date):
        raise ValueError("paired OOS calendars differ")
    difference = challenger.strategy_return.to_numpy() - champion.strategy_return.to_numpy()
    options = RobustnessOptions(confidence_level=1-(1-policy.confidence)/family_size,
        hac_lags=policy.block_days-1, bootstrap_block_days=policy.block_days,
        bootstrap_repetitions=policy.resamples, min_valid_days=policy.min_oos_days,
        min_coverage=1., holding_days=1, random_seed=policy.seed)
    raw = mean_uncertainty(difference, signal_dates=len(difference), options=options,
                           identity="champion_paired_net_returns")
    evidence = {k: (None if isinstance(v, (float, np.floating)) and not np.isfinite(v) else v)
                for k, v in raw.items()}
    blocks = [difference[i:i+policy.block_days] for i in range(0, len(difference), policy.block_days)
              if i+policy.block_days <= len(difference)]
    evidence.update(family_size=family_size, confidence=options.confidence_level,
        annual_mean_improvement=float(difference.mean()*policy.periods_per_year),
        positive_block_fraction=float(np.mean([b.mean() > 0 for b in blocks])) if blocks else 0.,
        interpretation="conditional historical OOS evidence; weak dependence and stability assumed; no profit guarantee")
    # Nonlinear objectives need paired block resampling of both return streams.
    # Keep memory bounded to a single resample and preserve within-day pairing.
    scores = []
    rng = np.random.default_rng(policy.seed)
    n = len(difference)
    left, right = challenger.strategy_return.to_numpy(), champion.strategy_return.to_numpy()
    if n >= 2*policy.block_days:
        for _ in range(policy.resamples):
            starts = rng.integers(0, n-policy.block_days+1, size=math.ceil(n/policy.block_days))
            positions = (starts[:, None]+np.arange(policy.block_days)).ravel()[:n]
            a, b = objective(left[positions], policy), objective(right[positions], policy)
            if a is not None and b is not None:
                scores.append(a-b)
    alpha = (1-options.confidence_level)/2
    interval = np.quantile(scores, [alpha, 1-alpha]) if len(scores) == policy.resamples else [None, None]
    evidence.update(score_ci_lower=None if interval[0] is None else float(interval[0]),
                    score_ci_upper=None if interval[1] is None else float(interval[1]),
                    bootstrap_tail_replicates=policy.resamples*alpha,
                    score_interval_method="paired_moving_block_percentile_bootstrap")
    return evidence


class ChampionSelector:
    def __init__(self, policy):
        self.policy = policy

    def select(self, incumbent, challengers, *, family_size, cooldown_elapsed=True):
        """Inputs contain candidate identity, risk metrics and net OOS NAV.

        Return the incumbent verbatim if no challenger clears every gate.
        """
        p = self.policy
        if type(family_size) is not int or family_size < max(1, len(challengers)):
            raise ValueError("family_size must cover every tested challenger")
        identities = [c["candidate"]["candidate_id"] for c in [incumbent, *challengers]]
        if len(identities) != len(set(identities)):
            raise ValueError("unique Champion and Challenger identities required")
        decisions = []
        for candidate in challengers:
            m = candidate["metrics"]
            reasons = qualification(m, p)
            e = paired_evidence(candidate["nav"], incumbent["nav"], p, family_size)
            if m["days"] < p.min_oos_days or m["tail_observations"]+1e-9 < p.min_tail_observations:
                reasons.append("insufficient_oos_or_tail_observations")
            baseline_score = incumbent["metrics"]["score"]
            if baseline_score is None or m["score"] is None or m["score"]-baseline_score < p.min_score_improvement:
                reasons.append("score_improvement_insufficient")
            threshold = p.min_annual_net_improvement/p.periods_per_year
            if (e["status"] != "OK" or e["bootstrap_status"] != "OK"
                    or any(e.get(k) is None or e[k] <= threshold
                           for k in ("hac_ci_lower", "bootstrap_ci_lower"))):
                reasons.append("oos_advantage_statistically_uncertain")
            if e["score_ci_lower"] is None or e["score_ci_lower"] <= p.min_score_improvement:
                reasons.append("risk_adjusted_advantage_statistically_uncertain")
            if e["bootstrap_tail_replicates"] < 5:
                reasons.append("insufficient_bootstrap_tail_resolution")
            if e["positive_block_fraction"] < p.min_positive_blocks:
                reasons.append("oos_block_consistency_insufficient")
            if not cooldown_elapsed:
                reasons.append("promotion_cooldown")
            decisions.append(dict(candidate_id=candidate["candidate"]["candidate_id"],
                                  eligible=not reasons, reasons=reasons, evidence=e))
        eligible = {d["candidate_id"] for d in decisions if d["eligible"]}
        ranked = sorted((c for c in challengers if c["candidate"]["candidate_id"] in eligible),
                        key=lambda c: (-c["metrics"]["score"], c["candidate"]["candidate_id"]))
        winner = ranked[0] if ranked else incumbent
        return dict(selected=winner["candidate"], promoted=bool(ranked), decisions=decisions,
                    reason="qualified_oos_improvement" if ranked else "retain_champion_no_qualified_challenger")
