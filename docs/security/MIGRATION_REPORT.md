# FactorLab P0 independent safety-root migration

NO LIVE MONEY · PIT_UNVERIFIED · FORMAL_ADMISSION_BLOCKED

This is a local implementation and evidence delivery. New security control
integrity and software acceptance are distinct from root authorization,
financial qualification, licensing and publication. V1 cannot authorize a
release. Independent approval remains BLOCKED.

## Frozen baseline

The starting commit is `98b133841a5cd5ecffcf715e8626b41db2c5b8b6`, on
`codex/factorlab-independent`, with a clean worktree and no remotes. Development
uses the new local branch `codex/factorlab-p0-independent-security`; the original
branch is preserved. Before edits, `local-p0/baseline` captured a verified Git
bundle, a tracked-worktree ZIP, raw SHA-256 identities for 550 tracked files and
341 old report/log/evidence files, and the complete Git index identity. These
materials remain on disk for independent inspection. No original report,
historical receipt, numerical reference or Golden is overwritten.

## Implementation

The V1 fixed verifier authenticates a closed source, test, configuration,
packaging and control scope against a separately retained anchor and required
external pin. It rejects changed or unexpected files, malformed/noncanonical
paths, unknown versions, altered project/source identities, duplicate JSON keys,
hardlinks, symlinks and reparse paths. Candidate Python is never executed.
An exact inventory returns integrity PASS and authorization BLOCKED. Runtime
formal admission retains the RAW/ADJUSTMENT/STATUS/PIT/SOURCE_BINDING/FORMAL
checks and then unconditionally rejects under the V1 review boundary.

New attacks cover tampered source/config/test/control bytes, deletion/addition/
rename, changed checker execution payloads, joint checker/receipt tampering,
controller/anchor/workflow resealing, version and origin changes, forged hashes,
self-reported approval, synthetic financial evidence, external Git environment
overrides and filesystem aliases. JUnit evidence records actual rejection
assertions. A setup error is a failed run and never accepted as a successful
attack rejection.

Historical controls are evaluated in a separate archive of genuine migration
baseline objects, with the original paths, receipts, checker and tests. The
seven old preimage attacks use their original bodies in that archive; no Qbot
preimage is manufactured inside FactorLab. Full details and mappings are in
`CONTROL_MAPPING.md` and the 186-row JSON/CSV matrix.

## Execution evidence

The definitive observed counts, statuses, Python versions, command targets,
input identities and raw-log/JUnit hashes are in `evidence/test-results.json`.
Historical outcomes and seven-preimage details are in
`evidence/archive-acceptance.json`. Source and test identities, preserved-old-
evidence checks and Golden/reference preservation are in
`evidence/final-source-test-identity.json`. Local raw execution materials are
under `local-p0`; their paths are recorded in the result receipts. Current
build contents and publication-boundary checks are recorded separately in
`evidence/distribution-integrity.json`.

All unsuccessful rounds remain available: an initial archive preparation met
an unrelated submodule gitlink, an older Qbot-main snapshot lacked later tests,
and the first applicable archive export used LF where the original Windows
source closure requires CRLF. The corrected harness reads the actual source
checkout policy and preserves historical evidence bytes and pins. Incomplete
software runs invalidated while freezing those inputs are retained and excluded
from PASS claims. No historical hash or expected conclusion was repaired to
make a test pass.

## Status meanings

| Status | Meaning in this delivery |
| --- | --- |
| PASS | A named executed engineering check or exact integrity observation succeeded |
| FAIL | A real executed assertion, integrity contradiction or setup/collection failure occurred |
| PARTIAL | Only part of an obligation executed or the matrix retains unresolved archival failures |
| BLOCKED | Independent root approval, financial admission, historical authenticity approval or licensing is unavailable |
| NOT_RUN | A platform/job/publication or historical check was not executed; not a success |

Software success does not certify financial evidence, PIT/HFQ validity,
tradability, publication rights or live trading. New-root release approval,
historical authenticity approval and unresolved licensing require independent
human review as listed in `CONTROL_MAPPING.md`. No remote is added, no GitHub
push occurs, no merge occurs and no software/PyPI publication occurs.

The durable phase observer initially conflicted with the existing test that
replaces `builtins.open` to prohibit generator filesystem reads. That attempt
ended in a pytest INTERNALERROR and remains a failed run. The observer now
binds its own reporting I/O before adversarial test patches; the unchanged
original attack passed setup/call/teardown in a targeted reproduction. A fresh
complete run follows the fix. No research function or test expectation was
changed to resolve this observer issue.

The complete declared historical inventory contains319 cases. All319 have
actual observations:318 PASS and1 FAIL. The186-record matrix remains185
completed dispositions plus1 PARTIAL disposition, with the genuine historical
FAIL retained. See HISTORICAL_REPLAY.md for original failed runs and explicit
named reruns.

## Completed engineering observations

| Scope | Result | Evidence |
| --- | --- | --- |
| Python3.14.0 complete independent software regression | PASS:3965 cases,8 declared deselections,0 failures/errors | `local-p0/software314-r4` |
| Python3.13.9 necessary compatibility | PASS:672 cases,1 historical deselection | `local-p0/compat313-r1` |
| New V1 security tests | PASS:49/49 under both versions | `evidence/new-attack-evidence.json` |
| Original seven preimage attacks | PASS:7/7 under both archive runtimes | `evidence/archive-acceptance.json` |
| All319 declared historical cases | PARTIAL:318 PASS,1 genuine FAIL | `HISTORICAL_REPLAY.md` |
| Original186-record dispositions |185 completed,1 PARTIAL with archival FAIL retained | JSON/CSV disposition matrix |
| Raw closed source/control scope | PASS:495 files; authorization BLOCKED | `evidence/integrity313.json`, `integrity314.json` |
| Wheel/sdist content and publication checks | PASS:176 Python modules,627 members,19 synthetic attacks rejected | `evidence/distribution-integrity.json` |
| Clean core-only wheel and sdist installation | PASS:dependency check,isolated imports,CLI and offline demo | `local-p0/packaging314-final-r3` |
| Independent root release approval | BLOCKED | V1 has no approval mechanism |
| Financial admission/PIT qualification | BLOCKED | NO LIVE MONEY;PIT_UNVERIFIED;FORMAL_ADMISSION_BLOCKED |
| Historical authenticity and license permissions | BLOCKED | Independent human review required |
| Remote CI,Linux,GitHub/PyPI publication | NOT_RUN | No remote,push,merge or publication |

All3965 JUnit results match11895 passing setup/call/teardown observations and
unchanged test inputs. No setup error is credited as rejection success. The672
compatibility cases retain exactly the same183 runtime-file and209 test-file
identities at final implementation freeze; changed reporting tools are bound
separately in `compatibility-input-binding.json`.

The initial optional uv clean-install attempts failed in interpreter-cache
persistence with OS error17. The original CI default pip flow then completed
for both artifact types, without changing dependency pins or source. Failed
installer logs remain local. See REVIEW.md for the local Git bundle, exact
raw-source snapshot and external final-HEAD binding receipt.
