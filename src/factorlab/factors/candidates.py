"""Versioned, close-known candidate families; parameter variants are not independent alpha."""

from dataclasses import asdict

from factorlab.exploratory.factor_discovery.contracts import Node
from factorlab.exploratory.factor_discovery.engine import compile_expression
from factorlab.factors.registry_v2 import FactorDefinition

CATALOG_VERSION = "p311-v1"
WINDOWS = (5, 10, 20, 60)


def _field(name):
    return Node("Field", value=name)


def _op(name, *args, param=None):
    return Node(name, args, param=param)


def candidate_catalog():
    """Return deterministic definitions and their economic-family metadata."""
    close, high, low, opening = map(_field, ("adj_close", "adj_high", "adj_low", "adj_open"))
    ret, volume, amount, turnover = map(_field, ("RET1", "volume", "amount", "turnover"))
    one = Node("Const", value=1)
    rows = []
    for window in WINDOWS:
        mean = lambda x: _op("RollingMean", x, param=window)
        std = lambda x: _op("RollingStd", x, param=window)
        ratio = lambda x, y: _op("SafeDiv", x, y)
        change = lambda x, y: _op("Sub", ratio(x, y), one)
        families = (
            ("momentum", "momentum_reversal", change(close, _op("Lag", close, param=window))),
            ("return_mean", "momentum_reversal", mean(ret)),
            ("price_deviation", "trend", change(close, mean(close))),
            ("mean_slope", "trend", change(mean(close), _op("Lag", mean(close), param=1))),
            ("return_volatility", "volatility", std(ret)),
            ("price_dispersion", "volatility", ratio(std(close), mean(close))),
            ("relative_range", "volatility", mean(ratio(_op("Sub", high, low), close))),
            ("intraday_return", "momentum_reversal", mean(change(close, opening))),
            ("volume_surprise", "volume_price", change(volume, mean(volume))),
            ("amount_surprise", "liquidity_proxy", change(amount, mean(amount))),
            ("turnover_level", "liquidity_proxy", mean(turnover)),
            ("turnover_dispersion", "liquidity_proxy", std(turnover)),
            ("volume_dispersion", "volume_price", ratio(std(volume), mean(volume))),
            ("amount_dispersion", "liquidity_proxy", ratio(std(amount), mean(amount))),
            ("volume_growth", "volume_price", change(volume, _op("Lag", volume, param=window))),
            ("turnover_surprise", "liquidity_proxy", change(turnover, mean(turnover))),
        )
        for family, category, node in families:
            compiled = compile_expression(node)
            definition = FactorDefinition.from_expression("C_" + family + "_" + str(window), CATALOG_VERSION, compiled)
            rows.append({"definition": definition, "family": family, "category": category,
                         "window": window, "formula": compiled.expression,
                         "structural_relation": family + ": window variants; cross-family correlation unverified"})
    return tuple(sorted(rows, key=lambda row: row["definition"].factor_id))


def catalog_records(available_fields=None):
    """Describe availability from supplied fields, without synthesizing inputs."""
    return [{**asdict(row["definition"]), **{k: v for k, v in row.items() if k != "definition"},
             "status": "AVAILABLE" if available_fields is None or set(row["definition"].input_fields) <= set(available_fields)
             else "NOT_AVAILABLE"} for row in candidate_catalog()]


def register_candidates(registry):
    return registry.register_many(tuple(row["definition"] for row in candidate_catalog()))
