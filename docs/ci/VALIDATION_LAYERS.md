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

## Trusted targeted subsets

Risk-selected files use the matrix and selector loaded beside the trusted CI
controller, never the candidate's copies. Every collected node outside the exact
historical subset must complete setup, call and teardown successfully. Missing
historical nodes, unexplained deselection, new failures, skipped/xfail phases and
collection errors fail the targeted check. Whole-file historical collection
blockers require byte equality with the trusted file before isolation, so changes
to such a file cannot hide newly introduced tests.

The V2 execution inventory and targeted receipt list isolated history as
`BLOCKED` with the trusted matrix digest. A targeted `PASS` describes current
software execution only, not historical certification. An entirely historical
selection can have pytest's original no-tests exit code 5; the receipt retains
that code and accepts only the exact, exhaustive historical partition with no
current nodes or execution errors. Legacy execution and its failures are unchanged.

TODO: Deploy the independent targeted controller through `ci-control-review`
and the trusted merge gate before treating dependent PR runs as validation of
this path. Candidate controller results do not approve their own deployment.

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
The module identifies its source root only at the canonical
`src/factorlab/config/loader.py` location. A wheel installed in an environment
inside a checkout does not inherit that checkout as its own source root.

PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED / NO LIVE MONEY
