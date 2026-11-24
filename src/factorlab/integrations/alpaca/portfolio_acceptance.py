"""Small, explicitly synthetic Paper portfolio acceptance driven by trade updates.

Run open, recover, rotate and cleanup in separate processes against one private
state directory. These fixed test targets are not v69 (or any strategy) signals.
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo
import os

from factorlab.trading.control import RiskLimits
from factorlab.trading.portfolio_execution import InstrumentRules, PortfolioStore
from factorlab.trading.storage import decode, dumps
from .portfolio import AlpacaPortfolioRunner, portfolio_quote

SYMBOLS = ('AAPL', 'QQQ')
WEIGHTS = {
    'open': {'AAPL': D('.00010'), 'QQQ': D('.00010')},
    'rotate': {'AAPL': D('.00005'), 'QQQ': D('.00015')},
    'cleanup': {'AAPL': D(0), 'QQQ': D(0)},
}


def run_acceptance(*, state_dir, phase, credentials_file):
    """Perform one test phase; wait for push events, never poll orders for fills."""
    from .adapter import AlpacaAdapter, OrderNotFound
    from .http import AlpacaConfig, AlpacaError, PaperHTTP
    from .market_data.client import Credentials, MarketDataClient
    from .market_data.inputs import Instrument
    from .market_data.service import MarketDataService
    from .market_data.sessions import AlpacaSessionCalendar
    from .stream import trade_updates

    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    credentials = Credentials.from_dpapi(credentials_file)
    http = PaperHTTP(AlpacaConfig(credentials.key, credentials.secret))
    adapter = AlpacaAdapter(http, root / 'broker.sqlite', limits=RiskLimits(
        max_order_notional=D(25), max_instrument_exposure=D(50),
        account_budget=D(100), orders_per_minute=30))
    client = MarketDataClient(credentials, feed='iex', attempts=1)
    store = PortfolioStore(root / 'portfolio.sqlite')
    runner = AlpacaPortfolioRunner(adapter, store, order_not_found=OrderNotFound, rejection_error=AlpacaError)
    try:
        initial = adapter.recover()
        facts = adapter.read_account_details()
        clock, received = client.clock()
        if not clock['is_open']:
            raise RuntimeError('official Paper market clock is closed')
        session = received.astimezone(ZoneInfo('America/New_York')).date().isoformat()
        rows, _ = client.calendar(session, session)
        calendar = AlpacaSessionCalendar(rows, coverage_start=session, coverage_end=session,
                                        version='ALPACA:v2/calendar')
        baseline_key = 'ALP-E03 TEST:baseline'
        with store.transaction():
            if store.event_payload(baseline_key) is None:
                if any(p.instrument_id in SYMBOLS and p.quantity for p in initial.positions):
                    raise ValueError('test symbols already contain non-test inventory')
                store.record_metadata(baseline_key, dict(cash=facts.cash, equity=facts.equity,
                    positions={p.instrument_id: p.quantity for p in initial.positions},
                    captured_at=facts.observed_at, process=os.getpid()))
        if phase in {'recover', 'replay'}:
            active = store.event_payload('ALP-E03 TEST:open-id')
            if active is None:
                raise ValueError('open phase must be persisted before recovery')
            identity = decode(json.loads(active))
            previous = store.event_payload('ALP-E03 TEST:open-process')
            if previous is not None and decode(json.loads(previous)) == os.getpid():
                raise ValueError('restart evidence requires a different process')
        else:
            if phase == 'rotate' and store.event_payload('ALP-E03 TEST:recover-complete') is None:
                raise ValueError('fresh-process recovery must complete before rotation')
            if phase == 'cleanup':
                for row in store.db.execute(
                        'SELECT id FROM portfolio_plans WHERE account=? AND strategy=?',
                        (facts.account_id, 'ALP-E03 TEST')).fetchall():
                    prior = store.plan(row[0])
                    if prior['phase'] == 'COMPLETE':
                        continue
                    runner.executor.abandon(prior['id'])
                    adapter.recover()
            identity = runner.executor.create(account_id=facts.account_id,
                strategy_id='ALP-E03 TEST', strategy_version='synthetic-1', trading_session=session,
                rebalance_id='ALP-E03 TEST:' + phase, weights=WEIGHTS[phase],
                owned_symbols=SYMBOLS, now=received, initial_capital=D(100000))
            if phase == 'open':
                with store.transaction():
                    store.record_metadata('ALP-E03 TEST:open-id', identity)
                    if store.event_payload('ALP-E03 TEST:open-process') is None:
                        store.record_metadata('ALP-E03 TEST:open-process', os.getpid())
        rule_map = {}
        for symbol in SYMBOLS:
            asset = adapter.read_asset(symbol)
            if not asset['tradable'] or not asset['fractionable']:
                raise ValueError('acceptance requires tradable fractional-share test assets')
            rule_map[symbol] = InstrumentRules(D('.000000001'), D(1), time_in_force='DAY',
                price_tick=D('.01'), limit_price_tolerance=D('.001'))
        started = datetime.now(timezone.utc)
        invocations = []

        market_data = MarketDataService(client, None, calendar)
        instruments = tuple(Instrument('ALPACA:' + symbol, symbol, started) for symbol in SYMBOLS)

        def fresh_quotes():
            window = (started - timedelta(minutes=2), started + timedelta(minutes=4))
            observed = market_data.refresh_quotes(instruments, decision_at=started,
                decision_window=window, max_age=timedelta(seconds=5))
            return {instrument.symbol: portfolio_quote(observed[instrument.instrument_id],
                        instrument_id=instrument.instrument_id, symbol=instrument.symbol)
                    for instrument in instruments if observed[instrument.instrument_id] is not None}

        def update():
            now = datetime.now(timezone.utc)
            if now - started > timedelta(minutes=4):
                raise TimeoutError('Paper test phase exceeded its event-driven execution window')
            result = runner.run_once(identity, quotes=fresh_quotes, rules=rule_map)
            invocations.append(dict(phase=result.phase, reasons=dict(result.reasons),
                                    orders=list(result.order_ids), observed_at=now.isoformat()))
            if result.phase == 'BLOCKED':
                raise RuntimeError('test phase blocked: ' + json.dumps(dict(result.reasons)))
            if result.phase == 'COMPLETE':
                if result.reasons:
                    raise RuntimeError('test phase has unexecuted allocation: ' + json.dumps(dict(result.reasons)))
                return True
            return False

        before_orders = tuple(row[0] for row in store.db.execute('SELECT id FROM portfolio_orders ORDER BY id'))
        before_positions = {p.instrument_id: p.quantity for p in initial.positions}
        if phase == 'replay':
            if store.event_payload('ALP-E03 TEST:rotate-complete') is None:
                raise ValueError('completed rotation required before historical replay')
            if not update():
                raise ValueError('historical completed plan did not remain complete')
            after_orders = tuple(row[0] for row in store.db.execute('SELECT id FROM portfolio_orders ORDER BY id'))
            after_positions = {p.instrument_id: p.quantity for p in adapter.read_account().positions}
            if after_orders != before_orders or after_positions != before_positions:
                raise ValueError('historical replay changed orders or successor holdings')
        else:
            from websockets.exceptions import ConnectionClosed
            try:
                trade_updates(adapter, on_update=update)
            except (TimeoutError, ConnectionClosed):
                # Continue this durable identity using official REST facts after
                # the stream has reconciled its disconnect boundary.
                if not update():
                    raise RuntimeError('REST recovery left original phase pending; resume the same phase and state directory')
        report = runner.report(identity)
        final = adapter.read_account()
        baseline = decode(json.loads(store.event_payload(baseline_key)))
        net_cash = sum((fill.notional * (D(-1) if fill.side.value == 'BUY' else D(1))
                        - fill.fee for fill in report.fills), D(0))
        expected_cash = baseline['cash'] + net_cash
        reconciliation = dict(official_cash=report.official_cash, expected_cash=expected_cash,
            cash_difference=report.official_cash - expected_cash, cash_tolerance=D('.01'),
            official_positions={p.instrument_id: p.quantity for p in final.positions},
            owned_positions={row.instrument_id: row.owned_quantity for row in report.positions},
            cleaned=(phase == 'cleanup' and all(row.owned_quantity == 0 for row in report.positions)),
            restored_in_new_process=(phase in {'recover', 'replay'}),
            historical_replay_unchanged=(phase == 'replay'),
            order_count_before=len(before_orders),
            order_count_after=store.db.execute('SELECT COUNT(*) FROM portfolio_orders').fetchone()[0])
        # Official cash is exposed in cents; fractional fills retain additional
        # precision. Preserve the difference rather than changing either ledger.
        if abs(reconciliation['cash_difference']) > reconciliation['cash_tolerance']:
            raise ValueError('official cash does not reconcile within its one-cent reporting precision')
        if phase == 'cleanup' and not reconciliation['cleaned']:
            raise ValueError('test-owned holdings remain after cleanup')
        owned = reconciliation['owned_positions']
        if phase in {'open', 'recover', 'rotate', 'replay'} and not all(owned.get(s, D(0)) > 0 for s in SYMBOLS):
            raise ValueError('official simultaneous two-instrument holdings required')
        if phase == 'rotate' and not (owned['AAPL'] < before_positions['AAPL']
                                      and owned['QQQ'] > before_positions['QQQ']):
            raise ValueError('official rotation must reduce AAPL and increase QQQ')
        order_facts = []
        for order in store.orders(identity):
            record = order['record']
            intent = record.snapshot.intent
            order_facts.append(dict(client_order_id=intent.client_order_id,
                symbol=intent.instrument_id, side=intent.side.value,
                quantity=intent.quantity, limit_price=intent.limit_price,
                status=record.snapshot.status.value,
                filled_quantity=record.snapshot.cumulative_filled_quantity,
                observed_at=record.observed_at))
        evidence = dict(purpose='ALP-E03 TEST: synthetic targets, not a strategy signal',
            phase=phase, account_fingerprint=sha256(facts.account_id.encode()).hexdigest()[:12],
            rebalance_id=identity, process=os.getpid(), invocations=invocations,
            orders=order_facts, limit_price_tolerance=D('.001'),
            report=asdict(report), reconciliation=reconciliation,
            captured_at=datetime.now(timezone.utc))
        # Decimal values retain their full exact text in the evidence artifact.
        artifact = root / ('evidence-' + phase + '.json')
        artifact.write_text(dumps(evidence) + '\n', encoding='utf-8')
        with store.transaction():
            store.record_metadata('ALP-E03 TEST:' + phase + '-complete', dict(
                identity=identity, process=os.getpid(), evidence=artifact.name))
        return dict(phase=phase, complete=True, evidence=str(artifact),
                    official_equity=str(report.official_equity), cash=str(report.official_cash),
                    cash_difference=str(reconciliation['cash_difference']),
                    owned_positions={row.instrument_id: str(row.owned_quantity) for row in report.positions})
    finally:
        store.close()
        adapter.store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--phase', required=True, choices=('open', 'recover', 'rotate', 'replay', 'cleanup'))
    parser.add_argument('--credentials-file', required=True)
    args = parser.parse_args()
    print(json.dumps(run_acceptance(state_dir=args.state_dir, phase=args.phase,
                                   credentials_file=args.credentials_file)))


if __name__ == '__main__':
    main()
