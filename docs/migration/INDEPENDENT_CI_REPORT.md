# Independent CI execution

The new `.github/workflows/factorlab.yml` defines Windows and Linux software
jobs for Python3.14/3.13, with a separate mandatory complete-upstream-contracts
job. It is an independent configuration, not a copied old workflow/trust root.
Action commits are pinned and existence-checked through the read-only connected
GitHub tool. No repository/remote/push was created. GitHub Actions execution is
NOT_RUN; a valid YAML or commit lookup is not a remote green run.

The workflow installs controlled core, test and optional supplier dependencies,
checks pip, executes retained runtime/security domains, builds wheel/sdist and
runs the synthetic CLI. Its packaging step installs both artifacts into
separate core-only environments and runs isolated offline module imports, CLI,
demo and archive secret/Legacy/data-path scan. The mandatory full job has no
continue-on-error and retains every original test, including unavailable
historical trust/receipt controls.

Final local Windows jobs were launched from implementation commit `607d335` with
`tools/ci/run_checks.py`. Each writes its exact targets, per-file input hashes
before/after, JUnit, coverage XML, duration, Python, subject/end Git SHA and status.
The hashed scope is src/factorlab,tests,configs,requirements,tools/ci,
examples/research,tools/migration,.github/workflows,docs/user,pyproject.toml,
MANIFEST.in,README,LICENSE,CONTRIBUTING,SECURITY,.gitattributes,.gitignore and
the exact immutable upstream-verifier test reference.
It excludes evolving migration reports/evidence. Independent
coverage databases and fresh same-drive temp directories prevent concurrent
run interference. A successful pytest exit is insufficient if those validated
inputs change during execution. Earlier07366d3 jobs had a narrower declared
snapshot and are not described as covering these additional inputs. Final
Git/input comparison binds the tested inventory to the delivered local HEAD.

Final job results are in `evidence/final-validation.json` and
`evidence/test-summary.json`; raw local job directories are:

* `local-ci/runtime314-final-r5`
* `local-ci/runtime313-final-r4`
* `local-ci/full314-final-r4`
* `local-ci/packaging314-final-r7`

| Final local job | Actual status | Passed | Failed | Errors | Skipped | Deselected | Driver seconds | Line coverage |
|---|---|---:|---:|---:|---:|---:|---:|---:|
|runtime314-final-r5|PASS|3915|0|0|0|8|2771.35|85.73% (18936/22088)|
|runtime313-final-r4|PASS|3915|0|0|0|8|3767.17|85.94% (19261/22413)|
|full314-final-r4|FAIL|3980|50|136|0|2|2820.98|85.73% (18936/22088)|

All three receipts bind470 unchanged validation input files, with both subject
and end SHA607d33528abc10950e8f0ec0c77e580eea6836a5. Pytest-only durations
are2763.04s,3758.53s and2813.26s respectively; driver durations include receipt
and input checks. The software jobs each deselect2 network tests and6 historical
assertions that are exercised by the full job. The full job deselects only the2
unauthorized network tests. Every job has75 warnings. Results overlap and must
not be summed. Coverage has different executable-line denominators under the two
interpreters despite the same source inventory, so rates are reported separately,
not asserted as cross-version coverage equivalence or performance evidence.

Earlier failed/interrupted/diagnostic rounds are preserved. Runtime314-r1 had
2680 pass/12fail; those ordinary namespace/layout issues were repaired. The
expanded final software scope selects all inherited software modules except
explicitly cataloged unavailable historical publication/control assertions.
Mixed files retain their financial refusal/source-identity tests. Every deferral
and reason is in the runtime receipt. Deferred assertions still run in the
mandatory full job. Software success cannot clear full CI or financial admission.

