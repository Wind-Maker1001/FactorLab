"""Synthetic arithmetic and existing vintage-selection contract checks."""
import importlib.util
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
spec = importlib.util.spec_from_file_location('audit_p3_d03', ROOT / 'tools/data/audit_p3_d03.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

from factorlab.data.corporate_actions.contracts import CorporateAction, EventCoverage, SourceSnapshot
from factorlab.data.corporate_actions.validation import known_versions, timestamp, date


class EvidenceTests(unittest.TestCase):
    def test_cash_price_is_not_extra_holder_income(self):
        result = audit.cash_candidates('100', '2')
        self.assertEqual(Decimal(result['forward']), Decimal('.98'))
        self.assertEqual(Decimal(result['ex_reference']) * 100 + Decimal('2') * 100, Decimal('10000'))
        self.assertEqual(Decimal(result['forward']) * Decimal(result['backward']), Decimal('1.000000000000000000000000000'))

    def test_future_correction_and_cancellation_do_not_enter_2018(self):
        original = CorporateAction('601857.SH', 'synthetic', 1, 'CASH_DIVIDEND',
                                   '2018-06-21', '2018-06-12', '2018-06-13',
                                   'synthetic', '0' * 64, cash=Decimal('.06'), synthetic_only=True)
        corrected = replace(original, revision=2, available_at='2019-01-01', cash=Decimal('.07'))
        cancelled = replace(corrected, revision=3, available_at='2019-02-01', status='CANCELLED')
        snapshot = SourceSnapshot('synthetic', 'v1', '2026-11-08', (original, corrected, cancelled), (), synthetic_only=True)
        self.assertEqual(known_versions(snapshot, date('2018-12-28'), timestamp('2018-12-28')), (original,))
        self.assertEqual(len(known_versions(snapshot, date('2018-12-28'), timestamp('2019-03-01'))), 3)
        current_export = replace(snapshot, events=(replace(original, available_at='2026-11-05'),))
        self.assertEqual(known_versions(current_export, date('2018-12-28'), timestamp('2018-12-28')), ())

    def test_empty_observations_do_not_assert_complete_coverage(self):
        cover = EventCoverage('601857.SH', '2017-01-03', '2018-12-28', '2026-11-08', '0' * 64)
        self.assertEqual(cover.status, 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
