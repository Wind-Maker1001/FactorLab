# Independent FactorLab V1 and historical Qbot contracts

NO LIVE MONEY · PIT_UNVERIFIED · FORMAL_ADMISSION_BLOCKED

## Acceptance boundaries

Independent software acceptance is `python tools/ci/run_checks.py --suite software`.
It retains all 3915 previously selected software cases and adds the V1 security
suite. No existing test body, numerical implementation or Golden is rewritten.
The explicit legacy selection remains visible in `HISTORY_NODES`,
`UPSTREAM_BOUND`, and `PUBLISHED_HISTORY`; these are archive obligations, not
financial exemptions. `--suite runtime` is a compatibility alias.
`--suite full` retains the original mixed-layout diagnostic, including its
failures; it is not the independent software release contract.

Historical acceptance is `tools/security/archive_replay.py`. It reads genuine
commit `0ec7af5870c54a0ee202fbceeb97b7d87c293411` from the existing migration
repository and checks out an isolated scratch archive inside this candidate.
Its source is read only, with optional Git locks disabled. The original Git
index, platform checkout policy, original tests, controls, receipts and Qbot
paths are retained. Nothing is copied into FactorLab's source namespace or Git
history. An alternate object store refers to real source objects; it does not
create missing historical objects. Missing objects remain NOT_RUN/BLOCKED.
Unrelated submodule gitlinks are recorded and not replayed.

The original tests intentionally create clearly synthetic attacker repositories
and commits in their temporary fixtures. Those are attack inputs and are never
reported as historical evidence. Archived software success does not establish
legal redistribution rights, historical financial evidence authenticity or new
FactorLab authority.

## Versioned mapping

| Original obligation | Independent V1 contract | Historical disposition |
| --- | --- | --- |
| Workflow-pinned C01 loader, checker, registry and anchor | Retained fixed verifier + explicit external anchor SHA-256; candidate code is never imported | Original bytes and pins stay in genuine archive |
| Registered Qbot postimages and protected modes | Closed 495-file source/test/config/control inventory; reject additions, removals, renames, raw-byte changes, hardlinks and reparse paths | Original Qbot mode and preservation assertions execute only in archive |
| Checker and receipt changed together | Candidate checker must match retained anchor before any candidate execution | Original seven preimage attacks run with real old preimages |
| Controller/anchor/workflow jointly resealed | External retained anchor is not read from candidate; V1 has no local approval path even if a new local anchor is supplied | Original joint-reanchor attacks retained |
| C01 current/predecessor versions | Strict `FACTORLAB_INDEPENDENT_SECURITY_V1`, version 1, project FactorLab and original migration baseline | Prior versions cannot certify the new layout |
| Old Git refs and Qbot source closure | Actual FactorLab file closure, independent source identity and unchanged financial row binding | Exact old refs remain historical evidence only |
| RAW / ADJUSTMENT / STATUS / PIT / SOURCE_BINDING / FORMAL | Existing replay and row checks remain before the additional V1 unconditional refusal | Original historical meanings stay unchanged |
| Self-rehashed packages, approval flags, synthetic data | Existing runtime financial counterexamples plus V1 explicit no-approval contract | No archive result is a new qualification receipt |
| Frozen real-derived publication files | Current wheel/sdist exact source comparison, namespace and publication scan | Old publication hashes/path/version assertions run against original archive |
| Current source version/path | `factorlab`, `src/factorlab`, software `0.1.0.dev0` | Qbot's old paths/version are retained, never aliased into FactorLab |

The row-level 186-case matrix records a VERSIONED_CONTRACT_REDESIGN relation.
It does not claim that a new test is identical to every old test. Original
assertions are evaluated in the archive, and new contracts are evaluated by the
V1 security and existing financial refusal suites. The single original
collection-error record also represents additional uncollected tests; their
recovery is reported separately rather than silently added to the 186 count.

## Trust and change permissions

`docs/security/v1/trust-anchor.json` is contributor-created **review material**,
not an independently approved root. `approval` is null. A reviewer must retain
the verifier and anchor outside the candidate, independently inspect their
hashes and run the retained verifier with an out-of-band digest. The verifier
uses only the standard library and reads candidate bytes without executing
candidate code. Do not invoke a candidate-supplied verifier as an approval
authority. `seal_review_material.py` creates materials but grants no authority.

The V1 protocol has no approval issuer, signature-verification trust store,
approval-file importer, environment override, or positive authorization branch.
Its process exit code is 2 for intact-but-BLOCKED and 1 for integrity FAIL.
`assert_migration_admitted()` delegates to the V1 unconditional runtime refusal.
Even a replaced external local pin and a resealed source inventory only yield
integrity PASS plus authorization BLOCKED. Checker and anchor updates in the
same commit cannot issue admission. A future approval protocol requires a new
version, separately established reviewer identity, independent reviewed pins,
bound source/artifact identities, permission boundaries and revocation policy.

The current closure authenticates raw bytes. Its anchor is bound to this
retained checkout and built artifact observation. A different EOL export is a
different observation requiring review; a Windows check does not certify
Linux or arbitrary source archives. Historical runtime EOL identities follow
the original registry and actual source checkout policy, not new invented
bytes. Integrity observations cannot eliminate arbitrary privileged filesystem
races; no financial or release authorization depends on a successful scan.

## Independent review requirements

1. Review V1 fixed-verifier code and scope, retain pins outside contributor
   control and establish a real independent reviewer/approval mechanism.
2. Authenticate original baseline objects, workflow/registry provenance and
   historical receipt lineage separately from archive replay consistency.
3. Review source and dataset license/redistribution permissions. Existing MIT
   metadata and local build success do not clear upstream or supplier rights.
4. Bind any future approval to exact source/test/control and wheel/sdist bytes,
   platform, Python environment and genuine financial evidence vintages.
5. Keep actual financial admission, tradability and live-money authority
   blocked. No new live-trading functionality exists in this change.
