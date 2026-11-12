# P8-E02 multi-security shared-account dividend acceptance

SYNTHETIC / ENGINEERING_ONLY / NOT_FINANCIAL_EVIDENCE

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

**P8_E02_ENGINEERING_ACCEPTED**

## Result and scope

The existing target-weight executor now handles multiple cash-dividend plans
inside its single shared-account loop. The accepted primary account contains
**12 consecutive sessions (2024-01-03 through 2024-01-18), 3 fictional securities,
4 events and 8 fills**. Two securities are held concurrently. Three events have
positive entitlement; the fourth records zero eligible shares. Gross and available
cash payments total **CNY 9,800** under explicit synthetic zero tax. Final available
cash is **CNY 16,913.66**, and final account NAV is **0.9971366**.

This is deterministic execution engineering, not historical investment evidence.
The 990001.SH, 990002.SH and 990003.SH identifiers are fictional fixture identities
in the existing canonical syntax, not certified listed-security inputs. Fixed
prices, targets, nonzero fees/slippage and event terms are frozen in
[synthetic-scenario.json](synthetic-scenario.json); seed is 0 with no random
generation. Source qualification and financial admission gates remain blocked.

Starting origin/main HEAD was `fc0fa9412af6f3fa33765b10476213ad7a784941`, tree
`64be64d6afe212a71903a56e020aa2fd46a54c6e`. The independent
`engineering/p8-e02` worktree started clean. P8-E01's report, successful
reconciliation and runner were read from its existing worktree without altering
it or repeating its full experiment.

## Actual missing connection

`run_target_weights` already prepared complete snapshots and dispatched them via
`_run_selected` into `_iterate_selected`, whose orders use `_execute_order` and
one `PositionBook`. It had no dividend-plan argument and did not call the existing
ledger lifecycle. In contrast, `run_target_positions` already called
`DividendExecutionLedger.before_open`, `after_orders` and `balances` in-loop.
That was the actual missing connection, not an absence of corporate-action
facilities.

The optional `dividend_plans` mapping now reaches the same existing loop. Existing
per-security evidence contracts and ledger lifecycle are reused; their cash deltas
all update the account's one available-cash variable. There is no second engine,
parallel cash account, new position system or copied dividend calculation.
[Integration design](integration-design.md) specifies the compatible interface,
ownership and timing.

## Entitlement, cash and NAV evidence

The primary account includes different record, ex and payment dates. Its first
security has 500 eligible shares, subsequently sells before payment, and receives
CNY 5,000 despite having no position on the payment date. The third security has
no record-date holding and receives zero. A separate record-close-sale case
confirms that selling at the record-date open removes entitlement; opening
holdings alone are not the entitlement basis.

There are positive payments for different securities on the same date, as well as
a second payment date. A same-day variant exercises three positive simultaneous
payments. Reversing the input plan insertion order preserves every account and
audit table exactly. Each recognition and payment is checked against its event's
registered shares and frozen terms, and every observed receivable balance is
reconciled to recognized less paid amounts.

Unpaid dividends enter receivables, not the buy budget. Opening target equity and
close NAV include the recognized receivables once. Date-only payments happen after
opening orders, transfer receivables to cash without creating income again, and
become spendable at the next observed open. The payment-boundary test places full
weight demands before, on and after payment: the pay-date order remains
cash-constrained and the following open can buy using paid cash. In the primary
account a price-limit sell rejection and cash-constrained buys also remain visible
in [rejections.json](rejections.json), rather than bypassing execution rules.

[Account reconciliation](account-ledger-reconciliation.json) reports maximum cash
residual **1.4551915228366852e-11 CNY**, position residual **0 shares**, and equity
residual **0 CNY**, with absolute monetary tolerance `1e-8`. Signed actual fills
reconcile cash and per-security positions. Actual fee components reconcile to the
date-effective fee schedule. Event recognition/payment identity, gross/net amounts,
available cash, restricted cash, marked positions and NAV are audited; payment
receipt tampering is rejected. No repeated recognition or payment occurred.
Detailed account tables are the existing executor's outputs, not a replacement NAV.

The same targets with no plans and with complete empty plans produce identical
ordinary NAV columns, fills, positions, targets, comparisons and rejections. No
cost, slippage, T+1, buy-priority or other-market logic was changed.

## Evaluation and reproducibility

