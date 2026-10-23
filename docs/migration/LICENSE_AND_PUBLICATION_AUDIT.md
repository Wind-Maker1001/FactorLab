# License and publication audit

Status: BLOCKED_SCOPE_UNRESOLVED for public distribution of inherited docs.
Local engineering inspection and testing continue; no remote is configured.

Root LICENSE grants MIT and credits Copyright (c) 2022 UFund-Me. It is copied
unchanged. docs/LICENSE declares GPLv3. Upstream docs/README.md:744-748 also
states Creative Commons Attribution-NonCommercial-ShareAlike 4.0. Applicability
to inherited research docs cannot be resolved by choosing one declaration.
The candidate preserves docs/LICENSE and does not copy docs/research wholesale.
New migration documents describe this task and do not relicense old material.

No upstream data/results directory was extracted. Historical docs contain
real-derived security coverage and quantile summaries; absence of raw OHLC does
not establish redistribution permission. Those files are excluded unless
independently reviewed. Synthetic Golden fixtures remain byte-identical; both
their manifests explicitly identify synthetic-only data.

The extracted minute adapter is limited to qbot/engineering/yhh30m. Nested
third-party trading plugins and their licenses are outside the extracted code.
Preserving the MIT notice is necessary but is not a legal resolution of the
upstream documentation scope. No public repository, release or PyPI upload is
authorized by this task.

Final distribution inspection found175 Python modules and the package YAML,
with all423 eligible sdist source/test/example/config/requirement/user-doc files
equal to current worktree bytes. Explicit secret/Legacy/data-path scans checked
619 archive members with0 findings. These bounded checks do not certify all-secret
absence or redistribution rights. The21 Parquet members are the exact preserved
synthetic Golden, not original supplier downloads.

One real-derived TDX notebook from stage one is preserved intact under
local-private-archive and excluded from current HEAD and both artifacts. The
initial local Git commit still contains it. Do not publish this repository's
history wholesale. Raw local logs/JUnit/environment snapshots may contain
absolute machine paths and belong to private review evidence. An independently
reviewed public export remains a separate release prerequisite.

An additional unchanged upstream migration-verifier source file is retained only
as a private source-checkout unit-test reference under docs/migration/evidence.
It has a separate fixed-Git-object/raw-hash receipt and is excluded from both
distributions. This does not resolve its inherited documentation-license scope
or authorize public redistribution of the local candidate history.

Final portability audit found two original environment inventories containing
the installing project's local file URL. Both original byte streams are now
preserved privately with exact hashes; derived dependency-only version lists
exclude the project URL and enter r6 artifacts. Old r1-r5 artifacts remain as
audit evidence and are superseded, not public candidates. The final explicit
scan requires a clean baseline and rejects19 synthetic archive mutations.

NO LIVE MONEY. PIT_UNVERIFIED. FORMAL_ADMISSION_BLOCKED.
