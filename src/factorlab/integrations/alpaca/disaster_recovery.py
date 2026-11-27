"""Recover Alpaca Paper ledgers without evaluating decisions or sending orders."""

from collections import defaultdict
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
from itertools import groupby
import json
import os
import sqlite3

from factorlab.trading.contracts import ZERO
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore

from .adapter import AlpacaAdapter, CORPORATE_ACTIVITY_TYPES, OrderNotFound
from .cloud_runtime import NY, ObservationHTTP, credential_config, now, owner_lock, private_root
from .corporate_actions import CorporateReceiptJournal
from .models import instant
from .portfolio import AlpacaPortfolioAdapter


class RecoveryHTTP(ObservationHTTP):
    calls = 0

    def request(self, method, path, **kwargs):
        if self.calls >= 128:
            raise ValueError('Alpaca disaster recovery exceeded its request budget')
        self.calls += 1
        return super().request(method, path, **kwargs)


class RecoveredAccount(AlpacaPortfolioAdapter):
    """Project the complete GET observations already committed to the raw journal."""

    def __init__(self, adapter, portfolio, snapshot, fills):
        super().__init__(adapter, portfolio, order_not_found=OrderNotFound)
        self.snapshot = snapshot
        self.fills_by_order = defaultdict(list)
        for fill in fills:
            self.fills_by_order[fill['order_id']].append(fill)

    def read_account(self):
        return self.snapshot

    def query_order(self, client_order_id):
        saved = self.adapter.store.order(client_order_id)
        if saved is None or saved[1] is None:
            return self._absent_order(client_order_id)
        if saved[2]:
            raise TimeoutError('original Alpaca order is still uncertain')
        details = self.adapter.stored_order_details(client_order_id)
        fills = self.fills_by_order[details['exchange_order_id']]
        return self._project_order(client_order_id, details, fills)


