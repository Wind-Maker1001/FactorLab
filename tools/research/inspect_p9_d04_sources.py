"""Inspect fixed P9-D04 native slices and issuer PDFs; keep raw rows private."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pandas as pd
from pypdf import PdfReader
import requests

H = lambda b: hashlib.sha256(b).hexdigest()


def main(args):
    OUT = args.output
    ACQUISITION = args.acquisition
    OUT.mkdir(exist_ok=False)
    notices = []
    for symbol in ('002185', '600095', '601615'):
        listing = json.loads((ACQUISITION / symbol / 'response').read_bytes())['data']['list']
        for row in listing:
            title = row['title']
            if ('停牌' not in title and '复牌' not in title) or '股东' in title:
                continue
            code = row['art_code']
            url = f'https://pdf.dfcfw.com/pdf/H2_{code}_1.pdf'
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            directory = OUT / code
            directory.mkdir()
            (directory / 'document.pdf').write_bytes(response.content)
            text = '\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(response.content)).pages)
            (directory / 'text.txt').write_text(text, encoding='utf-8')
            notices.append(dict(symbol=symbol, title=title, url=url, sha256=H(response.content),
                                notice_date=row['notice_date'], display_time=row['display_time'],
                                eiTime=row['eiTime'], art_code=code))
            print(code, title, text[:4500], flush=True)
    (OUT / 'notices.json').write_text(json.dumps(notices, ensure_ascii=False, indent=2), encoding='utf-8')
    manifest = json.loads(args.manifest.read_bytes())
    expected = {r['member']: r['actual_sha256'] for r in manifest['entries']}
    shards, selected = [], {}
    windows = [('002185.SZ', '2025-09-01', '2025-10-31'),
               ('600095.SH', '2025-03-01', '2025-03-31'),
               ('601615.SH', '2026-01-01', '2026-01-31')]
    with zipfile.ZipFile(args.stock) as archive:
        for symbol, start, end in windows:
            selected[symbol] = {}
            for table in ('cn_stock_real_bar1d', 'cn_stock_status'):
                frames = []
                for period in pd.period_range(start, end, freq='M'):
                    member = f'stock/{table}/{period.start_time:%Y-%m-%d}_{period.end_time:%Y-%m-%d}.parquet'
                    content = archive.read(member)
                    if H(content) != expected[member]:
                        raise ValueError('FROZEN_SHARD_DRIFT: ' + member)
                    shards.append(dict(member=member, sha256=H(content), bytes=len(content)))
                    columns = (['date', 'instrument', 'open', 'high', 'low', 'close', 'volume', 'adjust_factor']
                               if table == 'cn_stock_real_bar1d' else
                               ['date', 'instrument', 'st_status', 'suspended', 'exdr', 'is_risk_warning'])
                    frame = pd.read_parquet(io.BytesIO(content), columns=columns,
                                            filters=[('instrument', '==', symbol)])
                    frames.append(frame)
                frame = pd.concat(frames).sort_values('date')
                selected[symbol][table] = json.loads(frame.to_json(orient='records', date_format='iso'))
                print(symbol, table, frame.to_string(index=False), flush=True)
        metadata = ['stock/cn_stock_status/schema.json'] + [
            r['member'] + '.receipt.json' for r in shards if '/cn_stock_status/' in r['member']]
        print('status metadata', metadata)
        for member in metadata:
            data = archive.read(member)
            print(member, data[:16000].decode('utf-8', errors='replace'))
    (OUT / 'native.json').write_text(json.dumps(dict(shards=shards, securities=selected), indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'acquisition', 'manifest', 'stock'):
        parser.add_argument('--' + name, type=Path, required=True)
    main(parser.parse_args())
