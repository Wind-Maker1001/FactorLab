"""Inspect only build archives; report pattern names/paths, never secret values."""
import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import tarfile
import zipfile

PATTERNS = {
    'private_key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'aws_access_key': rb'\bAKIA[A-Z0-9]{16}\b',
    'github_token': rb'\bgh[pousr]_[A-Za-z0-9]{36,255}\b',
    'slack_token': rb'\bxox[baprs]-[0-9]{10,}-[A-Za-z0-9-]{10,}\b',
}
ROOT=Path(__file__).resolve().parents[2]


def safe_archive_path(name):
    # Archive paths use POSIX separators, regardless of the validation host.
    stripped=name[:-1] if name.endswith('/') else name
    path=PurePosixPath(stripped)
    return bool(stripped and not any(c in '<>:"\\|?*' or ord(c)<32 or ord(c)==127 for c in stripped) and
                not path.is_absolute() and not PureWindowsPath(stripped).drive and
                path.as_posix()==stripped and all(p not in ('','.','..') and p==p.rstrip(' .') and
                not re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)',p)
                for p in stripped.split('/')))


def scan(directory, output):
    findings=[]
    archives=[]
    mapping=json.loads((ROOT/'docs/migration/EXTRACTION_FILE_MANIFEST.json').read_text(encoding='utf-8'))
    golden={item['source_path']:item['source_sha256'] for item in mapping['files']
            if item['source_path'].startswith('tests/research/fixtures/') and
            item['source_path'].endswith('.parquet')}
    for archive_path in sorted(directory.glob('*')):
        if archive_path.suffix=='.whl':
            with zipfile.ZipFile(archive_path) as archive:
                names=[m.filename for m in archive.infolist()]
                if len(names)!=len(set(names)):
                    findings.append({'file':archive_path.name,'reason':'duplicate ZIP member'})
                for info in archive.infolist():
                    if stat.S_ISLNK(info.external_attr >> 16):
                        findings.append({'file':info.filename,'reason':'ZIP symbolic link'})
                members={n:archive.read(n) for n in archive.namelist() if not n.endswith('/')}
        elif archive_path.name.endswith('.tar.gz'):
            with tarfile.open(archive_path) as archive:
                entries=archive.getmembers()
                names=[m.name for m in entries]
                if len(names)!=len(set(names)):
                    findings.append({'file':archive_path.name,'reason':'duplicate TAR member'})
                for entry in entries:
                    if not entry.isfile() and not entry.isdir():
                        findings.append({'file':entry.name,'reason':'TAR link or special member'})
                members={m.name:archive.extractfile(m).read() for m in archive if m.isfile()}
        else:
            continue
        for name in names:
            if not safe_archive_path(name):
                findings.append({'file':name,'reason':'unsafe archive path'})
        parquet=[]
        for name, body in members.items():
            path=PurePosixPath(name)
            if any(part in ('qbot','local-private-archive','data','results') for part in path.parts):
                # factorlab/data is production code, not supplier data.
                if not ('factorlab' in path.parts and path.parts[path.parts.index('factorlab')+1]=='data'):
                    findings.append({'file':name,'reason':'prohibited data/legacy directory'})
            if path.suffix in ('.ipynb','.log','.xml','.pem','.key'):
                findings.append({'file':name,'reason':'prohibited private/notebook/raw-evidence file'})
            if path.suffix=='.parquet':
                parquet.append(name)
                logical=name[name.index('tests/research/fixtures/'):] if 'tests/research/fixtures/' in name else name
                if logical not in golden or hashlib.sha256(body).hexdigest()!=golden[logical]:
                    findings.append({'file':name,'reason':'Parquet absent from exact preserved baseline path/hash inventory'})
            for label, pattern in PATTERNS.items():
                if re.search(pattern,body):
                    findings.append({'file':name,'reason':'credential pattern '+label})
            if path.suffix=='.py' and ('/factorlab/' in '/'+name):
                try:
                    tree=ast.parse(body,filename=name)
                except (SyntaxError,ValueError) as exc:
                    findings.append({'file':name,'reason':'invalid Python source: '+type(exc).__name__})
                    continue
                for node in ast.walk(tree):
                    names=[a.name for a in node.names] if isinstance(node,ast.Import) else (
                        [node.module or ''] if isinstance(node,ast.ImportFrom) else [])
                    for module in names:
                        if module=='qbot' or module.startswith('qbot.'):
                            findings.append({'file':name,'reason':'production Legacy import'})
        archives.append({'file':archive_path.name,'sha256':hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                         'members_scanned':len(members),'synthetic_parquet':parquet})
    if len(archives)!=2:
        findings.append({'reason':'expected exactly wheel and sdist'})
    report={'status':'PASS' if not findings else 'FAIL','scope':'archive allowlist and explicit secret/Legacy patterns only',
            'archives':archives,'findings':findings,'license_clearance':'BLOCKED_SCOPE_UNRESOLVED',
            'formal_admission':'FORMAL_ADMISSION_BLOCKED',
            'limits':'Not a proof of all-secret absence, data rights, trust-root authorization or safe Git history publication.'}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(report['status'],len(findings),'findings;',sum(a['members_scanned'] for a in archives),'members scanned')
    return bool(findings)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('directory',type=Path)
    p.add_argument('output',type=Path)
    args=p.parse_args()
    raise SystemExit(scan(args.directory,args.output))
