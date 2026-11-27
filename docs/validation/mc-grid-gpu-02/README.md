# MC-GRID-GPU-02 evidence

`scale-report.json` contains frozen configuration, synthetic market definition, per-scale timing/resource receipts, financial summaries and selected Paper audit results. Selected audit archives remain local and are excluded from version control. Bulk terminal arrays were removed after aggregation and acceptance to release storage; frozen generation parameters and reproduction commands are retained.

## Economic verification

Windows Python 3.14: 64 CPU/Paper tests passed. RTX2070, Python 3.13.9 and CuPy 14.2.0: 45 explicit real-device tests passed. Comparisons retain `rtol=1e-12, atol=5e-10`; order-event cardinality and economic fields are compared, not discarded. Debian Linux under WSL, Python 3.13.18: all 64 CPU/Paper tests passed in 25.37 seconds using an isolated environment and the pinned core/dev requirements.

The initial CUDA compilation exposed a CPU-only standard-header include; the shared source now includes that header only for native compilation. Position-exit tests also identified missing native audit receipts for exit completion and market-order proposal type; both were corrected before scale acceptance.

All four scales (1,000; 10,000; 100,000; 1,000,000 paths), each at 168 hourly increments, completed for static, ATR, C and B. Every batch's CPU/CUDA terminal output matched. Two outcome-independent selected paths per scale matched Paper stepwise accounts, fills and order receipts for every profile/backend.

## Million-path timing

Seconds, excluding cold calls and selected Paper audits:

| Profile | CPU kernel | CUDA kernel | CPU full call | CUDA full call |
|---|---:|---:|---:|---:|
| Static | 13.477 | 0.503 | 21.623 | 10.578 |
| ATR | 14.171 | 0.581 | 22.242 | 10.655 |
| C | 201.356 | 14.505 | 209.452 | 24.668 |
| B | 14.033 | 0.716 | 22.098 | 10.956 |

Shared generation took 88.577 seconds at the million-path tier. Generation-inclusive time attributes this same cost once to each profile/backend; those totals are not additive. Full-call time includes input validation, allocation and transfers. First-profile calls are recorded separately and explicitly do not establish an empty compilation cache. Other CPU research jobs overlapped part of this run; CPU and end-to-end timings are not exclusive-host measurements. GPU work was coordinated without competing research GPU jobs.

Million-tier sampled process RSS peaked at 2,494,824,448 bytes. NVML device-wide used memory peaked at 2,541,252,608 bytes, including other device allocations and prior allocator caches. Process VRAM is `null` because WDDM/driver did not provide it. These 50 ms sampled values are distinct from allocator allocation counts and are not continuous maxima.

## Financial pilot

Synthetic GBM uses eight genuine simulated substeps per hour to produce OHLC, with shared six-decimal quote freezing. Funding uses signed event rates and exogenous settlement marks; it is applied during state transitions, not as a terminal overlay. The full model, seed and policies are in the JSON receipt.

At one million paths, mean paired terminal NAV differences versus static were ATR −0.007245 USDT, C −0.000120 USDT and B +0.050683 USDT on 1,000 USDT initial capital. The B 5th–95th percentile paired difference was −1.929404 to +2.083620 USDT; ATR was −2.076667 to +2.193842 USDT. These are synthetic implementation pilots, not evidence of strategy superiority.

## Reproduction

```sh
python -m pytest tests/research/test_grid_batch_simulation.py tests/research/test_grid_batch_numeric_boundaries.py tests/research/test_grid_batch_inputs.py tests/research/test_grid_batch_dynamic.py tests/research/test_grid_batch_funding.py -q
python -m pytest tests/research/cuda_grid_batch.py -q
python tools/research/run_grid_dynamic_batch.py --backend both --paths 1000 10000 100000 1000000 --output <new-output-directory>
```

Install the project from the working checkout so the runner imports the matching source. GPU runs additionally require CuPy, a real CUDA device and `nvidia-ml-py`; resource sampling requires `psutil`. The CPU test workflow runs on Windows and Ubuntu without CUDA skips.
