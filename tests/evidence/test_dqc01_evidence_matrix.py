"""Targeted admission-boundary and committed-output tests."""
import csv
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('dqc01', ROOT / 'tools/data/dqc01_evidence_matrix.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class AdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (m.OUT / 'data-admission-matrix.csv').open(encoding='utf-8', newline='') as stream:
            cls.rows = list(csv.DictReader(stream))
        cls.index = json.loads((m.OUT / 'evidence-index.json').read_text(encoding='utf-8'))

    def test_source_isolated_denominator(self):
        base = [r for r in self.rows if r['record_scope'] == 'TABLE']
        self.assertEqual(len({(r['source'], r['logical_table']) for r in base}), 156)
        self.assertEqual(sum(int(r['partitions_discovered']) for r in base), 3550)
        self.assertEqual(sum(int(r['partitions_content_scanned']) for r in base), 3507)

    def test_partial_history_stays_unknown(self):
        partial = [r for r in self.rows if r['record_scope'] == 'TABLE' and r['content_status'] == 'PARTIAL_CONTENT_SCANNED']
        self.assertEqual(sum(int(r['partitions_inherited_limited']) for r in partial), 43)
        for row in partial:
            self.assertEqual(row['total_physical_rows'], '')
            self.assertEqual(row['unscanned_rows'], '')
            self.assertNotEqual(row['research_admission'], 'READY_SCOPED')

    def test_candidate_conflict_is_not_business_certification(self):
        row = dict(conflicting_keys='1', candidate_key_variants='[["code", "date"]]')
        self.assertEqual(m.key_status(row), 'KEY_CONFLICT_OBSERVED')
        row['conflicting_keys'] = '0'
        self.assertEqual(m.key_status(row), 'CANDIDATE_KEY_EXACT')
        self.assertEqual(m.key_status(row, partial=True), 'NOT_ASSESSED')
        self.assertFalse(any(r['key_status'] == 'KEY_EXACT_VERIFIED' for r in self.rows))

    def test_business_and_financial_boundaries(self):
        for row in self.rows:
            self.assertEqual(row['pit_status'], 'PIT_UNVERIFIED')
            self.assertEqual(row['hfq_status'], 'HFQ_UNVERIFIED')
            if row['research_admission'] == 'READY_SCOPED':
                self.assertEqual(row['research_use_case'], '601857_RETROSPECTIVE_RAW_CONTROL')
                self.assertIn('487', row['evidence_scope'])
        pit = next(r for r in self.rows if r['research_use_case'] == 'HISTORICAL_PIT_FINANCIAL_SIGNAL')
        self.assertEqual(pit['research_admission'], 'BLOCKED')

    def test_references_are_versioned_and_resolve(self):
        entries = {r['path']: r for r in self.index['evidence']}
        for row in self.rows:
            commits = json.loads(row['evidence_commit'])
            for path in row['evidence_ref'].split(';'):
                self.assertEqual(commits[path], entries[path]['commit'])
                self.assertEqual(len(entries[path]['blob']), 40)
        for entry in entries.values():
            self.assertEqual(m.git('rev-parse', entry['commit']+':'+entry['path']), entry['blob'])
            if entry['audit'] == 'dq-02b':
                self.assertEqual(entry['integration_state'], self.index['bigquant_state'])

    def test_key_denominators_and_conflicts(self):
        base = [r for r in self.rows if r['record_scope'] == 'TABLE']
        counts = m.collections.Counter(r['key_status'] for r in base if r['source'] == 'BigQuant')
        self.assertEqual(counts, {'CANDIDATE_KEY_EXACT': 20, 'KEY_CHECK_BLOCKED_RESOURCE': 16, 'KEY_CONTRACT_UNKNOWN': 25})
        self.assertEqual(sum(r['key_status'] == 'KEY_CONFLICT_OBSERVED' for r in base), 21)

    def test_actual_reads_do_not_include_inherited_rows(self):
        base = [r for r in self.rows if r['record_scope'] == 'TABLE']
        totals = {s: sum(int(r['actual_scanned_rows']) for r in base if r['source'] == s) for s in ('CSMAR', 'CNRDS', 'BigQuant')}
        self.assertEqual(totals, {'CSMAR': 105936113, 'CNRDS': 43523306, 'BigQuant': 450958087})
        self.assertTrue(any(int(r['inherited_evidence_rows'] or 0) > 0 for r in base))


if __name__ == '__main__':
    unittest.main()
