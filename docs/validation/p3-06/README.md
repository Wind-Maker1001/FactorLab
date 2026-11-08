# P3-06: portfolio target execution

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

Baseline: `a64ebab03b3e7ca99fbb47fbd41a2161cad36d98`.

## API and reproduction

`AShareExecutionBacktester.run_target_weights(targets, bars, benchmark)` accepts a nonempty long-form DataFrame with `date`, `symbol`, `target_weight`. Each date is a complete long-only target snapshot; omitted old holdings target zero. Explicit zero weights liquidate. Weights are never rescaled. Dates must be naive daily benchmark sessions with a following session. Duplicate date/security inputs, non-session dates and final-session signals fail rather than being overwritten. Multiple rebalances execute at distinct following-session opens, never at signal-day closes. Explicit buys use canonical symbol order, not optimizer weight rank.

Bind the optimizer's security-indexed Series to its decision date explicitly:

```python
targets = optimization.target_weights.rename_axis("symbol").rename("target_weight").reset_index()
targets["date"] = signal_date
targets["optimizer_turnover"] = optimization.turnover  # omit when None
result = backtester.run_target_weights(targets, bars, benchmark)
```

The existing Top-K `run(scores, bars, benchmark)` still selects and ranks signals as before. Both entries use one execution loop, order executor, fee schedule and position book. `target.py` retains its separate single-security entry/exit and retry semantics.

Run from the worktree root with `D:/Environments/factorlab-py313/Scripts/python.exe`:

```sh
python -m pytest tests/research/test_portfolio_execution.py tests/research/test_execution_backtest_phase3.py tests/research/test_portfolio_optimizer.py tests/research/test_portfolio_backtest_phase2.py tests/research/test_dividend_execution.py tests/research/test_dual_ma.py -q
PYTHONPATH=src python -m examples.research.portfolio_execution_synthetic_demo
```

`pytest.txt` records the targeted acceptance result. `numerical.json` records the synthetic example and optimizer input identity. The example uses synthetic covariance, not calibrated expected-return forecasts. Its 64%/16% optimizer targets produce 600/100 shares at price 10, 7,000 buy notional, 2,986.5 remaining cash and 0.99865 final NAV after 13.5 costs. This intentionally preserves uninvested cash from rounding and fees.

## Execution evidence and units

- `signals` preserves requested weights and scheduled dates. `target_comparison` includes each target and old holding, zero actual positions, legal target quantity, actual quantity, post-trade opening weight and deviation. The denominator is post-cost opening portfolio value. Missing opening prices leave unknown target quantities as NaN, not fabricated share requests. Such targets receive `side=target` rejection evidence with unknown quantities; zero-weight sell requests remain quantitatively known.
- For missing held opening/closing prices, the existing engine carries prior marks. The comparison valuation label explicitly includes this possibility. Daily `positions.weight` uses end-of-day marks and is distinct from execution-time deviation.
- `nav.buy_notional` and `sell_notional` sum actual fills. Existing `nav.turnover` remains `max(buy_notional, sell_notional) / turnover_denominator`, where the denominator is opening pre-trade NAV, including blocked old holdings. Optional `optimizer_turnover` is the supplied optimizer gross security-weight change `sum(abs(target - previous))`, including exits; no executed-turnover equivalence is asserted. The example has 0.8 optimizer turnover versus 0.7 executed turnover.
- `ExecutionBacktestConfig.target_order_sizes` maps board or canonical security to `OrderSizeRule(minimum, increment)`. Explicit targets default to main/ChiNext 100/100 and STAR 200/1. Security overrides take priority; missing rule coverage fails. Changes below minimum order size remain unexecuted and visible as weight deviation; full liquidation can sell the residual. Existing Top-K retains its configured `board_lot`. These are configurable daily-bar research rules, not a complete exchange admission or live-order contract.
- Sells precede buys. Rejected sells keep their capital occupied. Buy cash sizing includes commission, taxes/transfer fees and slippage from the configured schedule. There is no implicit retry, terminal liquidation, redistribution of unfillable weights or assumption of full fills.

## Scope

Tests cover two-to-five-security hand calculations, multiple rebalances, pool exits, zero targets, blocked sells, successful sell/rejected buy, partial cash fills, missing execution prices, board minimums/increments, T+1 in the shared order executor, deterministic results, Top-K equivalence and accounting reconciliation. Future-close perturbation leaves opening orders unchanged. Daily snapshots cannot represent multiple intraday signals; upstream signal provenance remains the caller's responsibility, while the optimizer example timestamps covariance before its decision.

No corporate-action plan is supplied to this multi-security API. The example has no corporate actions; it generates neither dividend cash nor automatic adjusted-share/price changes. The existing single-security dividend-plan interface remains separate. Daily-bar price limits remain conservative approximations rather than queue simulation.
