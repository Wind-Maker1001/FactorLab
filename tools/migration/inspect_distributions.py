"""Inspect real build artifacts for namespace completeness and excluded content."""
import hashlib
import json
from pathlib import Path
import tarfile
import zipfile
import sys

ROOT = Path(__file__).resolve().parents[2]
directory = Path(sys.argv[1]).resolve() if len(sys.argv)>1 else ROOT/'local-artifacts/packaging-r1'
wheel = next(directory.glob('*.whl'))
sdist = next(directory.glob('*.tar.gz'))
expected = {p.relative_to(ROOT/'src').as_posix() for p in (ROOT/'src/factorlab').rglob('*.py')}
with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())
    python = {name for name in names if name.endswith('.py')}
    assert python == expected, {'missing':sorted(expected-python),'extra':sorted(python-expected)}
    assert all(name.startswith(('factorlab/','factorlab-0.1.0.dev0.dist-info/')) for name in names)
    assert 'factorlab/resources/csi500_5y.yaml' in names
    for name in python | {'factorlab/resources/csi500_5y.yaml'}:
        assert archive.read(name)==(ROOT/'src'/name).read_bytes(), 'stale packaged bytes: '+name
with tarfile.open(sdist) as archive:
    source_names = [m.name for m in archive if m.isfile()]
    assert not any('/docs/migration/' in name or '/qbot/' in name or '/local-' in name or
                   name.endswith(('.log','.xml','.ipynb','.pyc')) for name in source_names)
    prefix=source_names[0].split('/',1)[0]+'/'
    expected_source={p for directory in ('src/factorlab','tests','configs/research','examples/research','docs/user','requirements')
                     for p in (ROOT/directory).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts and
                     p.suffix in ('.py','.yaml','.json','.parquet','.csv','.md','.txt')}
    for path in expected_source:
        member=prefix+path.relative_to(ROOT).as_posix()
        assert member in source_names, 'sdist omits current file: '+member
        assert archive.extractfile(member).read()==path.read_bytes(), 'stale sdist bytes: '+member
report = {'status':'PASS','wheel':{'file':wheel.name,'sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),
           'python_module_count':len(python),'members':sorted(names)},
          'sdist':{'file':sdist.name,'sha256':hashlib.sha256(sdist.read_bytes()).hexdigest(),
                   'current_source_test_example_files_verified':len(expected_source),
                   'members':source_names},
          'scope':'Artifact content inspection only; installation execution is separate.'}
(ROOT/('docs/migration/evidence/distribution-inventory-'+directory.name+'.json')).write_text(json.dumps(report,indent=2),encoding='utf-8')
print('PASS:',len(python),'Python modules; no qbot namespace or raw evidence in distributions')
