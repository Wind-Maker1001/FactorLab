"""Bounded GET-only Grid Preview evidence; never an exchange fill estimator."""
import argparse
from contextlib import closing
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

from factorlab.trading.storage import decode, dumps
from .demo_credentials import DemoCredentialStore
from .demo_grid_supervisor import GridConsumer, grid_factory
from .demo_market_feed import DemoMarketFeed, SOURCE, timestamp
from .demo_runtime import open_grid_session
from .demo_strategy_supervisor import MarketClient, StrategyControl
from .demo_supervisor import DemoSupervisor
from .observability import RequestCounts


def utcnow():
    return datetime.now(timezone.utc)


def instant(value):
    return datetime.fromisoformat(value)


class ObservationFull(ValueError):
    """The configured evidence budget has been exhausted."""


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
        self.rows, self.proposals, self.pending = [], {}, {}
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
            os.fsync(stream.fileno())
        self._apply(event)

    def begin(self, identity, bar, live, at):
        if live and identity not in self.pending and not any(
                row['kind'] == 'capture' and row['source'] == identity for row in self.rows):
            self.append(dict(kind='begin', source=identity, at=at.isoformat(),
                             market_at=bar.closed_at.isoformat()))

    def capture(self, identity, grid, at, *, eligible=True):
        pending = self.pending.get(identity)
        if pending is None:
            return
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
            self.append(dict(kind='end', ids=keys, reason=reason,
                             at=(at or self.clock()).isoformat()), terminal=True)

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
                             received_at=received_at.isoformat()))
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
        self.append(dict(kind='book', market_at=at.isoformat(), received_at=received_at.isoformat(),
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

    def before_poll(self):
        if self.evidence.pending:
            self.bridge.recover()
            self.evidence.recover(self.bridge)
        super().before_poll()
        self._end_inactive()
        self.evidence.end('WINDOW_END', ids=[k for k, p in self.evidence.proposals.items()
            if self.clock() >= instant(p['deadline'])])

    def closed_bar(self, bar):
        accepted = super().closed_bar(bar)
        self._end_inactive()
        return accepted

    def observe_book(self, raw, received_at):
        self.evidence.book(raw, received_at, market_status=self.market_status)

    def _end_inactive(self):
        active = {self.bridge.grid.deployment_id + ':' + order.intent.client_order_id
                  for order in self.bridge.grid.active
                  if order.intent.client_order_id not in self.bridge.grid.cancellations}
        self.evidence.end('STRATEGY_INVALIDATED:' + str(self.bridge.grid.reason),
                          ids=set(self.evidence.proposals) - active)

    def disconnect(self):
        self.evidence.end('DATA_INTERRUPTION')
        super().disconnect()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True, help='source commit identity')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--seconds', type=float, default=180)
    parser.add_argument('--sample-seconds', type=float, default=5)
    parser.add_argument('--window-seconds', type=float, default=180)
    parser.add_argument('--max-bytes', type=int, default=5 * 1024 * 1024)
    parser.add_argument('--max-proposals', type=int, default=20)
    args = parser.parse_args(argv)
    if (not math.isfinite(args.seconds) or not 0 < args.seconds <= 3600
            or not math.isfinite(args.sample_seconds) or not 1 <= args.sample_seconds <= 60):
        parser.error('duration must be within 3600 seconds and sampling within 1..60 seconds')
    evidence = ShadowEvidence(args.output / 'events.csv', version=args.version,
        window=args.window_seconds, max_bytes=args.max_bytes, max_proposals=args.max_proposals)
    initial_books = evidence.summary()['books']
    requests = RequestCounts()
    result = dict(engineering='SEE_OFFLINE_TESTS', real_observation='NOT_RUN', requests=requests,
                  source=SOURCE, version=args.version, no_live_money=True,
                  parameters=dict(seconds=args.seconds, sample_seconds=args.sample_seconds,
                                  window_seconds=args.window_seconds, max_bytes=args.max_bytes,
                                  max_proposals=args.max_proposals))
    settings = json.loads(args.config.read_text(encoding='utf-8')) if args.config else None
    try:
        with open_grid_session(args.output / 'preview-state', DemoCredentialStore(), grid_factory(settings),
                               preview=True, continuous=True, request_counts=requests) as bridge:
            path = args.output / 'feed.sqlite3'
            with closing(StrategyControl(path, dict(source=SOURCE, mode='SHADOW_GET_ONLY'))) as control:
                consumer = ShadowConsumer(bridge, control, evidence)
                client = MarketClient(requests)
                info = client.request('GET', '/fapi/v1/exchangeInfo', signed=False)
                symbol = next(s for s in info['symbols'] if s['symbol'] == 'NVDAUSDT')
                evidence.append(dict(kind='market_status', status=symbol['status'],
                    received_at=utcnow().isoformat(), source='/fapi/v1/exchangeInfo'))
                consumer.market_status = symbol['status']
                with closing(DemoMarketFeed(client, consumer, state_path=path,
                        consumer_id='shadow:' + bridge.grid.deployment_id,
                        initial_bars=max(1, bridge.grid.config.atr_window if bridge.grid.config.mode == 'atr' else 1))) as feed:
                    supervisor = DemoSupervisor(feed, poll_seconds=args.sample_seconds,
                        before_poll=consumer.before_poll, on_pause=consumer.disconnect, request_counts=requests)
                    consumer.supervisor = supervisor
                    signal.signal(signal.SIGINT, lambda *_: supervisor.stop())
                    signal.signal(signal.SIGTERM, lambda *_: supervisor.stop())
                    supervisor.run(seconds=args.seconds)
                    result.update(real_observation='RUN' if evidence.summary()['books'] else 'NOT_RUN',
                                  feed_reason=feed.reason)
    except ObservationFull as error:
        result.update(termination='CAPACITY_LIMIT', reason=str(error))
        evidence.end('CAPACITY_LIMIT')
    except Exception as error:
        result.update(error=type(error).__name__, reason=str(error))
        evidence.end('ERROR:' + type(error).__name__)
        raise
    finally:
        evidence.end('RUN_END')
        result.update(evidence.summary())
        result['run_books'] = result['books'] - initial_books
        result['real_observation'] = 'RUN' if result['run_books'] else 'NOT_RUN'
        (args.output / 'summary.json').write_text(json.dumps(result, indent=2, default=str) + '\n', encoding='utf-8')
        print(json.dumps(result, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
