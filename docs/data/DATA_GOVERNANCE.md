# Immutable engineering vintages

`GovernanceStore` archives existing DataHub table reads with explicit source,
version, kind, symbol and capture window. The optional `GovernedTableAdapter`
exposes a selected vintage through the unchanged `DataHub.read` API. No default
source selection, fusion, consumer signature or financial admission changes.

```python
from pathlib import Path
from factorlab.data.evidence import snapshot_file
from factorlab.data.governance import GovernanceStore
from factorlab.data.governed_adapter import GovernedTableAdapter
from factorlab.data.hub import DataHub

store = GovernanceStore(Path("governance_archive"))
# hub is an existing DataHub bound to a LocalParquet ProviderAdapter.
# manifest and stocks_file are the ORIGINAL verified bundle files.
artifacts = tuple(snapshot_file(p, "licensed-local-source", acquired_at)
                  for p in (manifest, stocks_file))
store.capture(hub, "local", "daily_bars", version="revision-1",
              symbol="600000.SH", start="2024-01-02", end="2024-01-08",
              artifacts=artifacts, acquired_at=acquired_at)
archived = DataHub({"selected": GovernedTableAdapter(
    store, source="local", kind="daily_bars", version="revision-1")})
result = archived.read("selected", "daily_bars", symbol="600000.SH",
                       start="2024-01-02", end="2024-01-08")
```

`capture(..., predecessor="revision-1", version="revision-2")` records a new
revision. The predecessor must exist in the same source/kind/symbol namespace.
Version names are labels, not chronological or priority rules. Reusing a version
is rejected even for identical content. Original artifacts and normalized table
bytes are content-addressed; reads verify both. The capture window is explicit:
queries outside it refuse rather than pretending that missing rows prove absence.

CSMAR capture requires the exact original source archives pinned by the selected
replay references. LocalParquet capture requires its verified manifest and the
selected stock or benchmark Parquet file. Unrelated artifacts, manifest-only
captures and unsupported source bindings refuse. Vendor parsing remains in the
existing readers. Store the archive outside mutable supplier input directories.
Hashes bind local evidence; they do not authenticate a vendor or certify facts.

## Historical knowledge

The four times are distinct:

| Time | Table vintages | Corporate-action vintages |
|---|---|---|
| `effective_at` | existing row `date` | `effective_date` |
| `announced_at` | UNKNOWN | existing event `announced_at` |
| `provider_available_at` | UNKNOWN | existing event `available_at` |
| `acquired_at` | explicit capture acquisition metadata, plus original artifact acquisition | original snapshot acquisition |

An acquired file is not proof that its contents were available on the effective
date. CSMAR Dalyr and static LocalParquet bundles have no historical availability
contract, so governed table queries with `knowledge_as_of` raise
`UnsupportedQueryError`. Earlier acquisition or a predecessor label does not
change this. Retrospective reads always require an explicit version.

For actual event-level knowledge evidence, use
`GovernedCorporateActionAdapter.capture(snapshot, root)`, then reopen with
`GovernedCorporateActionAdapter(root, generation=adapter.generation)`. This reuses
`publish_snapshot`, `load_snapshot`, `validate_snapshot` and `select_events`.
The content-addressed generation preserves original artifacts and all revisions;
DataHub returns only events known at the requested cutoff. A revision must satisfy
both announcement and provider-availability times. Date-only times retain the
existing Shanghai end-of-day convention. All known revisions remain explicit,
including rescheduled events; this is not an automatically resolved final-state
table. Coverage and candidate statuses retain their original meaning.

Historical ST, suspension and membership observations must not be treated as
historical knowledge just because they have effective dates. CSMAR status tables
can be archived retrospectively; sources lacking a bound historical availability
contract do not gain one through governance. Existing membership/status adapters
remain unchanged and continue rejecting unsupported knowledge queries.

## Conflict diagnostics and explicit selection

```python
conflicts = store.diagnose("local", "revision-1", "other", "revision-a",
                           "daily_bars", symbol="600000.SH",
                           start="2024-01-02", end="2024-01-08")
```

Comparisons use exact values, not a guessed tolerance, currency conversion or
vendor priority. Each conflict contains the date/security key, field, `VALUE`,
`STATUS` or `MISSING` type, and both source/version/value/evidence references.
Status fields include `trade_status`, `is_st` and `is_member`. Missing on both
sides is not evidence of agreement about an unobserved event. Diagnostic results
are not merged data: select the desired source/version explicitly in an adapter.
Only compare fields with independently established matching units and semantics.

All paths remain **NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED /
FORMAL_ADMISSION_BLOCKED**. Historical event filtering is engineering behavior,
not PIT certification. In particular, storing a proposed dividend, bonus issue
or capitalization event does not promote it to an approved or implemented fact.
