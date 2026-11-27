# MC-COLAB-03 — research dispatch and batch evidence

Protocol: [MC-COLAB-03](../../research/mc-colab-03.md).

## Current evidence

Implementation and integration are in progress. Portable selector tests passed
40 cases; portable controller tests passed 37 cases; task and batch tests passed
50 cases before admission normalization integration. These use substituted
resources/controllers and are not real GPU/cloud execution evidence.

Real installed-wheel CPU/CUDA batch: NOT_RUN.
Windows → WSL → Colab multi-experiment batch: NOT_RUN.

TODO: Complete integrated tests and real installed-wheel CPU/CUDA/cloud runs,
then replace pending statuses with measured evidence in this report.

## Placement evidence

Prior [MC-COLAB-02](../mc-colab-02/REPORT.md) cold-process 100000-path,
252-step, one-asset, batch-1024 GBM medians favored CPU (2.467 s vs 3.596 s GPU);
Bootstrap likewise favored CPU (2.297 s vs 3.156 s). The million-path GBM
steady cell (batch 4096) measured CPU 17.041418 s vs GPU 0.455560 s, with one
independent seed and two warm repeats. These are workload-scoped measurements,
not universal cold crossover or cloud lifecycle estimates.

## Cloud accounting

This acceptance has a 10 CU total authorized envelope, shared across allocations,
failures and retries. No MC-COLAB-03 cloud allocation has occurred yet.
Record actual hardware, initial/final account balance, observed active rate,
deployment/execution/download/release times, retained results and server-side
release confirmation. Rounded balance differences and rate-times-wall estimates
are distinct evidence, not exact per-experiment billing.
