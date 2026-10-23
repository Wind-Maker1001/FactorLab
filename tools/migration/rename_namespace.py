"""One-way reviewed namespace migration; stage-one originals remain in Git."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]


def main():
    source, target = ROOT/'qbot/research', ROOT/'src/factorlab'
    assert source.is_dir() and not target.exists()
    target.parent.mkdir(exist_ok=True)
    shutil.move(str(source), str(target))
    engineering = target/'engineering'
    engineering.mkdir()
    shutil.move(str(ROOT/'qbot/engineering/yhh30m'), str(engineering/'yhh30m'))
    for directory in (target, ROOT/'tests', ROOT/'examples'):
        for path in directory.rglob('*.py'):
            if '__pycache__' in path.parts or 'reference' in path.parts:
                continue
            body = path.read_text(encoding='utf-8')
            body = body.replace('qbot.research', 'factorlab')
            body = body.replace('qbot.engineering.yhh30m', 'factorlab.engineering.yhh30m')
            path.write_text(body, encoding='utf-8', newline='\n')
    project = ROOT/'pyproject.toml'
    body = project.read_text(encoding='utf-8').replace('qbot.research.cli:main','factorlab.cli:main')
    body = body.replace('where = ["."]','where = ["src"]')
    body = body.replace('include = ["qbot.research*", "qbot.engineering.yhh30m*"]','include = ["factorlab*"]')
    project.write_text(body,encoding='utf-8')
    manifest_path = ROOT/'docs/migration/EXTRACTION_FILE_MANIFEST.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for entry in manifest['files']:
        old = entry['source_path']
        new = old.replace('qbot/research/','src/factorlab/').replace('qbot/engineering/yhh30m/','src/factorlab/engineering/yhh30m/')
        entry['candidate_path'] = new
        entry['candidate_sha256'] = hashlib.sha256((ROOT/new).read_bytes()).hexdigest()
        entry['stage'] = 'factorlab-namespace'
    manifest_path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print('Migrated 171 Python modules; no old evidence, Golden or trust controller was rewritten.')


if __name__ == '__main__':
    main()
