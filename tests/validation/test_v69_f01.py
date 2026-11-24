"""Evidence integrity and scope checks for the frozen V69-F01 discovery."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v69_f01', ROOT / 'tools/validate_tradfi_demo.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DiscoveryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence = Path(self.temp.name) / 'evidence'
        shutil.copytree(MODULE.ROOT, self.evidence)

    def test_frozen_coverage_and_execution_scope(self):
        mapping, grouped = MODULE.build(self.evidence)
        self.assertEqual({k: len(v) for k, v in grouped.items()},
                         {'TRADING': 20, 'PENDING_TRADING': 2, 'SETTLING': 2})
        self.assertEqual(set(grouped['PENDING_TRADING']), {'BMNRUSDT', 'DISUSDT'})
        self.assertEqual(set(grouped['SETTLING']), {'FAKEKR000660USDT', 'FAHOODUSDT'})
        self.assertEqual({r['stock']: r['classification'] for r in mapping},
                         dict(AAPL='MAINNET_ONLY', SNDK='MAINNET_ONLY', BA='NOT_LISTED',
                              C='NOT_LISTED', JPM='MAINNET_ONLY', GE='NOT_LISTED',
                              TQQQ='MAINNET_ONLY', QQQ='DEMO_LISTED_NOT_EXECUTED'))
        self.assertTrue(all(not r['issuer_identity_verified'] for r in mapping))
        validation = json.loads((self.evidence / 'validation.json').read_text())
        self.assertEqual(validation['market_discovery']['status'], 'COMPLETE')
        self.assertTrue(all(validation['market_discovery']['market_coverage'].values()))
        self.assertEqual(validation['trading_execution']['orders_submitted'], 0)
        self.assertFalse(validation['trading_execution']['execution_verified'])
        self.assertEqual(validation['market_discovery']['qqq_rules']['indicative_limit_buy_quantity'], '0.01')

    def test_modified_evidence_is_rejected(self):
        target = self.evidence / 'demo_listing_evidence.json'
        target.write_bytes(target.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            MODULE.build(self.evidence)

    def test_error_response_is_not_market_coverage(self):
        path = self.evidence / 'qqq_market_evidence.json'
        data = json.loads(path.read_text())
        data['funding'] = {'error_type': 'HTTPError', 'message': 'failed'}
        path.write_text(json.dumps(data))
        MODULE.build(self.evidence)
        result = json.loads((self.evidence / 'validation.json').read_text())
        self.assertFalse(result['market_discovery']['market_coverage']['funding'])


if __name__ == '__main__':
    unittest.main()
