"""Refresh candidate postimages without changing any original identity."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
path=ROOT/'docs/migration/EXTRACTION_FILE_MANIFEST.json'
value=json.loads(path.read_text(encoding='utf-8'))
for item in value['files']:
    if item['candidate_path'] is None:
        preserved=ROOT/item['private_preservation_path']
        assert hashlib.sha256(preserved.read_bytes()).hexdigest()==item['source_sha256']
        continue
    target=ROOT/item['candidate_path']
    if target.is_file():
        item['candidate_sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
        item['status']='PRESENT'
    elif item['source_path']=='examples/research/notebooks/tdx_data_quality_review.ipynb':
        preserved=ROOT/'local-private-archive/tdx_data_quality_review.original.ipynb'
        assert hashlib.sha256(preserved.read_bytes()).hexdigest()==item['source_sha256']
        item.update(candidate_path=None,candidate_sha256=None,status='QUARANTINED_NOT_FOR_PUBLICATION',
                    private_preservation_path='local-private-archive/tdx_data_quality_review.original.ipynb')
    else:
        raise FileNotFoundError(target)
value['identity_is_authorization']=False
value['formal_admission']='FORMAL_ADMISSION_BLOCKED'
value['new_modules']=[p.relative_to(ROOT).as_posix() for p in (ROOT/'src/factorlab').rglob('*.py')
                      if p.relative_to(ROOT).as_posix() not in {x['candidate_path'] for x in value['files']}]
path.write_text(json.dumps(value,indent=2),encoding='utf-8')
print('Preserved all original identities;',len(value['files']),'postimages recorded')
