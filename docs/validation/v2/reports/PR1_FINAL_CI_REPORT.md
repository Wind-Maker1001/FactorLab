# PR #1 final CI report

Public input `ed2b4c1a0a10afd794c674b14d105b748b01b9c8`; main `afac4f9d4c2b123c0b32787ccd1cf67fe965c723`. Tested candidate `b2b6b472d500b078cc872c3b942c6e1916054512`; actual executed merge `50e94ab96d2ca17cd75fa4dce225b5c834152869` (identical Git tree `23a324933ba447f2516328a6b16b4b426593e3a9`). Evidence index SHA-256 `81a23cb3e3704f8c7b8eb34725d41f4f7e4cf93d6d9c721ee0af43863bf86f9b`. [Remote run 40550674503](https://github.com/Wind-Maker1001/FactorLab/actions/runs/40550674503). Windows Server 2025 / Python 3.14.9; additional Linux/Python 3.14.8 and Windows/Python 3.13.16 evidence are identified in the index.

Remote conclusion: **FAILURE**. All original full-project
failures remain visible. No workflow rerun was requested for this commit.

| Job | Actual conclusion |
| --- | --- |
| Package Windows Python 3.14 | SUCCESS |
| Core, cross-market and financial gates Windows Python 3.14 | SUCCESS |
| Lock acceptance windows-latest Python 3.14 | SUCCESS |
| Core, cross-market and financial gates Windows Python 3.13 | SUCCESS |
| Full legacy regression Windows Python 3.14 (failures retained) | FAILURE |
| Lock acceptance ubuntu-latest Python 3.14 | SUCCESS |
| Native lock diagnosis Windows Python 3.14 (control failures retained) | SUCCESS |
| Independent V2 coverage comparison (no migration approval) | SUCCESS |

| Scope | Observed result |
| --- | --- |
| Current software Python 3.14.9 | 4184 PASS / 0 FAIL / 0 ERROR / 0 SKIP; exit 0 |
| Current software Python 3.13.16 | 4184 PASS / 0 FAIL / 0 ERROR / 0 SKIP; exit 0 |
| Complete project Python 3.14.9 | 4184 PASS / 50 FAIL / 136 ERROR / 0 SKIP; exit 1 |
| Package | Clean wheel/sdist checks, license/NOTICE, CLI, demos and hostile archive checks; 191 wheel Python modules match candidate source bytes |
| Windows lock acceptance | 127/127 rounds; 505 publications; 18/18 fault tests; zero failed rounds, no surviving locks in successful rounds |
| Linux lock acceptance | 127/127 rounds; 505 publications; 12 PASS / 6 Windows-only SKIP |
| Independent V2 outcome review | PASS; same frozen inputs=True; 4,157 original software nodes and 814 critical nodes retained |
| Local core-only environment, full software | 4,144 PASS / 13 FAIL / 27 ERROR, exit 1; all 40 lack the already declared exchange_calendars dependency; retained FAIL |
| Initial incomplete local overlay, targeted | 74 PASS / 13 FAIL / 27 ERROR; dependency copy stopped at an out-of-library console launcher; retained FAIL |
| Repaired isolated local dependencies, targeted | 114 PASS; all 40 previously nonpassing nodes pass; source and requirement declarations unchanged |

The 27 added nodes are 18 lock tests and nine comparison attacks. All old software
nodes map to themselves; no old test file, historical failure matrix, fixture,
license, dependency, V1 verifier or trust root changed. The 335 protected Git blob
identities are checked. Worktree EOL conversions are reported separately and never
used to upgrade V1 certification. Each of the 186 historical outcomes is compared;
186/186 original exception messages are identical to the prior remote run.
163 raw trace hashes match; the other 23 differ in temporary synthetic fixture
commit IDs and pytest object addresses. Both hashes remain individually visible;
this comparison does not rewrite traces, old archives, Golden or missing objects.
Full original and new JUnit/log bytes remain in verified artifacts. The collection
module's authentic hidden parameter count is still UNKNOWN, not counted as PASS.

The new V2 job reconstructs outcomes independently of the selection plugin and
compares both Python inventories. This is engineering verification, not human
approval. Full-project failure remains a merge blocker. The fixed V1 verifier
still reports SOURCE_SCOPE_CHANGED; no anchor or old receipt was resealed.

The supplemental local full run used a pre-existing Python 3.14.0 core-only
environment whose calendar dependency was overlooked during preflight. Its
real FAIL is preserved, not turned into a full PASS. Every one of its 40
nonpassing records is ModuleNotFoundError for exchange_calendars, already pinned
in requirements/cross-market.txt; those exact nodes pass both complete remote
runs. A first overlay preparation refused the distribution's external ecal.exe
launcher, leaving that targeted attempt incomplete and failing. The corrected
import-only overlay copies 158 pure-Python dependency/metadata/license files,
checks original RECORD hashes and uses the exact four versions in the verified
remote environment. The 114-test cross-market scope then passes with unchanged
source inputs. Neither shared environment nor dependency declarations changed;
the 45-minute local full run was not repeated. These local failures and scoped
revalidation are distinct from the complete qualified remote software PASS.

The final report publication is a documentation-only descendant of the tested
candidate. Only docs/validation/v2/reports changes avoid another identical full
run. They do not create an exact-final-head software PASS or waive the observed
full failure. The PR description records the final documentation SHA separately.
No branch-protection setting, Release, PyPI publication, merge or force push occurs.

The local worktree's pre-existing demo.py CRLF/eol=lf attribute discrepancy can
appear in git status. Its raw bytes equal the committed public blob and the
remote wheel; it was not included in either task commit. This observation does
not normalize historical bytes or conceal a source modification.

Public scope review covered all 12 code/CI/contract paths and later report-only
blobs. No restricted research chain was merged. Original MIT LICENSE/NOTICE and
dependencies remain intact; Microsoft API documentation was read, not copied as
implementation. Reports preserve synthetic credential/path rejection test IDs
where required for exact node mapping; these are pre-existing public test literals.

**PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED / NO LIVE MONEY**
