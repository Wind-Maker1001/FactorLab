# Batch Grid research simulator

## Profiling baseline

Frozen definition: `configs/mc05_grid_tail.json`; 8 GBM paths, seed 1729, 168 hourly increments plus initial anchor, independent USDM accounts, synthetic capacity 0.10. Run `PYTHONPATH=src python tools/research/profile_grid_batch.py --output <new-directory>`. Measurements and profiles are in `docs/validation/mc-grid-gpu-01/profile-initial/`.

Initial local measurements:

| Existing execution path | Wall seconds | Accounts/second |
| --- | ---: | ---: |
| File Paper, returned artifacts | 40.851 | 0.196 |
| In-memory Paper, returned artifacts | 1.108 | 7.219 |
| File Paper with MC FIFO/lifecycle reduction | 22.269 | 0.359 |
| File MC, 8 workers including startup | 12.661 | 0.632 |

File and memory Paper returned identical rows, trades, and result structures for all 8 paths. MC terminal equity and fill count agreed with Paper; parallel MC results agreed with serial MC.

These are single-pass measurements in fixed order, not stable speedup estimates. The two file variants differ in artifact extraction and reduction work; their observed ordering must not be interpreted as a causal benefit from extra analysis. The memory variant still executes SQLite transactions, serialization and confirmation logic; it removes filesystem durability and skips file-oriented evaluation exports, not all audit work.

For the first frozen path, cProfile measured 3.650 seconds in the file MC replay: SQLite `execute` consumed 2.977 seconds of self time. In-memory replay measured 0.243 seconds: `_paper_command` accumulated 0.209 seconds, with serialization, dataclass traversal, fingerprints, state saving and checkpoints prominent. These are nested cumulative times and must not be added. Only one fill occurred on that profiled path; fill-heavy deterministic cases require separate profiling before generalizing matching cost. FIFO/lifecycle are not the dominant cost in this sample.

The fill-heavy oscillating profile adds a different bottleneck: 168 fills caused `_historical_turnover` to consume 0.979 seconds cumulatively and `order_events` to decode 29,544 reports in a 2.236-second profiled in-memory replay. Admission rescans prior reports per fill, making this work grow with history. The independent research kernel maintains confirmed gross turnover incrementally; this does not change or optimize the authoritative executor.

## Economic contract identified so far

- Paper matching uses adverse slipped quote prices and then checks limit eligibility, not unconditional grid-line touch. Favorable gaps may improve execution relative to the limit.
- Each observation snapshots prior working orders, updates quote and strategy decisions, then matches those prior orders in submission order. Cancellations and new submissions follow matching; newly proposed reverse orders cannot fill on that same close.
- Capacity is shared across orders in one observation. Incremental quantities are floored to step; fill admission additionally enforces minimum quantity/notional and remaining turnover budget. Below-minimum fragments do not execute.
- Buy prices floor to tick, sells ceil. Proposal admission includes pending orders, available collateral, inventory and committed-capital bounds, and a paired-cycle cost test.
- Partial fills can accumulate an unsubmitted reverse remainder until order filters permit a legal adjacent-level proposal. Cancellation requests suppress reversal.
- TTL, breakout, drawdown and turnover can alter decisions before matching, while cancellation occurs afterward. Porting only grid spacing would not preserve these semantics.
- USDM keeps opening notional out of cash, reserves entry collateral and credits realized PnL on closing; equity adds mark unrealized PnL. Spot instead spends cash on purchases and includes position market value in equity. Paper average-entry realized PnL and FIFO cycle attribution are distinct outputs.

## Implementation status

