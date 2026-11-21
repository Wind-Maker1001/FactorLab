"""One bounded GET-only Preview window with concurrent book acquisition."""
import argparse
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

    def append(self, row):
        row = dict(elapsed_seconds=self.clock() - self.started, **row)
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
            consumer = GridConsumer(bridge, PreviewControl(), clock=bridge.session._now,
                request_counts=requests)
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
                    with requests.admission_lock:
                        requests.append(row)

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
    return dict(failure=failure, started_at=started_at, elapsed_seconds=monotonic() - requests.started,
        outcome=outcome, limits=dict(worker_seconds=worker_seconds, absolute_seconds=600,
            get_requests=600, evidence_bytes=5 * 1024 * 1024),
        admitted_get_requests=requests.used, completed_requests=dict(requests),
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
