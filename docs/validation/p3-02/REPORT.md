# P3-02 historical data governance

Base: `d165c65a17c1ff4b7d174a063a6607589cd38522`, fetched from origin/main.
P3-01 PR #18 merged to main at 2026-11-08T11:24:15Z; this base is its merge commit.
Branch: `feat/p3-02-data-governance`.
Environment: existing Python 3.13 at `D:/Environments/factorlab-py313/Scripts/python.exe`.

## Behavior and rationale

An optional append-only archive records explicit table versions while leaving
DataHub.read and its default routing unchanged. Import captures the original
CSMAR replay or LocalParquet reader result, requires exactly the original source
hashes bound by that reader, and stores both original bytes and normalized data.
Original LocalParquet file identities are now included in provider provenance.
Existing snapshots cannot be replaced; explicit predecessor links retain revision
history. Reopening verifies source and table bytes before returning data.

GovernedTableAdapter selects one source/kind/version explicitly. Diagnostics
report exact field-value, status and missing differences, with both source/version
identities and original artifact references. There is no ranking, tolerance-based
fusion or automatic winner. A capture window is not extended by filling gaps.

Historical knowledge uses only existing supported time evidence. CSMAR Dalyr and
static LocalParquet bundles remain availability UNKNOWN and reject all explicit
knowledge cutoffs, even after archival. Acquisition dates, effective dates and
revision labels cannot grant a PIT pass. Company-action governance reuses the
existing content-addressed event store, validation and selector: announcements and
provider availability must both satisfy the cutoff, future revisions remain out
of returned facts, and candidate statuses remain unchanged. Historical membership
and status readers keep their existing refusal of unsupported knowledge semantics.

No DatasetBuilder, Registry V2, Walk-forward, model algorithm or admission policy
changes. API details and usage are in `docs/data/DATA_GOVERNANCE.md`.

## Verification

Initial targeted run: **64 passed in 46.85s**, exit 0; see `initial-tests.log`.
This includes the initial governance cases, all existing DataHub tests, and local
and qualification admission-boundary tests. A state/missing diagnostic case and
explicit time-field mappings were added afterward. Final governance, existing
CSMAR source-binding and corporate-action regression run: **81 passed in 67.90s**,
exit 0; see `final-tests.log`.

The final eight governance cases cover reopen/payload/gate equivalence, linked
same-source revisions and cross-source value conflicts, overwrite/unbound-input
refusal, source and table corruption, CSMAR status provenance and unknown-time
refusal, manually calculable status/missing differences, and archived proposed
company actions before/at the provider-availability cutoff. The event case proves
that announcement alone cannot admit a revision before provider availability.
The existing admission-boundary tests continue rejecting unapproved consumption.

Reproduce from the repository root with the exact commands in the two logs.
Both runs use small offline fixtures; no supplier data is published in this
report. The logs record separate runs, not a summed count of distinct tests.

Engineering decision: **P302_ENGINEERING_ACCEPTED**.

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**.
This is software engineering validation, not financial certification.
