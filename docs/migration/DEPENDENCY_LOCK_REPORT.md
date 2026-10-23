# Dependency lock report

Core libraries are constrained in pyproject.toml and pinned for this migration
in requirements/py314-core.txt. The actual 23-package core runtime closure is
requirements/py314-win64.lock.txt. It excludes suppliers, pytest and coverage;
that exact closure was installed into a new wheel-only Python 3.14 environment.
All 175 installed modules imported offline, with no qbot module available.

The full development/supplier dependency versions are separately pinned in
py314-win64-full.lock.txt; py313-win64-full.lock.txt records compatibility.
These two lists are explicitly derived dependency-only snapshots: install the
reviewed FactorLab project/artifact separately. The exact original installer
inventories contained local file URLs and are preserved unchanged privately in
evidence/environments/*.original.txt, with a separate portability receipt.
Those original machine URLs do not enter the final wheel/sdist. Older r1-r5
artifacts are superseded and must not be published; the actual r5 portability
audit confirmed both original inventories carried the project URL.
These are platform/version snapshots, not cross-platform universal hash locks.
Build tooling is pinned to setuptools80.9.0/wheel0.45.1/build1.3.0. Build backend
and package discovery are conventional setuptools with namespace-aware find.

Core wheel resolution, actual installation and pip check passed. Native runtime
packages were installed as wheels. Optional jsonpath required a pure Python
source build; preserve py314-suppliers.log and r2/r3 logs for failed/repair rounds.
The final XGBoost pin is 2.1.4, preserving the baseline model behavior while
successfully running under Python 3.14. No baseline 3.9 dependencies were changed.

The installed distribution inventory, source mappings, wheel/sdist digests and
per-run artifact hashes are separate evidence. A freeze file alone does not
prove ABI compatibility, correctness, formal financial admission or licensing.
