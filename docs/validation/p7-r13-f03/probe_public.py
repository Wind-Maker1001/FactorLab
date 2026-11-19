"""Run one bounded, unsigned F03 probe with the production Demo feed."""

import argparse
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from factorlab.integrations.binance_futures_trading.demo_market_feed import DemoMarketFeed, FeedPaused, SYMBOL
from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import MarketClient
from factorlab.integrations.binance_futures_trading.demo_supervisor import DemoSupervisor
from factorlab.integrations.binance_futures_trading.protocol import ExchangeError
from factorlab.integrations.binance_futures_trading.forward_observation import (
    BoundedRequests, ObservationFull, ShadowEvidence, collector_source_sha256, instant, utcnow,
)


def run(output, version):
    start = utcnow()
    deadline = start + timedelta(seconds=155)
    identity = 'p7-r13-f03-public-diagnostic-a'
    # The signed experiment used three GETs before stopping.
    maximum = 597
    evidence = ShadowEvidence(output / 'events.csv', version=version, max_bytes=5 * 1024 * 1024 // 3 - 65536)
    if any(row['kind'] == 'experiment' for row in evidence.rows):
        raise ValueError('diagnostic identity already used')
    evidence.append(dict(kind='experiment', started_at=start.isoformat(), deadline=deadline.isoformat(),
        parameters=dict(experiment_id=identity, version=version, seconds=155, sample_seconds=5,
                        max_requests=maximum, max_bytes=5 * 1024 * 1024, max_proposals=20,
                        max_quote_gap_seconds=30, mode='PUBLIC_ONLY',
                        collector_source_sha256=collector_source_sha256())))
    requests = BoundedRequests(evidence, maximum=maximum, deadline=deadline)
    client = MarketClient(requests)
    reason = 'DEADLINE'
    try:
        info = client.request('GET', '/fapi/v1/exchangeInfo', signed=False)
        symbol = next(row for row in info['symbols'] if row['symbol'] == SYMBOL)
        evidence.append(dict(kind='market_status', status=symbol['status'], instrument=symbol))

        class Consumer:
            def observe_bar_receipt(self, bar, raw, received_at, known_at):
                evidence.append(dict(kind='bar_receipt', opened_at=bar.opened_at.isoformat(),
                    closed_at=bar.closed_at.isoformat(), received_at=received_at.isoformat(),
                    known_at=known_at.isoformat(), completed_bar=bar.complete, raw=raw))

            def closed_bar(self, bar):
                if bar.opened_at >= start:
                    evidence.append(dict(kind='public_closed_bar', market_at=bar.closed_at.isoformat()))

            def quote(self, quote):
                pass

            def observe_book(self, raw, received_at):
                previous = evidence.last_market
                evidence.book(raw, received_at, market_status=symbol['status'])
                row = evidence.rows[-1]
                if row['kind'] == 'rejected_book':
                    raise FeedPaused(row['reason'])
                if previous is not None and (
                        instant(row['market_at']) - previous).total_seconds() > 30:
                    raise FeedPaused('QUOTE_GAP')

        with closing(DemoMarketFeed(client, Consumer(), state_path=output / 'cursor.sqlite3',
                                    consumer_id=identity)) as feed:
            def interrupt():
                nonlocal reason
                reason = feed.reason
                evidence.append(dict(kind='interruption', at=utcnow().isoformat(), reason=reason))
                supervisor.stop()

            requests.enable_rounds(max_requests=maximum,
                sink=lambda row: evidence.append(dict(kind='poll_round', **row)))
            supervisor = DemoSupervisor(feed, poll_seconds=5, on_pause=interrupt, request_counts=requests,
                report=lambda _: None, metric_snapshot=lambda: dict(feed_commit=feed.commit_seconds))
            supervisor.run(seconds=(deadline - utcnow()).total_seconds())
    except ExchangeError as error:
        reason = error.category
    except ObservationFull as error:
        reason = str(error)
    finally:
        evidence.append(dict(kind='window_end', at=utcnow().isoformat(), reason=reason), terminal=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    run(args.output, args.version)
