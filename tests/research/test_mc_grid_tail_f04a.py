"""Focused failure and inference cases for the paired TTL study."""
from time import perf_counter

import pytest

from factorlab.experiments.mc_grid_tail_f04a import _account_id, _collect, admission, validate_triplet
from factorlab.experiments.mc_grid_tail_f04a_report import _participation_tests


def test_direction_requires_paired_significance_and_support_across_seeds():
    seeds = (1729, 2718, 31415)
    gains_by_seed = {
        'jump_down': (12, 0, 0),
        'regime_historical': (4, 3, 0),
        'moving_block_bootstrap': (4, 4, 0),
    }
    by_model = {
        model: [
            {
                48: {'seed': seed, 'traded': False},
                100: {'seed': seed, 'traded': path < gains},
                168: {'seed': seed, 'traded': path < gains},
            }
            for seed, gains in zip(seeds, gain_counts)
            for path in range(16)
        ]
        for model, gain_counts in gains_by_seed.items()
    }
    tests = _participation_tests(by_model)
    first = {test['model']: test for test in tests if test['low'] == 48}

    one_seed = first['jump_down']
    assert one_seed['holm_p'] < .05
    assert not one_seed['supports_direction']

    nominal = first['regime_historical']
    assert nominal['p'] < .05 < nominal['holm_p']
    assert not nominal['supports_direction']

    supported = first['moving_block_bootstrap']
    assert supported['paths'] == 48
    assert (supported['gained'], supported['lost']) == (8, 0)
    assert supported['supports_direction']
    assert all(not test['supports_direction'] for test in tests if test['low'] == 100)


def test_resource_admission_preserves_output_reserve_and_rejects_underestimated_peak():
    policy = dict(output_reserve_seconds=15, maximum_tree_rss_bytes=400,
                  maximum_temporary_bytes=100, maximum_evidence_bytes=100,
                  minimum_free_after_bytes=150)
    estimate = dict(wall_seconds=30, tree_rss_bytes=300, temporary_bytes=90, evidence_bytes=90)
    admitted = admission(policy, estimate, available_seconds=45, available_memory_bytes=500, free_bytes=330)
    assert admitted['status'] == 'RESOURCE_ADMITTED'
    rejected = admission(policy, estimate, available_seconds=44, available_memory_bytes=299, free_bytes=329)
    assert rejected['status'] == 'RESOURCE_NOT_ADMITTED'
    assert rejected['reasons'] == ['WALL_BUDGET', 'TREE_RSS', 'DISK_FREE']


def test_triplet_rejects_wrong_price_path_and_worker_failure():
    ttls = (48, 100, 168)
    identity = {'model': 'jump_down', 'seed': 1729, 'path_id': 0}

    def row(ttl):
        return identity | dict(ttl_hours=ttl, account_id=_account_id(identity, ttl), price_sha256='expected')

    rows = [row(ttl) for ttl in ttls]
    with pytest.raises(ValueError, match='incomplete or mismatched'):
        validate_triplet(rows[:-1], identity, 'expected', ttls)
    with pytest.raises(ValueError, match='incomplete or mismatched'):
        validate_triplet(rows, identity, 'different', ttls)

    def replay(prices, payload):
        _, ttl, _ = payload
        if ttl == 168:
            raise RuntimeError('account failed')
        return row(ttl)

    performance = dict(peak_tree_rss_bytes=0, peak_temporary_disk_bytes=0)
    policy = dict(maximum_tree_rss_bytes=10, maximum_temporary_bytes=10)
    groups = _collect(None, [(identity, (), None, 'expected')], ttls, replay, 1,
                      perf_counter()+10, policy, performance)
    with pytest.raises(RuntimeError, match='account failed'):
        next(groups)
