"""Bounded GET-only Grid Preview evidence; never an exchange fill estimator."""
import argparse
from contextlib import closing, nullcontext
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import hashlib
import io
import json
import math
import os
from pathlib import Path
import signal
from time import monotonic, perf_counter

from factorlab.trading.storage import decode, dumps
from .demo_credentials import DemoCredentialStore
from .demo_grid_supervisor import GridConsumer, grid_factory
from .demo_market_feed import DemoMarketFeed, FeedPaused, SOURCE, timestamp
from .demo_runtime import open_grid_session
from .demo_strategy_supervisor import MarketClient, StrategyControl
from .demo_supervisor import DemoSupervisor
from .observability import RequestCounts


def utcnow():
    return datetime.now(timezone.utc)


def instant(value):
    return datetime.fromisoformat(value)


def collector_source_sha256():
    """Bind the installed collector and trading implementation, including local edits."""
    root = Path(__file__).resolve().parents[2]
    paths = sorted({*Path(__file__).parent.glob('*.py'),
                    *(root / 'trading').glob('*.py'),
                    *(root / 'strategies').glob('grid*.py')})
    digest = hashlib.sha256()
    for path in paths:
        name, content = path.relative_to(root).as_posix().encode(), path.read_bytes()
        digest.update(len(name).to_bytes(8, 'big') + name)
        digest.update(len(content).to_bytes(8, 'big') + content)
    return digest.hexdigest()


class ObservationFull(ValueError):
    """The configured evidence budget has been exhausted."""


class ObservationDeadline(ObservationFull):
    """The frozen window ended; receipt time may be later than effective end."""


class BoundedRequests(RequestCounts):
    """Durably reserve every GET before dispatch, including Preview recovery."""
    def __init__(self, evidence, *, maximum, deadline, monotonic_deadline=math.inf):
        super().__init__()
        self.evidence, self.maximum, self.deadline = evidence, maximum, deadline
        self.monotonic_deadline = monotonic_deadline
        self.used = sum(r['kind'] == 'request' for r in evidence.rows)
        self.failed = any(r['kind'] == 'interruption' or
                          (r['kind'] == 'request_result' and r['error'] is not None)
                          for r in evidence.rows)

    def remaining(self, at):
        return min((self.deadline - at).total_seconds(), self.monotonic_deadline - monotonic())

    def check_deadline(self, at):
        if self.remaining(at) <= 0:
            raise ObservationDeadline('absolute experiment deadline reached')

    def before_request(self, source, method, path):
        if method != 'GET':
            raise ValueError('observation permits GET only')
        if self.failed:
            raise ObservationFull('request after interruption prohibited')
        if self.used >= self.maximum:
            raise ObservationFull('HTTP request budget exhausted')
        at = self.evidence.clock()
        self.check_deadline(at)
        self.evidence.append(dict(kind='request', source=source, method=method,
                                  endpoint=path, at=at.isoformat()))
        self.used += 1

    def record(self, source, method, path, seconds, error, weights, *, status=None):
        super().record(source, method, path, seconds, error, weights, status=status)
        completed_at = self.evidence.clock()
        if error is not None:
            self.failed = True
        self.evidence.append(dict(kind='request_result', source=source, method=method,
                                  endpoint=path, seconds=seconds, error=error, http_status=status,
                                  completed_at=completed_at.isoformat()))
        if error is None:
            self.check_deadline(completed_at)

    def clock_event(self, source, kind, details):
        self.evidence.append(dict(kind=kind, source=source, at=self.evidence.clock().isoformat(), **details))


