"""Manual numerical, adversarial AST, calendar, isolation and budget checks."""

from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from qbot.research.exploratory.factor_discovery.contracts import (
    DiscoveryConfig,
    DiscoveryError,
    ErrorCode,
    Node,
)
from qbot.research.exploratory.factor_discovery.engine import (
    ExpressionEngine,
    compile_expression,
    normalize_ast,
)
from qbot.research.exploratory.factor_discovery.synthetic import make_synthetic_data


def f(name):
    return Node("Field", value=name)


def op(name, *args, n=None):
    return Node(name, args, param=n)


@pytest.fixture
def bars():
    frame, _, dates = make_synthetic_data(stock_count=2, sessions=20)
    for symbol, offset in (("000001.SZ", 0), ("000002.SZ", 100)):
        mask = frame["symbol"].eq(symbol)
        frame.loc[mask, "volume"] = np.arange(1, 21, dtype=float) + offset
    return frame, dates


def test_lag_rolling_and_std_manual_per_security_and_original_key_alignment(bars):
    frame, dates = bars
    shuffled = frame.sample(frac=1, random_state=9)
    engine = ExpressionEngine(shuffled, dates)
    for expression, expected in (
        (op("Lag", f("volume"), n=1), lambda a: pd.Series(a).shift(1)),
        (op("Delta", f("volume"), n=5), lambda a: pd.Series(a).diff(5)),
        (op("RollingMean", f("volume"), n=5), lambda a: pd.Series(a).rolling(5).mean()),
        (
            op("RollingStd", f("volume"), n=5),
            lambda a: pd.Series(a).rolling(5).std(ddof=1),
        ),
    ):
        result = engine.compute(
            compile_expression(expression, require_dimensionless=False)
        )
        assert result.status == "SUCCESS"
        pd.testing.assert_index_equal(result.values.index, shuffled.index)
        for symbol, offset in (("000001.SZ", 0), ("000002.SZ", 100)):
            positions = frame.loc[frame["symbol"].eq(symbol)].index
            np.testing.assert_allclose(
                result.values.loc[positions],
                expected(np.arange(1, 21) + offset),
                equal_nan=True,
            )


def test_calendar_gap_not_compressed_or_forward_filled(bars):
    frame, dates = bars
    absent = frame.index[frame["symbol"].eq("000001.SZ") & frame["date"].eq(dates[5])][
        0
    ]
    frame = frame.drop(index=absent)
    engine = ExpressionEngine(frame, dates)
    lag = engine.compute(
        compile_expression(op("Lag", f("volume"), n=1), require_dimensionless=False)
    ).values
    mean = engine.compute(
        compile_expression(
            op("RollingMean", f("volume"), n=5), require_dimensionless=False
        )
    ).values
    for day in range(6, 10):
        index = frame.index[
            frame["symbol"].eq("000001.SZ") & frame["date"].eq(dates[day])
        ][0]
        assert pd.isna(mean.loc[index])
        if day == 6:
            assert pd.isna(lag.loc[index])
    dense_gap = engine.panel.loc[
        engine.panel["symbol"].eq("000001.SZ") & engine.panel["date"].eq(dates[5])
    ]
    assert dense_gap["volume"].isna().all()


def test_every_candidate_invariant_to_future_prices_with_no_stock_crosstalk():
    from qbot.research.exploratory.factor_discovery.generator import generate_candidates

    frame, _, dates = make_synthetic_data(stock_count=3, sessions=180)
    altered = frame.copy()
    mask = (altered["date"] > dates[140]) & altered["symbol"].eq("000002.SZ")
    for name in altered.columns:
        if name not in ("date", "symbol", "adj_factor"):
            altered.loc[mask, name] *= 9
    first, second = ExpressionEngine(frame, dates), ExpressionEngine(altered, dates)
    for record in generate_candidates().records:
        if record["status"] != "GENERATED":
            continue
        a, b = (
            first.compute(record["compiled"]).values,
            second.compute(record["compiled"]).values,
        )
        unchanged = (frame["date"] <= dates[140]) | ~frame["symbol"].eq("000002.SZ")
        pd.testing.assert_series_equal(
            a.loc[unchanged], b.loc[unchanged], check_exact=True
        )


