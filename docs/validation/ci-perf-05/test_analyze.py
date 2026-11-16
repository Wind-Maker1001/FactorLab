"""Synthetic regression tests for the fixed-observation analyzer."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock
import tempfile


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('ci_perf_05_analyze', HERE / 'analyze.py')
analyze = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyze)


def synthetic_source():
    def job(name, start, end):
        return dict(name=name, started_at=f'2026-01-01T00:{start}Z',
                    completed_at=f'2026-01-01T00:{end}Z')

    return dict(
        run=dict(created_at='2026-01-01T00:00:00Z'),
        jobs=dict(jobs=[
            job('Trusted risk selection', '00:02', '00:10'),
            job('Extended software Python 3.13 shard 0', '00:13', '01:10'),
            job('Extended software Python 3.14 shard 0', '00:17', '01:20'),
            job('Verify PR actual coverage', '01:24', '01:30'),
            job('FactorLab merge gate', '01:32', '01:36'),
            job('Other parallel checks', '00:01', '01:20'),
        ]),
        versions={
            '3.13': dict(baseline=[
                dict(step_residual_seconds=5, non_test_job_seconds=20),
                dict(step_residual_seconds=2, non_test_job_seconds=10),
            ]),
            '3.14': dict(baseline=[
                dict(step_residual_seconds=3, non_test_job_seconds=12),
                dict(step_residual_seconds=6, non_test_job_seconds=22),
            ]),
        },
    )


class ArchivedBaselineTests(unittest.TestCase):
    def test_raw_replay_uses_archived_v1_after_production_v2_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ['analyze.py', '--evidence', 'unused-evidence',
                    '--target', str(HERE / 'target-inventory.json'), '--output', directory]
            with mock.patch('sys.argv', args), mock.patch.object(
                    analyze.p03, 'read_run', side_effect=RuntimeError('captured baseline')) as read_run:
                with self.assertRaisesRegex(RuntimeError, 'captured baseline'):
                    analyze.main()
            expected = analyze.p03.load(HERE / 'previous_module_timings.json')['module_seconds']
            self.assertEqual(read_run.call_args.args[1], expected)


class MatchingWeightsTests(unittest.TestCase):
    def setUp(self):
        self.module = 'tests/test_measured.py'
        self.nodes = [self.module + '::test_a', self.module + '::test_b']
        self.hashes = {self.module: 'current-source'}
        self.data = dict(module_nodes={self.module: list(reversed(self.nodes))},
                         weights={self.module: 12.5})

    def match(self, source_hashes=None, runtime_matches=True):
        return analyze.matching_weights(
            self.data, self.nodes, self.hashes,
            self.hashes if source_hashes is None else source_hashes,
            runtime_matches=runtime_matches)

    def test_exact_inventory_is_measured_regardless_of_node_order(self):
        measured, states = self.match()
        self.assertEqual(measured, {self.module: 12.5})
        self.assertEqual(states[self.module], dict(
            status='MEASURED', measured_tests=2, target_tests=2,
            observed_seconds=12.5, nodes_match=True, source_match=True))

    def test_duplicate_observations_do_not_match_same_node_set(self):
        for observed in (self.nodes + [self.nodes[0]],
                         [self.nodes[0], self.nodes[0]]):
            with self.subTest(observed=observed):
                self.data['module_nodes'][self.module] = observed
                measured, states = self.match()
                self.assertEqual(measured, {})
                self.assertEqual(states[self.module]['status'], 'PARTIAL')
                self.assertFalse(states[self.module]['nodes_match'])
                self.assertEqual(states[self.module]['measured_tests'], len(observed))

    def test_source_hash_mismatch_or_missing_is_partial(self):
        for hashes in ({self.module: 'old-source'}, {}):
            with self.subTest(hashes=hashes):
                measured, states = self.match(source_hashes=hashes)
                self.assertEqual(measured, {})
                self.assertEqual(states[self.module]['status'], 'PARTIAL')
                self.assertTrue(states[self.module]['nodes_match'])
                self.assertFalse(states[self.module]['source_match'])

    def test_runtime_mismatch_is_partial_even_with_matching_module(self):
        measured, states = self.match(runtime_matches=False)
        self.assertEqual(measured, {})
        self.assertEqual(states[self.module]['status'], 'PARTIAL')
        self.assertTrue(states[self.module]['nodes_match'])
        self.assertTrue(states[self.module]['source_match'])

    def test_missing_observation_is_unmeasured_and_never_reuses_old_weight(self):
        for module_nodes in ({}, {self.module: []}):
            with self.subTest(module_nodes=module_nodes):
                self.data['module_nodes'] = module_nodes
                measured, states = self.match()
                self.assertEqual(measured, {})
                self.assertEqual(states[self.module]['status'], 'UNMEASURED')
                self.assertEqual(states[self.module]['measured_tests'], 0)
                # A stale observed cost is diagnostic, not a candidate weight.
                self.assertEqual(states[self.module]['observed_seconds'], 12.5)
                self.assertEqual(analyze.module_plan(self.nodes, measured)[self.module],
                                 dict(seconds=60.0, status='UNMEASURED'))
        self.data['weights'] = {}
        _, states = self.match()
        self.assertIsNone(states[self.module]['observed_seconds'])


class AssignmentAndEvaluationTests(unittest.TestCase):
    def test_modules_are_indivisible_and_ties_are_deterministic(self):
        modules = [f'tests/test_{letter}.py' for letter in 'abcdef']
        nodes = [module + suffix for suffix in ('::test_x', '::test_y')
                 for module in reversed(modules)]
        weights = dict.fromkeys(modules, 10.0)
        for count, expected_shards in ((3, [0, 1, 2, 0, 1, 2]),
                                       (4, [0, 1, 2, 3, 0, 1])):
            with self.subTest(count=count):
                expected = dict(zip(modules, expected_shards))
                for ordered_nodes in (nodes, list(reversed(nodes)), sorted(nodes)):
                    plan = analyze.assignment(ordered_nodes, weights, count)
                    self.assertEqual(plan, expected)
                    shards = analyze.partition(ordered_nodes, weights, count)
                    self.assertCountEqual([node for shard in shards for node in shard], nodes)
                    for module in modules:
                        occupied = [i for i, shard in enumerate(shards)
                                    if any(node.startswith(module + '::') for node in shard)]
                        self.assertEqual(occupied, [expected[module]])

    def test_shared_assignment_is_evaluated_using_each_versions_costs(self):
        nodes = [letter + '::test_x' for letter in 'abcdef']
        first_costs = dict(zip('abcdef', [9, 8, 7, 6, 5, 4]))
        other_costs = dict(zip('abcdef', [1, 2, 3, 4, 5, 60]))
        for count, first_loads, other_loads in (
                (3, [13, 13, 13], [61, 7, 7]),
                (4, [9, 8, 11, 11], [1, 2, 63, 9])):
            with self.subTest(count=count):
                plan = analyze.assignment(nodes, first_costs, count)
                self.assertNotEqual(plan, analyze.assignment(nodes, other_costs, count))
                for costs, loads in ((first_costs, first_loads), (other_costs, other_loads)):
                    result = analyze.evaluate(plan, costs, count)
                    self.assertEqual(result['loads_seconds'], loads)
                    self.assertEqual(result['total_seconds'], sum(costs.values()))
                    self.assertEqual(result['max_seconds'], max(loads))
                    lower = max(sum(costs.values()) / count, max(costs.values()))
                    self.assertEqual(result['lower_bound_seconds'], lower)
                    self.assertEqual(result['bound_gap_seconds'], max(loads) - lower)
                    self.assertAlmostEqual(result['imbalance'],
                                           max(loads) / (sum(costs.values()) / count) - 1)


class TimingModelTests(unittest.TestCase):
    def test_body_step_job_and_e2e_are_critical_path_not_parallel_sum(self):
        source = synthetic_source()
        costs = dict(a=40, b=30, c=20, d=10)
        for count, plan in ((3, dict(a=0, b=1, c=2, d=0)),
                            (4, dict(a=0, b=1, c=2, d=3))):
            with self.subTest(count=count):
                result = analyze.timing_model(source, '3.13', plan, costs, count)
                body = 50 if count == 3 else 40
                self.assertEqual(result['test_body_seconds'], body)
                self.assertEqual(result['test_step_seconds'], [body + 2, body + 5])
                self.assertEqual(result['job_seconds'], [body + 12, body + 25])
                self.assertEqual(result['prefix_seconds'], 10)
                self.assertEqual(result['dispatch_seconds'], [3, 7])
                self.assertEqual(result['residual_seconds'], [2, 5])
                self.assertEqual(result['non_test_job_seconds'], [10, 20])
                self.assertEqual(result['coverage_wait_seconds'], 4)
                self.assertEqual(result['coverage_gate_tail_seconds'], 12)
                self.assertEqual(result['other_parallel_finish_seconds'], 80)
                expected = [91, 108] if count == 3 else [86, 98]
                self.assertEqual(result['workflow_e2e_seconds'], expected)
                self.assertEqual(result['e2e_margin_seconds'], [900 - expected[1], 900 - expected[0]])
                self.assertEqual(result['kind'],
                                 'OBSERVED_OVERHEAD_ENVELOPE_NOT_CONFIDENCE_INTERVAL')

    def test_other_parallel_branch_can_dominate_both_endpoints(self):
        source = synthetic_source()
        source['jobs']['jobs'][-1]['completed_at'] = '2026-01-01T00:03:20Z'
        source['jobs']['jobs'][4]['started_at'] = '2026-01-01T00:03:22Z'
        source['jobs']['jobs'][4]['completed_at'] = '2026-01-01T00:03:26Z'
        result = analyze.timing_model(source, '3.13', dict(a=0, b=1, c=2),
                                      dict(a=50, b=30, c=20), 3)
        self.assertEqual(result['workflow_e2e_seconds'], [206, 206])

    def test_version_uses_its_own_overhead_envelope(self):
        result = analyze.timing_model(synthetic_source(), '3.14',
                                      dict(a=0, b=1, c=2), dict(a=50, b=30, c=20), 3)
        self.assertEqual(result['test_step_seconds'], [53, 56])
        self.assertEqual(result['job_seconds'], [65, 78])
        self.assertEqual(result['workflow_e2e_seconds'], [94, 111])


if __name__ == '__main__':
    unittest.main()
