"""Inspect bounded existing Binance snapshot manifests without extracting archives."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assets = []
    for path in sorted(args.assets.glob('*/runs/*/manifest.json')):
        manifest = json.loads(path.read_text(encoding='utf-8'))
        tables = {}
        for name, table in manifest.get('tables', {}).items():
            payload = path.parent / table['path']
            actual = digest(payload)
            if actual != table['sha256']:
                raise ValueError(f'table digest mismatch: {payload}')
            tables[name] = dict(path=table['path'], rows=table.get('rows'),
                                sha256=actual, bytes=payload.stat().st_size)
        contract = manifest['contract']
        assets.append(dict(manifest=str(path.resolve()), manifest_sha256=digest(path),
            data_version=manifest['data_version'], interval=manifest.get('interval'),
            range=manifest['range'], integrity=manifest['integrity'],
            instrument_id=contract['instrument_id'], contract_type=contract['contract_type'],
            onboard_time=contract['onboard_time'],
            onboard_utc=datetime.fromtimestamp(contract['onboard_time']/1000, timezone.utc).isoformat(),
            tables=tables))
    frozen = {}
    for relative in ('configs/grid_lifecycle_nvda_hourly.json',
                     'configs/grid_paper_nvda_hourly.json', 'configs/grid_paper_nvda_rules.json',
                     'docs/validation/p7-r03/design.json'):
        path = args.repository / relative
        frozen[relative] = dict(sha256=digest(path), content=json.loads(path.read_text(encoding='utf-8')))
    import sys
    sys.path.insert(0, str(args.repository / 'src'))
    from factorlab.strategies.grid import GridConfig
    from factorlab.strategies.grid_execution import GridRules
    from factorlab.experiments.grid_empirical import load_hourly, D
    settings = frozen['configs/grid_paper_nvda_hourly.json']['content']
    GridConfig(**settings)
    GridRules(**{k: D(v) for k, v in frozen['configs/grid_paper_nvda_rules.json']['content'].items()})
    hourly = next(a for a in assets if a['interval'] == '1h')
    _, quality = load_hourly(Path(hourly['manifest']).parent)
    result = dict(schema_version=1, observed_at=datetime.now(timezone.utc).isoformat(),
                  existing_hourly_quality=quality, config_constructor_check='PASS',
                  scope='only supplied P6-D02 root, depth four; not a machine-wide inventory',
                  assets=assets, frozen_sources=frozen,
                  disk_free_bytes=shutil.disk_usage(args.assets).free,
                  archive_extractions=0, market_payload_copies=0,
                  new_sample_quality='NOT_VALIDATED',
                  status='P7_R04A_DATA_PARTIAL')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(dict(assets=len(assets), output=str(args.output),
                         onboard_dates=sorted({a['onboard_utc'] for a in assets}),
                         disk_free_bytes=result['disk_free_bytes'])))


if __name__ == '__main__':
    main()
