"""Capture local baseline identity without accessing account/config secrets."""
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(r'D:\Code\FactorLab-extraction')
DEST = Path(__file__).resolve().parents[2] / 'docs/migration'


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args]).decode('utf-8').strip()


def main():
    payload = {
        'recorded_at': datetime.now(timezone.utc).isoformat(),
        'python': sys.version, 'executable': sys.executable,
        'platform': platform.platform(),
        'baseline_sha': git(ROOT, 'rev-parse', 'HEAD'),
        'baseline_tree': git(ROOT, 'rev-parse', 'HEAD^{tree}'),
        'branch': git(ROOT, 'branch', '--show-current'),
        'status': git(ROOT, 'status', '--porcelain=v1'),
        'packages': {d.metadata['Name']: d.version for d in metadata.distributions()},
        'safety': ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED'],
    }
    assert payload['baseline_sha'] == '0ec7af5870c54a0ee202fbceeb97b7d87c293411'
    (DEST / 'BASELINE_ENVIRONMENT.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    original = Path(r'D:\Code\量化TV')
    protected = {}
    # Restrict content reads to the research source and named pending audit evidence.
    for relative in ('qbot/research', 'docs/research/PIT_EVIDENCE_CONSOLIDATION.md',
                     'docs/research/PIT_P0_TARGETED_EVIDENCE_2026-10-22.md',
                     'docs/research/evidence/pit-evidence-consolidation-20261022-v1',
                     'docs/research/evidence/pit-p0-targeted-20261022-v1',
                     'REPOSITORY_AUDIT_2026-10-15.md'):
        path = original / relative
        for item in ([path] if path.is_file() else path.rglob('*')):
            if item.is_file() and '__pycache__' not in item.parts:
                protected[item.relative_to(original).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    snapshot = {'head': git(original, 'rev-parse', 'HEAD'),
                'status': git(original, 'status', '--porcelain=v1'), 'files': protected}
    (DEST / 'evidence/baseline/protected-worktree.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
    print(json.dumps({'baseline': payload['baseline_sha'], 'python': platform.python_version(),
                      'protected_files': len(protected)}, indent=2))


if __name__ == '__main__':
    main()
