"""Read catalog-selected native CSMAR workbooks for anomaly keys only."""
import argparse
import csv
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from factorlab.data.csmar import native


def run(args):
    root = Path(args.private_root)
    targets = set()
    for name in ['missing_keys.csv', 'null_fields.csv']:
        with (root / name).open(encoding='utf-8') as f:
            targets.update((r['instrument'][:6], r['date'][:10]) for r in csv.DictReader(f))
    targets.update(('302132', day) for code, day in list(targets) if code == '300114')
    codes = {c for c, _ in targets}
    catalog = json.loads(Path(args.catalog).read_text(encoding='utf-8'))
    selected = []
    for source in catalog['sources']:
        for member in source['members']:
            if (member.get('family') == 'TRDNEW_Dalyr' and member.get('date_max', '') >= '2021-09-13'
                    and codes.intersection(member.get('codes', []))):
                selected.append((source, member))
    output = root / 'csmar_target_rows.jsonl'
    receipts = []
    with output.open('x', encoding='utf-8') as out:
        for source, member in selected:
            count = 0
            with zipfile.ZipFile(Path(args.source_root) / source['name']) as archive:
                with native.workbook(archive, member['name'], member['sha256'], root) as book:
                    strings = native.shared_strings(book)
                    for sheet, xml in native.sheets(book).items():
                        rows = iter(native.rows(book, xml, strings))
                        headers = next(rows)[1]
                        names = next(rows)[1]
                        units = next(rows)[1]
                        ci, di = headers.index('Stkcd'), headers.index('Trddt')
                        for row, values in rows:
                            if (values[ci], values[di]) in targets:
                                count += 1
                                out.write(json.dumps({'source': source['name'], 'member': member['name'], 'sheet': sheet,
                                    'row': row, 'source_sha256_claim': source['sha256'], 'member_sha256_verified': member['sha256'],
                                    'values': dict(zip(headers, values))}, ensure_ascii=False) + '\n')
            receipts.append({'source': source['name'], 'member': member['name'], 'selected_rows': count,
                             'headers': headers, 'names': names, 'units': units})
            print(json.dumps(receipts[-1], ensure_ascii=False), flush=True)
    (root / 'csmar-query-receipts.json').write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('--private-root', required=True)
    p.add_argument('--catalog', required=True)
    p.add_argument('--source-root', required=True)
    run(p.parse_args())
