# Multi-security dividend integration

SYNTHETIC / ENGINEERING_ONLY / NOT_FINANCIAL_EVIDENCE

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

## Interface and ownership

`AShareExecutionBacktester.run_target_weights(targets, bars, benchmark, *, dividend_plans=None)`
accepts a mapping of canonical target symbols to existing `DividendExecutionPlan` objects.
When enabled, coverage must match the full target security set, including explicit
complete empty-event plans. Every plan is validated before the first order. Event
identities must be unique across the account. Only raw opening and closing marks
are permitted. Default `None` preserves the ordinary execution path.

`_run_selected` and `_iterate_selected` forward the optional plans. The existing
`ExecutionBacktestResult` gains default-empty entitlement, recognition, payment and
return-accounting tables, plus optional `dividend_policy`. No existing positional
argument or required result field changes. The test evidence constructor's optional
`symbol` argument preserves its previous default; it is not a production interface.

One `PositionBook` remains authoritative for all holdings. One `cash` variable
remains authoritative for available cash. Existing `DividendExecutionLedger`
instances own only their existing entitlement/receivable/restricted-cash state;
there is no per-security available-cash account or duplicated dividend arithmetic.
Their audit rows are exposed with security identity in the account result.

## Timing and valuation

Before open, each existing ledger supplies only cash actually paid on an earlier
calendar date. Same-date payments occur after all opening orders. Record-date
entitlements read the shared position book after fills, rather than using opening
holdings; ex-date recognition and pay-date settlement retain the existing contract.

Opening target sizing and post-trade comparison equity include recognized
receivables and restricted cash. Actual buy budgets still receive only available
cash. Close NAV adds the same non-cash assets exactly once. Posting payment
removes the corresponding receivable and adds cash, not new income. Unknown tax
continues to restrict cash and withhold precise net equity through the existing
ledger policy.

The existing sell-before-buy order, symbol buy priority, date-effective fees,
slippage, board lots, price-limit logic and T+1 executor are unchanged. The
single-security adapter, other-market executors and forward-session recovery
interfaces are not extended by this change.

## Rejection and audit boundaries

Existing snapshot/association/term validation rejects unknown, unsupported,
missing or conflicting event terms before account execution. Cross-security
identity duplication and missing security coverage are additionally rejected at
the account boundary. Payment amounts are calculated from existing immutable
terms and registered shares; there is no external payment-amount override.
The acceptance audit rejects a tampered payment receipt rather than certifying
its NAV. Reproduction starts from a complete fresh initial state; it does not
introduce persistent restart state.