class ShadowEvidence:
    """One bounded replayable CSV; each row is an indivisible observation batch."""
    def __init__(self, path, *, version, window=180, max_bytes=5 * 1024 * 1024,
                 max_proposals=20, clock=utcnow):
        if (not math.isfinite(window) or not 0 < window <= 3600
                or not 65536 <= max_bytes <= 10 * 1024 * 1024
                or not 1 <= max_proposals <= 100):
            raise ValueError('invalid observation bounds')
        self.path, self.clock = Path(path), clock
        self.version, self.window = version, window
        self.max_bytes, self.max_proposals = max_bytes, max_proposals
        self.deadline = None
        self.rows, self.proposals, self.pending = [], {}, {}
        self.fsync_seconds = 0.
        self.last_market = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        binding = dict(version=version, window=window, max_bytes=max_bytes, max_proposals=max_proposals)
        if self.path.exists():
            if self.path.stat().st_size > max_bytes:
                raise ObservationFull('existing evidence exceeds byte budget')
            with self.path.open(newline='', encoding='utf-8') as stream:
                for row in csv.DictReader(stream, strict=True):
                    self._apply(json.loads(row['event']))
            if not self.rows or self.rows[0] != dict(kind='binding', **binding):
                raise ValueError('observation binding differs')
            self.append(dict(kind='resume', at=clock().isoformat(),
                             last_market_at=self.last_market.isoformat() if self.last_market else None,
                             coverage='UNOBSERVED_DURING_PROCESS_GAP'))
        else:
            with self.path.open('x', newline='', encoding='utf-8') as stream:
                csv.writer(stream).writerow(['event'])
            self.append(dict(kind='binding', **binding))

    def _apply(self, event):
        self.rows.append(event)
        kind = event['kind']
        if kind == 'begin':
            self.pending[event['source']] = event
        elif kind == 'capture':
            for proposal in event['proposals']:
                self.proposals[proposal['id']] = proposal
            self.pending.pop(event['source'], None)
        elif kind == 'book':
            self.last_market = instant(event['market_at'])
            for update in event['updates']:
                self.proposals[update['id']].update(update)
        elif kind == 'end':
            for key in event['ids']:
                self.proposals[key].update(status='ENDED', reason=event['reason'], ended_at=event['at'],
                    right_censored=self.proposals[key]['first_touch'] is None)

    def append(self, event, *, terminal=False):
        buffer = io.StringIO(newline='')
        csv.writer(buffer).writerow([json.dumps(event, separators=(',', ':'), sort_keys=True)])
        encoded = buffer.getvalue().encode('utf-8')
        # Reserve enough room for all terminal identities and their censoring reason.
        reserve = 0 if terminal else 16384
        if self.path.stat().st_size + len(encoded) + reserve > self.max_bytes:
            raise ObservationFull('evidence byte budget exhausted')
        with self.path.open('ab') as stream:
            stream.write(encoded)
            stream.flush()
            started = perf_counter()
            try:
                os.fsync(stream.fileno())
            finally:
                self.fsync_seconds += perf_counter() - started
        self._apply(event)

    def begin(self, identity, bar, live, at):
        if live and identity not in self.pending and not any(
                row['kind'] == 'capture' and row['source'] == identity for row in self.rows):
            if self.deadline is not None and at >= self.deadline:
                raise ObservationDeadline('absolute experiment deadline reached')
            receipt = getattr(self, 'bar_receipt', None)
            self.append(dict(kind='begin', source=identity, at=at.isoformat(),
                             market_at=bar.closed_at.isoformat(),
                             bar_receipt=({k: v for k, v in receipt.items() if k != 'raw'}
                                          if receipt is not None else None), live=live))

    def capture(self, identity, grid, at, *, eligible=True):
        pending = self.pending.get(identity)
        if pending is None:
            return
        if self.deadline is not None and at >= self.deadline:
            raise ObservationDeadline('absolute experiment deadline reached')
        deployment = json.loads(dumps(grid.deployment()))
        config_hash = hashlib.sha256(dumps(grid.deployment()).encode()).hexdigest()
        proposals = []
        for order in grid.active if eligible else ():
            intent = order.intent
            key = grid.deployment_id + ':' + intent.client_order_id
            if key in self.proposals:
                continue
            if len(self.proposals) + len(proposals) >= self.max_proposals:
                raise ObservationFull('proposal budget exhausted')
            proposals.append(dict(id=key, deployment=grid.deployment_id,
                proposal=intent.client_order_id, source=identity, limit=str(intent.limit_price),
                side=intent.side.value, quantity=str(intent.quantity), proposed_at=pending['at'],
                captured_at=at.isoformat(), market_at=pending['market_at'],
                deadline=(instant(pending['at']) + timedelta(seconds=self.window)).isoformat(),
                deployment_config=deployment, configuration_hash=config_hash, version=self.version,
                status='OBSERVING', reason=None, first_touch=None, first_potential=None,
                max_deviation_bps='0', right_censored=False))
        self.append(dict(kind='capture', source=identity, proposals=proposals))

    def recover(self, bridge):
        for identity in tuple(self.pending):
            saved = bridge.controller.store.event_payload(bridge.prefix + 'source:' + identity)
            if saved is not None:
                event = decode(json.loads(saved))
                bridge.observe(event, dispatch=False, source_id=identity)
                self.capture(identity, bridge.grid, self.clock(),
                             eligible=event.get('reseed_withdrawn', False))

    def end(self, reason, *, ids=None, at=None):
        keys = [key for key, p in self.proposals.items() if p['status'] == 'OBSERVING'
                and (ids is None or key in ids)]
        if keys:
            at = at or self.clock()
            if self.deadline is not None:
                at = min(at, self.deadline)
            self.append(dict(kind='end', ids=keys, reason=reason,
                             at=at.isoformat()), terminal=True)

    def book(self, raw, received_at, *, market_status='UNKNOWN'):
        at = timestamp(raw['time'])
        age = (received_at - at).total_seconds()
        bid, ask = D(raw['bidPrice']), D(raw['askPrice'])
        reason = None
        if not 0 <= age <= 5:
            reason = 'STALE_OR_FUTURE_BOOK'
        elif self.last_market is not None and at <= self.last_market:
            reason = 'NONINCREASING_BOOK'
        elif not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
            reason = 'INVALID_BOOK'
        if reason:
            self.append(dict(kind='rejected_book', reason=reason, market_at=at.isoformat(),
                             received_at=received_at.isoformat(), raw=raw))
            return
        self.end('WINDOW_END', ids=[k for k, p in self.proposals.items()
            if received_at >= instant(p['deadline'])], at=received_at)
        updates = []
        for key, p in self.proposals.items():
            # The batch's book was fetched before strategy generation: it is baseline,
            # not forward evidence, even when it is delivered after the new proposal.
            if p['status'] != 'OBSERVING':
                continue
            limit = D(p['limit'])
            forward = at > instant(p['captured_at']) and received_at > instant(p['captured_at'])
            mid = (bid + ask) / 2
            buy = p['side'].upper() == 'BUY'
            touch = forward and (mid <= limit if buy else mid >= limit)
            potential = forward and (ask <= limit if buy else bid >= limit)
            first = p['first_touch'] or (at.isoformat() if touch else None)
            deviation = max(D(p['max_deviation_bps']), abs(mid - limit) / limit * 10000) if forward else D(p['max_deviation_bps'])
            updates.append(dict(id=key, first_touch=first,
                first_potential=p['first_potential'] or (at.isoformat() if potential else None),
                waiting_seconds=(instant(first) - instant(p['proposed_at'])).total_seconds() if first else None,
                max_deviation_bps=str(deviation), distance_bid_bps=str((limit / bid - 1) * 10000),
                distance_ask_bps=str((limit / ask - 1) * 10000), forward=forward,
                opposite_top_quantity=raw.get('askQty' if buy else 'bidQty'),
                target_covered_at_top=(D(raw['askQty' if buy else 'bidQty']) >= D(p['quantity'])
                    if raw.get('askQty' if buy else 'bidQty') is not None else None)))
        self.append(dict(kind='book', raw=raw, market_at=at.isoformat(), received_at=received_at.isoformat(),
            bid=str(bid), ask=str(ask), spread=str(ask - bid), age_seconds=age,
            bid_quantity=raw.get('bidQty'), ask_quantity=raw.get('askQty'),
            depth_scope='TOP_OF_BOOK_ONLY', market_status=market_status,
            utc_hour=at.hour, utc_weekday=at.weekday(), updates=updates))

    def summary(self):
        proposals = list(self.proposals.values())
        return dict(proposals=len(proposals), price_touched=sum(p['first_touch'] is not None for p in proposals),
            potential_conditions=sum(p['first_potential'] is not None for p in proposals),
            not_touched=sum(p['first_touch'] is None for p in proposals),
            right_censored=sum(p['right_censored'] for p in proposals),
            confirmed_exchange_fills=0, books=sum(r['kind'] == 'book' for r in self.rows),
            rejected_books=sum(r['kind'] == 'rejected_book' for r in self.rows))


