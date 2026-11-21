"""One bounded GET-only Preview window with concurrent book acquisition."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
from time import monotonic

from factorlab.integrations.binance_futures_trading.demo_credentials import DemoCredentialStore
from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import GridConsumer, grid_factory
from factorlab.integrations.binance_futures_trading.demo_market_collection import DemoBookCollection, collection_client
from factorlab.integrations.binance_futures_trading.demo_market_feed import DemoMarketFeed
from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session
from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import MarketClient
from factorlab.integrations.binance_futures_trading.demo_supervisor import DemoSupervisor
from factorlab.integrations.binance_futures_trading.observability import RequestCounts


class WindowEnded(ValueError):
    """A request or evidence budget ended the single observation window."""
    category = 'WINDOW_ENDED'


class WindowRequests(RequestCounts):
    def __init__(self, *, clock=monotonic, maximum=600, seconds=600, max_bytes=5 * 1024 * 1024):
        super().__init__()
        self.clock = clock
        self.started = clock()
        self.deadline = self.started + seconds
        self.maximum, self.max_bytes = maximum, max_bytes - 65536
        self.used = 0
        self.rows = []
        self.bytes = 0
        self.poll_round = 0

    def append(self, row):
        row = dict(elapsed_seconds=self.clock() - self.started, poll_round=self.poll_round, **row)
        size = len(json.dumps(row, separators=(',', ':')).encode()) + 1
        if self.bytes + size > self.max_bytes:
            raise WindowEnded('evidence byte budget exhausted')
        self.rows.append(row)
        self.bytes += size

    def before_request(self, source, method, path):
        if method != 'GET':
            raise WindowEnded('GET-only observation')
        if self.clock() >= self.deadline:
            raise WindowEnded('absolute observation deadline')
        if self.used >= self.maximum:
            raise WindowEnded('request budget exhausted')
        # Reserve room for the bounded response diagnostics before dispatch.
        if self.bytes + 16384 > self.max_bytes:
            raise WindowEnded('insufficient response evidence budget')
        self.append(dict(kind='request', source=source, method=method, endpoint=path))
        self.used += 1

    def record(self, source, method, path, seconds, error, weights, *, status=None):
        super().record(source, method, path, seconds, error, weights, status=status)
        self.append(dict(kind='request_result', source=source, method=method,
            endpoint=path, seconds=seconds, error=error, http_status=status))

    def clock_event(self, source, kind, details):
        self.append(dict(kind=kind, source=source, **details))


class PreviewControl:
    def desired(self):
        return 'RUNNING'


class PreviewConsumer(GridConsumer):
    """Sample the account evidence the consumer had on entry to each callback."""
    def __init__(self, bridge, requests):
        super().__init__(bridge, PreviewControl(), clock=bridge.session._now, request_counts=requests)
        self.poll_facts = None
        self.first_poll_event = len(requests.rows)
        requests.poll_round = 1

    def before_poll(self):
        adapter = self.bridge.session.adapter
        previous = adapter.facts
        super().before_poll()
        if adapter.facts is not previous:
            self.poll_facts = adapter.facts

    def _account_at_delivery(self):
        session = self.bridge.session
        known_at = self.clock()
        facts = self.poll_facts
        if facts is None or facts is not session.adapter.facts:
            return dict(delivered_at=known_at.isoformat(), account_valid=False, account_expired=False)
        age = session.client.monotonic() - facts.read_started_monotonic
        calibrated_age = (known_at - facts.observed_at).total_seconds()
        return dict(delivered_at=known_at.isoformat(), account_read_started_at=facts.observed_at.isoformat(),
            account_age_seconds=age, calibrated_account_age_seconds=calibrated_age,
            account_valid=self.bridge.ready and 0 <= age <= 5 and 0 <= calibrated_age <= 5,
            account_expired=age > 5 or calibrated_age > 5)

    def _append(self, kind, **details):
        with self.request_counts.admission_lock:
            self.request_counts.append(dict(kind=kind, **details))

    def observe_bar_receipt(self, bar, raw, received_at, known_at):
        self._append('bar_receipt', closed_at=bar.closed_at.isoformat(),
                     received_at=received_at.isoformat(), known_at=known_at.isoformat())

    def closed_bar(self, bar):
        evidence = self._account_at_delivery()
        accepted = super().closed_bar(bar)
        self._append('bar_delivery', closed_at=bar.closed_at.isoformat(),
                     fresh_bar=accepted and self.live_bar, **evidence)
        return accepted

    def quote(self, quote):
        evidence = self._account_at_delivery()
        result = super().quote(quote)
        self._append('quote_delivery', market_at=quote.observed_at.isoformat(), **evidence)
        return result

    def report_poll(self, report):
        requests = self.request_counts
        with requests.admission_lock:
            self._report_poll(report)
            self.first_poll_event = len(requests.rows)
            requests.poll_round += 1
        self.poll_facts = None

    def _report_poll(self, report):
        deliveries = [row for row in self.request_counts.rows[self.first_poll_event:]
                      if row['kind'] in ('bar_delivery', 'quote_delivery')]
        consumed = deliveries or [self._account_at_delivery()]
        valid = all(row['account_valid'] for row in consumed)
        expired = (report['reason'] in ('STALE_ACCOUNT', 'AccountReadExpired')
                   or any(row['account_expired'] for row in consumed))
        if expired:
            classification = 'expired'
        elif report['status'] == 'RUNNING' and report['reconciled'] and valid:
            classification = 'valid'
        else:
            classification = 'paused'
        paired = (classification == 'valid'
                  and any(row.get('fresh_bar') for row in deliveries)
                  and any(row['kind'] == 'quote_delivery' for row in deliveries))
        self.request_counts.append(dict(account_round=classification, fresh_bar_and_account=paired, **report))


def account_evidence(session):
    status = session.status()
    facts = session.adapter.facts
    age = None if facts is None else session.client.monotonic() - facts.read_started_monotonic
    return dict(reconciled=status['reconciled'], unknown_count=len(status['unknown']),
        account_age_seconds=age, recovery_seconds=session.last_recovery_seconds)


def observe(root, *, credentials=None, worker_seconds=155):
    requests = WindowRequests()
    outcome = None
    failure = None
    stage = 'session_initialization'
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with open_grid_session(root, credentials or DemoCredentialStore(), grid_factory(None),
                preview=True, continuous=True, request_counts=requests) as bridge:
            requests.append(dict(kind='initialized', **account_evidence(bridge.session)))
            stage = 'consumer_initialization'
            consumer = PreviewConsumer(bridge, requests)
            reader, queued = collection_client(requests, MarketClient)
            reader.share_demo_clock(bridge.session.client)
            collection = DemoBookCollection(reader, poll_seconds=2, clock=reader.monotonic,
                on_acquired=queued.acquired, exchange_time=reader.demo_time)
            stage = 'feed_initialization'
            with closing(DemoMarketFeed(MarketClient(requests), consumer,
                    state_path=root / 'feed.sqlite3', consumer_id='get-recovery-preview',
                    interval='1m', catch_up=True, exchange_time=bridge.session.client.demo_time,
                    receipt_clock=bridge.session._now)) as feed:
                def report(raw):
                    row = json.loads(raw)
                    row.update(kind='poll', live_bar=consumer.live_bar,
                        eligible_after=consumer.eligible_after.isoformat(),
                        last_bar=consumer.last_bar.isoformat() if consumer.last_bar else None,
                        **account_evidence(bridge.session))
                    consumer.report_poll(row)

                supervisor = DemoSupervisor(feed, before_poll=consumer.before_poll,
                    on_pause=consumer.disconnect, report=report, request_counts=requests,
                    collection=collection, collection_requests=queued,
                    on_recovery_delay=consumer.require_fresh_bar)
                consumer.supervisor = supervisor
                stage = 'supervisor_run'
                supervisor.run(seconds=min(worker_seconds, requests.deadline - monotonic()))
    except Exception as error:
        outcome = getattr(error, 'category', type(error).__name__)
        frames = []
        traceback = error.__traceback__
        while traceback is not None:
            code = traceback.tb_frame.f_code
            frames.append(dict(module=Path(code.co_filename).name,
                function=code.co_name, line=traceback.tb_lineno))
            traceback = traceback.tb_next
        failure = dict(stage=stage, exception=type(error).__name__, frames=frames[-8:])
    polls = [row for row in requests.rows if row['kind'] == 'poll']
    rounds = Counter(row['account_round'] for row in polls)
    return dict(failure=failure, started_at=started_at, elapsed_seconds=monotonic() - requests.started,
        outcome=outcome, limits=dict(worker_seconds=worker_seconds, absolute_seconds=600,
            get_requests=600, evidence_bytes=5 * 1024 * 1024),
        admitted_get_requests=requests.used, completed_requests=dict(requests),
        rounds={name: rounds[name] for name in ('valid', 'expired', 'paused')},
        fresh_bar_and_account_rounds=[row['poll_round'] for row in polls if row['fresh_bar_and_account']],
        metrics=requests.snapshot(), events=requests.rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=False)
    with args.output.open('x', encoding='utf-8') as output:
        result = observe(args.state)
        json.dump(result, output, separators=(',', ':'))
        output.write('\n')
    print(json.dumps({key: result[key] for key in ('outcome', 'elapsed_seconds', 'admitted_get_requests')}))


if __name__ == '__main__':
    main()