def test_ret1_derived_from_past_close_and_ignores_supplied_target_and_ret1(bars):
    frame, dates = bars
    frame["RET1"] = 999999
    frame["EXCESS_RET5"] = "malicious arbitrary object"
    result = ExpressionEngine(frame, dates).compute(compile_expression(f("RET1")))
    expected = frame.groupby("symbol")["adj_close"].pct_change(fill_method=None)
    np.testing.assert_allclose(result.values, expected, equal_nan=True)


@pytest.mark.parametrize(
    "name",
    [
        "EXCESS_RET5",
        "label",
        "STOCK_FWD_RET5",
        "model_prediction",
        "future_market_state",
        "close",
        "__class__",
    ],
)
def test_hidden_or_ambiguous_fields_rejected(name):
    with pytest.raises(DiscoveryError) as raised:
        compile_expression(f(name))
    assert raised.value.code == ErrorCode.UNAUTHORIZED_FIELD


@pytest.mark.parametrize(
    "node",
    [
        "__import__('os').system('dir')",
        {"op": "Field", "value": "volume"},
        Node("Call", (f("volume"),)),
        Node("Field", (f("volume"),), "volume"),
        Node("Add", [f("turnover"), f("turnover")]),
        op("Lag", f("RET1"), n=-1),
        op("Lag", f("RET1"), n=0),
        op("RollingMean", f("RET1"), n=3),
        op("Lag", f("RET1"), n=True),
        Node("Const", value=np.inf),
        Node("Const", value=True),
        Node("Const", value=1.01),
        Node("Field", value="RET1", param=1),
        op("Add", f("turnover")),
    ],
)
def test_invalid_ast_or_execution_payload_never_accepted(node):
    with pytest.raises(DiscoveryError):
        compile_expression(node)


def test_cycles_and_oversize_rejected_without_recursion_exhaustion():
    cyclic = Node("Lag", param=1)
    object.__setattr__(cyclic, "args", (cyclic,))
    with pytest.raises(DiscoveryError, match="cycle"):
        compile_expression(cyclic)
    node = f("RET1")
    for _ in range(10000):
        node = op("Lag", node, n=1)
    with pytest.raises(DiscoveryError, match="COMPLEXITY_LIMIT"):
        compile_expression(node)


def test_dimensions_and_price_basis_even_after_cancelled_ratios():
    ratio = compile_expression(op("SafeDiv", f("adj_close"), f("adj_open")))
    assert ratio.dimension.is_dimensionless
    assert ratio.dimension.as_dict()["price_basis"] == "HFQ"
    cash = compile_expression(
        op("SafeDiv", f("amount"), f("volume")), require_dimensionless=False
    )
    raw = compile_expression(f("raw_close"), require_dimensionless=False)
    assert cash.dimension.units == raw.dimension.units
    for illegal in (
        op("Add", f("volume"), f("amount")),
        op("SafeDiv", f("raw_close"), f("adj_close")),
        op(
            "Mul",
            op("SafeDiv", f("raw_close"), f("raw_open")),
            op("SafeDiv", f("adj_close"), f("adj_open")),
        ),
        op("Add", f("RET1"), f("turnover")),
    ):
        with pytest.raises(DiscoveryError):
            compile_expression(illegal)
    legal = compile_expression(
        op("SafeDiv", op("SafeDiv", f("amount"), f("volume")), f("raw_close"))
    )
    assert legal.dimension.is_dimensionless
    with pytest.raises(DiscoveryError, match="NON_DIMENSIONLESS"):
        compile_expression(f("volume"))


