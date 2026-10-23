"""Freeze the actually installed runtime dependency closure, without dev extras."""
import importlib.metadata as metadata
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOTS = ('numpy','pandas','scipy','scikit-learn','pyarrow','matplotlib','PyYAML','lightgbm','xgboost','tzdata')
pending, versions = list(ROOTS), {}
while pending:
    name = canonicalize_name(pending.pop())
    if name in versions:
        continue
    dist = metadata.distribution(name)
    versions[name] = dist.version
    for raw in dist.requires or ():
        requirement = Requirement(raw)
        if requirement.marker is None or requirement.marker.evaluate({'extra':''}):
            pending.append(requirement.name)
output = Path(__file__).resolve().parents[2]/'requirements/py314-win64.lock.txt'
output.write_text('# Actual core runtime closure; CPython 3.14 Windows x86-64\n'+
                  ''.join(name+'=='+versions[name]+'\n' for name in sorted(versions)), encoding='utf-8')
print('Locked',len(versions),'core packages; suppliers and test tools excluded')
