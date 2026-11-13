"""Bounded official-source probes; payloads stay outside Git, receipts are compact."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.assets.mkdir(parents=True, exist_ok=False)
    args.output.mkdir(parents=True, exist_ok=False)
    budget = dict(expected_rows=2160, rest_full_pages=2, rest_probe_requests=2,
                  public_object_probes_max=4, retry_requests=0,
                  raw_estimated_bytes=[327680, 2097152], parquet_estimated_bytes=[104858, 629146],
                  temporary_reserved_bytes=10485760, total_reserved_bytes=104857600,
                  disk_free_bytes=shutil.disk_usage(args.assets).free,
                  cleanup='Retain useful official payloads and receipts; remove only failed partial payloads in new request directories; never touch R02.',
                  window=['2026-05-14T00:00:00Z', '2026-08-12T00:00:00Z'],
                  connect_timeout_seconds=8, total_timeout_seconds=20)
    (args.output / 'resource-budget.json').write_text(json.dumps(budget, indent=2)+'\n')
    observations = []

    def request(label, url):
        directory = Path(tempfile.mkdtemp(prefix=label+'-', dir=args.assets))
        body, headers = directory/'body', directory/'headers'
        started = datetime.now(timezone.utc).isoformat()
        tick = time.monotonic()
        result = subprocess.run(['curl', '--silent', '--show-error', '--connect-timeout', '8',
                                 '--max-time', '20', '--max-filesize', '2097152',
                                 '--dump-header', str(headers), '--output', str(body),
                                 '--write-out', '%{http_code}', url], capture_output=True, text=True)
        payload = body.read_bytes() if body.exists() else b''
        record = dict(label=label, url=url, started_at=started,
                      finished_at=datetime.now(timezone.utc).isoformat(),
                      seconds=round(time.monotonic()-tick, 3), curl_exit=result.returncode,
                      http_status=result.stdout, error=result.stderr.strip(),
                      received_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest() if payload else None,
                      raw_path=str(body) if payload else None,
                      headers=headers.read_text(errors='replace') if headers.exists() else '')
        observations.append(record)
        (args.output/'network-observations.json').write_text(json.dumps(dict(observations=observations,
            actual_requests=len(observations), retries=0), indent=2)+'\n')
        print(json.dumps(record), flush=True)
        return record, payload

    probe, payload = request('rest-window', 'https://fapi.binance.com/fapi/v1/klines?symbol=NVDAUSDT&interval=1h&startTime=1778716800000&endTime=1778720399999&limit=1')
    request('rest-identity', 'https://fapi.binance.com/fapi/v1/exchangeInfo')
    if probe['curl_exit'] == 0 and probe['http_status'] == '200' and isinstance(json.loads(payload), list) and json.loads(payload):
        print('REST_CANDIDATE_AVAILABLE', flush=True)
        return
    # Probe exact monthly objects, not the HTML directory shell or a bucket listing.
    for month in ('05', '06', '07', '08'):
        request('public-2026-'+month,
                'https://data.binance.vision/data/futures/um/monthly/klines/NVDAUSDT/1h/NVDAUSDT-1h-2026-'+month+'.zip')


if __name__ == '__main__':
    main()