def test_safe_commutativity_without_associativity_or_subtraction_division_rewrites():
    a, b, c = f("turnover"), Node("Const", value=1), Node("Const", value=100)
    for name in ("Add", "Mul"):
        x, y = compile_expression(op(name, a, b)), compile_expression(op(name, b, a))
        assert x.canonical_json == y.canonical_json
        assert x.expression_sha256 == y.expression_sha256
        assert normalize_ast(op(name, a, b)) == x.canonical_ast
        assert (
            compile_expression(op(name, op(name, a, b), c)).expression_sha256
            != compile_expression(op(name, a, op(name, b, c))).expression_sha256
        )
    for name in ("Sub", "SafeDiv"):
        assert (
            compile_expression(op(name, a, b)).expression_sha256
            != compile_expression(op(name, b, a)).expression_sha256
        )


def test_zero_nonfinite_missing_and_overflow_stay_missing_with_reason_counts(bars):
    frame, dates = bars
    frame.loc[0, "turnover"] = 0
    frame.loc[1, "turnover"] = np.inf
    frame.loc[2, "turnover"] = np.nan
    frame.loc[3, "turnover"] = 1e300
    engine = ExpressionEngine(frame, dates)
    divided = engine.compute(
        compile_expression(op("SafeDiv", Node("Const", value=1), f("turnover")))
    )
    assert divided.values.loc[[0, 1, 2]].isna().all()
    assert divided.reason_counts["ZERO_DENOMINATOR"] == 1
    assert divided.reason_counts["INPUT_NONFINITE"] == 1
    assert divided.reason_counts["INPUT_MISSING"] == 1
    overflow = engine.compute(
        compile_expression(op("Mul", f("turnover"), f("turnover")))
    )
    assert pd.isna(overflow.values.loc[3])
    assert overflow.reason_counts["NONFINITE_RESULT"] >= 1
    assert not np.isinf(overflow.values).any()


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_key",
        "duplicate_index",
        "bad_date",
        "numeric_date",
        "intraday",
        "timezone",
        "wrong_code",
        "rename",
        "numeric_value",
        "calendar_duplicate",
        "calendar_reverse",
    ],
)
def test_invalid_identity_dates_numeric_types_and_calendar_rejected(bars, change):
    frame, dates = bars
    frame = frame.copy()
    if change == "duplicate_key":
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    elif change == "duplicate_index":
        frame.index = [0] * len(frame)
    elif change in ("wrong_code", "rename"):
        frame.loc[0, "symbol"] = "bad.SZ" if change == "wrong_code" else "NEW_NAME"
    elif change == "numeric_value":
        frame["volume"] = frame["volume"].astype(str)
    elif change == "calendar_duplicate":
        dates = dates.insert(1, dates[0])
    elif change == "calendar_reverse":
        dates = dates[::-1]
    else:
        value = {
            "bad_date": "2023-02-30",
            "numeric_date": 20230102,
            "intraday": pd.Timestamp(dates[0]) + pd.Timedelta(hours=1),
            "timezone": pd.Timestamp(dates[0], tz="Asia/Shanghai"),
        }[change]
        frame["date"] = frame["date"].astype(object)
        frame.loc[0, "date"] = value
    with pytest.raises(DiscoveryError):
        ExpressionEngine(frame, dates)


def test_static_limits_cache_operation_and_windows_portable_clock_budgets(
    bars, monkeypatch
):
    frame, dates = bars
    node = op("SafeDiv", f("volume"), op("RollingMean", f("volume"), n=5))
    for changes in ({"max_depth": 1}, {"max_nodes": 2}, {"max_lookback": 2}):
        with pytest.raises(DiscoveryError, match="COMPLEXITY_LIMIT"):
            compile_expression(node, replace(DiscoveryConfig(), **changes))
    for changes in (
        {"max_cell_operations": 1},
        {"max_cached_subexpressions": 1},
        {"max_cache_bytes": 1},
    ):
        config = replace(DiscoveryConfig(), **changes)
        output = ExpressionEngine(frame, dates, config).compute(
            compile_expression(node, config)
        )
        assert output.status == "BUDGET_EXCEEDED" and output.values.isna().all()
    with pytest.raises(DiscoveryError, match="BUDGET_EXCEEDED"):
        ExpressionEngine(frame, dates, replace(DiscoveryConfig(), max_panel_rows=1))
    config = replace(DiscoveryConfig(), max_expression_seconds=1)
    engine = ExpressionEngine(frame, dates, config)
    clock = iter((0.0, 2.0, 3.0))
    monkeypatch.setattr(
        "qbot.research.exploratory.factor_discovery.engine.time.perf_counter",
        lambda: next(clock),
    )
    output = engine.compute(compile_expression(node, config))
    assert output.status == "BUDGET_EXCEEDED"


