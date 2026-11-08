# P3-01 DataHub engineering acceptance

Base: `fe2b250e03b7004fe795efd12e6d0076d7bc8f19` (origin/main at startup).
Branch: `feat/p3-01-datahub`.
Environment: existing `D:/Environments/factorlab-py313/Scripts/python.exe`.

## Delivered behavior

`factorlab.data.hub.DataHub.read` routes explicitly named sources to independent
Provider, CSMAR Dalyr, membership, security-bundle and corporate-action adapters.
It returns original domain data separately from a single provenance/time envelope.
The facade does not parse vendor bytes, add a cache, fuse sources, implement a
PIT certifier or change financial admission. Existing Provider, DatasetBuilder,
Registry V2, Walk-forward and partitioned interfaces are unchanged.

Local data and CSMAR candidates are compared to their original readers. A small
100-session/four-security fixture passes original six-table inputs through the
facade to dense Registry V2 DatasetBuilder and single-worker partitioned output,
with exact output and dataset identity equivalence.

Explicit historical-knowledge cutoffs are rejected for the supported market,
observed-calendar, membership and security-bundle paths because those contracts
do not prove historical availability. Company-event knowledge selection delegates
to the existing selector and exposes only its selected facts, not a future-bearing
snapshot. Announcement, availability, effective and acquisition times remain
distinct; unknown acquisition/availability is not filled from generation or replay
time. All built-in results retain:

`PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`.

The workflow remains research-only: **NO LIVE MONEY**.

The API and limitations are described in `docs/data/DATAHUB.md`.

## Verification evidence

| Run | Result | Log |
|---|---|---|
| Initial new-feature tests | 17 passed | `datahub-tests.log` |
| Post-review CSMAR selection tests | 2 passed (one affected case rerun, one added case) | `csmar-fix-tests.log` |
| Related reader and research-entry regressions | 278 passed, 2 failed | `related-regression.log` |
| Exact two failures on unchanged base | Same 2 failures | `baseline-failures.log` |

The two regression failures are:

- `test_existing_public_metadata_stays_unknown_without_original_bytes`: missing `docs/research/evidence/five-year-data-feasibility-v1/pit-events.json`.
- `test_public_receipts_bind_exact_published_bytes_across_checkouts`: missing `docs/research/evidence/pit-effective-membership-evidence-v1/baseline-receipt.json`.

Both raise `FileNotFoundError` before DataHub is involved and reproduce on the
unchanged base, as pinned in `baseline-failures.log`. No skips, fabricated receipts
or changes to these tests were used to obtain this result.

TODO: Restore the authentic historical public-evidence artifacts required by these
two baseline tests in their owning evidence task. Do not synthesize replacement
receipts or turn these failures into passes by changing financial checks.

One read-only review of this change identified multi-security reference selection
and out-of-window overlap rejection in the CSMAR adapter. Both were corrected:
full original-reference replay precedes aligned symbol selection, and cross-source
duplicate checks apply to the requested result window. Targeted regression cases
cover these corrections; there is no source-priority or revision-conflict engine.

## Reproduction

From the task checkout in Git Bash:

```sh
PY=D:/Environments/factorlab-py313/Scripts/python.exe
"$PY" -m pytest tests/research/test_datahub.py -q
"$PY" -m pytest \
  tests/research/test_datahub.py::test_csmar_existing_replay_content_status_and_observed_calendar \
  tests/research/test_datahub.py::test_csmar_multisecurity_reference_preserves_pins_and_selects_aligned_rows -q
"$PY" -m pytest \
  tests/research/test_local_market_provider.py \
  tests/research/test_csmar_local_prepare.py \
  tests/research/test_csmar_source_binding.py \
  tests/research/test_security_status_import.py \
  tests/research/test_pit_event_evidence.py \
  tests/research/test_dataset_registry_v2.py::test_all_14_builtins_exact_legacy_dataset_equivalence \
  tests/research/test_walk_forward_factor_selection.py::test_weight_contract_and_fixed_default -q
```

The first command now collects the additional multi-security regression case; the
initial logged run predates that case. After the CSMAR fixes only the two affected
cases are rerun, not unrelated tests. For baseline attribution, run the following
on a clean checkout of the base SHA using the same environment:

```sh
"$PY" -m pytest \
  tests/research/test_pit_event_evidence.py::test_existing_public_metadata_stays_unknown_without_original_bytes \
  tests/research/test_pit_event_evidence.py::test_public_receipts_bind_exact_published_bytes_across_checkouts -q
```

`SHA256SUMS` binds the delivered implementation, API document, tests and logs.
Logs normalize absolute checkout paths to portable placeholders. No raw supplier
data or synthetic supplier archives are archived in this change.

## Remaining capability boundaries

Standalone announcement history, official CSMAR exchange calendars, additional
CSMAR families and unsupported historical knowledge views explicitly refuse
queries. Missing rows are not filled; supplied-date membership is not a complete
exchange calendar. Company events retain the existing known-revisions contract,
not a resolved latest-state table. Automatic source fusion and historical revision
conflict governance remain P3-02 scope.

Engineering decision: **P301_ENGINEERING_ACCEPTED**. All 18 distinct new-feature
cases have passed across the initial and post-review targeted runs. The selected
existing regressions have no new failures relative to the pinned base; the two
missing-artifact failures remain recorded above. Formal financial admission remains
blocked.
