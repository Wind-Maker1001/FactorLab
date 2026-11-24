"""Acceptance planning consumes the account health's real funds schema."""
from copy import deepcopy
from decimal import Decimal as D
import ast
import json

import pytest

from test_s04_validation_budget import ROOT, RULES, validation


@pytest.fixture
def official_health():
    evidence = json.loads((ROOT / 'docs/validation/p8-s04/official-demo-acceptance.json').read_text(encoding='utf-8'))
    health = deepcopy(evidence['before']['health'])
    health['global_budget']['remaining_posts'] = 2
    return health


def test_real_health_schema_admits_fully_reserved_pair(official_health):
    plan = validation.acceptance_plan(official_health, RULES, dict(askPrice='150'))
    assert plan['mode'] == 'ROUNDTRIP_2_POST'
    config = plan['configuration']
    assert D(config['quantity']) % D('.001') == 0
    assert D(config['max_entry_price']) % D('.01') == 0
    assert D(config['exit_budget_notional']) == D(config['quantity']) * D(config['max_exit_price'])
    assert D(official_health['global_budget']['remaining_notional']) >= D(config['exit_budget_notional']) * 2


@pytest.mark.parametrize('posts', [0, 1])
def test_real_schema_never_spends_last_post(official_health, posts):
    official_health['global_budget']['remaining_posts'] = posts
    assert validation.acceptance_plan(official_health, RULES, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'


def test_real_nav_loss_blocks_entry(official_health):
    official_health['equity'].update(walletBalance='4998', unrealizedProfit='0', availableBalance='4998')
    official_health['nav_usdt'] = '4998'
    assert validation.acceptance_plan(official_health, RULES, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'


def test_nav_is_not_available_margin(official_health):
    official_health['equity']['availableBalance'] = '1'
    assert validation.acceptance_plan(official_health, RULES, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'


@pytest.mark.parametrize('field', ['walletBalance', 'unrealizedProfit', 'availableBalance'])
@pytest.mark.parametrize('value', [None, '', 'invalid', 'NaN', 'Infinity', '-Infinity', {}, [], True, False])
def test_invalid_funds_safely_disable_entry(official_health, field, value):
    official_health['equity'][field] = value
    assert validation.acceptance_plan(official_health, RULES, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'


@pytest.mark.parametrize('field', ['walletBalance', 'unrealizedProfit', 'availableBalance'])
def test_missing_funds_safely_disable_entry(official_health, field):
    del official_health['equity'][field]
    assert validation.acceptance_plan(official_health, RULES, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'


@pytest.mark.parametrize('value', [None, '', 'invalid', 'NaN', 'Infinity', {}, '5000'])
def test_missing_invalid_or_inconsistent_nav_disables_entry(official_health, value):
    official_health['nav_usdt'] = value
    assert validation.acceptance_plan(official_health, RULES, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'


def test_missing_nav_or_nonobject_equity_disables_entry(official_health):
    del official_health['nav_usdt']
    assert validation.acceptance_plan(official_health, RULES, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'
    official_health['equity'] = '100'
    assert validation.acceptance_plan(official_health, RULES, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'


def test_tick_quantity_and_total_notional_still_constrain_real_schema(official_health):
    rules = deepcopy(RULES)
    rules['filters'][1]['stepSize'] = '.03'
    assert validation.acceptance_plan(official_health, rules, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'
    official_health['global_budget']['remaining_notional'] = '7'
    assert validation.acceptance_plan(official_health, RULES, dict(askPrice='150'))['mode'] == 'ZERO_POST_SWITCH_ONLY'


def test_hold_phases_have_accurate_evidence_and_no_order_claims():
    source = (ROOT / 'tools/validate_p8_s04_demo.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    phases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'dict':
            fields = {kw.arg: kw.value for kw in node.keywords}
            name = fields.get('name')
            if isinstance(name, ast.Constant) and str(name.value).startswith(('phase2_', 'phase3_')):
                phases[name.value] = fields
    hold = phases['phase2_hold_hot_switch_and_restart_recovery']
    fault = phases['phase3_hot_switch_and_automatic_fault_rollback']
    assert ast.literal_eval(hold['active_order_restart_verified']) is False
    assert ast.literal_eval(fault['cancellation_verified']) is False
    assert 'zero_post_verified' in hold and 'zero_post_verified' in fault
    assert 'resting_order_before_restart' not in hold
    assert 'a2-resting-buy' not in source
    assert "'a2-hold'" in source
    assert "real_fill_verified=False" in source
