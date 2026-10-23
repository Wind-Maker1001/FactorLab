"""Run with isolated mode outside any checkout, against an installed wheel."""
import importlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import socket
import sys

def denied(*args, **kwargs):
    raise AssertionError('installed import attempted network access')


socket.socket.connect = denied
socket.create_connection = denied
import factorlab

root = Path(factorlab.__file__).resolve().parent
assert 'site-packages' in root.parts
assert importlib.util.find_spec('qbot') is None, 'Legacy namespace leaked into installed wheel'
for dependency in ('pytest','coverage','akshare','baostock'):
    assert importlib.util.find_spec(dependency) is None, 'unexpected non-core dependency: '+dependency
failed, imported = {}, []
for path in sorted(root.rglob('*.py')):
    relative = path.relative_to(root).with_suffix('')
    pieces = list(relative.parts)
    if pieces[-1] == '__init__':
        pieces.pop()
    name = '.'.join(['factorlab']+pieces)
    try:
        importlib.import_module(name)
        imported.append(name)
    except Exception as exc:
        failed[name] = type(exc).__name__+': '+str(exc)
from factorlab._identity import software_identity, assert_migration_admitted
identity = software_identity()
assert identity['git_commit'] is None
try:
    assert_migration_admitted()
except factorlab.FactorLabError as exc:
    rejection = str(exc)
else:
    raise AssertionError('migration self-authorized formal admission')
report = {'status':'PASS' if not failed else 'FAIL', 'python':sys.version,
          'package_path':str(root),'modules_imported':len(imported),'failures':failed,
          'legacy_available':False,'network_used':False,'software_identity':identity,
          'formal_rejection':rejection,
          'dev_tools_absent':{p:importlib.util.find_spec(p) is None for p in ('pytest','coverage')},
          'optional_suppliers_absent':{p:importlib.util.find_spec(p) is None for p in ('akshare','baostock')}}
Path(sys.argv[1]).write_text(json.dumps(report,indent=2),encoding='utf-8')
print(report['status'],report['modules_imported'],'modules;',failed)
raise SystemExit(bool(failed))
