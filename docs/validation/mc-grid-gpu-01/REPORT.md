# Static Grid CPU/CUDA research evidence

## Experiment

The final scale run is `final-scale/report.json`: GBM seed 1729, the drift/volatility and static geometric Grid from `configs/mc05_grid_tail.json`, 168 hourly increments plus the initial quote, independent USDM accounts, initial equity 1000 USDT, capacity 0.10, fee 0.0004 and adverse slippage 0.0006. Every generated quote is explicitly frozen to six decimals before either execution backend sees it. Sizes use nested prefixes, not independent experiments. Input generation is CPU NumPy; these timings do not measure GPU random-path generation.

Hardware: NVIDIA RTX 2070, 8 GiB, compute capability 7.5, driver 617.42; Python 3.13.9, CuPy 14.2.0. The native CPU baseline is single-threaded and compiles the same path-state source as CUDA. Compiler setup uses a cached library; it is not a cold native compilation benchmark. CUDA's first size includes context initialization and compile/cache loading. `environment.json` records the host environment.

## Final scale measurements

Times in seconds. GPU wall includes quote generation, freezing, input validation, host/device transfers, setup and result collection, but excludes the separate CPU/Paper checks and selected full-account audits. CPU call and CUDA call below both exclude random-path generation. Kernel columns exclude their Python wrappers and transfers.

| Paths | GPU wall | CUDA call | CUDA kernel | Native CPU call | CPU kernel | GPU wall paths/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 0.601340 | 0.600689 | 0.010823 | 0.011640 | 0.000624 | 166 |
| 1,000 | 0.013211 | 0.007488 | 0.001979 | 0.008204 | 0.004647 | 75,696 |
| 10,000 | 0.092502 | 0.036086 | 0.005907 | 0.072946 | 0.045138 | 108,106 |
| 100,000 | 0.829307 | 0.346711 | 0.052593 | 0.687336 | 0.453458 | 120,583 |
| 1,000,000 | 8.340301 | 3.505277 | 0.748254 | 6.722103 | 4.548161 | 119,900 |

At one million paths, same-source CPU/CUDA kernel time ratio is approximately 6.08; complete backend-call ratio is approximately 1.92. These are observed single-run ratios on this device, not a promised acceleration factor. Small workloads can favor CPU, particularly with cold CUDA setup. Path generation alone consumed 4.806494 seconds at the largest scale and dominates the final GPU wall time.

The largest in-use CuPy array allocation measured 6,081,024 bytes with batch size 4096; this is **array-pool allocation**, not total device memory including context, driver allocations and thread-local spill storage. Process peak RSS was 691,355,648 bytes, including the retained million-path terminal array and the later CPU/Paper comparisons. Host memory is not constant in path count because terminal results are retained for population risk and audit selection; full price paths are regenerated in batches.

The run used a local GPU, not Colab billing. CU cost is recorded as null with `LOCAL_DEVICE_NO_PLATFORM_CU_RATE`. A platform run can supply its observed `--cu-per-hour`; the report then multiplies that rate by GPU end-to-end wall time, not by kernel time alone.

## Correctness evidence

- Related existing Grid/Paper/MC tests plus the initial batch suite: 124 passed (`targeted-tests.xml`).
- Final kernel/integration suite: 24 passed on Python 3.13.9 (`kernel-final-tests.xml`) and 24 passed on Python 3.14.0 (`python314-kernel-tests.xml`), including 64 independently generated volatile paths per static grid mode, matching both native CPU and CUDA against Paper at every observation and every fill. Wheel construction succeeded and retained both native Python adapter and CUDA/shared-state source resources.
- All terminal columns of all five requested sizes, including one million paths, matched native CPU/CUDA at `rtol=1e-12`, `atol=5e-10`. At every size, the first 100 paths were also independently executed by in-memory Paper and matched.
- Eight additional full-account audits were selected from the million-path population: three independent random draws, three terminal-equity quartile representatives, and both terminal-equity extremes. Labels and original path IDs are in `final-scale/report.json`. Each received stepwise and per-fill CUDA/Paper comparison and persisted Paper exports with FIFO attribution. Their maximum absolute per-step error was 1.2761146306328186e-13 across the exported columns. These selected paths do not estimate population probabilities or CVaR.
- Numerical decision boundaries have separate exact-touch/minimum-notional tests and explicit unsupported refusals near floating monetary decision thresholds. This exclusion is not a relaxed account-comparison tolerance. The simulator refuses the whole affected batch; it does not discard unsupported paths from risk statistics.

