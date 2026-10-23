# Historical archive acceptance

NO LIVE MONEY · PIT_UNVERIFIED · FORMAL_ADMISSION_BLOCKED

The historical contract is **PARTIAL**. The latest named-case observations are
**318 PASS and 1 FAIL**, with no unresolved setup error. The 319 declared historical checks all have actual run observations, including
the 185 individual original nonpassing cases, 27 tests recovered from the
single original collection-error record and 107 additional historical checks. The 186-record disposition matrix is
therefore **185 completed dispositions and 1 PARTIAL disposition**; its remaining
archival assertion is explicitly FAIL, not a waived success.

The original seven preimage attacks passed **7/7 under Python 3.14 and 7/7 under
the original Python 3.9.25 contract**. Their original bodies execute in a genuine
`0ec7af5870c54a0ee202fbceeb97b7d87c293411` archive, with real old Git objects,
original Qbot paths, exact controls and unchanged historical receipts. No dummy
Qbot path, renamed FactorLab source or invented old object supplies a preimage.
The archive's tracked content remained unchanged after full and targeted runs;
the source migration repository's HEAD and status remained unchanged.

## Raw runs and final observation rule

| Material | Observed outcome | Interpretation |
| --- | --- | --- |
| `local-p0/archive314-r4` | 209 PASS, 2 FAIL, 1 ERROR | Completed 3.14 archive diagnostic; original receipt remains FAIL |
| `local-p0/archive39-r1` | 209 PASS, 3 FAIL | Completed original-version archive run; original receipt remains FAIL |
| `local-p0/archive39-repair-r2` | 2 PASS | Only the two named environment-affected original cases were rerun with a working 3.9.25 interpreter |
| `local-p0/archive39-complete-extra` | 107 PASS | Remaining declared historical cases, including previously passing checks |
| Final named-case observations | 318 PASS, 1 FAIL | Each replacement names its actual run/JUnit hash; no new full-run PASS is manufactured |

Python 3.14 encountered `SOURCE_CHANGED_DURING_READ` before synthetic-package
assertions in two archived cases. The first 3.9 invocation's relocated venv
could not start child interpreters (`No pyvenv.cfg file`), affecting the squash
replay and nested frozen-baseline test. A working existing 3.9.25 environment
executed both unchanged cases successfully. No old source check was weakened,
no qualification receipt was altered and no setup error was credited as an
attack rejection. The original 3.14 and 3.9 failed runs are retained.

Earlier preparation/diagnostic rounds also remain under `local-p0`: an unrelated
submodule gitlink was initially treated as a file; an older Qbot-main snapshot
lacked later tests; and an LF export disagreed with the registered Windows
runtime source bytes. The final harness retains the actual original checkout
policy (`core.autocrlf=true`) and the archived registry's platform identities.
The first targeted repair attempt had a missing temporary parent and produced
two setup errors; it remains a failed prerequisite attempt and contributes no
successful observation.

## Remaining genuine contradiction

Matrix row 81 is
`tests/market_structure/test_delivery.py::test_protected_tracked_content_is_unchanged`.
It fails under both versions because two real historical changes fall outside
that test's approved set:

- `docs/research/evidence/ci-runtime-optimization-v1/static-runs.json`
- `docs/research/evidence/ci-runtime-optimization-v1/baseline-detailed-receipts.json`

`evidence/historical-blocker.json` binds the actual blob IDs, SHA-256 identities,
lengths and last-change commits at historical baseline
`39eecf869c9c2075ba3f6e3c76aeba3338c63432` and archive commit `0ec7af5`.
These are genuine available Git objects. The contradiction is not missing
evidence and is not resolved by moving the test out of software acceptance.

Independent review must authenticate the old scope, the reason for those
metadata changes and any separately bound corrective authorization. Original
receipts, historical hashes, test expectations and conclusions remain intact.
This delivery does not issue that authorization. Historical authenticity review
remains **BLOCKED**, independently of replay consistency and new software tests.

## Evidence access

`evidence/archive-acceptance.json` lists raw run/JUnit/log hashes, seven-attack
rows and named-case aggregation rules. The complete 186-row JSON and CSV retain
the original failure categories and trace hashes, original execution phases,
new security obligations and mappings to V1. They distinguish migrated security
obligations, historical-only evidence and legitimate namespace/path/version
changes without claiming one-to-one test equivalence.

Raw historical pytest outputs remain local and outside Git. Some legacy failure
formatting expands process environment representations; review artifacts use
hashes and concise causes rather than reproducing such values. No raw log,
environment, data or archive is uploaded or published.

The exact 319-node inventory and original module selectors are retained in
`evidence/historical-scope-inventory.json`. The additional run selects only
cases absent from the original 212-case replay, and all319 declared node IDs
are checked against the union of actual JUnit observations. For a fresh replay,
prepare the genuine archive with `archive_replay.py --prepare-only`, then run
its original Python3.9.25 pytest against `module_targets` from that inventory.
No original test needs to be edited.