The engine's account NAV is passed directly to `ResearchEvaluation` with a clearly
synthetic flat benchmark. Drawdown and other portfolio metrics are stored in
[evaluation.json](evaluation.json). `ExperimentTracker` saves the frozen economic
configuration identity, payment policy, SYNTHETIC qualification and ledger table
identities; existing immutable-record tests verify overwrite refusal. The runner
requires a new output directory. Its two complete primary runs start from the
same initial cash and empty book and compare all ten result tables exactly.

Configuration identity:
`a11c71444517733143d943c00b178ae78a8830dbed98942efc48979a98e7d2f3`.
The final evidence was executed from clean implementation commit
`1aeba4c6552d7a3d6852cdc76ca0d019e56a917a`, tree
`c5f55163260c479fbdb54073d2c2fb2590d3cff6`; the existing provenance capture
records that actual checkout in both the experiment run and
[execution-provenance.json](execution-provenance.json), separately from the
starting-main identity.

**14 new cases passed** in the final targeted run (3.40 seconds).
**93 existing targeted cases passed** in the initial combined run. That initial
run also reported one new test failure caused by duplicate fixture targets; after
correcting that input, the boundary test exposed a fixture payment smaller than
one board lot. The boundary variant was changed to the already-required same-day
positive payment scenario, retaining the engine's cash constraint unchanged.
The corrected new suite passed in full. Total distinct passing cases: **107**.
No whole-repository tests, market downloads, walk-forward experiment, new virtual
environment/database or performance benchmark were used.

Reproduce from the checkout in Git Bash with the existing environment and a new
output directory:

```sh
PYTHONPATH='src;.' D:/Environments/factorlab-py313/Scripts/python.exe \
  tools/research/run_p8_e02.py --output research/p8-e02-reproduction
PYTHONPATH='src;.' D:/Environments/factorlab-py313/Scripts/python.exe -m pytest \
  tests/research/test_portfolio_dividend_execution.py \
  tests/research/test_portfolio_execution.py \
  tests/research/test_execution_backtest_phase3.py \
  tests/research/test_dividend_execution.py \
  tests/research/test_experiment_tracker.py -q
```

The runner blocks socket connections. Invalid/unknown events, missing payment
terms, duplicated event identities, conflicting cash terms, adjusted-price use,
incomplete plan coverage and unsupported share-change events fail explicitly.
The acceptance runner writes its successful verification only after account audit,
repeatability, evaluation and tracking succeed.

## Required answers

1. **Missing connection:** optional plan injection and lifecycle calls in the
   existing multi-security `_iterate_selected` account loop, plus receivable-aware
   valuation; single-security lifecycle machinery already existed.
2. **Reused modules:** `DividendExecutionLedger`, evidence validation,
   `PositionBook`, existing cash state, `_execute_order`, fees/price-limit rules,
   `dividend_return_accounting`, `ResearchEvaluation`, `ExperimentTracker` and
   deterministic evidence constructors.
3. **Interfaces:** optional keyword-only `dividend_plans` on `run_target_weights`,
   internal forwarding, and default-empty audit tables/optional payment policy on
   `ExecutionBacktestResult`. Existing callers remain valid.
4. **Old no-dividend results:** unchanged in exact ordinary-output comparison;
   existing targeted execution regressions pass.
5. **Sessions:** 12 in the primary continuous account, executed twice identically;
   separate small boundary/regression variants are tests, not appended history.
6. **Securities/events:** 3 fictional securities and 4 events, of which 3 have
   positive entitlement and 1 has zero entitlement.
7. **Historical cash availability:** correct; receivables cannot fund orders,
   date-only pay-date cash cannot fund that day's opening orders.
8. **NAV:** recognizes eligible receivables once, settles them into cash without
   double-counting, and reconciles to actual account assets.
9. **Ledger audit:** passed; cash residual below `1e-8`, exact position and equity
   conservation, fee and event amount audit passed.
10. **Duplicate recognition/payment:** none; duplicate identities are rejected.
11. **Tests:** 14 new, 93 existing reused and executed, 107 distinct passing cases.
12. **Real functional gaps:** none remaining within this cash-dividend synthetic
    account scope. Unsupported share changes and financial qualification retain
    their explicit refusal boundaries.
13. **Interruption recovery:** no further recovery development is required for
    P8-E02; persistent restart remains a separately scoped capability, not a
    prerequisite for this fresh-state repeatability acceptance.
14. **Draft PR:** [#126](https://github.com/Wind-Maker1001/FactorLab/pull/126),
    targeting `main` from `engineering/p8-e02`; no automatic merge.
