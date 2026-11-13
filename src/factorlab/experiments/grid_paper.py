"""Close-quote research driven exclusively by authoritative Paper transactions."""
from dataclasses import asdict, replace
from datetime import timezone
from decimal import Decimal
import json
from pathlib import Path

import pandas as pd

from factorlab.dataset.identity import json_hash
from factorlab._identity import source_context_root
from factorlab.experiments.provenance import capture_provenance
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation, compare_results
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, project_paper_account
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side, OrderStatus, Position, ZERO, decimal_value
from factorlab.trading.control import RiskLimits, TradingController, RiskRejected
from factorlab.trading.execution import execution_candidate
from factorlab.trading.paper import Instrument, PaperBroker, Quote, InsufficientCollateral
from factorlab.trading.storage import PaperStore


SCHEMA_VERSION = 1
SYMBOL = 'GRIDUSD'
EVIDENCE = ['SOFTWARE_ONLY', 'SYNTHETIC_QUOTE_EXECUTION', 'NO LIVE MONEY',
            'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']


def D(value):
    return Decimal(str(value))


def deterministic_bars():
    """Small fixed daily path with oscillation, a gap and ATR warmup."""
    prices = [100, 90, 80, 90, 100, 110, 120, 100, 85, 95, 105, 115]
    return [dict(id=f'bar-{i}', time=str(day.date()), price=price, volume=20,
                 completed_bar=True, high=price + 2, low=price - 2, close=price)
            for i, (day, price) in enumerate(zip(pd.bdate_range('2026-01-01', periods=len(prices)), prices))]


def _write(path, value):
    path.write_text(json.dumps(value, default=str, indent=2, sort_keys=True,
                               allow_nan=False) + '\n', encoding='utf-8')


def _bars(bars, start, end, interval='1d'):
    frame = pd.DataFrame(bars).copy()
    required = {'id', 'time', 'price', 'volume', 'high', 'low', 'close', 'completed_bar'}
    if not required.issubset(frame):
        raise ValueError('daily completed OHLC bars required')
    if interval not in {'1d', '1h'}:
        raise ValueError('interval must be 1d or 1h')
    dates = pd.to_datetime(frame.time)
    if interval == '1h':
        if dates.dt.tz is None or str(dates.dt.tz) != 'UTC':
            raise ValueError('hourly bars require UTC timestamps')
        aligned = dates.eq(dates.dt.floor('h')).all()
    else:
        aligned = dates.dt.tz is None and dates.eq(dates.dt.normalize()).all()
    if (dates.isna().any() or dates.duplicated().any()
            or not dates.is_monotonic_increasing or not aligned):
        raise ValueError('bars require unique ordered aligned timestamps')
    if frame.id.duplicated().any() or not frame.completed_bar.eq(True).all():
        raise ValueError('unique completed bars required')
    if start is not None:
        bound = pd.Timestamp(start)
        if interval == '1h':
            bound = bound.tz_localize('UTC') if bound.tzinfo is None else bound.tz_convert('UTC')
        frame = frame.loc[dates >= bound]
    if end is not None:
        bound = pd.Timestamp(end)
        if interval == '1h':
            bound = bound.tz_localize('UTC') if bound.tzinfo is None else bound.tz_convert('UTC')
        frame = frame.loc[dates <= bound]
    if frame.empty:
        raise ValueError('empty research window')
    return frame.to_dict('records')


