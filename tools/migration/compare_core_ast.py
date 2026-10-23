"""Compare immutable baseline research math AST after import-name normalization only."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[2]
BASELINE='0ec7af5870c54a0ee202fbceeb97b7d87c293411'
SOURCE=Path(r'D:\Code\FactorLab-extraction')
DOMAINS=('factors/','dataset/','labels/','models/','backtest/','metrics/','validation/','portfolio/')


def normalized(body):
    tree=ast.parse(body)
    for node in ast.walk(tree):
        if isinstance(node,ast.ImportFrom) and node.module and node.module.startswith('qbot.research'):
            node.module='factorlab'+node.module[len('qbot.research'):]
        elif isinstance(node,ast.Import):
            for alias in node.names:
                if alias.name.startswith('qbot.research'):
                    alias.name='factorlab'+alias.name[len('qbot.research'):]
    return ast.dump(tree,include_attributes=False)


def main():
    files=json.loads((ROOT/'docs/migration/EXTRACTION_FILE_MANIFEST.json').read_text(encoding='utf-8'))['files']
    results=[]
    for item in files:
        name=item['source_path']
        if not name.endswith('.py') or not name.startswith(tuple('qbot/research/'+d for d in DOMAINS)):
            continue
        original=subprocess.check_output(['git','-C',str(SOURCE),'show',BASELINE+':'+name])
        current=(ROOT/item['candidate_path']).read_bytes()
        # Stage-one source_sha256 records git-archive exported bytes (Windows EOL),
        # not necessarily the raw immutable blob bytes. Keep both identities.
        results.append({'original':name,'candidate':item['candidate_path'],
                        'original_git_blob_sha256':hashlib.sha256(original).hexdigest(),
                        'recorded_archive_sha256':item['source_sha256'],
                        'status':'PASS' if normalized(original)==normalized(current) else 'REVIEW_REQUIRED'})
    report={'status':'PASS' if all(r['status']=='PASS' for r in results) else 'PARTIAL',
            'baseline':BASELINE,'modules':results,
            'scope':'Selected core math domains, AST with import package names normalized only; not authorization.'}
    (ROOT/'docs/migration/evidence/core-math-ast-comparison.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(report['status'],len(results),'modules;', [r for r in results if r['status']!='PASS'])


if __name__=='__main__':
    main()
