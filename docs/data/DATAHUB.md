# DataHub

`factorlab.data.hub.DataHub` is a read-only coordination facade. Sources are
explicitly bound to independent adapters; it does not choose a preferred vendor,
merge sources, cache data, certify PIT/HFQ, or grant financial admission.

```python
from pathlib import Path
from factorlab.data.hub import DataHub, ProviderAdapter
from factorlab.data.providers.local import LocalParquetProvider

hub = DataHub({"local": ProviderAdapter(LocalParquetProvider(Path("market_bundle")))})
result = hub.read("local", "daily_bars", symbol="600000.SH",
                  start="2024-01-02", end="2024-01-31")
bars = result.data                  # Original provider table; no metadata columns.
source_version = result.metadata.data_version
capabilities = hub.capabilities()
```

## API and supported sources

`read(source, kind, *, start, end, symbol=None, security_id=None,
knowledge_as_of=None)` reads an inclusive daily window. Supply canonical
exchange-qualified symbols; the facade never infers an exchange from a bare
code. Company events use the existing stable `security_id`, not a guessed
symbol-to-identity mapping. `calendar` uses the bound benchmark and takes no
symbol.

| Adapter | Capabilities | Binding |
|---|---|---|
| `ProviderAdapter` | `daily_bars`, `index_bars`, `calendar` | Existing `MarketDataProvider`; LocalParquet identities come from its verified manifest |
| `CsmarAdapter` | `daily_bars`, `security_status`, `observed_sessions` | Existing pinned `TRDNEW_Dalyr` references, external `source_root`, and `scratch`; parsing and source checks use existing replay |
| `MembershipAdapter` | `membership` | `DataFramePointInTimeUniverse` and explicit `index_symbol` |
| `SecurityDataAdapter` | `security_status`, `listings` | Existing security bundle root plus `calendar`, `membership`, `universe_sha256`; delegates to `load_verified_security_data` |
| `CorporateActionAdapter` | `corporate_actions` | Existing validated `SourceSnapshot`, optionally loaded by the existing corporate-action store |

CSMAR references may pin multiple securities. The adapter verifies the complete
reference through existing replay, then selects aligned raw/native/lineage rows
for the requested exchange-qualified symbol before calling `market_candidate`.
Replays use existing scratch handling, not another cache. CSMAR
returns the existing market-candidate fields unchanged: `Adjprcwd` is a comparable
reinvestment-close candidate, not certified HFQ or an execution price. Its
observed security sessions are **not** an official exchange calendar.

Unknown source names, unsupported capabilities and historical queries without
adequate time evidence raise `UnsupportedQueryError` (a `DataQualityError`).
Original reader errors, including absent symbols/snapshots and corrupt evidence,
propagate. Duplicate market keys and overlapping CSMAR source rows are rejected,
including identical duplicates; the facade does not silently deduplicate or
establish a vendor priority. It does not provide standalone announcement history;
company-event announcement times remain in the existing event objects.

## Time and evidence

`QueryResult.data` retains existing tables or company-event objects.
`QueryResult.metadata` carries one source envelope for the whole response:
source identity, data version, calendar identity/type, coverage, time-field
mapping, provenance and evidence status. Shared hashes and versions are not
repeated on market rows. `None` means UNKNOWN, not a verification failure silently
filled with a guessed value.

- Effective time: existing `date`, `listing_date` or `effective_date`.
- Announcement/public time: existing event `announced_at`; unknown for market rows.
- Provider availability: existing event `available_at`, where supported; otherwise
  UNKNOWN. Snapshot acquisition is not substituted for this field.
- Acquisition: existing event snapshot `acquired_at`; UNKNOWN for sources without
  a distinct acquisition-time contract. Local manifest generation and replay time
  are not treated as supplier acquisition or historical vintage evidence.

No cutoff means **retrospective engineering reading**, not historically available
information. An explicit `knowledge_as_of` requests a historical knowledge view.
The first four adapters reject it because their supported read contracts cannot
prove historical availability. A date-effective membership table or status bundle
alone does not establish what was known at the time.

Company-event selection delegates to existing `select_events`: public and available
times must satisfy its validated contract, and future versions do not enter the
returned tuple. Date-only evidence/cutoffs retain the existing Shanghai end-of-day
upper-bound convention; timestamps require a timezone. The response does not
expose the selector's unfiltered snapshot. Known revisions of a relevant event
remain together, including a rescheduled revision outside the effective window;
they are not an already-resolved latest-state table. Coverage records are limited
to the security, overlapping dates and the knowledge cutoff. Empty results with
unknown/partial coverage do not prove absence of events.

All built-in adapters retain `PIT_UNVERIFIED`, `HFQ_UNVERIFIED` and
`FORMAL_ADMISSION_BLOCKED`. DataHub does not promote source evidence to VERIFIED.
Source issues, synthetic boundaries and raw reference identities remain evidence,
not financial approval.

## Existing research consumers

Use `.data` to pass the original six inputs to
`DatasetBuilder.build(bars, benchmark, membership, status, listings, calendar)` or
its existing `build_chunked` entry. No Provider, DatasetBuilder, Registry V2,
Walk-forward or partitioned-dataset signature changes are required. Explicit V2
`data_version`, `calendar_id` and `FactorDataContext` remain consumer bindings;
DataHub does not invent a fused dataset identity or bypass existing admission.

Membership exposes exact supplied dates and rejects absent endpoint snapshots;
it does not infer an exchange calendar or fill interior gaps. Status/market
windows retain missing rows as missing. Existing downstream completeness checks
remain authoritative. General announcements, automatic cross-source fusion,
revision conflict governance and additional native CSMAR families are outside
this version.
