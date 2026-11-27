"""Configuration contracts for FactorLab external strategy independent validation."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any

DEFAULT_TICKERS: tuple[str, ...] = ("AAPL", "SNDK", "BA", "C", "JPM", "GE", "TQQQ", "QQQ")


@dataclass(frozen=True)
class SourcePackageConfig:
    """Paths and provenance metadata for external strategy delivery packages."""

    v179_v182_dir: str
    v69_dir: str
    qlib_python: str | None = None
    gpu_python: str | None = None
    p10_03_native_commit: str | None = None
    binance_nvda_bars: str | None = None
    binance_nvda_funding: str | None = None


@dataclass(frozen=True)
class WindowSpec:
    """One named historical evaluation window and its frozen market source."""

    name: str
    start: str
    end: str
    market_subdir: str
    benchmark_file: str
    window_role: str  # development | repeated_target | historical_challenge | long_overlap | fresh_oos
    default_omit_sndk: bool = False


@dataclass(frozen=True)
class StressScenarioSpec:
    """Independent execution robustness scenario specification."""

    name: str
    fee_rate: float = 0.001
    slippage_bps: float = 0.0
    volume_cap_ratio: float = 0.001
    fill_ratio: float = 1.0
    execution_delay_days: int = 0
    initial_capital: float = 300_000.0
    omit_symbols: tuple[str, ...] = ()
    open_price_mode: str = "float64"  # float64 | qlib_float32 | open_high_mid
    description: str = ""


@dataclass(frozen=True)
class MonteCarloValidationConfig:
    """Configuration for Tier-1 fast portfolio MC and Tier-2 full dynamic strategy MC."""

    tier1_path_scales: tuple[int, ...] = (1_000, 10_000, 50_000)
    tier2_path_scales: tuple[int, ...] = (256, 512, 1_024, 2_048, 4_096)
    tier2_max_scale: int = 10_000
    horizon_days: int = 252
    warmup_days: int = 252
    block_size: int = 20
    seeds: tuple[int, ...] = (1729, 2718, 31415, 65537, 104729)
    models: tuple[str, ...] = (
        "corr_gbm",
        "sync_mbb",
        "regime_cond_bootstrap",
        "down_jump_stress",
        "corr_shock_stress",
        "student_t_garch",
    )
    calibration_windows: tuple[str, ...] = ("target_4y", "history_10y", "history_20y")
    drawdown_thresholds: tuple[float, ...] = (0.10, 0.15, 0.20)
    bootstrap_resamples: int = 999
    max_workers: int = 12
    precision_target_cvar95_halfwidth_pp: float = 0.50
    precision_target_cvar99_halfwidth_pp: float = 1.00
    precision_target_seed_sd_pp: float = 0.25


@dataclass(frozen=True)
class SharedAccountValidationConfig:
    """Configuration for Phase 6 multi-strategy shared account risk study."""

    alpaca_initial_capital_usd: float = 300_000.0
    alpaca_fee_rate: float = 0.001
    alpaca_gross_cap: float = 0.90
    alpaca_max_symbol_weight: float = 0.35
    alpaca_reserve_fee_buffer: float = 0.001
    alpaca_window: str = "target"
    alpaca_long_window: str = "twenty"
    binance_initial_capital_usdt: float = 10.0
    allocation_schemes: tuple[str, ...] = (
        "equal_weight_3",
        "risk_budget_inv_vol_3",
        "fixed_20_40_40",
        "pair_v69_v179_50_50",
        "pair_v69_v182_50_50",
        "pair_v179_v182_50_50",
        "dynamic_regime_research_only",
    )


@dataclass(frozen=True)
class ExternalValidationConfig:
    """Top-level reproducible configuration for external strategy independent validation."""

    schema: str = "FACTORLAB_EXTERNAL_VALIDATION_V1"
    study_id: str = "p10-04-external-validation"
    strategies: tuple[str, ...] = ("v69", "v179", "v182")
    tickers: tuple[str, ...] = DEFAULT_TICKERS
    initial_capital_usd: float = 300_000.0
    gross_cap: float = 0.90
    v69_reference_cap: float = 0.375
    normal_fee_rate: float = 0.001
    stress_fee_rate: float = 0.003
    rebalance_deadband: float = 0.005
    volume_cap_ratio: float = 0.001
    packages: SourcePackageConfig = field(
        default_factory=lambda: SourcePackageConfig(
            v179_v182_dir="",
            v69_dir="",
        )
    )
    windows: tuple[WindowSpec, ...] = (
        WindowSpec("target", "2023-01-01", "2026-11-23", "fixtures/target", "fixtures/target/benchmark.parquet", "repeated_target", False),
        WindowSpec("dev_a", "2013-01-01", "2018-12-31", "fixtures/history", "fixtures/history/dev_benchmark.parquet", "development", True),
        WindowSpec("dev_b", "2019-01-01", "2022-12-31", "fixtures/history", "fixtures/history/dev_benchmark.parquet", "development", True),
        WindowSpec("dev_crisis", "2006-11-20", "2012-12-31", "fixtures/history", "fixtures/history/long_benchmark.parquet", "development", True),
        WindowSpec("twenty", "2006-11-20", "2026-11-20", "fixtures/history", "fixtures/history/long_benchmark.parquet", "long_overlap", False),
        WindowSpec("challenge", "2001-01-02", "2006-11-17", "fixtures/history", "fixtures/history/long_benchmark.parquet", "historical_challenge", False),
    )
    monte_carlo: MonteCarloValidationConfig = field(default_factory=MonteCarloValidationConfig)
    shared_account: SharedAccountValidationConfig = field(default_factory=SharedAccountValidationConfig)
    champion_incumbent: str = "v69"
    oos_eligibility: str = "HISTORICAL_REUSE_PSEUDO_OOS"

    def window_map(self) -> dict[str, WindowSpec]:
        return {w.name: w for w in self.windows}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def save_validation_config(config: ExternalValidationConfig, path: Path | str) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dest


def load_validation_config(path: Path | str) -> ExternalValidationConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") != "FACTORLAB_EXTERNAL_VALIDATION_V1":
        raise ValueError("Unsupported external validation configuration schema")
    packages = SourcePackageConfig(**raw["packages"])
    windows = tuple(WindowSpec(**w) for w in raw["windows"])
    mc_raw = dict(raw["monte_carlo"])
    for key in ("tier1_path_scales", "tier2_path_scales", "seeds", "models", "calibration_windows", "drawdown_thresholds"):
        if key in mc_raw and isinstance(mc_raw[key], list):
            mc_raw[key] = tuple(mc_raw[key])
    mc = MonteCarloValidationConfig(**mc_raw)
    sa_raw = dict(raw["shared_account"])
    if "allocation_schemes" in sa_raw and isinstance(sa_raw["allocation_schemes"], list):
        sa_raw["allocation_schemes"] = tuple(sa_raw["allocation_schemes"])
    sa = SharedAccountValidationConfig(**sa_raw)
    return ExternalValidationConfig(
        schema=raw["schema"],
        study_id=raw["study_id"],
        strategies=tuple(raw["strategies"]),
        tickers=tuple(raw["tickers"]),
        initial_capital_usd=float(raw["initial_capital_usd"]),
        gross_cap=float(raw["gross_cap"]),
        v69_reference_cap=float(raw["v69_reference_cap"]),
        normal_fee_rate=float(raw["normal_fee_rate"]),
        stress_fee_rate=float(raw["stress_fee_rate"]),
        rebalance_deadband=float(raw["rebalance_deadband"]),
        volume_cap_ratio=float(raw["volume_cap_ratio"]),
        packages=packages,
        windows=windows,
        monte_carlo=mc,
        shared_account=sa,
        champion_incumbent=raw.get("champion_incumbent", "v69"),
        oos_eligibility=raw.get("oos_eligibility", "HISTORICAL_REUSE_PSEUDO_OOS"),
    )