class ShadowConsumer(GridConsumer):
    def __init__(self, bridge, control, evidence, **kwargs):
        if not bridge.preview:
            raise ValueError('shadow observation requires Preview')
        super().__init__(bridge, control, proposal_observer=evidence, **kwargs)
        self.evidence = evidence
        self.market_status = 'UNKNOWN'

    def recovered(self):
        if self.evidence.pending:
            with self.request_counts.measure('shadow_recovery') if self.request_counts is not None else nullcontext():
                self.evidence.recover(self.bridge)

    def before_poll(self):
        super().before_poll()
        self._end_inactive()
        self.evidence.end('WINDOW_END', ids=[k for k, p in self.evidence.proposals.items()
            if self.clock() >= instant(p['deadline'])])

    def observe_bar_receipt(self, bar, raw, received_at, known_at):
        self.evidence.bar_receipt = dict(opened_at=bar.opened_at.isoformat(),
            closed_at=bar.closed_at.isoformat(), received_at=received_at.isoformat(),
            known_at=known_at.isoformat(), completed_bar=bar.complete,
            interval_seconds=bar.interval_seconds, raw=raw,
            sha256=hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        self.evidence.append(dict(kind='bar_receipt', **self.evidence.bar_receipt))

    def closed_bar(self, bar):
        running = self.running
        if getattr(self.evidence, 'prospective_start', None) is not None:
            self.running = running and bar.opened_at >= self.evidence.prospective_start
        try:
            accepted = super().closed_bar(bar)
            self._end_inactive()
            return accepted
        finally:
            self.running = running
            self.evidence.bar_receipt = None

    def observe_book(self, raw, received_at):
        previous = self.evidence.last_market
        self.evidence.book(raw, received_at, market_status=self.market_status)
        if getattr(self.evidence, 'prospective_start', None) is not None:
            if self.evidence.rows[-1]['kind'] == 'rejected_book':
                raise FeedPaused(self.evidence.rows[-1]['reason'])
            if previous is not None and (timestamp(raw['time']) - previous).total_seconds() > self.evidence.max_quote_gap:
                raise FeedPaused('R13_QUOTE_GAP')

    def _end_inactive(self):
        active = {self.bridge.grid.deployment_id + ':' + order.intent.client_order_id
                  for order in self.bridge.grid.active
                  if order.intent.client_order_id not in self.bridge.grid.cancellations}
        self.evidence.end('STRATEGY_INVALIDATED:' + str(self.bridge.grid.reason),
                          ids=set(self.evidence.proposals) - active)

    def disconnect(self):
        at = self.clock()
        self.evidence.append(dict(kind='interruption', at=at.isoformat(),
                                  reason=self.supervisor.feed.reason))
        self.evidence.end('DATA_INTERRUPTION', at=at)
        super().disconnect()
        self.supervisor.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True, help='source commit identity')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--experiment-id', help='independent R13 window identity; existing output cannot restart')
    parser.add_argument('--max-quote-gap-seconds', type=float, default=30)
    parser.add_argument('--seconds', type=float, default=180)
    parser.add_argument('--sample-seconds', type=float, default=5)
    parser.add_argument('--window-seconds', type=float, default=180)
    parser.add_argument('--max-bytes', type=int, default=5 * 1024 * 1024)
    parser.add_argument('--max-proposals', type=int, default=20)
    parser.add_argument('--max-requests', type=int, default=1500)
    args = parser.parse_args(argv)
    if (not math.isfinite(args.seconds) or not 0 < args.seconds <= 3600
            or not math.isfinite(args.sample_seconds) or not 1 <= args.sample_seconds <= 60):
        parser.error('duration must be within 3600 seconds and sampling within 1..60 seconds')
    if not math.isfinite(args.max_quote_gap_seconds) or args.max_quote_gap_seconds <= 0:
        parser.error('max quote gap must be finite and positive')
    if not 1 <= args.max_requests <= 1500:
        parser.error('max requests must be within 1..1500')
    if not 131072 <= args.max_bytes <= 5 * 1024 * 1024 or not 1 <= args.max_proposals <= 20:
        parser.error('research budget must be 128 KiB..5 MiB and proposals 1..20')
    if args.experiment_id and args.max_bytes < 393216:
        parser.error('R13 requires at least 384 KiB for raw and normalized evidence')
    # R13 retains a normalized envelope and analysis beside the raw stream.
    event_budget = args.max_bytes // 3 - 65536 if args.experiment_id else args.max_bytes - 65536
    host_started, monotonic_started = utcnow(), monotonic()
    evidence = ShadowEvidence(args.output / 'events.csv', version=args.version,
        window=args.window_seconds, max_bytes=event_budget, max_proposals=args.max_proposals, clock=utcnow)
    settings = json.loads(args.config.read_text(encoding='utf-8')) if args.config else None
    parameters = dict(seconds=args.seconds, sample_seconds=args.sample_seconds,
                      window_seconds=args.window_seconds, max_bytes=args.max_bytes,
                      max_proposals=args.max_proposals, max_requests=args.max_requests,
                      settings=settings, version=args.version, symbol='NVDAUSDT')
    if args.experiment_id:
        parameters.update(experiment_id=args.experiment_id,
            max_quote_gap_seconds=args.max_quote_gap_seconds,
            collector_source_sha256=collector_source_sha256(),
            strategy_definition=dict(factory='P7_M13_GRID_FACTORY', settings=settings,
                default_range_multipliers=['0.99', '1.01'], levels=1, initial_inventory=0,
                quantity=1, max_inventory=1, max_cash_committed=7, breakout='pause'),
            market='BINANCE_USDM_DEMO', interval_seconds=60,
            interruption_rule='STOP_WITHOUT_RETRY', restart_rule='REFUSE',
            limit_rule='ORIGINAL_LEGAL_PROPOSAL_LIMIT_IMMUTABLE',
            termination=['ABSOLUTE_DEADLINE', 'REQUEST_BUDGET', 'BYTE_BUDGET',
                         'PROPOSAL_BUDGET', 'DATA_INTERRUPTION', 'SIGNAL'],
            minimum_request_interval_seconds=0,
            minimum_poll_delay_seconds=args.sample_seconds)
        parameters['strategy_definition_sha256'] = hashlib.sha256(json.dumps(
            parameters['strategy_definition'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    experiments = [r for r in evidence.rows if r['kind'] == 'experiment']
    if args.experiment_id and experiments:
        raise ObservationFull('R13 experiment restart prohibited')
    if experiments:
        experiment = experiments[0]
        if experiment['parameters'] != parameters:
            raise ValueError('experiment parameters differ')
    else:
        if any(row['kind'] == 'request' for row in evidence.rows):
            raise ObservationFull('experiment startup interrupted before its time bounds were recorded')
        experiment = dict(kind='experiment', parameters=parameters, started_at=host_started.isoformat(),
                          deadline=(host_started + timedelta(seconds=args.seconds)).isoformat())
    # Existing reports without host_deadline stored their original bounds in host time.
    host_deadline = instant(experiment.get('host_deadline', experiment['deadline']))
    monotonic_deadline = monotonic_started + (host_deadline - host_started).total_seconds()
    initial_books = evidence.summary()['books']
    requests = BoundedRequests(evidence, maximum=args.max_requests, deadline=host_deadline,
                               monotonic_deadline=monotonic_deadline)
    result = dict(engineering='SEE_OFFLINE_TESTS', real_observation='NOT_RUN', requests=requests,
                  source=SOURCE, version=args.version, no_live_money=True,
                  parameters=dict(seconds=args.seconds, sample_seconds=args.sample_seconds,
                                  window_seconds=args.window_seconds, max_bytes=args.max_bytes,
                                  max_proposals=args.max_proposals))
    experiment_pending = not experiments
    end_reason = 'RUN_END'
    try:
        with open_grid_session(args.output / 'preview-state', DemoCredentialStore(), grid_factory(settings),
                               preview=True, continuous=True, request_counts=requests) as bridge:
            evidence.clock = bridge.gateway.clock = bridge.session._now
            exchange_now, monotonic_now = evidence.clock(), monotonic()
            if 'host_deadline' in experiment:
                # On resume the persisted exchange deadline is authoritative after calibration.
                remaining = instant(experiment['deadline']) - exchange_now
                requests.monotonic_deadline = monotonic_now + remaining.total_seconds()
            else:
                # Spend startup time before projecting the original window onto exchange time.
                duration = instant(experiment['deadline']) - instant(experiment['started_at'])
                deadline = exchange_now + timedelta(seconds=monotonic_deadline - monotonic_now)
                experiment = dict(experiment, started_at=(deadline - duration).isoformat(),
                                  deadline=deadline.isoformat(), host_deadline=host_deadline.isoformat())
            requests.deadline = evidence.deadline = instant(experiment['deadline'])
            if experiment_pending:
                evidence.append(experiment)
                experiment_pending = False
            if args.experiment_id:
                evidence.prospective_start = instant(experiment['started_at'])
                evidence.max_quote_gap = args.max_quote_gap_seconds
                account = bridge.session.status()
                evidence.append(dict(kind='account_reconciled', at=evidence.clock().isoformat(),
                    reconciled=account['reconciled'], positions=len(account['positions']),
                    open_orders=len(account['open_orders']), unknown_orders=len(account['unknown'])))
                deployment = json.loads(dumps(bridge.grid.deployment()))
                evidence.append(dict(kind='strategy_binding', at=evidence.clock().isoformat(),
                    deployment_config=deployment,
                    configuration_hash=hashlib.sha256(dumps(bridge.grid.deployment()).encode()).hexdigest(),
                    version=args.version, instrument='NVDAUSDT', interval_seconds=60))
            path = args.output / 'feed.sqlite3'
            with closing(StrategyControl(path, dict(source=SOURCE, mode='SHADOW_GET_ONLY'))) as control:
                requests.enable_rounds(max_requests=args.max_requests,
                    max_quote_gap_seconds=args.max_quote_gap_seconds,
                    sink=lambda row: evidence.append(dict(kind='poll_round', watermarks=feed.watermarks(), **row)))
                consumer = ShadowConsumer(bridge, control, evidence, request_counts=requests, clock=evidence.clock)
                client = MarketClient(requests)
                info = client.request('GET', '/fapi/v1/exchangeInfo', signed=False)
                symbol = next(s for s in info['symbols'] if s['symbol'] == 'NVDAUSDT')
                evidence.append(dict(kind='market_status', status=symbol['status'],
                    instrument=symbol, received_at=evidence.clock().isoformat(), source='/fapi/v1/exchangeInfo'))
                consumer.market_status = symbol['status']
                with closing(DemoMarketFeed(client, consumer, state_path=path,
                        consumer_id='shadow:' + bridge.grid.deployment_id,
                        clock=utcnow, receipt_clock=evidence.clock,
                        exchange_time=bridge.session.client.demo_time,
                        initial_bars=max(1, bridge.grid.config.atr_window if bridge.grid.config.mode == 'atr' else 1))) as feed:
                    supervisor = DemoSupervisor(feed, poll_seconds=args.sample_seconds,
                        before_poll=consumer.before_poll, on_pause=consumer.disconnect, request_counts=requests,
                        metric_snapshot=lambda: dict(
                            controller_commit=bridge.controller.store.commit_seconds,
                            protocol_commit=bridge.gateway.adapter.store.commit_seconds,
                            feed_commit=feed.commit_seconds,
                            evidence_fsync=evidence.fsync_seconds))
                    consumer.supervisor = supervisor
                    signal.signal(signal.SIGINT, lambda *_: supervisor.stop())
                    signal.signal(signal.SIGTERM, lambda *_: supervisor.stop())
                    remaining = requests.remaining(evidence.clock())
                    if remaining <= 0:
                        raise ObservationDeadline('absolute experiment deadline reached')
                    supervisor.run(seconds=remaining)
                    result.update(real_observation='RUN' if evidence.summary()['books'] else 'NOT_RUN',
                                  feed_reason=feed.reason)
    except ObservationDeadline as error:
        end_reason = 'DEADLINE'
        result.update(termination='DEADLINE', reason=str(error))
    except ObservationFull as error:
        end_reason = 'CAPACITY_LIMIT'
        result.update(termination='CAPACITY_LIMIT', reason=str(error))
    except Exception as error:
        end_reason = 'ERROR:' + type(error).__name__
        result.update(error=type(error).__name__, reason=str(error))
        raise
    finally:
        ended_at = evidence.clock()
        if experiment_pending:
            evidence.append(experiment, terminal=True)
        # Failed re-entry has no calibrated time to end proposals loaded from disk.
        if not experiments or evidence.deadline is not None:
            evidence.end(end_reason, at=min(ended_at, requests.deadline))
        if args.experiment_id:
            evidence.append(dict(kind='window_end', at=ended_at.isoformat(),
                reason=result.get('reason') or result.get('feed_reason') or 'RUN_END'), terminal=True)
        result.update(evidence.summary())
        result['run_books'] = result['books'] - initial_books
        result['real_observation'] = 'RUN' if result['run_books'] else 'REAL_OBSERVATION_NOT_RUN'
        result.update(api=requests.snapshot(), reserved_requests=requests.used,
                      evidence_fsync_seconds=evidence.fsync_seconds,
                      experiment=experiments[0] if experiments else experiment,
                      started_at=experiment['started_at'], deadline=experiment['deadline'],
                      ended_at=ended_at.isoformat())
        (args.output / 'summary.json').write_text(json.dumps(result, indent=2, default=str) + '\n', encoding='utf-8')
        print(json.dumps(result, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
