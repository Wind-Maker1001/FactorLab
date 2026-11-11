"""Bounded source acquisition for the three P9-D04 suspension windows."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

import requests


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(output, metadata):
    output.mkdir(exist_ok=False, parents=True)
    session = requests.Session()
    receipts = []
    urls = [('dictionary', 'https://bigquant.com/data/datasources/cn_stock_status')]
    for symbol, start, end in [('002185', '2025-09-20', '2025-10-20'),
                               ('600095', '2025-03-12', '2025-04-02'),
                               ('601615', '2026-01-08', '2026-01-27')]:
        urls.append((symbol, 'https://np-anotice-stock.eastmoney.com/api/security/ann?'
                     f'sr=-1&page_size=100&page_index=1&ann_type=A&stock_list={symbol}'
                     f'&begin_time={start}&end_time={end}'))
    for name, url in urls:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        directory = output / name
        directory.mkdir()
        data = response.content
        (directory / 'response').write_bytes(data)
        receipts.append(dict(name=name, url=url, status=response.status_code,
                             sha256=digest(data), bytes=len(data)))
        if name != 'dictionary':
            result = response.json()
            print(name, json.dumps(result, ensure_ascii=False))
        else:
            print(name, data[:1000].decode('utf-8', errors='replace'))
    with zipfile.ZipFile(metadata) as archive:
        catalog = archive.read('catalog.json')
        (output / 'catalog.json').write_bytes(catalog)
        data = json.loads(catalog)
        print('catalog type', type(data).__name__)
        print(json.dumps(data, ensure_ascii=False)[:1000])
    (output / 'acquisition.json').write_text(json.dumps(receipts, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    args = parser.parse_args()
    run(args.output, args.metadata)
