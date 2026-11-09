"""Four-record representation experiment; supplier algorithms remain unresolved."""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re

DATES = ('2017-02-27', '2017-05-03', '2018-10-12', '2018-12-13')
VERSIONS = {
    'csmar': 'sha256:d77417709364d664f1041418c51a434e836a72cbebbc53f7b2158556478ccb64',
    'bigquant': 'sha256:eb816b086d75984f726bd436a9d479a35571922914dcf5d952bb43b172a4c4d6',
}


def binary64(text):
    match = re.fullmatch(r'np\.float64\(([-+0-9.eE]+)\)', text)
    if match is None:
        raise ValueError('unsupported saved numeric representation')
    return float(match[1])


def analyze(raw):
    data = json.loads(raw)
    if data['source_versions'] != VERSIONS:
        raise ValueError('private sample source version mismatch')
    differences = data['differences']
    native = {row['date']: row['native_Dnvaltrd'] for row in data['native_amounts']}
    if tuple(row['date'] for row in differences) != DATES or set(native) != set(DATES):
        raise ValueError('expected exactly the four authorized dates')
    rows = []
    for row in differences:
        cs, bq = binary64(row['csmar']), binary64(row['bigquant'])
        decimal = Decimal(native[row['date']])
        delta = Decimal.from_float(bq) - Decimal.from_float(cs)
        if delta != Decimal(row['bigquant_minus_csmar_exact']):
            raise ValueError('saved delta inconsistent with binary64 inputs')
        rows.append({
            'date': row['date'],
            'native_input_sha256': hashlib.sha256(native[row['date']].encode()).hexdigest(),
            'native_decimal_exactly_representable_binary64': Decimal.from_float(float(decimal)) == decimal,
            'native_to_prepared_equal': float(decimal) == cs,
            'exact_equal': cs == bq,
            'bigquant_minus_csmar_exact': str(delta),
            'signed_ulps': float(delta / Decimal.from_float(math.ulp(cs))),
            'adjacent_binary64': math.nextafter(cs, bq) == bq,
            'candidate_decimal_places_equal': {
                str(p): Decimal.from_float(cs).quantize(Decimal(1).scaleb(-p)) ==
                        Decimal.from_float(bq).quantize(Decimal(1).scaleb(-p))
                for p in (0, 2, 4, 6, 8)
            },
        })
    return {
        'schema': 'P4D02_AMOUNT_EXPERIMENT_V1',
        'input_sha256': hashlib.sha256(raw).hexdigest(),
        'source_versions': VERSIONS,
        'evidence_status': 'PREVIOUS_EVIDENCE_REUSED',
        'observations': rows,
        'economic_unit_bigquant': 'UNKNOWN',
        'bigquant_original_decimal_input': 'UNKNOWN',
        'supplier_algorithm': 'UNRESOLVED',
        'historical_known_at': 'UNKNOWN',
        'exact_cross_source_status': 'FAIL',
        'candidate_rule_certified': False,
        'note': 'Rounding agreement is a candidate experiment, not a field or precision contract.',
    }


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--private-sample', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.private_sample.read_bytes())
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
