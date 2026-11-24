# V69-F03 implementation status (partial)

The new trading.portfolio_target module reuses target_quantity and account
contracts to plan long-only, one-times-collateral portfolio targets from a single
USDT balance. Reductions form a separate phase; increases require a subsequent
reconciled snapshot. Missing prices/rules preserve cash. Ownership is supplied
explicitly; this planner does not infer ownership from exchange positions.
Economic multipliers are explicit instrument metadata (TQQQUSDT requires 3).

This is a planning foundation, not an operational portfolio executor. It is not
wired to DemoStrategyRuntime, Supervisor, Feed or Deployment. Existing Demo
runtime remains NVDAUSDT-only. The planner's caller must supply fresh prices,
current rules, verified ownership and account equity including unrealized P&L.
Lot/minimum-notional admission remains the execution gateway's responsibility.

Outstanding: durable rebalance identities and restart recovery; multi-symbol
Demo gateway/session admission and account allocation; campaign integration;
fill/cancel/UNKNOWN recovery integration; funding, fee and NAV reconciliation;
complete required fault-injection tests and official online validation.

Validation: python -m pytest tests/integrations/test_portfolio_target.py -q
Result: 7 passed. Covers shared cash, reduce-first planning, unavailable symbol,
explicit ownership, economic multiplier, dynamic quantity step and bad weights.

Online record: NOT RUN. No multi-symbol admitted execution path has been
implemented; no exchange request or order was made for this task. This record
must not be treated as official Demo acceptance evidence.
