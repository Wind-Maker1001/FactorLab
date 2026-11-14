"""Read-only Demo feed: python -m factorlab.integrations.binance_futures_trading.demo_supervisor."""
import argparse
import json
from pathlib import Path
import signal
from threading import Event
from time import monotonic

from factorlab.errors import DataQualityError
from .demo_market_feed import DemoMarketFeed, FeedPaused, INTERVALS, SOURCE
from .protocol import Environment, ExchangeError, FuturesClient, ReadOnlyNetworkTransport


class DemoSupervisor:
    def __init__(self, feed, *, poll_seconds=2, max_backoff=60, report=print,
                 before_poll=None, on_pause=None):
        if poll_seconds <= 0 or max_backoff < poll_seconds:
            raise ValueError('invalid polling bounds')
        self.feed, self.poll_seconds, self.max_backoff = feed, poll_seconds, max_backoff
        self.report, self.stopped = report, Event()
        self.before_poll, self.on_pause = before_poll, on_pause

    def stop(self):
        self.stopped.set()

    def run(self, *, seconds=None):
        if seconds is not None and seconds <= 0:
            raise ValueError('positive duration required')
        deadline = monotonic() + seconds if seconds is not None else None
        backoff = self.poll_seconds
        try:
            while not self.stopped.is_set() and (deadline is None or monotonic() < deadline):
                delay = self.poll_seconds
                try:
                    if self.before_poll is not None:
                        self.before_poll()
                    if self.stopped.is_set():
                        break
                    result = self.feed.poll()
                    backoff = self.poll_seconds
                except (DataQualityError, FeedPaused, OSError) as error:
                    self.feed.pause(str(error) if isinstance(error, FeedPaused) else type(error).__name__)
                    if self.on_pause is not None:
                        self.on_pause()
                    result = dict(bars=0, quotes=0)
                    delay = max(backoff, error.retry_after or 0) if isinstance(error, ExchangeError) else backoff
                    backoff = min(self.max_backoff, backoff * 2)
                self.report(json.dumps(dict(status=self.feed.status, reason=self.feed.reason,
                    source=SOURCE, interval=self.feed.interval, watermarks=self.feed.watermarks(), **result)))
                if deadline is not None:
                    delay = min(delay, max(0, deadline - monotonic()))
                self.stopped.wait(delay)
        finally:
            self.feed.pause('STOPPED')


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
