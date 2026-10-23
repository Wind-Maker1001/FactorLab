"""Export reviewed tracked paths from the immutable migration baseline.

No working-tree data, vendor responses, credentials, or historical report
rewrites. source_blob is the raw Git object ID; source_sha256 is the archive
export SHA256, which can include Windows EOL conversion. The supplemental
bind_git_blob_identities.py independently adds the original raw blob SHA256.
"""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(r'D:\Code\FactorLab-extraction')
DEST = Path(__file__).resolve().parents[2]
REV = '0ec7af5870c54a0ee202fbceeb97b7d87c293411'
SCOPES = ['qbot/research', 'qbot/engineering/yhh30m', 'tests/research',
          'tests/model_readiness', 'tests/market_structure', 'tests/market_admission',
          'tests/minute_data', 'tests/evidence', 'tests/fixtures/research',
          'tests/conftest.py', 'examples/research', 'configs/research',
          'dev/requirements-research.txt', 'dev/requirements-research-ci.txt',
          'dev/requirements-source-review.txt', 'dev/requirements-evidence-ci.txt',
          '.github/scripts/ci', 'LICENSE', 'docs/LICENSE', '.gitattributes']


def main():
    assert subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).decode().strip() == REV
    tracked = subprocess.check_output(['git', '-C', str(ROOT), 'ls-tree', '-r', REV, '--', *SCOPES]).decode('utf-8')
    entries = {}
    for line in tracked.splitlines():
        meta, name = line.split('\t', 1)
        mode, kind, blob = meta.split()
        assert kind == 'blob' and mode in ('100644', '100755'), name
        entries[name] = blob
    stream = subprocess.check_output(['git', '-C', str(ROOT), 'archive', '--format=tar', REV, *SCOPES])
    manifest = []
    with tarfile.open(fileobj=io.BytesIO(stream)) as archive:
        for member in archive:
            if not member.isfile():
                continue
            name = member.name
            assert name in entries
            target = DEST / name
            assert target.resolve().is_relative_to(DEST.resolve())
            assert not target.exists(), 'refusing overwrite: ' + name
            body = archive.extractfile(member).read()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            manifest.append({'source_path': name, 'source_blob': entries[name],
                             'source_sha256': hashlib.sha256(body).hexdigest(),
                             'candidate_path': name, 'stage': 'legacy-namespace'})
    (DEST / 'tests/__init__.py').touch()
    (DEST / 'examples/__init__.py').touch()
    (DEST / 'README.md').write_text('# FactorLab independent candidate\n\nNO LIVE MONEY. PIT_UNVERIFIED. FORMAL_ADMISSION_BLOCKED.\n', encoding='utf-8')
    (DEST / 'docs/migration/EXTRACTION_FILE_MANIFEST.json').write_text(
        json.dumps({'baseline': REV, 'files': manifest,
                    'excluded': 'All upstream data/results and unreviewed historical docs; no credentials read.'}, indent=2), encoding='utf-8')
    print('exported', len(manifest), 'baseline-tracked files; source_sha256 binds exported bytes')


if __name__ == '__main__':
    main()