The profiling entry point, CPU in-memory Paper reference adapter, RawKernel, per-step/per-fill trace output and two-layer research CLI are implemented. The final 24-test kernel/integration suite passed on RTX 2070, covering static modes, pause/exit, partial fills, capacity exhaustion, fees, slippage, initial inventory, TTL, exact boundaries and volatile random paths. Scale execution completed from 100 to one million paths, with all-path native CPU/CUDA terminal comparison and independent Paper samples. The `cpu` backend now compiles the same state-transition source into a single-threaded native library using the optional Zig compiler; `paper` remains the existing independent authoritative in-memory reference. The shared CPU/CUDA comparison isolates native execution hardware more closely than comparing CUDA with Paper. Multi-core CPU execution is not represented by this single-thread baseline. Inputs explicitly require six-decimal quotes (use `freeze_prices` before freezing the shared Paper/GPU input); execution eligibility compares integer slipped-price numerators to integer limits. Quantity state uses integral quantity steps. Account valuation uses FP64. Scope is static arithmetic/geometric USDM, pause/exit, TTL without reseeding, proportional fees, fixed synthetic capacity; budget sizing, ATR/rebuild and external lifecycle actions are rejected. Runtime order-buffer exhaustion and controller submission rejection are explicit unsupported errors, never truncated output. Measured performance, accounting comparisons, audit selection and reproduction commands are recorded in `docs/validation/mc-grid-gpu-01/REPORT.md`.

## Running

Install the project with `.[monte-carlo-native,monte-carlo-cuda]`. The CPU backend compiles its library on first use and caches it outside the source tree. The compiler is not required for the Paper or CUDA backends.

```sh
python tools/research/run_grid_batch.py --sizes 100 1000 10000 100000 1000000 \
  --output <new-output-directory>
```

Existing `MonteCarloRunner` workflows can pass their single-asset price output directly through the research boundary:

```python
from factorlab.research.grid_simulation import freeze_prices, simulate

# mc_result comes from MonteCarloRunner with save_full_paths=True.
prices = freeze_prices(mc_result.asset_paths[:, :, 0])
research = simulate(prices, batch_config, backend='cuda')
risk = research.risk(batch_config.capital)
# Pass exactly these frozen prices, not the original unrounded array, to Paper audits.
```

The standalone benchmark CLI uses its explicitly recorded NumPy GBM stream, not the historical MC-05 `seed + batch_id` stream; it is not a drop-in replay of MC-05 model/seed identities. The simulation API accepts any frozen positive single-asset paths, including existing GBM/bootstrap outputs, within its supported economics.

The default runs native CPU at every requested scale and authoritative in-memory Paper for the first 100 paths of each scale. `--cpu-max-paths` and `--paper-max-paths` independently bound those comparisons; report their actual sample counts. `--audit-samples` selects independent random paths in addition to terminal-equity quartile representatives and both extremes. Selected paths are replayed with full Paper exports and FIFO attribution. Their selection labels are retained, and they do not replace the population used for risk statistics.

## Numeric and support boundary

Prices are explicitly frozen to six decimal places for *both* engines, not silently rounded inside `simulate`. Ticks and fee/slippage fractions must be representable at that scale. Quantity steps and minimum notional use at most eight decimals. The integer price/slippage numerator remains within signed 64-bit range for quotes <=1000000; notional eligibility uses a two-word product comparison to avoid product overflow. Quantity state is integral step counts. Monetary valuations and proportional cost estimates use FP64, with fused multiply-add disabled.

Exact touch and exact minimum-notional cases are tested separately. Near FP64-sensitive monetary risk and admission thresholds, the kernel returns unsupported condition 3 rather than deciding with a tolerance. The current exclusion band is `128 * epsilon * (observations + 128) * max(capital, max_cash_committed)`; it is a conservative engineering restriction, not a proof covering arbitrary unbounded horizons. Account comparisons currently require `rtol=1e-12`, `atol=5e-10`; discrete state, quantity, order identity and fill sequencing must agree. Runtime errors 1/2 mean the 128-working-order capacity or controller-admission support boundary was reached.

Supported market is dedicated long-only USDM at hourly close observations, fixed synthetic capacity, proportional broker fees and default proportional fee-budget schedule; funding is not simulated. The horizon is bounded to 4096 increments. `capital` determines endowed account equity; `GridConfig.initial_cash` is overwritten by the Paper contract, not separately added. Actual fees use `fee_rate`; reservation cost estimates use that fee rate plus `GridConfig.slippage_bps`, independently from actual matching `slippage`, as Paper does. Output `realized_gross`/`unrealized` are Paper average-entry gross accounting, not FIFO cycle PnL. FIFO lot age, completed-cycle count and net cycle/residual attribution remain in selected full Paper audits, not the batch output. No spot market, custom fee schedule, ATR, budget sizing, range rebuild, expiry reseed, external recovery, minute execution clock or adaptive policy is implied. A research result is not an authoritative execution receipt.