def run_experiment(output, *, bars, config, rules, capital, fee_rate, slippage,
                   start=None, end=None, participation=1, fee_estimate=None, no_trade=False,
                   market=Market.BINANCE_EQUITY, instrument_id=SYMBOL, data_identity=None,
                   interval='1d', buy_hold=False):
    """Run one fresh dedicated equity Paper account and save auditable receipts.

    Capital is total starting equity; declared inventory is an endowed position,
    not a fictitious initial fill. Slippage and fee_rate are decimal fractions.
    fee_estimate is solely the Grid reservation schedule, including any minimum
    fee assumption; Paper always charges proportional fee_rate, never minima.
    Orders consume a shared bar-volume budget at close, only on later bars.
    """
    bars = _bars(bars, start, end, interval)
    capital, fee_rate, slippage, participation = map(D, (capital, fee_rate, slippage, participation))
    decimal_value(capital, 'capital', positive=True)
    decimal_value(fee_rate, 'fee_rate')
    decimal_value(slippage, 'slippage')
    if fee_rate >= 1 or slippage >= 1 or not ZERO <= participation <= 1:
        raise ValueError('fee_rate/slippage must be below one and participation in [0, 1]')
    inventory = D(config.initial_inventory) * rules.quantity_unit
    cash = capital - inventory * D(config.initial_price) if market == Market.BINANCE_EQUITY else capital
    currency = 'USD' if market == Market.BINANCE_EQUITY else 'USDT'
    if cash < 0:
        raise ValueError('capital does not cover endowed inventory')
    config = replace(config, initial_cash=float(cash))
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    def close_time(bar):
        return (pd.Timestamp(bar['time']) + pd.Timedelta(interval)
                - pd.Timedelta(microseconds=1)).to_pydatetime().replace(tzinfo=timezone.utc)

    now = [close_time(bars[0])]
    broker = PaperBroker(market, 'research-grid', (Instrument(instrument_id, currency),),
                        {currency: cash}, fee_rate=fee_rate)

    def initialize(state):
        if inventory:
            state.positions[instrument_id] = Position(instrument_id, inventory, D(config.initial_price),
                                                      D(config.initial_price), currency)

    store = PaperStore(output / 'paper.sqlite')
    try:
        controller = TradingController(broker, store, clock=lambda: now[0], initialize_state=initialize,
            limits=RiskLimits(capital, capital, capital, 1000))
        grid = GridExecution(config, rules, deployment_id='research', account_id='research-grid',
            market=market, instrument_id=instrument_id, initial_equity=capital,
            fee_rate=fee_rate, fees=fee_estimate)
        rows, trades, risk = [], [], []
        order_details = {}
        last_risk = None
        estimated_total = ZERO
        peak = capital
        for bar in bars:
            now[0] = close_time(bar)
            close = D(bar['close'])
            prior = [(key, record.snapshot) for key, record in broker.state.orders.items()
                     if record.observed_at < now[0] and record.snapshot.status not in
                     {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}]
            if no_trade or buy_hold:
                controller.quote(Quote(instrument_id, close, now[0]))
            else:
                observation = dict(bar)
                if interval == '1h':
                    # Grid's observation contract uses timezone-free UTC coordinates.
                    observation['time'] = pd.Timestamp(bar['time']).tz_convert('UTC').tz_localize(None).isoformat()
                grid.observe_paper(controller, observation, delivery=grid.delivery + 1, observed_at=now[0])
            budget = D(bar['volume']) * participation
            day_fee = day_slippage = day_notional = ZERO
            for key, order in prior:
                candidate = execution_candidate(order, ask=close, bid=close, budget=budget,
                                                slippage=slippage, step=rules.step_size)
                if candidate is None:
                    continue
                quantity, price = candidate
                try:
                    event = (controller.fill(key, quantity, price) if buy_hold else
                             grid.fill_paper(controller, key, quantity, price, delivery=grid.delivery + 1))
                except InsufficientCollateral:
                    risk.append(dict(time=bar['time'], kind='INSUFFICIENT_COLLATERAL', order_id=key))
                    continue
                if event is None:
                    risk.append(dict(time=bar['time'], kind='FILL_ADMISSION_DENIED', order_id=key))
                    continue
                quantity = event.cumulative_filled_quantity - order.cumulative_filled_quantity
                fee = event.cumulative_fee - order.cumulative_fee
                slip = quantity * abs(price - close)
                estimate = fee + slip if buy_hold else grid.estimated_cost(order.intent.side, price, quantity)
                estimated_total += estimate
                notional = quantity * price
                trades.append(dict(execution_date=bar['time'], event_time=event.event_time.isoformat(),
                    order_id=key, event_id=event.event_id,
                    cumulative_filled_quantity=str(event.cumulative_filled_quantity),
                    cumulative_fee=str(event.cumulative_fee),
                    side=order.intent.side.value, grid_level=order_details[key]['grid_level'],
                    executed_shares=float(quantity), execution_price=float(price),
                    trade_notional=float(notional), fee=float(fee), slippage=float(slip),
                    transaction_cost=float(fee + slip), estimated_budget_cost=float(estimate)))
                day_fee += fee
                day_slippage += slip
                day_notional += notional
                budget -= quantity
            if buy_hold and not broker.state.orders:
                limit = (close * (1 + slippage) // rules.tick_size) * rules.tick_size
                quantity = (capital / (limit * (1 + fee_rate)) // rules.step_size) * rules.step_size
                intent = OrderIntent(market, instrument_id, Side.BUY, OrderType.LIMIT,
                                     quantity, limit, 'buy-hold')
                order_details['buy-hold'] = dict(deployment_id='research', grid_level=None,
                                                 target_price=str(limit))
                controller.submit(intent)
            if not no_trade and not buy_hold:
                for key in sorted(grid.cancellations):
                    grid.cancel_paper(controller, key, delivery=grid.delivery + 1)
                if grid.pending_bar is not None:
                    grid.complete_bar_paper(controller, delivery=grid.delivery + 1)
                for key, order in list(grid.orders.items()):
                    if order.status == OrderStatus.NEW:
                        order_details[key] = dict(deployment_id=grid.deployment_id,
                            grid_level=grid.levels[key], target_price=str(grid.targets[key]))
                        try:
                            grid.submit_paper(controller, key, delivery=grid.delivery + 1)
                        except RiskRejected as error:
                            risk.append(dict(time=bar['time'], kind='SUBMISSION_REJECTED',
                                             order_id=key, reason=error.decision.reason))
                current_risk = (grid.state, grid.reason)
                if (grid.reason is not None and grid.state in {'PAUSED', 'EXITING'}
                        and current_risk != last_risk):
                    risk.append(dict(time=bar['time'], kind='RISK_TRIGGER', state=grid.state, reason=grid.reason))
                last_risk = current_risk
            account = project_paper_account(controller, instrument_id)
            peak = max(peak, account.equity)
            position = broker.state.positions.get(instrument_id)
            unrealized = (position.quantity * (position.mark_price - position.entry_price)
                          if position else ZERO)
            fees_to_date = sum((record.snapshot.cumulative_fee for record in broker.state.orders.values()), ZERO)
            rows.append(dict(date=bar['time'], close=float(close),
                unrealized=float(unrealized),
                realized_gross=float(account.equity - capital - unrealized + fees_to_date),
                net_exposure=float(account.inventory * close),
                equity=float(account.equity), cash=float(broker.state.cash[currency]),
                inventory=float(account.inventory), committed=float(account.committed),
                available=float(account.available), drawdown=float(1-account.equity/peak),
                transaction_cost=float(day_fee+day_slippage), fee=float(day_fee), slippage=float(day_slippage),
                turnover=float(day_notional/capital), holdings=int(account.inventory != 0)))
        daily = pd.DataFrame(rows)
        benchmark = 1 + float(inventory / capital) * (
            pd.Series([float(D(b['close'])) for b in bars]) - config.initial_price)
        daily['strategy_nav'] = daily.equity / float(capital)
        daily['benchmark_nav'] = benchmark.astype(float)
        for prefix in ('strategy', 'benchmark'):
            daily[prefix+'_return'] = daily[prefix+'_nav'].pct_change().fillna(daily[prefix+'_nav'].iloc[0]-1)
        daily.date = pd.to_datetime(daily.date)
        ledger = pd.DataFrame(trades, columns=['execution_date', 'event_time', 'order_id', 'event_id',
            'cumulative_filled_quantity', 'cumulative_fee', 'side', 'grid_level',
            'executed_shares', 'execution_price', 'trade_notional', 'fee', 'slippage',
            'transaction_cost', 'estimated_budget_cost'])
        for column in ('trade_notional', 'transaction_cost'):
            ledger[column] = ledger[column].astype(float)
        evaluation_daily, evaluation_ledger = daily.copy(), ledger.copy()
        if interval == '1h':
            evaluation_daily.date = evaluation_daily.date.dt.tz_convert('UTC').dt.tz_localize(None).dt.normalize()
            sums = {'transaction_cost', 'fee', 'slippage', 'turnover'}
            evaluation_daily = evaluation_daily.groupby('date', as_index=False).agg(
                {c: 'sum' if c in sums else 'last' for c in daily.columns if c != 'date'})
            for prefix in ('strategy', 'benchmark'):
                nav = evaluation_daily[prefix + '_nav']
                evaluation_daily[prefix + '_return'] = nav.pct_change().fillna(nav.iloc[0] - 1)
            evaluation_ledger.execution_date = pd.to_datetime(
                evaluation_ledger.execution_date, utc=True).dt.tz_localize(None).dt.normalize()
        context = EvaluationContext(dataset_id=json_hash(bars), universe_id=instrument_id,
            label='not_used', label_id='not_used', benchmark_id='endowment-no-trade',
            calendar=tuple(evaluation_daily.date.dt.strftime('%Y-%m-%d')), holding_days=1,
            periods_per_year=365 if interval == '1h' else 252,
            source_kind='REAL_CANDIDATE' if isinstance(data_identity, dict) else 'SYNTHETIC')
        result = ResearchEvaluation(context).portfolio('no-trade' if no_trade else 'buy-hold' if buy_hold else config.mode,
                                                     evaluation_daily, trades=evaluation_ledger)
        final = dict(schema_version=SCHEMA_VERSION, evidence=EVIDENCE,
            execution=dict(interval=interval, buy_hold=buy_hold,
                           price='later-bar close quote', participation=str(participation),
                           slippage=str(slippage), actual_fee_model='proportional', fee_rate=str(fee_rate)),
            config=asdict(config), rules=asdict(rules), capital=str(capital),
            market=market.value, instrument_id=instrument_id, funding='NOT_SIMULATED',
            fee_budget=dict(rules=[asdict(rule) for rule in grid.fees.rules],
                minimum_fees_applied_by_broker=False, estimated_cost=str(estimated_total),
                actual_fees=str(sum((D(t['fee']) for t in trades), ZERO)),
                actual_cost=str(sum((D(t['transaction_cost']) for t in trades), ZERO)),
                estimate_minus_actual=str(estimated_total-sum((D(t['transaction_cost']) for t in trades), ZERO))),
            final=rows[-1], fills=len(trades),
            totals=dict(fees=float(daily.fee.sum()), slippage=float(daily.slippage.sum()),
                transaction_cost=float(daily.transaction_cost.sum()), turnover=float(daily.turnover.sum()),
                max_drawdown=float(daily.drawdown.max()), max_committed=float(daily.committed.max()),
                max_inventory=float(daily.inventory.max()), risk_triggers=len(risk)), risk=risk,
            orders={key: asdict(record.snapshot) | order_details[key]
                    for key, record in broker.state.orders.items()})
        daily.to_csv(output / 'daily.csv', index=False)
        ledger.to_csv(output / 'trades.csv', index=False)
        _write(output / 'evaluation.json', result.to_dict())
        _write(output / 'result.json', final)
        _write(output / 'manifest.json', dict(schema_version=SCHEMA_VERSION,
            dataset_id=json_hash(bars), data_identity=data_identity,
            window=dict(start=bars[0]['time'], end=bars[-1]['time'], bars=len(bars)),
            provenance=capture_provenance(source_context_root()), deterministic=True,
            config=asdict(config), rules=asdict(rules), capital=capital, fee_rate=fee_rate,
            slippage=slippage, participation=participation, no_trade=no_trade,
            interval=interval, buy_hold=buy_hold,
            market=market.value, instrument_id=instrument_id,
            fee_estimate=[asdict(rule) for rule in grid.fees.rules]))
        return result
    finally:
        store.db.close()


def run_comparison(output, *, bars, config, **kwargs):
    """Evaluate four arms with the identical window, endowment and Paper fees."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    bars = list(bars)
    results = [run_experiment(output / 'no-trade', bars=bars, config=config, no_trade=True, **kwargs)]
    for mode in ('arithmetic', 'geometric', 'atr'):
        results.append(run_experiment(output / mode, bars=bars, config=replace(config, mode=mode), **kwargs))
    compare_results(results).to_csv(output / 'comparison.csv', index=False)
    return results
