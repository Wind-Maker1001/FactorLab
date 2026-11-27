"""Real-device parity: pytest tests/research/cuda_grid_batch.py."""
import pytest

from tests.research.test_grid_batch_simulation import (
    config,
    test_paper_stepwise_parity,
    test_partial_zero_cost_and_cash_limits,
    test_expiry_and_endowed_inventory,
    test_volatile_random_step_and_fill_parity,
    test_existing_mc_runner_integration,
)
from tests.research.test_grid_batch_numeric_boundaries import (
    test_exact_touch_and_minimum_notional,
    test_reverse_fragments_accumulate_before_minimum_order,
    test_ambiguous_nav_boundary_is_explicitly_unsupported,
)


@pytest.fixture
def backend():
    import cupy as cp

    assert cp.cuda.is_available(), 'CUDA device required for hardware acceptance'
    return 'cuda'
