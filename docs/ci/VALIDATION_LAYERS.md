# Software and historical validation

`integration_checks.py --suite current` runs every configured offline test
except the exact 186 records in the unchanged `CI_BASELINE_FAILURE_MATRIX.csv`.
The plugin refuses selection drift. It emits a complete node inventory and
does not classify newly failing tests as historical. A relative configuration
test, a concurrency test, source integrity, CN/US, all four models, accounting,
financial refusal and exploratory isolation therefore remain current software.
The older core/cross-market/financial-gates selections remain useful targeted
checks; their overlap is not a unique-case total or exhaustive acceptance.

`--suite legacy` still runs the full original configured project with no
additional deselection, ignore option, xfail, continue-on-error or exit-code
override. Its actual historical FAIL/ERROR, collection errors, JUnit and exit
code remain observable. The two network tests retain their original offline
exclusion. The old matrix and original failure observations are not rewritten.

Use `review_validation_boundary.py --full <legacy output> --current <current
output> --output <report.json>` to compare all node outcomes and frozen input
identities independently of the selection algorithm. This detects missing
software tests, historical status drift, source changes and divergent outcomes.
This is an independently implemented comparison, not an external human review.

The current layer is a new software check. This change does not change GitHub
branch protection, remove the failing full check from required contexts, or
grant approval to accept historical failures. Software merge readiness remains
blocked until outstanding locking and historical-check risk decisions are
resolved. Replaying genuine archives or an approved append-only migration is
required for historical certification; creating replacement pins is forbidden.

## Configuration path contract

An explicit `repo_root` must be absolute and name an existing directory. It
controls relative config and artifact paths, including when the config itself
is absolute. From source, relative configs use the checkout containing the
loaded module, including from outside that checkout. An absolute config in a
different source checkout selects that checkout. Other absolute configs retain
the module source root as their relative artifact base. If a relative config
is called from a different source checkout, the loader rejects the ambiguous
roots and asks for an absolute config or explicit root.

In a wheel, a standalone absolute config uses its parent as the artifact base;
an absolute config inside a source checkout uses that checkout. Relative wheel
configs require an explicit absolute root. The loader never searches arbitrary
repositories for a matching filename or falls back to the process cwd. Existing
CSI500 YAML files, schemas, financial gates and backtest contracts are unchanged.

PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED / NO LIVE MONEY
