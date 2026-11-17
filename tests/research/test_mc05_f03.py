"""Regression tests for complete prefixes, resource refusals, and thin tail weights."""
from types import SimpleNamespace

import pytest

from factorlab.experiments import mc_grid_tail_f03_resources as resources
from factorlab.experiments.mc_grid_tail_f03_report import _checkpoint_counts
from factorlab.experiments.mc_grid_tail_f03_statistics import _fill_tail_support
from factorlab.research.monte_carlo.reliability import tail_statistics


def test_a_complete_checkpoint_requires_every_seed():
    settings = {'sizes': [16, 32], 'seeds': [1729, 2718, 31415]}
    first = [dict(model='jump_down', n_paths=16, seed=seed) for seed in settings['seeds']]
    interrupted = dict(model='jump_down', n_paths=32, seed=1729)
    assert _checkpoint_counts(first, settings, ['jump_down']) == {'jump_down': 16}
    with pytest.raises(ValueError, match='incomplete/duplicate seed checkpoint'):
        _checkpoint_counts([*first, interrupted], settings, ['jump_down'])


def test_narrow_empirical_interval_cannot_supply_99_percent_tail():
    losses = [0.] * 64
    tail = tail_statistics(losses, resamples=9)['0.99']
    _fill_tail_support(tail, losses, 99)
    assert tail['cvar_interval']['lower'] == tail['cvar_interval']['upper'] == 0
    assert tail['cvar_conservative_interval']['upper_unbounded']
    assert tail['expected_tail_paths'] == .64
    assert tail['tail_paths_with_nonzero_weight'] == 1
    assert tail['largest_path_tail_weight'] == 1
    assert tail['f03_status'] == 'INSUFFICIENT_TAIL_SUPPORT'


def test_admission_keeps_safety_and_refuses_a_complete_group(tmp_path, monkeypatch):
    monkeypatch.setattr(resources, '_require_psutil', lambda: SimpleNamespace(
        virtual_memory=lambda: SimpleNamespace(available=1024)))
    monkeypatch.setattr(resources.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1024))
    calibration = dict(models={'jump_down': dict(wall_seconds=2, tree_cpu_seconds=3,
        peak_temporary_disk_bytes=10)}, performance=dict(startup_wall_seconds=1,
        peak_tree_rss_bytes=100), evidence_bytes_per_account=1)
    definition = dict(resources=dict(pilot_paths_per_model=8, max_workers=8,
        output_reserve_seconds=1, minimum_free_after_bytes=10))
    planner = resources.ResourcePlanner(calibration, definition)
    decision = planner.admit(model='jump_down', size=32, begin=16, accounts=48, elapsed=5,
        temporary=tmp_path, output=tmp_path, settings=dict(budget_seconds=20, cost_safety_factor=1.2))
    assert decision['projected_additional_seconds'] == pytest.approx(14.4)
    assert decision['admitted'] is False
    assert decision['reasons'] == ['WALL_BUDGET']
