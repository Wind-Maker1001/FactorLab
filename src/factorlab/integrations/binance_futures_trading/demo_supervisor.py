"""Read-only Demo feed: python -m factorlab.integrations.binance_futures_trading.demo_supervisor."""
import argparse
import json
from pathlib import Path
import signal
from threading import Event
import sys
from time import monotonic

from factorlab.errors import DataQualityError
from .demo_market_feed import DemoMarketFeed, FeedPaused, INTERVALS, SOURCE
from .protocol import Environment, ExchangeError, FuturesClient, ReadOnlyNetworkTransport
from .observability import RequestCounts


class DemoSupervisor:
    def __init__(self, feed, *, poll_seconds=2, max_backoff=60, report=print,
                 before_poll=None, on_pause=None, request_counts=None, metric_snapshot=None,
                 collection=None, collection_requests=None, on_recovery_delay=None, recover_after_bars=False):
        if poll_seconds <= 0 or max_backoff < poll_seconds:
            raise ValueError('invalid polling bounds')
        self.feed, self.poll_seconds, self.max_backoff = feed, poll_seconds, max_backoff
        self.report, self.stopped = report, Event()
        self.before_poll, self.on_pause = before_poll, on_pause
        self.request_counts = request_counts
        self.metric_snapshot = metric_snapshot
        self.collection, self.collection_requests = collection, collection_requests
        self.on_recovery_delay = on_recovery_delay
        self.recover_after_bars = recover_after_bars

    def stop(self):
        self.stopped.set()

    def _recover(self, deadline):
        started = monotonic()
        if self.before_poll is not None:
            self.before_poll()
        if self.on_recovery_delay is not None and monotonic() - started >= self.feed.seconds:
            self.on_recovery_delay()
        return not self.stopped.is_set() and (deadline is None or monotonic() < deadline)

    def run(self, *, seconds=None):
        if seconds is not None and seconds <= 0:
            raise ValueError('positive duration required')
        deadline = monotonic() + seconds if seconds is not None else None
        backoff = self.poll_seconds
        recovering = True
        try:
            if self.collection is not None:
                self.feed.collection = self.collection
                self.collection.start()
            while not self.stopped.is_set() and (deadline is None or monotonic() < deadline):
                delay = self.poll_seconds
                if isinstance(self.request_counts, RequestCounts):
                    self.request_counts.phase = 'recovery' if recovering else 'poll'
                telemetry = (self.request_counts if isinstance(self.request_counts, RequestCounts)
                             and self.request_counts.rounds is not None else None)
                baseline = self.metric_snapshot() if telemetry is not None and self.metric_snapshot else {}
                if telemetry is not None:
                    telemetry.begin_round()
                wait_seconds = 0.
                completed = False
                caught_error = None
                result = dict(bars=0, quotes=0)
                try:
                    try:
                        if self.collection is not None:
                            self.collection.check()
                        if self.recover_after_bars:
                            delivery = self.feed.poll(before_delivery=lambda: self._recover(deadline))
                            if delivery is None:
                                break
                            result = delivery
                        else:
                            if not self._recover(deadline):
                                break
                            result = self.feed.poll()
                        recovering = False
                        backoff = self.poll_seconds
                    except (DataQualityError, FeedPaused, OSError) as error:
                        caught_error = getattr(error, 'category', type(error).__name__)
                        recovering = True
                        self.feed.pause(str(error) if isinstance(error, FeedPaused) else caught_error)
                        if self.on_pause is not None:
                            self.on_pause()
                        result = dict(bars=0, quotes=0)
                        delay = max(backoff, error.retry_after or 0) if isinstance(error, ExchangeError) else backoff
                        backoff = min(self.max_backoff, backoff * 2)
                    if self.collection_requests is not None:
                        self.collection_requests.drain()
                    self.report(json.dumps(dict(status=self.feed.status, reason=self.feed.reason,
                        source=SOURCE, interval=self.feed.interval, watermarks=self.feed.watermarks(), **result)))
                    if deadline is not None:
                        delay = min(delay, max(0, deadline - monotonic()))
                    wait_started = monotonic()
                    try:
                        self.stopped.wait(delay)
                    finally:
                        wait_seconds = monotonic() - wait_started
                    completed = True
                finally:
                    if self.collection_requests is not None:
                        self.collection_requests.drain()
                    if telemetry is not None:
                        exc = sys.exc_info()[1]
                        metrics = self.metric_snapshot() if self.metric_snapshot else {}
                        deltas = {name: value - baseline[name] for name, value in metrics.items()}
                        for name, value in deltas.items():
                            telemetry.sample(name, value)
                        telemetry.sample('scheduling_wait', wait_seconds)
                        telemetry.end_round(wait_seconds=wait_seconds, scheduled_wait_seconds=delay,
                            interrupted=not completed or caught_error is not None,
                            error=type(exc).__name__ if exc is not None else caught_error,
                            caught_error=caught_error, status=getattr(self.feed, 'status', None),
                            reason=getattr(self.feed, 'reason', None), **result,
                            cumulative_seconds=deltas)
        finally:
            self.feed.pause('STOPPED')
            if self.collection is not None:
                try:
                    self.collection.close()
                finally:
                    if self.collection_requests is not None:
                        self.collection_requests.drain()


class ReadOnlyConsumer:
    """No execution gateway, credentials, or strategy order callbacks."""
    def closed_bar(self, bar):
        print(json.dumps(dict(event='closed_bar', source=bar.source, symbol=bar.symbol,
            interval_seconds=bar.interval_seconds, closed_at=bar.closed_at.isoformat(), close=str(bar.close))))

    def quote(self, quote):
        print(json.dumps(dict(event='quote', source=SOURCE, symbol=quote.instrument_id,
            observed_at=quote.observed_at.isoformat(), price=str(quote.price))))


def main():
    parser = argparse.ArgumentParser(description='NO LIVE MONEY: unsigned Binance Demo market GETs only')
    parser.add_argument('--interval', choices=INTERVALS, default='1m')
    parser.add_argument('--poll-seconds', type=float, default=2)
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--max-backfill', type=int, default=120)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    client = FuturesClient(environment=Environment.DEMO,
        transport=ReadOnlyNetworkTransport(permit_account_read=True, environment=Environment.DEMO))
    feed = DemoMarketFeed(client, ReadOnlyConsumer(), state_path=args.state,
        consumer_id='readonly-console-v1', interval=args.interval, max_backfill=args.max_backfill)
    supervisor = DemoSupervisor(feed, poll_seconds=args.poll_seconds)
    signal.signal(signal.SIGINT, lambda *_: supervisor.stop())
    signal.signal(signal.SIGTERM, lambda *_: supervisor.stop())
    try:
        supervisor.run(seconds=args.seconds)
    finally:
        feed.close()


if __name__ == '__main__':
    main()
