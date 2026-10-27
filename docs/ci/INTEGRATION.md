# Incremental software validation and historical regression

This source-only integration adds CN/US daily research adapters, dynamic
universes, four-model walk-forward interfaces, corporate-action accounting and
conditional scenarios. Existing factors and formal consumers retain their
original contracts. The US fixture is generated from a fixed random seed.
No real observations, supplier dictionaries, downloaded announcements,
row-level derived results or local audit logs are added here.

The local development chain remains preserved. This branch starts at the
public baseline and copies only reviewed software, tests and configuration.
Research artifacts require separate redistribution review and remain local.
LICENSE and NOTICE are retained. The separately installed optional
`exchange-calendars==4.13.2` has an Apache-2.0 license; its source is not bundled.

CI runs on PRs to main and manual dispatch. Windows Python 3.14 is primary,
with 3.13 as independent compatibility. Linux/macOS are outside this matrix.
PR receipts record the actual executed merge SHA and the distinct PR head SHA.

| Layer | Scope |
|---|---|
| Package | wheel/sdist build, module bytes, LICENSE/NOTICE, hostile archive rejection, isolated installs, imports, CLI, offline demo and publication scan |
| Core | factor, model, walk-forward, statistics, portfolio and accounting files declared in `tools/ci/integration_checks.py` |
| Cross-market | all `tests/cross_market`, including conditional scenario isolation |
| Financial gates | V1 attacks, real-data qualification rejection, forward activation/policy and scenario consumer rejection |
| Full legacy | complete configured project collection; the existing two network cases remain excluded |

Incremental success applies only to its declared domains. Full legacy failures
keep their job red and remain merge blockers. There is no continue-on-error,
xfail conversion, additional deselection or exit-code override. JUnit, logs,
process status, subject SHA and input hashes are uploaded even after failure.
Artifact upload success does not mean tests passed.
Uploads select receipts, dependency snapshots, JUnit/logs and the built
wheel/sdist. Temporary test trees and installed environments are excluded.

Historical controls require genuine upstream roots, original Git objects and
published artifacts absent from this independent repository. Copying a YAML
file or manufacturing evidence cannot repair that condition. See
`CI_BASELINE_AUDIT.md` and `CI_BASELINE_FAILURE_MATRIX.csv` in this directory.

The fixed V1 anchor, validator, pins and historical receipts are unchanged.
New scope remains unauthorized by V1. Software hashes cannot issue approval.

RESEARCH_ONLY / PIT_UNVERIFIED / HFQ_UNVERIFIED /
FORMAL_ADMISSION_BLOCKED / NO LIVE MONEY.
