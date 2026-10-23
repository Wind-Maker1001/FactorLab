"""Generation determinism, structural audit, and data/search boundary tests."""

import builtins
from dataclasses import replace
import inspect

import pytest

from factorlab.exploratory.factor_discovery.contracts import (
    DiscoveryConfig,
    DiscoveryError,
    ErrorCode,
)
from factorlab.exploratory.factor_discovery.generator import generate_candidates
from factorlab.factors.registry import FACTOR_REGISTRY


def _identity(result):
    return [
        (
            record["candidate_id"],
            record["generation_sequence"],
            record["family"],
            record["meaning"],
            record["original_expression"],
            record["status"],
            record["rejection_reason"],
            record["duplicate_of"],
            record["compiled"].canonical_json if record["compiled"] else None,
            record["compiled"].expression_sha256 if record["compiled"] else None,
        )
        for record in result.records
    ]


def test_default_finite_generation_has_unique_dimensionless_economic_candidates():
    result = generate_candidates()
    unique = [record for record in result.records if record["status"] == "GENERATED"]
    assert len(result.records) == 40
    assert len(unique) >= 20
    assert len({record["family"] for record in unique}) >= 3
    assert len({record["compiled"].expression_sha256 for record in unique}) == len(
        unique
    )
    assert result.stats["rejected"] == 0
    assert result.stats["structurally_unique"] == len(unique)
    for record in unique:
        compiled = record["compiled"]
        assert compiled.dimension.is_dimensionless
        assert compiled.lookback <= 130
        assert record["original_expression"]
        assert record["meaning"]
        assert record["rejection_reason"] is None
        assert record["duplicate_of"] is None
    assert [record["generation_sequence"] for record in result.records] == list(
        range(1, 41)
    )


def test_repeated_generation_and_registry_unchanged():
    before = dict(FACTOR_REGISTRY)
    first = generate_candidates()
    second = generate_candidates()
    assert _identity(first) == _identity(second)
    assert first.stats == second.stats
    assert FACTOR_REGISTRY == before
    assert len(FACTOR_REGISTRY) == 14


def test_commutative_duplicate_is_audited_without_another_computation_task():
    result = generate_candidates()
    by_id = {record["candidate_id"]: record for record in result.records}
    duplicates = [
        record for record in result.records if record["status"] == "EXACT_DUPLICATE"
    ]
    assert len(duplicates) == 1
    duplicate = duplicates[0]
    original = by_id[duplicate["duplicate_of"]]
    assert original["status"] == "GENERATED"
    assert duplicate["generation_sequence"] > original["generation_sequence"]
    assert duplicate["original_expression"] != original["original_expression"]
    assert duplicate["compiled"].canonical_json == original["compiled"].canonical_json
    assert (
        duplicate["compiled"].expression_sha256
        == original["compiled"].expression_sha256
    )
    assert result.stats["generated"] + result.stats["exact_duplicates"] == len(
        result.records
    )


@pytest.mark.parametrize("cap", [1, 20, 25, 40, 50])
def test_record_budget_truncates_frozen_order_including_duplicate(cap):
    whole = generate_candidates(replace(DiscoveryConfig(), max_candidates=50))
    limited = generate_candidates(replace(DiscoveryConfig(), max_candidates=cap))
    assert len(limited.records) == min(cap, len(whole.records))
    assert _identity(limited) == _identity(whole)[:cap]
    assert limited.stats["total_records"] <= cap
    assert limited.stats["truncated"] == limited.stats["enumerated"] - len(
        limited.records
    )


def test_whitelist_order_does_not_change_ascending_parameters():
    config = replace(
        DiscoveryConfig(), windows=(60, 5, 20, 10), lags=(60, 1, 20, 10, 5)
    )
    assert _identity(generate_candidates(config)) == _identity(generate_candidates())
    assert [
        record["node"].args[1].param for record in generate_candidates().records[:4]
    ] == [5, 10, 20, 60]


@pytest.mark.parametrize(
    "change, expected_reason",
    [
        ({"max_depth": 1}, "COMPLEXITY_LIMIT"),
        ({"max_nodes": 1}, "COMPLEXITY_LIMIT"),
        ({"max_rolling_operators": 1}, "COMPLEXITY_LIMIT"),
        ({"max_lookback": 5}, "COMPLEXITY_LIMIT"),
        ({"fields": ("adj_close",)}, "UNAUTHORIZED_FIELD"),
        (
            {
                "operators": (
                    "Field",
                    "Lag",
                    "Delta",
                    "RollingMean",
                    "RollingStd",
                    "Add",
                    "Sub",
                    "Mul",
                )
            },
            "UNAUTHORIZED_NODE",
        ),
        ({"lags": (1,)}, "INVALID_PARAMETER"),
    ],
)
def test_narrower_static_limits_and_whitelists_produce_visible_rejections(
    change, expected_reason
):
    config = replace(DiscoveryConfig(), **change)
    result = generate_candidates(config)
    rejected = [record for record in result.records if record["status"] == "REJECTED"]
    assert rejected
    assert result.stats["rejected"] == len(rejected)
    assert any(expected_reason in record["rejection_reason"] for record in rejected)
    assert all(
        record["compiled"] is None and record["duplicate_of"] is None
        for record in rejected
    )
    assert result.stats["generated"] + result.stats["exact_duplicates"] + len(
        rejected
    ) == len(result.records)


def test_generator_has_no_data_label_partition_or_filesystem_input(monkeypatch):
    def forbid_read(*args, **kwargs):
        raise AssertionError("generation attempted filesystem access")

    monkeypatch.setattr(builtins, "open", forbid_read)
    result = generate_candidates()
    assert len(result.records) == 40
    assert tuple(inspect.signature(generate_candidates).parameters) == ("config",)
    for prohibited in ("data", "labels", "validation", "test"):
        with pytest.raises(TypeError):
            generate_candidates(**{prohibited: object()})


def test_invalid_config_and_unbounded_candidate_budget_rejected():
    with pytest.raises(DiscoveryError) as raised:
        generate_candidates({"labels": "future_return"})
    assert raised.value.code == ErrorCode.INVALID_PARAMETER
    with pytest.raises(DiscoveryError):
        replace(DiscoveryConfig(), max_candidates=51)