The corpus covers upward/downward trends, oscillations, gaps, extreme positive prices, no fills, multiple eligible orders, capacity-limited partial fills, accumulated reverse fragments, insufficient capital, initial inventory and exhaustion, expiration, breakout pause/exit, price/quantity rules and costs. Realized gross PnL in batch output uses Paper average-entry accounting. FIFO net-cycle and residual attribution is produced by the existing audit engine, not approximated by the batch kernel.

## CPU CI and hardware regression separation

The CPU/Paper suite now runs independently of CuPy and CUDA. Native compilation uses `ziglang==0.15.2` from the existing CI development requirements; missing compiler dependencies fail the tests rather than skip them. The explicit CUDA suite reuses the economic and numerical scenarios with a CUDA backend fixture and requires an actual device.

On 2026-11-27, the CPU-only Python 3.13 and 3.14 environments each executed all 20 collected tests: 20 passed, 0 skipped, 0 failed. The RTX 2070 Python 3.13 environment executed all 19 explicit CUDA tests: 19 passed, 0 skipped, 0 failed. These are test execution counts, not source-line coverage. The CPU suite includes input/support validation, native/Paper stepwise and fill comparisons, partial fills, costs, cash limits, expiry, endowed inventory, stochastic paths, MC integration and numerical boundary acceptance/refusal. The historical scale measurements and independent Paper audit evidence above are unchanged.

## Performance attribution

Initial full-precision profiling (`profile-initial/`) diagnosed durable SQLite transaction cost in a low-fill path. In-memory Paper removed most filesystem cost but retained confirmation and serialization work. Fill-heavy profiling (`profile-oscillating/`) exposed repeated full order-event scans during turnover admission: 168 fills decoded 29,544 event reports and spent 0.979 seconds cumulatively in `_historical_turnover`.

The native research state machine removes transactional persistence, serialized checkpoints, replay verification and growing report rescans, while maintaining turnover incrementally. Its gain over Paper therefore combines research-state representation and removal of authority/audit work, not GPU hardware. The native CPU/CUDA comparison above holds the state-transition source fixed and is the hardware comparison. Neither comparison demonstrates equivalence for dynamic strategies, unsupported economic inputs or different market microstructure.

`profile-frozen/` supplies the dedicated same-six-decimal-input ablation: 8 accounts took 15.795188 seconds through file Paper, 1.061611 seconds through in-memory Paper, and 18.256163 seconds with MC FIFO/lifecycle reduction. Warm native CPU took 0.000206 seconds; warm CUDA took 0.002183 seconds, demonstrating the launch/transfer disadvantage for a tiny workload. The 8-worker MC run took 20.288854 seconds including process startup. These cold/warm measurements are separate from the final scale run; the short 8-path workload is not used to extrapolate million-account Paper performance. File/memory differences include skipped file-oriented evaluation exports, so this is persistence/audit-work stripping, not a pure SQLite-only ablation. No full-million-Paper timing is claimed.

## Reproduction

```sh
python -m pip install -e '.[dev,monte-carlo-native,monte-carlo-cuda]'
# CPU/Paper regressions (no CUDA dependency)
python -m pytest tests/research/test_grid_batch_simulation.py tests/research/test_grid_batch_numeric_boundaries.py
# Hardware acceptance; fails when CuPy or a CUDA device is unavailable
python -m pytest tests/research/cuda_grid_batch.py
python tools/research/profile_grid_batch.py --paths 8 --batch-backends --output <new-ablation-output>
python tools/research/run_grid_batch.py --sizes 100 1000 10000 100000 1000000 --output <new-study-output>
```

Large terminal arrays, profiler binaries and temporary SQLite databases are not versioned. Final selected price paths, per-step/per-fill exports, manifests and evaluation reports are retained; the CLI recreates the temporary/full Paper databases. The support contract and exclusions are in `docs/research/mc-grid-gpu-01.md`. No live account, trading executor, authoritative ledger implementation or Azure deployment is changed.