The07366d3 full round actually had3945PASS/51FAIL/169ERROR. Trace review identified
one obsolete3.9-only/missing-namespace packaging assertion among those failures;
it was an ordinary portability test issue, not historical financial evidence.
It now checks3.13/3.14,source CLI and actual namespace discovery (both direct
version probes PASS), and was removed from runtime deferrals. A further audit
restored33 executable archive-verifier unit cases from an unchanged baseline Git
blob; they now run in both software jobs. The reference is SHA-checked and the
same bytes directly compiled, never accepted through a pyc cache. A new negative
test confirms changed bytes are refused. The final607d335 jobs rerun the complete
updated scope. Earlier incomplete3.13 rounds were interrupted
and retained as diagnostic; only completed final-r4 is the3.13 suite acceptance.

The upstream revision-contract module requires a trusted old controller during
collection. Its27 baseline cases cannot execute in this candidate; the collection
error is in full JUnit, and those27 cases are explicitly NOT_RUN/BLOCKED. Other
original-history assertions and pinned-checker fixtures also fail/error without
the unavailable objects. The actual full-suite exit remains FAIL even when
executable software functionality passes.

Actual final full-r4 results are3980PASS/50FAIL/136ERROR/0SKIPPED, with2 network
tests deselected. Both the primary review and a separate read-only agent read
the186 nonpassing messages/traces. There are24 distinct module/message groups:
136errors (135 setup plus1 collection) and4failures directly miss the old
market-admission.yml;37failures miss uncarried historical publications;2failures
require unavailable original Git objects;7failures stop in forged_revision while
copying the old qbot/research/cli.py preimage. Those7 attack assertions did not
execute and are not counted as successful refusals. Restoring bounded old-source
archival replay remains separate from approval of a migrated financial root.
The reviewed causes and exact JUnit digest are in full-failure-review.json.

The final exact testcase-name inventory also preserves parameter identities.
Only35 CSMAR executing-source path parameters receive explicit namespace/resource
mappings; original control-path parameters remain unchanged. The only27 missing
original cases are revision-contract collection failures. Added entries are7
migration counterexamples,2 CSMAR dependency cases and1 collection-error record.
See exact-test-case-inventory.json and test-case-inventory-comparison.json.

Local packaging r7 binds final validation-script hashes before/after and r6 artifacts.
Local packaging r1 stalled during a standard pip NumPy download and was
interrupted, retaining logs/environment. Local r2 through r7 use uv0.9.5 and the already
populated local wheel cache for installation; product checks use the same clean
environments and artifacts. Remote CI defaults to standard pip and is NOT_RUN.
This is a local installer/network limitation, not remote execution evidence.
The final driver also checks exact archive completeness, immutable fixture bytes,
duplicate/link members, absence of Legacy/dev/suppliers and initial import
network refusal. Separate clean r3 installs preceded the driver as well.
Seven synthetic archive attacks were actually rejected in archive-attacks314-r2;
the initial attack-driver round exposed invalid linked Python text causing an
unstructured parse error. The scanner now records that as an explicit refusal,
and the successful rerun retains the original failed log/copies separately.
Seven attacks were also rerun against r4 artifacts; the final archive-attacks314-r5
checked18 mutations, adding Windows drive/UNC/backslash/ADS/device/trailing-name
and separator aliases plus unsafe directory members. Final artifacts have zero
findings under those explicit checks. Final archive-attacks314-r6 checks19 mutations,
including a local dependency file URL. Its unmodified artifact baseline must pass
first, so unrelated existing findings cannot make mutation cases falsely pass.
Old r1-r5 artifacts are superseded, not public candidates; the actual r5 audit
confirmed private project URLs in installer snapshots. Original inventories remain private;
new dependency-only lists omit the local project URL without changing dependency
versions. The r6 scan and explicit requirement-file inspection pass.

The first r6 installation attempt failed with PyPI tzdata TLS handshake EOF.
Its original log and partial environment were preserved, not relabeled successful.
The cached offline retry succeeded; final r7 repeats the cleaned r6 artifacts
with UV_OFFLINE=1. No real market data or account was accessed.

Linux is NOT_RUN: WSL has no installed distribution and Docker is unavailable.
No claims of Linux ABI, symlink behavior or remote matrix success are made.
Concurrent local durations are not speed benchmarks.

NO LIVE MONEY / PIT_UNVERIFIED / FORMAL_ADMISSION_BLOCKED.
