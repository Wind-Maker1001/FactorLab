"""Transaction-driven Paper oracle for extended batch-grid research."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from time import perf_counter

import numpy as np

from factorlab.strategies.grid_execution import GridExecution
from factorlab.trading.contracts import FundingEvent, Market, OrderStatus, Position, Side, ZERO
from factorlab.trading.control import RiskLimits, RiskRejected, TradingController
from factorlab.trading.execution import execution_candidate
from factorlab.trading.paper import Instrument, InsufficientCollateral, PaperBroker
from factorlab.trading.storage import PaperStore


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SYMBOL = 'BINANCE:USDM:NVDAUSDT'
_ACCOUNT = 'batch-paper'
_DONE = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}


def _decimal(value):
    return Decimal(str(value))


def _utc(microseconds):
    return _EPOCH + timedelta(microseconds=int(microseconds))


def canonical_order_events(events):
    """Compare command-level receipts without transient intra-command state.

    Paper publishes a staged command atomically, while the kernel audits its
    internal transitions. State/reason are compared at risk transitions and in
    every observation trace; non-risk receipts retain their economic fields.
    """
    rows = np.asarray(events, dtype=float).copy().reshape(-1, 12)
    nonrisk = rows[:, 1] != 7
    rows[nonrisk, 9:11] = 0
    if len(rows):
        rows = rows[np.lexsort(tuple(rows[:, i] for i in reversed(range(12))))]
    return rows


def run_paper_reference(values, config, prepared, trace):
    """Replay prepared observations through the actual strategy and Paper authority.

    Funding uses raw rates and settlement marks, independently of packed native
    coefficients. Working orders are snapshotted before funding and observation;
    cancellations first requested by complete_bar remain pending until next time.
    """
    from . import BatchGridResult, COLUMNS, REASONS, STATES, UnsupportedGridSimulation
    from factorlab.experiments.grid_funding_adaptive import FundingSensitiveGrid, VolatilityAdaptiveGrid

    started = perf_counter()
    terminal = np.empty((len(values), len(COLUMNS)))
    traces = np.empty((*values.shape, len(COLUMNS))) if trace else None
    all_fills, all_events, raw_events = ([] if trace else None for _ in range(3))
    for path, prices in enumerate(values):
        now = [_utc(prepared.observation_us[0])]
        broker = PaperBroker(Market.BINANCE_USDM, _ACCOUNT,
            (Instrument(_SYMBOL, 'USDT'),), {'USDT': config.capital}, fee_rate=config.fee_rate)
        inventory = _decimal(config.grid.initial_inventory) * config.rules.quantity_unit

        def initialize(state):
            if inventory:
                state.positions[_SYMBOL] = Position(_SYMBOL, inventory,
                    _decimal(config.grid.initial_price), _decimal(config.grid.initial_price), 'USDT')

        funding_history = [(_utc(t), _decimal(rate), _decimal(mark), f'funding-{i}')
            for i, (t, rate, mark) in enumerate(zip(prepared.funding_us,
                prepared.funding_rates_decimal[path], prepared.funding_marks_decimal[path]))]
        factory, options = GridExecution, {}
        if config.funding_policy is not None:
            factory, options = FundingSensitiveGrid, dict(funding_policy=config.funding_policy,
                funding_events=[(t, mark, rate, identity) for t, rate, mark, identity in funding_history])
        elif config.volatility_policy is not None:
            factory, options = VolatilityAdaptiveGrid, dict(vol_policy=config.volatility_policy,
                warmup_closes=list(prepared.warmup[path]))
        store = PaperStore(':memory:', continuous_capacity=True)
        try:
            controller = TradingController(broker, store, clock=lambda: now[0],
                initialize_state=initialize,
                limits=RiskLimits(config.capital, config.capital, config.capital, 1000))
            grid = factory(replace(config.grid, initial_cash=float(config.capital)), config.rules,
                deployment_id='research', account_id=_ACCOUNT, market=Market.BINANCE_USDM,
                instrument_id=_SYMBOL, initial_equity=config.capital, fee_rate=config.fee_rate,
                continuous_capacity=True, **options)
            fees = slip = funding_total = ZERO
            peak = config.capital
            maximum_drawdown = 0.
            count = 0
            event_index = int(np.searchsorted(prepared.funding_us, prepared.observation_us[0], side='right'))
            fills, events, receipts = [], [], []
            seen_orders, seen_cancels = set(), set()
            order_details = {}
            prior_rebuilds = 0
            prior_risk = (grid.state, grid.reason)

            def emit(step, kind, key=None, *, quantity=0, price=0, value=0, extra=0):
                if not trace:
                    return
                order = (grid.orders.get(key) or order_details[key][0]) if key is not None else None
                identity = int(key.rsplit('-', 1)[1]) if key is not None else 0
                side = int(order.intent.side == Side.SELL) if order is not None else 0
                level = order_details[key][1] if key is not None else -1
                events.append([step, kind, identity, side, level, float(quantity), float(price),
                    float(value), 0., STATES.index(grid.state), REASONS.index(grid.reason), float(extra)])
                receipts.append(dict(step=step, kind=kind, order_id=key,
                    state=grid.state, reason=grid.reason, quantity=str(quantity), price=str(price),
                    value=str(value), extra=str(extra)))

            def capture(step):
                nonlocal seen_cancels, prior_rebuilds, prior_risk
                if grid.rebuilds != prior_rebuilds:
                    emit(step, 5, price=grid.center,
                        value=(grid.prices[-1] - grid.prices[0]) / (len(grid.prices) - 1), extra=grid.rebuilds)
                    prior_rebuilds = grid.rebuilds
                for key in grid.orders:
                    if key not in seen_orders:
                        order = grid.orders[key]
                        order_details[key] = (order, grid.levels[key])
                        emit(step, 1, key, quantity=order.intent.quantity,
                            price=grid.targets[key], value=order.intent.limit_price or 0,
                            extra=int(order.intent.order_type.value == 'MARKET'))
                        seen_orders.add(key)
                for key in sorted(grid.cancellations - seen_cancels):
                    order = grid.orders[key]
                    emit(step, 2, key, quantity=order.intent.quantity - order.cumulative_filled_quantity,
                        price=order.intent.limit_price or 0)
                seen_cancels = set(grid.cancellations)
                current = (grid.state, grid.reason)
                if current != prior_risk and grid.reason is not None:
                    emit(step, 7)
                prior_risk = current

            for step, price in enumerate(prices):
                observation_time = _utc(prepared.observation_us[step])
                now[0] = observation_time
                prior = [(key, record.snapshot) for key, record in broker.state.orders.items()
                    if record.observed_at < now[0] and record.snapshot.status not in _DONE]
                day_funding = day_fee = day_slip = ZERO
                while event_index < len(funding_history) and funding_history[event_index][0] <= observation_time:
                    event_time, rate, mark, identity = funding_history[event_index]
                    position = broker.state.positions.get(_SYMBOL)
                    quantity = position.quantity if position else ZERO
                    amount = (-quantity * mark * rate).quantize(Decimal('.00000001'), rounding=ROUND_HALF_EVEN)
                    # GridAccount forbids negative available/equity; reject this
                    # unsupported economic state before entering its transaction.
                    account = grid.account
                    if account is not None and (account.available + amount < 0 or account.equity + amount < 0):
                        raise UnsupportedGridSimulation('funding produces negative available or equity')
                    now[0] = event_time
                    event = FundingEvent(_ACCOUNT, identity, _SYMBOL, 'USDT', 'FUNDING_FEE',
                        amount, event_time, 'RESEARCH_SIMULATION', identity,
                        dict(rate=str(rate), mark=str(mark), quantity=str(quantity)))
                    grid.apply_funding_paper(controller, event, delivery=grid.delivery + 1)
                    day_funding += amount
                    funding_total += amount
                    emit(step, 6, quantity=quantity, price=mark, value=amount, extra=rate)
                    if trace:
                        events[-1][2] = event_index + 1
                        receipts[-1]['event_id'] = identity
                    capture(step)
                    peak = max(peak, grid.account.equity)
                    maximum_drawdown = max(maximum_drawdown, float(1 - grid.account.equity / peak))
                    event_index += 1
                now[0] = observation_time
                high, low = prepared.bars[path, step]
                bar_open = observation_time - timedelta(hours=1, microseconds=-1)
                observation = dict(id=f'bar-{step}', time=bar_open.replace(tzinfo=None).isoformat(),
                    price=float(price), close=float(price), high=float(high), low=float(low),
                    volume=1, completed_bar=True)
                grid.observe_paper(controller, observation, delivery=grid.delivery + 1, observed_at=now[0])
                capture(step)
                budget = config.capacity
                for key, order in prior:
                    candidate = execution_candidate(order, ask=_decimal(price), bid=_decimal(price),
                        budget=budget, slippage=config.slippage, step=config.rules.step_size)
                    if candidate is None:
                        continue
                    quantity, execution_price = candidate
                    try:
                        event = grid.fill_paper(controller, key, quantity, execution_price, delivery=grid.delivery + 1)
                    except InsufficientCollateral:
                        continue
                    if event is None:
                        continue
                    quantity = event.cumulative_filled_quantity - order.cumulative_filled_quantity
                    fee = event.cumulative_fee - order.cumulative_fee
                    cost = quantity * abs(execution_price - _decimal(price))
                    day_fee += fee
                    day_slip += cost
                    budget -= quantity
                    count += 1
                    if trace:
                        fills.append([step, int(key.rsplit('-', 1)[1]), int(order.intent.side == Side.SELL),
                            order_details[key][1], float(quantity), float(execution_price), float(fee), float(cost),
                            int(order.intent.order_type.value == 'MARKET')])
                    capture(step)
                for key in sorted(grid.cancellations):
                    order = grid.orders[key]
                    event = grid.cancel_paper(controller, key, delivery=grid.delivery + 1)
                    if event is not None:
                        emit(step, 3, key, quantity=order.intent.quantity - order.cumulative_filled_quantity,
                            price=order.intent.limit_price or 0)
                    capture(step)
                if grid.pending_bar is not None:
                    grid.complete_bar_paper(controller, delivery=grid.delivery + 1)
                    capture(step)
                for key, order in list(grid.orders.items()):
                    if order.status == OrderStatus.NEW:
                        try:
                            grid.submit_paper(controller, key, delivery=grid.delivery + 1)
                        except RiskRejected:
                            capture(step)
                            if grid.state == 'PAUSED':
                                break
                            continue
                        emit(step, 4, key, quantity=order.intent.quantity, price=order.intent.limit_price or 0)
                        if trace:
                            events[-1][8] = float(broker.state.orders[key].reservation_price)
                        capture(step)
                fees += day_fee
                slip += day_slip
                account = grid.account
                position = broker.state.positions.get(_SYMBOL)
                unrealized = position.quantity * (position.mark_price - position.entry_price) if position else ZERO
                peak = max(peak, account.equity)
                drawdown = float(1 - account.equity / peak)
                maximum_drawdown = max(maximum_drawdown, drawdown)
                row = dict(cash=broker.state.cash['USDT'], inventory=account.inventory,
                    available=account.available, committed=account.committed,
                    realized_gross=account.equity - config.capital - unrealized + fees - funding_total,
                    unrealized=unrealized, equity=account.equity, fee=day_fee, slippage=day_slip,
                    drawdown=drawdown, fills=count, state=STATES.index(grid.state), reason=REASONS.index(grid.reason),
                    total_fees=fees, total_slippage=slip, maximum_drawdown=maximum_drawdown,
                    funding_pnl=day_funding, total_funding_pnl=funding_total, rebuilds=grid.rebuilds,
                    center=grid.center, spacing=(grid.prices[-1] - grid.prices[0]) / (len(grid.prices) - 1),
                    volatility=getattr(grid, 'current_vol_1h', 0.), buy_blocked=getattr(grid, 'buy_blocked', False),
                    pending_rebuild=grid.pending_rebuild is not None, last_rebuild=grid.last_rebuild,
                    funding_bps_h=getattr(grid, 'last_funding_bps_h', 0.))
                data = [float(row[column]) for column in COLUMNS]
                if trace:
                    traces[path, step] = data
            terminal[path] = data
            if trace:
                all_fills.append(np.asarray(fills, dtype=float).reshape(-1, 9))
                all_events.append(np.asarray(events, dtype=float).reshape(-1, 12))
                raw_events.append(receipts)
        finally:
            store.close()
    return BatchGridResult(terminal, traces, dict(backend='paper', evidence='AUTHORITATIVE_PAPER_IN_MEMORY',
        seconds=perf_counter() - started, quote_decimals=6, paper_events=raw_events), all_fills, all_events)
