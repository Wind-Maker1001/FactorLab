"""Offline research execution adapter for the shared strategy decision protocol."""
from decimal import Decimal as D
from dataclasses import asdict
from pathlib import Path

from factorlab.dataset.identity import file_hash
from factorlab.experiments.tracker import ExperimentTracker, _id
from factorlab.strategies.decision_runtime import DecisionJournal, SignalAdapter, ControllerExecution, OrderStrategyRuntime
from factorlab.trading.contracts import OrderIntent, OrderType, Side
from factorlab.trading.target import target_quantity
from factorlab.trading.realtime import identity
from factorlab.trading.storage import dumps


class ResearchTargetRuntime:
    def __init__(self, journal, *, step_size=D('.01')):
        self.journal, self.controller = journal, journal.controller
        self.strategy = SignalAdapter(journal)
        self.execution = ControllerExecution(self.controller, deployment_id=journal.deployment_id)
        self.bars, self.pending, self.step_size = [], None, step_size

    def closed_bar(self, bar):
        if (bar.symbol != self.journal.symbol or bar.interval_seconds != self.journal.interval_seconds
                or bar.closed_at > self.controller.clock() or not bar.complete):
            raise ValueError('confirmed deployment bar required')
        if self.bars and bar.opened_at != self.bars[-1].closed_at:
            raise ValueError('consecutive bars required')
        self.bars.append(bar)
        del self.bars[:-self.strategy.history_limit]
        self.pending = self.strategy.signal(self.bars, self.controller.clock())

    def quote(self, quote):
        self.controller.quote(quote)
        s = self.pending
        if s is None or s.target_weight is None or quote.observed_at <= s.input_watermark:
            return
        from datetime import timedelta
        if quote.observed_at >= s.input_watermark + timedelta(seconds=self.journal.interval_seconds):
            self.pending = None
            return
        from factorlab.strategies.grid_execution import project_paper_account
        from factorlab.strategies.protocol import Decision
        c = self.controller
        account = project_paper_account(c, self.journal.symbol)
        quantity = target_quantity(equity=account.equity, weight=s.target_weight, price=quote.price,
            budget_price=quote.price, fee_rate=c.broker.state.fee_rate, current=account.inventory,
            available=account.available, step=self.step_size) - account.inventory
        self.pending = None
        if quantity:
            side = Side.BUY if quantity > 0 else Side.SELL
            intent = OrderIntent(c.broker.state.market, self.journal.symbol, side, OrderType.LIMIT,
                abs(quantity), quote.price, self.journal.deployment_id + '-' + s.signal_id[:16],
                position_side='BOTH', reduce_only=side is Side.SELL)
            return self.execution.dispatch(Decision(orders=(intent,)))


def run_protocol_research(strategy, controller, events, *, symbol, interval_seconds,
                          deployment_id, store, experiment_id, run_id, step_size=D('.01'),
                          source_kind='SYNTHETIC'):
    """Consume explicit causal events using existing PaperBroker execution.

    The caller supplies quotes/fills (no OHLC liquidity inference). All decisions
    use the same loaded strategy as Demo. The existing immutable tracker binds
    configuration, source identity, state journal and account results.
    """
    _id(experiment_id)
    _id(run_id)
    if source_kind not in {'SYNTHETIC', 'REAL_CANDIDATE'}:
        raise ValueError('research source_kind must be SYNTHETIC or REAL_CANDIDATE')
    journal = DecisionJournal(strategy, controller, deployment_id=deployment_id, symbol=symbol,
        interval_seconds=interval_seconds, data_types=(['closed_bar'] if strategy.spec.decision_kind == 'target'
            else ['closed_bar', 'quote', 'order_event']))
    runner = (ResearchTargetRuntime(journal, step_size=step_size) if strategy.spec.decision_kind == 'target'
              else OrderStrategyRuntime(journal))
    tracker = ExperimentTracker(store)
    definition = dict(model=strategy.spec.strategy_id, parameters=strategy.binding, factors=[],
        dataset={'source_kind': source_kind, 'events': 'explicit causal observations and fills'},
        label={'name': 'protocol_strategy'}, windows={'train': None, 'validation': None, 'test': None},
        walk_forward={'mode': 'fixed_no_fit'}, costs={'fee_rate': str(controller.broker.state.fee_rate)})
    tracker.create_experiment(experiment_id, definition)
    code = {'commit': None, 'tree': None, 'strategy_sha256': strategy.code_sha256}
    tracker.append_run(experiment_id, run_id + '-start', code=code, environment={},
        command=['run_protocol_research'], status='RUNNING', qualification=source_kind,
        validation={'binding': strategy.binding})
    status = 'FAILED'
    try:
        for kind, value in events:
            if isinstance(runner, OrderStrategyRuntime):
                runner.recover()
            if kind == 'closed_bar':
                runner.closed_bar(value)
            elif kind == 'quote':
                controller.quote(value)
                runner.quote(value)
            elif kind == 'fill':
                controller.fill(**value)
            else:
                raise ValueError('unsupported research event')
        status = 'SUCCEEDED'
    except Exception:
        status = 'FAILED'
        raise
    finally:
        evidence = tracker.root / 'evidence'
        evidence.mkdir(exist_ok=True)
        path = evidence / (run_id + '.json')
        # An immutable export remains inspectable even if the working account
        # continues to receive events after the research run.
        with path.open('x', encoding='utf-8') as stream:
            stream.write(dumps(dict(binding=strategy.binding, account=asdict(controller.account()),
                journal=list(controller.store.db.execute('SELECT id,payload FROM events ORDER BY rowid')))))
        tracker.append_run(experiment_id, run_id, code=code, environment={}, command=['run_protocol_research'],
            status=status, qualification=source_kind,
            validation={'binding': strategy.binding, 'state_version': strategy.spec.state_version,
                        'final_state_identity': identity(journal._latest()),
                        'sample_hash': identity(list(controller.store.db.execute('SELECT id,payload FROM events ORDER BY rowid')))},
            artifacts={'journal': {'path': str(path), 'sha256': file_hash(path)}},
            portfolio_metrics={'orders': len(controller.broker.state.orders)})
    return runner
