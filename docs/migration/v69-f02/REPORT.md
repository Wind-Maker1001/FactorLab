# V69-F02 migration status

Partial implementation; original equivalence is NOT established. No deployable strategy is delivered.

## Source investigation

FactorLab origin/main 5e9f29a: no v48/v50/v69 or omit_sndk matches in src/docs/tests, filenames, or all local commit subjects. GitHub quant-tv HEAD recursive tree contained no version filenames; owner-scoped GitHub code search for omit_sndk returned no results. These searches are not proof that originals do not exist. Source location was requested from the user.

source-manifest.json records missing originals as null, not fabricated hashes. Once provided, freeze complete originals and dependencies with SHA256, upstream commit/path, license, data adjustment provenance and reference input/output fixtures before implementing signals.

## Delivered boundary

Three variant identities, complete eight-stock weight validation, deterministic stateless contract projection without renormalization, explicit causal timestamp constraint, session-calendar weekly predicate and per-stock CSV comparison writer. No implicit TQQQ conversion: input weights are nominal NAV fractions. Missing contracts retain their allocation uninvested. Contract identifiers must be supplied from the existing execution framework, not guessed.

The existing strategies.protocol.Decision supports a single target_weight; this module is deliberately not registered in the live loader. An account-wide portfolio adapter remains necessary. Existing account ledgers, clients and cloud runtime are untouched.

## Blocked acceptance

Original TARGET, TQQQ conversion, MA28 volatility channel and hysteresis, MA63 details, breadth, 42-day regression conventions, quantile interpolation, two-day confirmation, weak-market exits, both 1.10 adjustments, omit_sndk, adjustment/volume basis and precise weekly event ordering are unverified. No v85 logic added. Warmup, missing bars, suspension and state replay semantics cannot be claimed equivalent. No historical weight comparison was run and no synthetic output is represented as original evidence.

REFERENCE/CAUSAL timing differences cannot yet be quantified. CAUSAL must execute strictly after the latest observation, using a subsequent executable quote rather than the observed official open. The weekly helper reuses cross_market.calendar and requires Monday coverage, including holidays and year boundaries. Mapping never changes the signal universe. Suspension/data gaps must not silently drop stocks.

## Validation scope

Tests cover missing-source rejection for all variants, input pool and finite weight checks, contract mapping and retained allocations, deterministic repeated projection, comparison log rows, timezone-aware causal ordering, and holiday/year-boundary weekly selection through the existing SessionCalendar. These boundary tests do not validate the unavailable strategy. Real XNYS DST, suspension, warmup, restart state and original reference replay remain pending.
