"""Certify pinned official monthly archives without extracting or filling bars."""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

import pandas as pd


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--assets', type=Path, required=True)
    args = parser.parse_args()
    repo = args.repository.resolve()
    sys.path.insert(0, str(repo/'src'))
    from factorlab.integrations.binance.market import bars, completeness
    from factorlab.integrations.binance.store import publish, Reader
    from factorlab.data.evidence import json_bytes
    output = repo/'docs/validation/p7-r04b'
    network = json.loads((output/'network-observations.json').read_text())
    extra_path = output/'supplementary-requests.json'
    extra = json.loads(extra_path.read_text()) if extra_path.exists() else []
    def get(label, url):
        previous = next((r for r in extra if r['url'] == url), None)
        if previous is not None:
            return previous
        directory = Path(tempfile.mkdtemp(prefix=label+'-', dir=args.assets))
        path = directory/'body'
        started = datetime.now(timezone.utc).isoformat()
        r = subprocess.run(['curl', '-sS', '--connect-timeout', '8', '--max-time', '20',
                            '--max-filesize', '2097152', '-o', str(path), '-w', '%{http_code}', url],
                           capture_output=True, text=True)
        record = dict(label=label, url=url, started_at=started,
                      finished_at=datetime.now(timezone.utc).isoformat(), curl_exit=r.returncode,
                      http_status=r.stdout, error=r.stderr.strip(), raw_path=str(path) if path.exists() else None,
                      received_bytes=path.stat().st_size if path.exists() else 0,
                      sha256=digest(path) if path.exists() else None)
        extra.append(record)
        (output/'supplementary-requests.json').write_bytes(json_bytes(extra))
        return record
    objects = network['observations'][2:]
    for record in objects:
        checksum = get(record['label']+'-checksum', record['url']+'.CHECKSUM')
        if checksum['http_status'] != '200' or checksum['curl_exit']:
            raise ValueError('official checksum unavailable')
        expected = Path(checksum['raw_path']).read_text().split()[0]
        if expected != digest(Path(record['raw_path'])):
            raise ValueError('official archive checksum mismatch')
    earliest = get('first-month', 'https://data.binance.vision/data/futures/um/monthly/klines/NVDAUSDT/1h/NVDAUSDT-1h-2026-03.zip')
    for kind in ('fundingRate', 'markPriceKlines', 'indexPriceKlines'):
        suffix = '/1h/NVDAUSDT-1h-2026-05.zip' if kind != 'fundingRate' else '/NVDAUSDT-fundingRate-2026-05.zip'
        get('auxiliary-'+kind, 'https://data.binance.vision/data/futures/um/monthly/'+kind+'/NVDAUSDT'+suffix)
    def rows(record):
        with zipfile.ZipFile(record['raw_path']) as archive:
            names = archive.namelist()
            if len(names) != 1 or not names[0].endswith('.csv'):
                raise ValueError('unexpected archive members')
            return list(csv.reader(io.StringIO(archive.read(names[0]).decode())))[1:]
    a = int(pd.Timestamp('2026-05-14T00:00:00Z').timestamp()*1000)
    b = int(pd.Timestamp('2026-08-12T00:00:00Z').timestamp()*1000)
    raw = [r for obj in objects for r in rows(obj) if a <= int(r[0]) < b]
    times = [int(r[0]) for r in raw]
    if times != sorted(set(times)):
        raise ValueError('raw timestamps not strictly ordered and unique')
    cutoff = int(datetime.now(timezone.utc).timestamp()*1000)
    frame = bars(raw, a, b, 3600000, cutoff)
    replay = bars(raw, a, b, 3600000, cutoff)
    pd.testing.assert_frame_equal(frame, replay)
    old_path = Path('D:/Code/FactorLab-worktrees/p6-d02-binance-history/results/p6-d02/hourly/runs/bddaab1cd5c943e28caa62c0fc49ef33')
    old = Reader(old_path)
    old_manifest = old.manifest
    old.read('bars')
    frozen = json.loads((repo/'docs/validation/p7-r04a/preregistration-lock.json').read_text())
    locked_blob = subprocess.run(['git', '-C', str(repo), 'show', 'HEAD:'+frozen['path']],
                                 capture_output=True, check=True).stdout
    checkout_bytes = (repo/frozen['path']).read_bytes()
    if (hashlib.sha256(locked_blob).hexdigest() != frozen['sha256']
            or checkout_bytes.replace(b'\r\n', b'\n') != locked_blob):
        raise ValueError('preregistration changed')
    old_receipt = next(r for r in old_manifest['raw_receipts'] if r['endpoint'] == 'exchangeInfo' and r['status']==200)
    old_raw = old_path/old_receipt['raw_path']
    if digest(old_raw) != old_receipt['sha256']:
        raise ValueError('old identity digest mismatch')
    contract = old_manifest['contract']
    if (contract['symbol'], contract['contract_type'], contract['quote_asset'], contract['margin_asset']) != ('NVDAUSDT','TRADIFI_PERPETUAL','USDT','USDT'):
        raise ValueError('contract identity mismatch')
    quality = completeness(frame, a, b, 3600000, cutoff, contract['onboard_time'])
    if any(Decimal(r[3]) <= 0 for r in raw):
        raise ValueError('nonpositive low')
    quality.update(duplicate_timestamps=0, invalid_ohlcv=0, zero_volume_bars=sum(Decimal(r[5])==0 for r in raw),
                   flat_bars=sum(Decimal(r[2])==Decimal(r[3]) for r in raw),
                   raw_roundtrip_equal=True, implicit_fill=False, original_rows=len(raw))
    root = args.assets/'certified'
    pending = root/'pending'
    (pending/'requests').mkdir(parents=True, exist_ok=False)
    receipts=[]
    # Reference the one retained source object; no raw payload copies.
    for r in objects:
        receipts.append(dict(endpoint='PUBLIC_DATA_MONTHLY_KLINES', url=r['url'], status=200,
                             acquired_at=r['finished_at'], sha256=r['sha256'], external_raw_path=r['raw_path']))
    metadata=dict(contract=contract, interval='1h', acquired_at=max(r['finished_at'] for r in objects),
                  history_cutoff_at=datetime.now(timezone.utc).isoformat(),
                  range=dict(start_ms=a,end_ms=b,end_exclusive=True), integrity=quality,
                  source='BINANCE_USDM_PUBLIC_DATA', external_raw_receipts=receipts,
                  identity_evidence=dict(path=str(old_raw), sha256=old_receipt['sha256'],
                                         classification='C: 2026-11-10 exchangeInfo, not refreshed'),
                  generator='tools/p7_r04b_certify.py', tool_sha256=digest(Path(__file__)))
    snapshot=publish(root,pending,{'bars':frame},metadata)
    snapshot.read('bars')
    # Compare serialized repeat conversion in memory, not a second retained asset.
    buf=io.BytesIO(); replay.to_parquet(buf,index=False)
    if hashlib.sha256(buf.getvalue()).hexdigest()!=snapshot.manifest['tables']['bars']['sha256']:
        raise ValueError('repeat conversion bytes differ')
    first = rows(earliest)[0] if earliest['http_status']=='200' and not earliest['curl_exit'] else None
    report=dict(status='P7_R04B_ASSUMPTION_BOUND_DATA_READY' if quality['complete'] else 'P7_R04B_DATA_PARTIAL',
                snapshot=str(snapshot.root), generation_id=snapshot.root.name,
                manifest_sha256=digest(snapshot.root/'manifest.json'), data_version=snapshot.manifest['data_version'],
                table=snapshot.manifest['tables']['bars'], quality=quality,
                first_official_archive_bar_utc=pd.Timestamp(int(first[0]),unit='ms',tz='UTC').isoformat() if first else None,
                first_bar_scope='Earliest observed in March listing-month archive; not an exhaustive earlier-month absence proof',
                identity=metadata['identity_evidence'], units=dict(price='USDT per base unit',volume='base units',quote_volume='USDT',precision='original decimal strings'),
                old_r02_bars_sha256=old_manifest['tables']['bars']['sha256'],
                old_r02_manifest_sha256=digest(old_path/'manifest.json'), preregistration_sha256=frozen['sha256'],
                actual_requests=network['actual_requests']+len(extra), retries=0,
                retained_asset_bytes=sum(p.stat().st_size for p in args.assets.rglob('*') if p.is_file()),
                disk_free_bytes=__import__('shutil').disk_usage(args.assets).free,
                research_admission='DATA_ONLY; R04B-E01 and provenance declaration required before formal matrix',
                boundaries=['NO LIVE MONEY','PIT_UNVERIFIED','HFQ_UNVERIFIED','FORMAL_ADMISSION_BLOCKED'])
    (output/'certification.json').write_bytes(json_bytes(report))
    (output/'snapshot-manifest.json').write_bytes((snapshot.root/'manifest.json').read_bytes())
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
