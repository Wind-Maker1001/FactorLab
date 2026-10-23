"""The forward contract freezes research choices but permits a rolling end."""

from dataclasses import replace
from datetime import date

import pytest

from qbot.research.config import ForwardConfig, load_config
from qbot.research.errors import ConfigurationError
from qbot.research.forward.contract import (
    forward_contract_payload,
    forward_contract_sha256,
    selected_forward_model_parameters,
)


def _enabled_config():
    base = load_config("configs/research/csi500_5y.yaml")
    return replace(
        base,
        forward=ForwardConfig(
            enabled=True,
            activation_date=date(2026, 9, 25),
            model_name="ridge",
            model_version="ridge-forward-v1",
            training_asof=date(2026, 9, 11),
            parameter_candidate_index=1,
        ),
    )


def test_contract_excludes_only_the_rolling_data_end():
    config = _enabled_config()
    extended = replace(
        config,
        data=replace(config.data, end=date(2026, 10, 9)),
    )

    assert forward_contract_sha256(config) == forward_contract_sha256(extended)
    assert "end" not in forward_contract_payload(config)["data"]


def test_contract_changes_when_a_research_choice_changes():
    config = _enabled_config()
    changed = replace(
        config,
        forward=replace(config.forward, model_version="ridge-forward-v2"),
    )

    assert forward_contract_sha256(config) != forward_contract_sha256(changed)

    changed_cost = replace(
        config,
        fees=replace(config.fees, commission_bps=config.fees.commission_bps + 1.0),
    )
    assert forward_contract_sha256(config) != forward_contract_sha256(changed_cost)


def test_selected_parameters_are_explicit_not_test_selected():
    config = _enabled_config()

    parameters = selected_forward_model_parameters(config)

    assert parameters["alpha"] == config.models.ridge.alphas[1]
    assert set(parameters) == {"alpha", "fit_intercept", "max_iter", "tolerance"}


def test_xgboost_contract_includes_the_fixed_non_tuned_gamma_default():
    config = _enabled_config()
    config = replace(
        config,
        forward=replace(
            config.forward,
            model_name="xgboost",
            parameter_candidate_index=0,
            model_version="xgboost-forward-v1",
        ),
    )

    parameters = selected_forward_model_parameters(config)

    assert parameters["gamma"] == 0.0


def test_forward_version_selection_is_explicitly_frozen_before_outcomes():
    config = _enabled_config()
    payload = forward_contract_payload(config)
    assert payload["schema_version"] == 2
    assert payload["forward"]["evaluation_version_policy"] == "FIRST_ON_TIME"
    altered = replace(
        config,
        forward=replace(config.forward, evaluation_version_policy="BEST_RETURN"),
    )
    assert forward_contract_sha256(altered) != forward_contract_sha256(config)
    with pytest.raises(ConfigurationError, match="FIRST_ON_TIME"):
        altered.validate()


@pytest.mark.parametrize("policy", [None, True, 1, "LATEST", "first_on_time", "FIRST_ON_TIME "])
def test_no_loose_version_selection_flag_is_accepted(policy):
    config = _enabled_config()
    config = replace(config, forward=replace(config.forward, evaluation_version_policy=policy))
    with pytest.raises(ConfigurationError, match="FIRST_ON_TIME"):
        config.validate()
