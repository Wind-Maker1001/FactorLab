"""Event-driven, collateralized paper execution with one durable account authority."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import hashlib
import sqlite3

from factorlab.integrations.binance_equity.market import stream_rows
from factorlab.integrations.binance_equity_trading.protocol import Rules, Session, StockOrder, admit, fresh, Unavailable
from factorlab.integrations.binance_futures_trading.rules import RuleObservation
from .contracts import Market, OrderIntent, OrderType, Side, TERMINAL, SAFETY_LABELS, utc
from .control import RiskLimits, TradingController, RiskRejected
from .paper import Instrument, PaperBroker, Quote
from .storage import PaperStore, dumps
from .simulation import PaperSimulationPolicy


class PersistenceConflict(RuntimeError):
    pass


def timestamp(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    utc(result)
    return result


def identity(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def checkpoint(state):
    value = deepcopy(state)
    value.runtime.pop('checkpoint', None)
    return identity(value)


class RealtimeAccount:
    """Single writer. PaperStore snapshot + event journal are the paper authority.

    Quote liquidity is an explicitly hypothetical executable budget, not exchange
    executions. Orders can fill only on a later source-time event. FIFO shares
    one participation budget across orders; no OHLC inference or hidden queue.
    """
    def __init__(self, path, market, *, clock=lambda: datetime.now(timezone.utc),
                 quantity=D('0.1'), threshold=D('0.001'), participation=D('0.1'),
                 slippage=D('0.0001'), limits=None, data_mode='offline',
                 simulation_policy=None, acceptance_signal=False, fee_rate=D('0.001')):
        if simulation_policy is not None and type(simulation_policy) is not PaperSimulationPolicy:
            raise ValueError('only PAPER_SIMULATION is supported by the paper runner')
        if acceptance_signal and simulation_policy is None:
            raise ValueError('acceptance signal requires explicit PAPER_SIMULATION')
        self.simulation_policy = simulation_policy
        for value in (quantity, threshold, participation, slippage, fee_rate):
            if not isinstance(value, D) or not value.is_finite() or value < 0:
                raise ValueError('finite nonnegative Decimal configuration required')
        if quantity <= 0 or threshold <= 0 or not 0 < participation <= 1 or slippage >= 1:
            raise ValueError('invalid strategy or execution configuration')
        if data_mode not in {'offline', 'public'}:
            raise ValueError('unknown market-data mode')
        self.market, self.clock = Market(market), clock
        self.symbol, currency = ('NVDAUSDT', 'USDT') if self.market == Market.BINANCE_USDM else ('NVDA', 'USD')
        configured_limits = limits or RiskLimits(D('100'), D('500'), D('800'), 10)
        self.configuration = dict(quantity=quantity, threshold=threshold, participation=participation,
                                  slippage=slippage, data_mode=data_mode, limits=asdict(configured_limits))
        if simulation_policy is not None:
            self.configuration.update(simulation_policy=simulation_policy.describe(),
                acceptance_signal=acceptance_signal, fee_rate=fee_rate)
        elif fee_rate != D('0.001'):
            raise ValueError('custom synthetic fees require PAPER_SIMULATION')
        self.store = PaperStore(path)
        def validate_restore(restored):
            if not restored.runtime or restored.runtime.get('checkpoint') != checkpoint(restored):
                raise PersistenceConflict('PAUSED: persisted checkpoint conflict')
            if restored.runtime['configuration'] != self.configuration:
                raise PersistenceConflict('PAUSED: runtime configuration changed')
            for event in restored.confirmations.values():
                row = self.store.db.execute('SELECT payload FROM events WHERE id=?', (event.event_id,)).fetchone()
                if row is None or row[0] != dumps(event):
                    raise PersistenceConflict('PAUSED: confirmation journal conflict')
        broker = PaperBroker(self.market, 'realtime-' + currency,
                             (Instrument(self.symbol, currency),), {currency: D('1000')}, fee_rate=fee_rate)
        def recover(state):
            if not state.runtime:
                state.runtime = dict(configuration=self.configuration, status='STOPPED', reason='INITIAL',
                    watermarks={}, previous=None, signals=0, orders=0, fills=0, rejected=0,
                    last_valid=None, mark=None, rules=None, session=None)
            # Only local, atomically committed confirmations may clear restart UNKNOWN.
            if all(k in state.confirmations for k in state.orders):
                broker.reconcile()
            state.runtime.update(status='PAUSED', reason='START_REQUIRED', rules=None, session=None)
            state.runtime['checkpoint'] = checkpoint(state)
        try:
            self.controller = TradingController(broker, self.store, clock=clock, limits=configured_limits,
                validate_restore=validate_restore, initialize_state=recover)
        except BaseException:
            self.store.close()
            raise
        self.conflict = False
        self._verified_observations = {}
        self.expected = dumps(self.controller.broker.state)

    @property
    def state(self):
        return self.controller.broker.state

    def _change(self, operation):
        if self.conflict:
            raise PersistenceConflict('account is paused after persistence conflict')
        def checked():
            if dumps(self.state) != self.expected:
                raise PersistenceConflict('persisted account changed outside this runner')
            result = operation()
            self.state.runtime['checkpoint'] = checkpoint(self.state)
            return result
        try:
            result = self.controller._atomic(checked)
        except (Unavailable, RiskRejected):
            raise
        except (PersistenceConflict, sqlite3.Error, OSError, ValueError):
            # Conflicting writers must not overwrite the other writer's snapshot.
            self.conflict = True
            raise
        self.expected = dumps(self.state)
        return result

    def control(self, action):
        if action not in {'start', 'pause', 'stop', 'kill', 'unkill'}:
            raise ValueError('unknown control action')
        def operation():
            if action in {'kill', 'unkill'}:
                self.state.kill_switch = action == 'kill'
            else:
                self.state.runtime.update(status={'start': 'RUNNING', 'pause': 'PAUSED', 'stop': 'STOPPED'}[action],
                                          reason=action.upper())
        return self._change(operation)

    def disconnect(self, reason='DISCONNECTED'):
        return self._change(lambda: self.state.runtime.update(status='PAUSED', reason=reason, previous=None))

    def tick(self):
        last = self.state.runtime['last_valid']
        if self.state.runtime['status'] == 'RUNNING' and (last is None or
                self.clock() - last > timedelta(seconds=self.controller.limits.max_quote_age_seconds)):
            self.disconnect('STALE_QUOTE')

    def _admit(self, intent, *, filling=False):
        runtime, now = self.state.runtime, self.clock()
        rules = runtime['rules']
        if rules is None:
            raise Unavailable('UNKNOWN_RULES')
        if self.configuration['data_mode'] == 'public' and self._verified_observations.get('rules') != identity(rules):
            raise Unavailable('UNVERIFIED_RULE_PROVENANCE')
        fresh(rules['time'], rules['received'], now, timedelta(seconds=300))
        if (self.configuration['data_mode'] == 'public'
                and rules['evidence']['parsed']['validity'] != 'VALID'):
            raise Unavailable('UNKNOWN_RULES')
        if self.market == Market.BINANCE_EQUITY:
            session = runtime['session']
            if session is None:
                raise Unavailable('MISSING_SESSION')
            if self.configuration['data_mode'] == 'public':
                if self._verified_observations.get('session') != identity(session):
                    raise Unavailable('UNVERIFIED_SESSION_PROVENANCE')
            fresh(session['time'], session['received'], now, timedelta(seconds=300))
            if self.configuration['data_mode'] == 'public' and self.simulation_policy is None:
                raise Unavailable('UNKNOWN_STOCKS_ACCOUNT_PERMISSION')
            observed = Rules.from_exchange_info(rules['payload'])
            price = self.state.quotes[self.symbol].price
            market_buy = intent.order_type == OrderType.MARKET and intent.side == Side.BUY
            # MARKET BUY retains the admitted fixed-price/notional assumption.
            notional = intent.quantity * self.state.orders[intent.client_order_id].reservation_price if filling and market_buy else intent.quantity * price
            order = StockOrder(self.symbol, intent.side.value, intent.order_type.value, intent.client_order_id,
                None if market_buy else intent.quantity, notional if market_buy else None,
                intent.limit_price, 'EXTENDED' if intent.order_type == OrderType.LIMIT else None,
                'DAY' if intent.order_type == OrderType.MARKET else intent.time_in_force)
            opened = sum(r.snapshot.status not in TERMINAL for r in self.state.orders.values())
            reference = rules['reference']
            if reference is None:
                if self.simulation_policy is None:
                    raise Unavailable('UNKNOWN_REFERENCE_PRICE')
                reference = price
            admit(order, observed, Session(session['phase']), price=price,
                  reference_price=D(reference), open_orders=0 if filling else opened, permission=True)
        else:
            if self.configuration['data_mode'] == 'public':
                session = runtime['session']
                if session is None or self._verified_observations.get('session') != identity(session):
                    raise Unavailable('UNKNOWN_FUTURES_STATUS')
                fresh(session['time'], session['received'], now, timedelta(seconds=300))
                if session['phase'] != 'TRADING':
                    raise Unavailable('FUTURES_NOT_TRADING')
            mark = runtime['mark']
            if mark is None:
                raise Unavailable('MISSING_MARK')
            fresh(mark['time'], mark['received'], now, timedelta(seconds=5))
            if self.simulation_policy is not None:
                observation = self.simulation_policy.futures_rules(
                    rules['payload']['symbol'], rules['received'], rules['source'])
            else:
                if rules['payload'].get('bracket') is None:
                    raise Unavailable('UNKNOWN_ACCOUNT_RISK_BRACKETS')
                observation = RuleObservation.from_exchange(rules['payload']['symbol'], rules['payload']['bracket'],
                    rules['received'], rules['source'])
            account = self.controller.account()
            decision = observation.validate(intent, mark_price=D(mark['price']), leverage=1,
                available_margin=account.assets[0].available,
                current_notional=sum((abs(p.quantity) * D(mark['price']) for p in account.positions), D(0)), now=now)
            if not decision.approved:
                raise Unavailable(decision.reason)

    def submit(self, intent):
        def operation():
            if self.state.runtime['status'] != 'RUNNING':
                raise Unavailable('RUNNER_NOT_RUNNING')
            return self._submit(intent)
        return self._change(operation)

    def _submit(self, intent):
        if intent.client_order_id in self.state.orders:
            old = self.state.orders[intent.client_order_id].snapshot
            if old.intent != intent or old.reconcile_required:
                raise Unavailable('EXISTING_ORDER_QUERY_ONLY')
            return self.state.confirmations[intent.client_order_id]
        self._admit(intent)
        decision = self.controller.assess(intent)
        self.state.runtime['last_risk'] = dict(simulated=True, **asdict(decision))
        if not decision.approved:
            raise RiskRejected(decision)
        now = self.clock()
        self.state.submissions = [t for t in self.state.submissions if now - t < timedelta(minutes=1)] + [now]
        event = self.controller.broker.submit_order(intent)
        self.state.runtime['orders'] += 1
        return event

    def cancel(self, order_id):
        return self._change(lambda: self.controller.broker.cancel_order(order_id))

    def replay(self, event):
        return self._change(lambda: self.controller.broker.apply(event))

    def refresh_rules(self, *, network=False, **reader_options):
        """One request, no retries; only this acquisition path authenticates rules."""
        from .observations import acquire_rules
        # Invalidate before IO so a failed refresh cannot retain previous approval.
        self._verified_observations.pop('rules', None)
        self._change(lambda: self.state.runtime.update(rules=None))
        value = acquire_rules(self.market, self.symbol, network=network, clock=self.clock, **reader_options)
        accepted = self.event(value)
        if accepted:
            official = value['evidence']['provenance'] == 'OFFICIAL_TRANSPORT'
            if official:
                self._verified_observations['rules'] = identity(self.state.runtime['rules'])
            if self.market == Market.BINANCE_USDM:
                session = dict(kind='session', symbol=self.symbol, time=value['time'], received=value['received'],
                    phase=value['evidence']['parsed']['session'], evidence=value['evidence'])
                session_accepted = self.event(session)
                if session_accepted and official:
                    self._verified_observations['session'] = identity(self.state.runtime['session'])
        return accepted

    def _stream_event(self, value, raw, *, official_transport=False):
        """Called by the public reader, not by JSON replay/bootstrap."""
        if value['kind'] == 'session':
            value = dict(value, evidence=dict(endpoint='wss://nbstream.binance.com/equity/stream',
                version='stocks-calendar-v1', raw=raw, fetched_at=value['received'],
                provenance='OFFICIAL_TRANSPORT' if official_transport else 'OFFLINE_INJECTED'))
        accepted = self.event(value)
        if accepted and value['kind'] == 'session' and official_transport:
            self._verified_observations['session'] = identity(self.state.runtime['session'])
        return accepted

    def event(self, value):
        """Consume normalized replay/public data; never accepts broker execution messages."""
        value = deepcopy(value)
        kind = value['kind']
        if kind not in {'trade', 'quote', 'mark', 'rules', 'session'}:
            raise ValueError('unsupported realtime event')
        if value['symbol'] != self.symbol:
            raise ValueError('market instrument mismatch')
        source, received = timestamp(value['time']), timestamp(value['received'])
        age = timedelta(seconds=300) if kind == 'session' else timedelta(seconds=300 if kind == 'rules' else 5)
        try:
            fresh(int(source.timestamp() * 1000), received, self.clock(), age)
        except Unavailable:
            self.disconnect('STALE_OR_FUTURE_OBSERVATION')
            return False
        def operation():
            runtime = self.state.runtime
            previous = runtime['watermarks'].get(kind)
            digest = identity({k: v for k, v in value.items() if k != 'received'})
            # Source watermark also rejects replay after a process restart without
            # keeping a second full market-data history.
            key = (source, int(value.get('sequence', 0)))
            digests = [digest]
            if previous is not None:
                old_key = (previous['time'], previous['sequence'])
                if key < old_key:
                    return False
                if key == old_key:
                    seen = previous.get('digests', [previous['digest']])
                    if digest in seen:
                        return False
                    if kind != 'quote':
                        raise PersistenceConflict('market identity payload conflict')
                    # Equity timestamps are not unique quote sequence numbers.
                    # Retain only the bounded identities at the current watermark.
                    if len(seen) >= 128:
                        runtime.update(status='PAUSED', reason='QUOTE_IDENTITY_CAP', previous=None)
                        return False
                    digests = seen + [digest]
                if kind == 'trade' and 'sequence' in value and key[1] != previous['sequence'] + 1:
                    runtime.update(status='PAUSED', reason='TRADE_SEQUENCE_GAP', previous=None)
            runtime['watermarks'][kind] = dict(time=source, sequence=key[1], digest=digest)
            if kind == 'quote':
                runtime['watermarks'][kind]['digests'] = digests
            ms = int(source.timestamp() * 1000)
            if kind == 'rules':
                runtime['rules'] = dict(payload=value['payload'], reference=value.get('reference'),
                    source=value['source'], time=ms, received=received, evidence=value.get('evidence'))
                return True
            if kind == 'session':
                runtime['session'] = dict(phase=value['phase'], time=ms, received=received,
                                          evidence=value.get('evidence'))
                return True
            if kind == 'mark':
                price = D(value['price'])
                if not price.is_finite() or price <= 0:
                    raise ValueError('invalid mark price')
                runtime['mark'] = dict(price=price, time=ms, received=received)
                self._risk_mark()
                return True
            expected_kind = 'trade' if self.market == Market.BINANCE_USDM else 'quote'
            if kind != expected_kind:
                raise ValueError('quote source market mismatch')
            price = D(value['price']) if kind == 'trade' else D(value['ask'])
            size = D(value['size']) if kind == 'trade' else min(D(value['bid_size']), D(value['ask_size']))
            bid = D(value.get('bid', price))
            if not all(p.is_finite() for p in (price, bid, size)) or bid <= 0 or price < bid or size < 0:
                raise ValueError('invalid executable observation')
            self.controller.broker.set_quote(Quote(self.symbol, price, source))
            self._risk_mark()
            runtime['last_valid'] = source
            if runtime['status'] != 'RUNNING':
                return True
            self._fills(source, price, bid, size)
            previous_price = runtime['previous']
            runtime['previous'] = price
            first_quote = runtime.setdefault('first_signal_quote', source)
            change = D(0) if previous_price is None else price / previous_price - 1
            position = self.state.positions.get(self.symbol)
            side = Side.BUY if change >= self.configuration['threshold'] and position is None else (
                Side.SELL if change <= -self.configuration['threshold'] and position is not None else None)
            test_signal = (side is None and self.configuration.get('acceptance_signal', False)
                and runtime['signals'] == 0 and position is None
                and source - first_quote >= timedelta(seconds=30))
            if test_signal:
                side = Side.BUY
            if side is None or any(r.snapshot.status not in TERMINAL for r in self.state.orders.values()):
                return True
            runtime['last_signal'] = 'DETERMINISTIC_PAPER_ACCEPTANCE_NO_PREDICTIVE_CLAIM' if test_signal else 'PRICE_CHANGE'
            runtime['signals'] += 1
            order_id = 'rt' + identity((self.market.value, runtime['signals']))[:30]
            quantity = self.configuration['quantity'] if side == Side.BUY else abs(position.quantity)
            stock = self.market == Market.BINANCE_EQUITY
            intent = OrderIntent(self.market, self.symbol, side, OrderType.LIMIT if stock else OrderType.MARKET,
                quantity, (price if side == Side.BUY else bid).quantize(D('.01')) if stock else None, order_id,
                time_in_force='GTC')
            try:
                self._submit(intent)
            except (Unavailable, RiskRejected) as error:
                runtime['rejected'] += 1
                runtime['last_rejection'] = (str(error) if isinstance(error, Unavailable)
                    else error.decision.reason)
            return True
        return self._change(operation)

    def _risk_mark(self):
        mark = self.state.runtime['mark']
        position = self.state.positions.get(self.symbol)
        if self.market == Market.BINANCE_USDM and mark is not None and position is not None:
            self.state.positions[self.symbol] = replace(position, mark_price=D(mark['price']))

    def _fills(self, source, ask, bid, size):
        budget = size * self.configuration['participation']
        for record in tuple(self.state.orders.values()):
            order = record.snapshot
            if order.status in TERMINAL or order.reconcile_required or source <= record.observed_at or budget <= 0:
                continue
            intent = order.intent
            try:
                self._admit(intent, filling=True)
            except Unavailable as error:
                self.state.runtime['last_fill_rejection'] = str(error)
                continue
            price = (ask if intent.side == Side.BUY else bid) * (
                1 + self.configuration['slippage'] * (1 if intent.side == Side.BUY else -1))
            if self.market == Market.BINANCE_EQUITY and intent.order_type == OrderType.MARKET and intent.side == Side.BUY:
                price = record.reservation_price
            if intent.order_type == OrderType.LIMIT and (
                    intent.side == Side.BUY and price > intent.limit_price or
                    intent.side == Side.SELL and price < intent.limit_price):
                continue
            quantity = min(budget, intent.quantity - order.cumulative_filled_quantity)
            # Partial stock execution respects the observed fractional share step.
            rules = self.state.runtime['rules']['payload']
            step = Rules.from_exchange_info(rules).step if self.market == Market.BINANCE_EQUITY else D(0)
            if step:
                quantity = (quantity // step) * step
            if not quantity:
                continue
            self.controller.broker.fill(intent.client_order_id, quantity, price)
            runtime = self.state.runtime
            reference = ask if intent.side == Side.BUY else bid
            runtime['slippage_cost'] = runtime.get('slippage_cost', D(0)) + quantity * abs(price - reference)
            self._risk_mark()
            budget -= quantity
            self.state.runtime['fills'] += 1

    def observation_health(self):
        result = {}
        for kind, seconds in (('rules', 300), ('session', 300)):
            value = self.state.runtime[kind]
            if value is None:
                result[kind] = dict(validity='UNKNOWN')
                continue
            current = timedelta(0) <= self.clock() - value['received'] <= timedelta(seconds=seconds)
            verified = self._verified_observations.get(kind) == identity(value)
            evidence = value.get('evidence') or {}
            result[kind] = dict(validity='EXPIRED' if not current else 'VERIFIED' if verified else 'UNVERIFIED',
                received=value['received'], source_time=value['time'], endpoint=evidence.get('endpoint'),
                version=evidence.get('version'), parsed=evidence.get('parsed'))
        return result

    def health(self):
        runtime = self.state.runtime
        stale = runtime['last_valid'] is None or self.clock() - runtime['last_valid'] > timedelta(
            seconds=self.controller.limits.max_quote_age_seconds)
        return dict(market=self.market.value, status='PAUSED' if self.conflict else runtime['status'],
            reason='PERSISTENCE_CONFLICT' if self.conflict else runtime['reason'], stale=stale,
            kill_switch=self.state.kill_switch, reconciled=self.state.reconciled,
            last_valid=runtime['last_valid'], signals=runtime['signals'], orders=runtime['orders'],
            fills=runtime['fills'], rejected=runtime['rejected'], settlement='UNKNOWN',
            observations=self.observation_health(), simulated=True,
            simulation_policy=None if self.simulation_policy is None else self.simulation_policy.describe(),
            signal_source=runtime.get('last_signal'),
            fee_rate=self.state.fee_rate, slippage_rate=self.configuration['slippage'],
            fees=sum((r.snapshot.cumulative_fee for r in self.state.orders.values()), D(0)),
            filled_quantity=sum((r.snapshot.cumulative_filled_quantity for r in self.state.orders.values()), D(0)),
            slippage_cost=runtime.get('slippage_cost', D(0)),
            last_rejection=runtime.get('last_rejection'), last_fill_rejection=runtime.get('last_fill_rejection'),
            mark=runtime['mark'], risk=runtime.get('last_risk'),
            result='TRADED' if runtime['fills'] else 'NO_TRADE',
            account=dict(simulated=True, **asdict(self.controller.account())), labels=SAFETY_LABELS)

    def close(self):
        try:
            if not self.conflict:
                self.control('stop')
        finally:
            self.store.close()


def equity_events(value, received):
    """Reuse the Equity source normalizer; Futures observations never enter here."""
    table, rows = stream_rows(value, received.isoformat(), 'realtime-memory')
    for row in rows:
        if row.get('symbol', 'NVDA') != 'NVDA' or row['event_time'] is None:
            continue
        result = dict(symbol='NVDA', time=datetime.fromtimestamp(row['event_time'] / 1000, timezone.utc), received=received)
        if table == 'sessions':
            yield dict(result, kind='session', phase=row['session'])
        elif table == 'quotes':
            emitted = row['server_event_time']
            fresh(emitted, received, received, timedelta(seconds=5))
            if emitted < row['event_time']:
                raise ValueError('quote emitted before source time')
            yield dict(result, kind='quote', bid=row['bid_price'], ask=row['ask_price'],
                       bid_size=row['bid_size'], ask_size=row['ask_size'], sequence=row['server_event_time'])
