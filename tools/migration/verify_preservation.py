"""Reobserve protected source/evidence; read only the previously named scope."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[2]
original=Path(r'D:\Code\量化TV')
snapshot=json.loads((ROOT/'docs/migration/evidence/baseline/protected-worktree.json').read_text(encoding='utf-8'))
changed=[]
for name,expected in snapshot['files'].items():
    path=original/name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
        changed.append(name)
status=subprocess.check_output(['git','-C',str(original),'status','--porcelain=v1']).decode('utf-8').strip()
head=subprocess.check_output(['git','-C',str(original),'rev-parse','HEAD']).decode().strip()
golden=[]
for kind in ('factor_golden','adjustment_golden'):
    directory=ROOT/'tests/research/fixtures'/kind
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    golden.append({'kind':kind,'synthetic_only':manifest.get('synthetic_only')})
mapping=json.loads((ROOT/'docs/migration/EXTRACTION_FILE_MANIFEST.json').read_text(encoding='utf-8'))
frozen=[]
for item in mapping['files']:
    name=item['source_path']
    if name.startswith(('tests/research/fixtures/','tests/research/reference/')):
        actual=hashlib.sha256((ROOT/item['candidate_path']).read_bytes()).hexdigest()
        if actual!=item['source_sha256']:
            frozen.append(name)
report={'status':'PASS' if not changed and status==snapshot['status'] and head==snapshot['head'] and not frozen else 'FAIL',
        'original_head_before':snapshot['head'],'original_head_after':head,
        'original_status_unchanged':status==snapshot['status'],'protected_file_count':len(snapshot['files']),
        'changed_protected_files':changed,'changed_golden_or_reference_files':frozen,
        'golden_metadata':golden,'scope':'named protected research source and pending PIT evidence; not a scan of account files'}
(ROOT/'docs/migration/evidence/protected-preservation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(report)
raise SystemExit(report['status']!='PASS')
