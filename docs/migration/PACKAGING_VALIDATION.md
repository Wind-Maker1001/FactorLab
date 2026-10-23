# Independent package validation

Final local build directory: `local-artifacts/packaging-r6`. Conventional
setuptools80.9.0/wheel0.45.1 produced a py3-none-any wheel and source tarball
for `factorlab==0.1.0.dev0`, requiring Python>=3.13,<3.15. The wheel's Python
tag describes this package's own source; its numerical dependencies have
platform-specific native wheels.

`tools/migration/inspect_distributions.py` compared actual archive contents to
the current source. All175 Python modules plus the YAML package resource are
byte-identical. This includes namespace packages lacking __init__.py, notably
model_readiness and market_structure. There is no qbot namespace, stale Legacy
build tree, bytecode, raw migration log/XML or notebook in either distribution.
All423 eligible source/test/example/config/requirement/user-doc files in the
sdist match the working tree, including the final frozen reference adapter,
untouched original reference and checksum files. Machine-readable member lists
and artifact SHA256 are in `evidence/distribution-inventory-packaging-r6.json`.

Final separate core-only Python3.14 environments are at
local-ci/packaging314-final-r7/wheel and local-ci/packaging314-final-r7/sdist.
Earlier clean environments remain at local-envs/wheel314 and local-envs/sdist314.
These environments contain the23-package runtime closure, no Qbot,
pytest/coverage/AKShare/BaoStock. Actual installation, pip check, isolated -I
import of all175 modules, CLI help and full synthetic demo are executed outside
both checkouts. Earlier r1/r3 installs used D:/Environments; the final driver uses
the checkout's parent directory. Final r6 artifacts include the ported3.13/3.14
namespace packaging test,updated verifier fixture loader and portable dependency
snapshots; packaging314-final-r7
binds the final scripts. The unchanged historical verifier itself remains a private
source-checkout test reference, excluded with docs/migration from both artifacts.
The stage-one qbot wheel is preserved
separately and is never included in the final distribution.

A third fresh environment,local-envs/pip-source314, explicitly executed standard
`python -m pip install --no-deps --no-build-isolation .` from the source directory,
after installing the pinned core closure and exact build tools. It built and
installed0.1.0.dev0 and passed pip check. The flags prevent new dependency/build
tool resolution; this is an actual standard pip source install, not an editable
path or a claim inferred from a wheel. Outside-checkout isolated import/CLI/demo
receipts are in evidence/pip-source314-*.

Installed software Git identity is null, its actual site-packages source and
resource hashes are nonempty, and identity_is_authorization is false. Readiness
that specifically requires committed source correctly refuses an installed
wheel with SOURCE_CHECKOUT_REQUIRED. The installed market-structure and factor
discovery synthetic CLIs also passed earlier r1, with no real data calls.

CLI: `factorlab --help`; public offline demo:
`factorlab demo --output <new-output-directory>`. All output is SYNTHETIC /
SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE with formal/forward/live=false. The demo
refuses altered external inputs rather than falsely stamping them synthetic.

The distributions are local engineering artifacts. Content inspection and
installation do not clear unresolved upstream copyright/documentation scope,
historical Git publication content or independent security trust roots.
No public repository, upload, release or PyPI publication occurred.