def _cash_baseline(root):
    uri = (root / 'runtime.sqlite3').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute(
            "SELECT payload FROM daily_reports WHERE json_extract(payload, '$.source')='ALPACA_PAPER_ACCOUNT' "
            "AND json_extract(payload, '$.cash_request_started_at') IS NOT NULL "
            "AND json_extract(payload, '$.cash_observed_at') IS NOT NULL ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
    if row is not None:
        report = json.loads(row[0])
        return (instant(report['cash_request_started_at']), instant(report['cash_observed_at']),
                Decimal(report['cash']))
    return None


def _cash_evidence(adapter, baseline, fills, current_cash):
    started, observed, cash = baseline
    before_observed = _nontrades(adapter, started - timedelta(microseconds=1))
    after_observed = _nontrades(adapter, observed)
    for row in after_observed.values():
        if row['activity_type'] in CORPORATE_ACTIVITY_TYPES:
            cash += Decimal(row['net_amount'])
    # Account activity creation time bounds the query. A settlement within the
    # cash request has no proved ordering relative to the executions in it.
    ambiguous_corporate = any(before_observed[receipt]['activity_type'] in CORPORATE_ACTIVITY_TYPES
                              for receipt in before_observed.keys() - after_observed.keys())
    if ambiguous_corporate:
        return None, 'CASH_AND_CORPORATE_SETTLEMENT_OVERLAP'

    uncertain = []
    for fill in fills:
        at = instant(fill['transaction_time'])
        if at < started:
            continue
        amount = Decimal(fill['qty']) * Decimal(fill['price'])
        delta = amount if fill['side'] == 'sell' else -amount
        if at > observed:
            cash += delta
        else:
            uncertain.append((at, delta))

    # Check each ordered boundary in the request interval. Replaying journaled
    # fills always uses their original identities, independently of this check.
    candidates = [cash]
    for _, group in groupby(sorted(uncertain, reverse=True), key=lambda fill: fill[0]):
        candidates.append(candidates[-1] + sum((delta for _, delta in group), ZERO))
    if candidates.count(current_cash) == 1:
        return '0', None
    if len(candidates) == 1:
        return str(current_cash - candidates[0]), 'CASH_MISMATCH'
    return None, 'CASH_REQUEST_BOUNDARY_UNRESOLVED'


def _retain_activities(adapter, receipts):
    with adapter.store.transaction():
        for receipt in receipts:
            key = 'account-activity:' + receipt['id']
            prior = adapter.store.get(key)
            if prior is None:
                adapter.store.put(key, receipt)
            elif prior != receipt:
                raise ValueError('official account activity identity changed')


def _nontrades(adapter, after):
    receipts = {}
    for row in adapter.read_account_activities(after=after.isoformat()):
        if row['activity_type'] != 'FILL' and receipts.setdefault(row['id'], row) != row:
            raise ValueError('official account activity identity changed during pagination')
    return receipts


def _corporate_replay(adapter, portfolio, executor, account_id, receipts):
    if not receipts:
        return False
    announcements = adapter.http.request('GET', '/v2/corporate_actions/announcements', params={
        'ca_types': 'dividend,merger,spinoff,split', 'since': min(row['date'][:10] for row in receipts),
        'until': now().astimezone(NY).date().isoformat(), 'date_type': 'ex_date',
    })
    if len(announcements) >= 1000:
        raise ValueError('saturated Alpaca corporate-action announcement window')
    journal = CorporateReceiptJournal(portfolio, account_id)
    blocked = journal.reconcile_account(executor, announcements=announcements, receipts=receipts)
    return bool(blocked) or any(portfolio.db.execute(
        'SELECT 1 FROM portfolio_corporate_actions WHERE account=? AND event=? LIMIT 1',
        (account_id, 'alpaca-nta:' + receipt['id'])).fetchone() is None for receipt in receipts)


def _recover_portfolio(root, adapter, snapshot, fills, receipts, reasons, *, equity):
    portfolio_path = root / 'portfolio.sqlite3'
    owned_orders, owned_positions = set(), {}
    unposted_corporate = bool(receipts)
    budget = dict(verified=False)
    if portfolio_path.is_file():
        portfolio = PortfolioStore(portfolio_path)
        with closing(portfolio.db):
            account_id = snapshot.account_id
            bridge = RecoveredAccount(adapter, portfolio, snapshot, fills)
            executor = PortfolioExecutor(bridge, portfolio, quote_currency='USD')
            executor.reconcile(account_id)
            orders = portfolio.account_orders(account_id)
            verified = not any(order['uncertain'] for _, order in orders)
            if not verified:
                reasons.append('PORTFOLIO_ORDER_EVIDENCE_INCOMPLETE')
            owned_orders = {order['record'].snapshot.intent.client_order_id for _, order in orders}
            unposted_corporate = _corporate_replay(adapter, portfolio, executor, account_id, receipts)
            owned_positions = portfolio.holdings(account_id)
            budget = dict(verified=verified, journaled_orders=len(orders),
                          filled_notional=sum((order.get('filled_notional', ZERO) for _, order in orders), ZERO))
            if portfolio.capital_policy.is_configured(account_id):
                risk = portfolio.capital_policy.risk_snapshot(executor, account_id, official_equity=equity)
                budget.update(verified=verified and risk.reconciled,
                              configured_budget=risk.budget, allocated=risk.allocated)
    return owned_orders, owned_positions, unposted_corporate, budget


def recover_paper(root, http, *, since):
    """Advance the local journals from official facts and report any admission blockers."""
    cash_baseline = _cash_baseline(root)
    if cash_baseline is not None:
        since = min(since, cash_baseline[0])
    floor = since - timedelta(seconds=1)
    path = root / 'account.sqlite3'
    if not path.is_file():
        raise ValueError('restored Alpaca account journal required')
    reasons = []
    with closing(AlpacaAdapter(http, path)) as adapter:
        if adapter.store.get('account_id') is None:
            raise ValueError('restored Alpaca account binding required')
        snapshot = adapter.recover(after=floor.isoformat())
        nontrades = _nontrades(adapter, floor)
        _retain_activities(adapter, nontrades.values())
        if not snapshot.reconciled:
            return {'reconciled': False, 'blockers': ['ORDER_OR_FILL_EVIDENCE_INCOMPLETE'],
                    'nontrade_receipts': len(nontrades)}
        fills = adapter.store.activities()
        facts = adapter.read_account_details()
        corporate = tuple(row for row in nontrades.values() if row['activity_type'] in CORPORATE_ACTIVITY_TYPES)
        owned_orders, owned_positions, unposted_corporate, budget = _recover_portfolio(
            root, adapter, snapshot, fills, corporate, reasons, equity=facts.equity)
        if unposted_corporate:
            reasons.append('CORPORATE_OWNERSHIP_UNRESOLVED')
        if len(corporate) != len(nontrades):
            reasons.append('ACCOUNT_ACTIVITY_OWNERSHIP_UNRESOLVED')

        late_executions = {row['order_id'] for row in fills if instant(row['transaction_time']) >= since}
        unknown_orders = {order.intent.client_order_id for order in snapshot.open_orders} - owned_orders
        for identity, (_, row, _, _) in adapter.store.orders():
            if row is not None and identity not in owned_orders:
                if row['id'] in late_executions or instant(row['submitted_at'] or row['created_at']) >= since:
                    unknown_orders.add(identity)
        if unknown_orders:
            reasons.append('ORDER_OWNERSHIP_UNRESOLVED')
        actual_positions = {p.instrument_id: p.quantity for p in snapshot.positions if p.quantity}
        if actual_positions != {symbol: qty for symbol, qty in owned_positions.items() if qty}:
            reasons.append('INVENTORY_MISMATCH')

        if facts.cash != snapshot.assets[0].total:
            reasons.append('ACCOUNT_CASH_CHANGED_DURING_RECOVERY')
        if facts.account_blocked or facts.trading_blocked or facts.status != 'ACTIVE':
            reasons.append('BROKER_ACCOUNT_BLOCKED')
        cash_residual = None
        if cash_baseline is None:
            reasons.append('OFFICIAL_CASH_ANCHOR_MISSING')
        else:
            cash_residual, blocker = _cash_evidence(adapter, cash_baseline, fills, facts.cash)
            if blocker:
                reasons.append(blocker)
        return {'reconciled': not reasons, 'blockers': reasons, 'since': since.isoformat(),
                'fill_count': len(fills), 'unowned_orders': len(unknown_orders),
                'nontrade_receipts': len(nontrades), 'cash_residual': cash_residual, 'budget': budget}


def catch_up(root, *, since):
    root = private_root(root)
    with owner_lock(root, allow_quarantined=True):
        marker = json.loads((root / 'restore-quarantine.json').read_text(encoding='utf-8'))
        if marker['reason'] != 'restored-state-requires-GET-only-recovery':
            raise ValueError('Alpaca disaster recovery requires a quarantined restoration')
        http = RecoveryHTTP(credential_config(os.environ['FACTORLAB_ALPACA_SECRET_FILE']))
        evidence = recover_paper(root, http, since=since)
        return dict(evidence, api_requests=http.calls)
