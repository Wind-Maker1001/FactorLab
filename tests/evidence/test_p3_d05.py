"""Synthetic audit regressions; no supplier data required."""
from io import BytesIO
import json
import math
from pathlib import Path
import sys
import unittest
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/data'))
import audit_p3_d05 as audit
import summarize_p3_d05_notices as notices
import pandas as pd
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.errors import DataQualityError


def result(values):
    frame = pd.DataFrame({'date': pd.to_datetime(['2018-01-02'] * len(values)),
                          'symbol': '601857.SH', 'amount': values})
    for field in audit.FIELDS[:-1]:
        frame[field] = 10.
    return QueryResult(frame, SourceMetadata('SYNTHETIC', 'v1'))


class AuditTests(unittest.TestCase):
    def test_one_ulp_is_not_silently_rounded_and_private_values_stay_private(self):
        public, private = audit.exact_differences(result([100.]), result([math.nextafter(100., math.inf)]))
        self.assertEqual(len(public), 1)
        self.assertEqual(public[0]['ulps_at_csmar'], 1)
        self.assertGreater(public[0]['absolute_delta'], 0)
        self.assertNotIn('csmar', public[0])
        self.assertIn('csmar', private[0])

    def test_missing_amount_is_not_a_zero_value_or_arithmetic_delta(self):
        public, _ = audit.exact_differences(result([100.]), result([float('nan')]))
        self.assertEqual(public[0]['type'], 'MISSING')
        self.assertNotIn('absolute_delta', public[0])

    def test_duplicate_keys_refuse_comparison(self):
        with self.assertRaises(DataQualityError):
            audit.exact_differences(result([100., 100.]), result([100.]))

    def test_boundaries_do_not_assume_january_first_is_a_session(self):
        boundary = audit.boundaries(pd.to_datetime(['2017-01-03', '2017-12-29', '2018-01-02']))
        self.assertEqual(boundary['2017'], {'count': 2, 'first': '2017-01-03', 'last': '2017-12-29'})
        self.assertEqual(boundary['2018']['first'], '2018-01-02')

    def test_archive_selection_excludes_other_symbols_and_years(self):
        stream = BytesIO()
        table = 'stock/synthetic'
        with zipfile.ZipFile(stream, 'w') as z:
            z.writestr(table + '/schema.json', '{}')
            frame = pd.DataFrame({'date': pd.to_datetime(['2017-01-03', '2017-01-03']),
                                  'instrument': ['601857.SH', 'OTHER']})
            payload = BytesIO()
            frame.to_parquet(payload)
            name = table + '/2017-01-01_2017-12-31.parquet'
            z.writestr(name, payload.getvalue())
            z.writestr(name + '.receipt.json', json.dumps({'sha256': audit.sha256(payload.getvalue()),
                                                         'exported_at_utc': '2026-11-09T00:00:00Z'}))
            z.writestr(table + '/2019-01-01_2019-12-31.parquet', b'not read')
        with zipfile.ZipFile(stream) as z:
            frame, binding = audit.archive_table(z, table)
        self.assertEqual(frame.instrument.tolist(), ['601857.SH'])
        self.assertEqual(len(binding['members']), 1)
        self.assertTrue(binding['members'][0]['receipt_payload_hash_matches'])

    def test_failed_quality_and_empty_factors_cannot_gain_raw_pass(self):
        checks = {k: {'status': 'PASS'} for k in ('coverage', 'duplicates', 'missing', 'ohlc', 'anomalies')}
        source = {'checks': checks, 'factors': []}
        self.assertEqual(audit.raw_eligibility({'cs': source, 'bq': source}), 'UNKNOWN')
        checks['missing']['status'] = 'FAIL'
        self.assertEqual(audit.raw_eligibility({'cs': source, 'bq': source}), 'FAIL')

    def test_incomplete_official_pagination_does_not_prove_event_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = json.dumps({'result': [], 'pageHelp': {'pageCount': 2, 'pageNo': 1, 'total': 5},
                                  'beginDate': '2017-01-01', 'endDate': '2017-12-31'}).encode()
            (root / 'response.bin').write_bytes(payload)
            (root / 'receipt.json').write_text(json.dumps({'sha256': audit.sha256(payload), 'path': 'private'}))
            summary = notices.summarize([root], [])
        self.assertEqual(summary['indices'][0]['pagination_coverage'], 'FAIL')
        self.assertEqual(summary['event_completeness'], 'UNKNOWN')
        self.assertNotIn('path', summary['indices'][0]['receipt'])

    def test_changed_official_response_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'response.bin').write_bytes(b'changed')
            (root / 'receipt.json').write_text(json.dumps({'sha256': '0' * 64}))
            with self.assertRaises(ValueError):
                notices.response(root)

    def test_html_challenge_is_not_an_official_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b'<html>cookie challenge</html>'
            (root / 'response.bin').write_bytes(payload)
            (root / 'receipt.json').write_text(json.dumps({'sha256': audit.sha256(payload), 'status': 200}))
            summary = notices.summarize([], [root])
        self.assertFalse(summary['pdf_attempts'][0]['pdf_magic_present'])
        self.assertEqual(summary['pdf_attempts'][0]['official_pdf_qualification'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
