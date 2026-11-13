# P7-M04 bounded Demo LIMIT quote

**P7_M04_BOUNDED_QUOTE_ENGINEERING_ACCEPTED** — offline bounded-policy engineering and targeted safety validation passed; a fresh signed GET-only account/market observation is still required before any policy can be approved.

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

Baseline: `02c450e4c1862d6de497455395047cf033eb3686`.

## Approval boundary

This work prepares an optional bounded opening quote, not a continuous trading strategy.
The approved object must bind the credential-derived account, deterministic pricing
formula, fixed quantity, absolute price and independent economic limits, expiry,
unique order identity and cleanup/recovery behavior. The historical M03 fixed-price
approval does not authorize this policy. A serialized policy never creates a
process-local network-write capability.

Actual M04 network traffic so far: **0 GET, 0 POST, 0 DELETE, 0 account-configuration writes**.
Offline fixtures are not exchange requests or evidence of an official Demo fill.

The bounded quote plus existing Demo network/execution/plan-review suites passed
**111 tests**, and the preparation plus read-only preflight regression suites passed
**56 tests**, in the configured Python 3.13 runtime. The bounded suite includes the
18 requested scenarios plus dedicated lost-DELETE and structured-receipt checks.
Compilation of the modified modules also passed.

## Read-only preparation

`tools/prepare_bounded_demo_quote.py` uses the existing masked credential dialog or
Demo-specific environment variables and the GET-only preflight transport. It checks
the exact newly selected client ID before recording market observations. Only
`ORDER_NOT_FOUND` establishes absence; another error remains UNKNOWN. The complete
account binding is written exclusively to the selected private output; the public
receipt substitutes a shortened hash reference. No key, secret, signature or raw
private-account response belongs in either output.

Candidate budgets are proposals for a new independent approval, not reuse of an old
risk allowance. The candidate uses a fixed 50 bps offset, a bounded interval around
the observed bid within the current filters/mark band, and the minimum fixed
step-aligned quantity meeting minimum notional at the approved lower limit. The
quantity is not increased during execution. Full-notional collateral and a 0.10%
fee reserve provide independent nominal and total-funds budgets. The preparation
receipt records the exact arithmetic and all proposed limits.

`max_loss` gates opening admission. It neither guarantees a maximum account loss
nor implements automatic stop-loss, cancellation or closing. A partial fill remains
real inventory. Closing requires independent reduce-only approval. A GTC order can
remain at the exchange after authority expires; expiry prohibits DELETE as well as
POST, while GET reconciliation remains available.
