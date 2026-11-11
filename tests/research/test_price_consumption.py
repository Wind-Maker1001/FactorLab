"""Research-use gates do not promote source or financial admission."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from factorlab.backtest.execution import ExecutionBacktestConfig
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.data.hub.price_consumption import (
    SCHEMA, PriceConsumptionError, compute_factors, decimal_text, execution_bars,
    raw_forward_returns, require_fields,
)
from factorlab.exploratory.factor_discovery.contracts import FIELDS, Node
from factorlab.exploratory.factor_discovery.engine import compile_expression
from factorlab.factors.registry_v2 import FactorDefinition, FactorRegistrySnapshot

IDENTITY = ("fixture", "version-1")


def envelope(frame=None, **changes):
    if frame is None:
        frame = pd.DataFrame({"date": pd.bdate_range("2020-01-01", periods=8),
                              "symbol": "600000.SH", "raw_open": np.arange(10., 18.),
                              "raw_close": np.arange(10., 18.)})
    contracts = {}
    for field in frame.columns.difference(["date", "symbol"]):
        contracts[field] = {"consumption": dict(
            schema=SCHEMA, field=field, source_id=IDENTITY[0], data_version=IDENTITY[1],
            basis="OBSERVED_RAW", qualification="RESEARCH_QUALIFIED", unit="CNY/share",
            adjustment_policy="NONE", adjustment_event_dates=[],
            available_at=None, evidence=["fixture-only"], reasons=[],
            purposes=["RAW_LABEL", "RAW_FACTOR", "EXECUTION", "BOOK_VALUATION"],
            scope=[dict(symbol=s, start=str(g.date.min().date()), end=str(g.date.max().date()))
                   for s, g in frame.groupby("symbol")])}
        contracts[field]["consumption"].update(changes)
    return QueryResult(frame, SourceMetadata(*IDENTITY, field_semantics=contracts))


def check(result, **kwargs):
    return require_fields(result, {"raw_open": ("RAW_LABEL", "CNY/share")}, identity=IDENTITY, **kwargs)


@pytest.mark.parametrize("basis", ["ADJUSTED_CANDIDATE", "SUPPLIER_FILLED", "MODEL_VALUATION_ASSUMPTION", "UNKNOWN", "CONFLICT"])
def test_raw_rejects_nonraw_even_with_declared_permission(basis):
    with pytest.raises(PriceConsumptionError, match="BASIS_NOT_ALLOWED"):
        check(envelope(basis=basis))


@pytest.mark.parametrize("changes,reason", [
    ({"qualification": "UNKNOWN", "reasons": ["price policy unclosed"]}, "UNQUALIFIED"),
    ({"unit": None}, "UNIT_UNKNOWN"),
    ({"adjustment_policy": "FUTURE_CASH"}, "ADJUSTMENT_POLICY"),
    ({"adjustment_event_dates": ["2020-09-01"]}, "RAW_HAS_ADJUSTMENT_EVENTS"),
    ({"evidence": []}, "EVIDENCE_MISSING"),
    ({"purposes": []}, "PURPOSE_NOT_QUALIFIED"),
    ({"scope": []}, "OBSERVATION_SCOPE"),
    ({"source_id": "other"}, "CONTRACT_IDENTITY"),
    ({"data_version": "version-2"}, "CONTRACT_IDENTITY"),
    ({"field": "raw_close"}, "FIELD_IDENTITY"),
])
def test_specific_gaps(changes, reason):
    with pytest.raises(PriceConsumptionError, match=reason):
        check(envelope(**changes))


def test_missing_contract_and_source_name_never_grant_qualification():
    result = envelope()
    result = replace(result, metadata=SourceMetadata("CSMAR", "version-1"))
    with pytest.raises(PriceConsumptionError, match="CONTRACT_MISSING"):
        check(result)


@pytest.mark.parametrize("identity", [("other", "version-1"), ("fixture", "version-2"), ("fixture", None)])
def test_envelope_identity_drift(identity):
    result = envelope()
    with pytest.raises(PriceConsumptionError, match="SOURCE_VERSION"):
        check(replace(result, metadata=replace(result.metadata, source_id=identity[0], data_version=identity[1])))


def test_scope_cannot_be_reused_on_other_security_or_date():
    result = envelope()
    for column, value in [("symbol", "OTHER.SH"), ("date", pd.Timestamp("2021-01-01"))]:
        frame = result.data.copy()
        frame[column] = value
        with pytest.raises(PriceConsumptionError):
            check(replace(result, data=frame))


def test_overlapping_scope_not_silently_resolved():
    result = envelope()
    semantics = deepcopy(result.metadata.field_semantics)
    scope = semantics["raw_open"]["consumption"]["scope"]
    scope.append(scope[0].copy())
    with pytest.raises(PriceConsumptionError, match="AMBIGUOUS"):
        check(replace(result, metadata=replace(result.metadata, field_semantics=semantics)))


def test_retrospective_raw_is_not_pit():
    result = envelope()
    check(result)
    with pytest.raises(PriceConsumptionError, match="AVAILABILITY_UNKNOWN"):
        check(result, cutoff="2020-01-20T16:00:00+08:00")
    with pytest.raises(PriceConsumptionError, match="NOT_YET_AVAILABLE"):
        check(envelope(available_at="2021-01-01"), cutoff="2020-01-20")


def test_adjusted_factor_future_event_cutoff():
    result = envelope(basis="ADJUSTED_CANDIDATE", adjustment_policy="NEXT_CASH",
                      adjustment_event_dates=["2020-06-01"], purposes=["ADJUSTED_FACTOR"],
                      available_at="2020-01-01")
    with pytest.raises(PriceConsumptionError, match="FUTURE_ADJUSTMENT_EVENT"):
        require_fields(result, {"raw_open": ("ADJUSTED_FACTOR", "CNY/share")},
                       identity=IDENTITY, cutoff="2020-02-01")


def test_lossless_text_and_raw_label_no_source_selection():
    assert decimal_text("1.2300E+2") == Decimal("123")
    result = envelope()
    result.data.raw_open = result.data.raw_open.map(lambda v: f"{v}E0")
    output = raw_forward_returns(result, identity=IDENTITY, calendar=result.data.date)
    assert output.raw_forward_return.iloc[0] == float(Decimal(16)/Decimal(11)-1)
    assert output.raw_forward_return.notna().sum() == 2


def test_missing_session_is_not_row_shifted_or_filled():
    result = envelope()
    dates = result.data.date
    result = replace(result, data=result.data.drop(index=3))
    output = raw_forward_returns(result, identity=IDENTITY, calendar=dates)
    assert output.raw_forward_return.isna().all()


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "bad"])
def test_invalid_numeric_text(value):
    with pytest.raises(PriceConsumptionError):
        decimal_text(value)


def test_carry_can_value_but_never_execute_even_under_raw_name():
    result = envelope(basis="MODEL_VALUATION_ASSUMPTION", adjustment_policy="CARRY",
                      purposes=["BOOK_VALUATION", "EXECUTION"])
    require_fields(result, {"raw_close": ("BOOK_VALUATION", "CNY/share")}, identity=IDENTITY)
    with pytest.raises(PriceConsumptionError, match="BASIS_NOT_ALLOWED"):
        execution_bars(result, identity=IDENTITY, config=ExecutionBacktestConfig())


def test_execution_keeps_missing_raw_and_drops_independent_valuation():
    result = envelope()
    result.data.loc[0, "raw_close"] = np.nan
    result.data["valuation_price"] = 99.
    result.data["suspended"] = 1
    output = execution_bars(result, identity=IDENTITY, config=ExecutionBacktestConfig())
    assert pd.isna(output.raw_close.iloc[0])
    assert "valuation_price" not in output
    assert output.suspended.eq(1).all()


def test_factor_adapter_reuses_definition_and_unit_contract():
    result = envelope()
    for field in FIELDS:
        if field != "RET1" and field not in result.data:
            result.data[field] = np.nan
    close = Node("Field", value="raw_close")
    definition = FactorDefinition.from_expression("RAW", "1", compile_expression(
        Node("SafeDiv", (close, Node("Lag", (close,), param=1)))))
    snapshot = FactorRegistrySnapshot((definition,))
    actual = compute_factors(result, snapshot, (definition.ref,), identity=IDENTITY,
                             calendar=result.data.date, units={"raw_close": "CNY/share"})
    expected = snapshot.compute(result.data, (definition.ref,), calendar=result.data.date)
    pd.testing.assert_frame_equal(actual, expected)
    with pytest.raises(PriceConsumptionError, match="UNIT_UNDECLARED"):
        compute_factors(result, snapshot, (definition.ref,), identity=IDENTITY,
                        calendar=result.data.date, units={})
    contracts = deepcopy(result.metadata.field_semantics)
    contracts["raw_close"]["consumption"]["basis"] = "ADJUSTED_CANDIDATE"
    with pytest.raises(PriceConsumptionError, match="BASIS_NOT_ALLOWED"):
        compute_factors(replace(result, metadata=replace(result.metadata, field_semantics=contracts)),
                        snapshot, (definition.ref,), identity=IDENTITY,
                        calendar=result.data.date, units={"raw_close": "CNY/share"})


def test_builtin_liquidity_requires_units_without_breaking_legacy():
    result = envelope()
    definition = FactorDefinition.builtin("AMIHU20")
    assert definition.consumption_requirements == {"adj_close": "ADJUSTED_FACTOR", "amount": "UNIT_FACTOR"}
    volume = FactorDefinition.builtin("VOLUME_RATIO")
    assert volume.consumption_requirements == {"volume": "UNIT_FACTOR"}
    for field in FIELDS:
        if field != "RET1" and field not in result.data:
            result.data[field] = np.nan
    snapshot = FactorRegistrySnapshot((volume,))
    with pytest.raises(PriceConsumptionError, match="CONTRACT_MISSING"):
        compute_factors(result, snapshot, (volume.ref,), identity=IDENTITY,
                        calendar=result.data.date, units={"volume": "shares"})
    snapshot.compute(result.data, (volume.ref,), calendar=result.data.date)


def test_raw_study_adapter_gates_before_existing_label_builder():
    from factorlab.research.price_inputs import build_raw_panel
    from factorlab.research.real_model_study import build_panel
    rng = np.random.default_rng(12)
    dates = pd.bdate_range("2020-01-01", periods=100)
    pieces = []
    for symbol in ("600000.SH", "600001.SH", "600002.SH", "600003.SH"):
        close = np.exp(np.cumsum(rng.normal(0, .01, len(dates)))) * 10
        pieces.append(pd.DataFrame(dict(date=dates, symbol=symbol, raw_open=close*.999,
            raw_close=close, raw_high=close*1.02, raw_low=close*.98,
            volume=rng.uniform(100, 1000, len(dates)), suspended=0, st_status=0)))
    frame = pd.concat(pieces, ignore_index=True)
    result = envelope(frame)
    contract = result.metadata.field_semantics["volume"]["consumption"]
    contract.update(basis="NOT_PRICE", unit="shares", adjustment_policy="NOT_APPLICABLE", purposes=["UNIT_FACTOR"])
    members = frame[["date", "symbol"]]
    config = dict(price_basis="RAW_PRICE_EXPLORATORY", minimum_cross_section=2)
    expected = build_panel(frame, members, dates, config)
    actual = build_raw_panel(result, members, dates, config, identity=IDENTITY)
    assert len(actual[0]) > 0
    pd.testing.assert_frame_equal(actual[0], expected[0], check_exact=True)
    assert actual[1] == expected[1]
    result = replace(result, metadata=replace(result.metadata, field_semantics={}))
    with pytest.raises(PriceConsumptionError, match="CONTRACT_MISSING"):
        build_raw_panel(result, members, dates, config, identity=IDENTITY)