def test_cached_equal_subexpression_not_recomputed_and_compiled_tamper_rejected(bars):
    frame, dates = bars
    engine = ExpressionEngine(frame, dates)
    compiled = compile_expression(
        op("SafeDiv", f("volume"), op("RollingMean", f("volume"), n=5))
    )
    first, second = engine.compute(compiled), engine.compute(compiled)
    assert second.cache_hits > 0 and first.peak_cache_bytes == second.peak_cache_bytes
    pd.testing.assert_series_equal(first.values, second.values, check_exact=True)
    forged = replace(compiled, canonical_json=json.dumps({"op": "Call"}))
    with pytest.raises(DiscoveryError, match="compiled metadata"):
        engine.compute(forged)


def test_default_130_session_lookback_and_two_rolling_limits():
    allowed = op("Lag", op("Lag", op("RollingMean", f("RET1"), n=10), n=60), n=60)
    assert compile_expression(allowed).lookback == 130
    excessive = op("Lag", op("Lag", op("RollingMean", f("RET1"), n=20), n=60), n=60)
    with pytest.raises(DiscoveryError, match="lookback"):
        compile_expression(excessive)
    too_many = op(
        "RollingMean", op("RollingStd", op("RollingMean", f("RET1"), n=5), n=5), n=5
    )
    with pytest.raises(DiscoveryError, match="rolling operator"):
        compile_expression(too_many)


def test_canonical_new_code_cannot_silently_replace_frozen_security(bars):
    frame, dates = bars
    universe = sorted(frame["symbol"].unique())
    frame.loc[0, "symbol"] = "000003.SZ"
    with pytest.raises(DiscoveryError, match="universe mismatch"):
        ExpressionEngine(frame, dates, universe=universe)


def test_derived_cash_per_share_price_cannot_hide_raw_basis_after_normalization():
    price = op("SafeDiv", f("amount"), f("volume"))
    typed = compile_expression(price, require_dimensionless=False)
    assert typed.dimension.as_dict()["price_basis"] == "RAW"
    assert typed.dimension.value_type == "raw_price"
    relative = op("SafeDiv", price, op("RollingMean", price, n=5))
    assert compile_expression(relative).dimension.as_dict()["price_basis"] == "RAW"
    mixed = op("Mul", relative, op("SafeDiv", f("adj_close"), f("adj_open")))
    with pytest.raises(DiscoveryError) as raised:
        compile_expression(mixed)
    assert raised.value.code == ErrorCode.PRICE_BASIS_MISMATCH
    # Delaying the share division until the root cannot conceal the price.
    hidden = op(
        "SafeDiv",
        op("Mul", f("amount"), op("SafeDiv", f("adj_close"), f("adj_open"))),
        f("volume"),
    )
    with pytest.raises(DiscoveryError, match="PRICE_BASIS_MISMATCH"):
        compile_expression(hidden, require_dimensionless=False)
    inverse = op("SafeDiv", f("volume"), f("amount"))
    inverse_relative = op("SafeDiv", inverse, op("RollingMean", inverse, n=5))
    assert (
        compile_expression(inverse_relative).dimension.as_dict()["price_basis"] == "RAW"
    )
    with pytest.raises(DiscoveryError, match="PRICE_BASIS_MISMATCH"):
        compile_expression(
            op("Mul", inverse_relative, op("SafeDiv", f("adj_close"), f("adj_open")))
        )
