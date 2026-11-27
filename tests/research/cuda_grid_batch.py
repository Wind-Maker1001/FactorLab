"""Real-device parity: pytest tests/research/cuda_grid_batch.py."""
import pytest

from tests.research.test_grid_batch_simulation import (
    config,
    test_paper_stepwise_parity,
    test_partial_zero_cost_and_cash_limits,
    test_expiry_and_endowed_inventory,
    test_volatile_random_step_and_fill_parity,
    test_existing_mc_runner_integration,
    test_collect_fills_preserves_static_account_and_events,
)
from tests.research.test_grid_batch_numeric_boundaries import (
    test_exact_touch_and_minimum_notional,
    test_reverse_fragments_accumulate_before_minimum_order,
    test_ambiguous_nav_boundary_is_explicitly_unsupported,
)
from tests.research.test_grid_batch_dynamic import (
    test_breakout_rebuild_and_old_order_fill,
    test_atr_real_highlow_and_deferred_cancellation,
    test_atr_same_point_complete_bar_cancels_remain_pending,
    test_volatility_real_subclass_fallback_and_extreme,
    test_future_ohlc_and_prices_do_not_change_past,
    test_continuous_history_order_capacity,
    test_collect_fills_preserves_dynamic_account_and_events,
)
from tests.research.test_grid_batch_funding import (
    test_funding_own_mark_and_prefill_inventory,
    test_decimal_half_even_boundaries,
    test_multiple_settlements_in_one_interval_and_history_not_charged,
    test_funding_drawdown_precedes_current_fills,
    test_negative_available_is_explicitly_unsupported,
    test_funding_sensitive_gate_causal_history_and_unblock,
    test_future_funding_does_not_change_past_decisions,
    test_funding_sensitive_position_exits,
)


@pytest.fixture
def backend():
    import cupy as cp

    assert cp.cuda.is_available(), 'CUDA device required for hardware acceptance'
    return 'cuda'
