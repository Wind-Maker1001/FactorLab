"""Index local evidence bytes without copying raw logs or private paths to archives."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
EVIDENCE=ROOT/'docs/migration/evidence'
paths={p for p in EVIDENCE.rglob('*') if p.is_file() and p.name!='evidence-index.json'}
for directory in (ROOT/'local-ci').iterdir():
    if directory.is_dir():
        paths.update(p for p in directory.iterdir() if p.is_file())
for directory in (ROOT/'local-artifacts').iterdir():
    if directory.is_dir():
        paths.update(p for p in directory.iterdir() if p.is_file())
report={'scope':'Actual local evidence files and top-level CI receipts/logs/XML/inputs/artifacts; raw bytes retained locally',
        'formal_admission':'FORMAL_ADMISSION_BLOCKED',
        'files':{p.relative_to(ROOT).as_posix():{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
                 for p in sorted(paths)}}
(EVIDENCE/'evidence-index.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print('Indexed',len(paths),'local evidence files')
