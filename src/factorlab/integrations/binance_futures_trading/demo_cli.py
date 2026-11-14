"""Manage bounded Binance USD-M Demo execution for the current Windows user."""

import argparse
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
import json
import sqlite3
import subprocess
import sys
from uuid import UUID, uuid4

from factorlab.errors import DataQualityError
from factorlab.trading import Side
from factorlab.trading.contracts import OrderStatus, TERMINAL
from factorlab.trading.control import RiskRejected
from factorlab.trading.storage import PaperStore

from .demo_credentials import DemoCredentialStore, DemoCredentialStoreError
from .demo_runtime import (
    DEMO_LIMITS, MAX_QUANTITY, SMOKE_CAMPAIGN, DemoRuntime, bind_account, journal_id, now, operator_lock, private_binding,
    state_root,
)
from .demo_session import DEMO, SYMBOL, DemoSession, demo_account_configuration, observe_demo_account
from .preflight import demo_credential_dialog
from .protocol import Credentials, SafetyBlocked


OPERATING_ERRORS = (SafetyBlocked, DataQualityError, RiskRejected, TimeoutError)


def configure(store, requests, *, replace=False):
    credential = None if replace else store.read()
    if credential is None and not replace:
        try:
            credential = Credentials.from_env(DEMO)
        except ValueError:
            pass
    if credential is None:
        credential = demo_credential_dialog()
    if credential is None:
        raise SafetyBlocked('local Demo credential entry cancelled')
    facts, _ = observe_demo_account(credential, request_counts=requests)
    configuration = demo_account_configuration(facts)
    bind_account(store, credential, facts, configuration)
    return dict(status='DEMO_CONFIGURED', configuration=configuration, can_trade=facts.can_trade,
                credential_reference=store.reference)


@contextmanager
def open_session(root, store, runtime, requests, *, campaign=None):
    binding = private_binding(store)
    credential = store.read()
    if credential is None:
        raise SafetyBlocked('saved Demo credential missing; run setup')
    authorization = runtime.authority(binding, campaign) if campaign is not None else None
    expected = None

    def guard(method, params, plan):
        runtime.reserve(campaign, method, params, plan, expected=expected, facts=session.adapter.facts)

    def expect(client_id, side, quantity):
        nonlocal expected
        expected = (client_id, side.value, quantity, 'true' if side is Side.SELL else 'false')

    def record(receipt):
        runtime.record_receipt(campaign, receipt)

    journal = root / journal_id(binding['account_id'])
    journal.mkdir(exist_ok=True)
    with (closing(PaperStore(journal / 'protocol.sqlite3')) as protocol_store,
          closing(PaperStore(journal / 'controller.sqlite3')) as controller_store):
        session = DemoSession(credential, protocol_store=protocol_store, controller_store=controller_store,
            account_id=binding['account_id'], configuration=binding['configuration'], authorization=authorization,
            limits=DEMO_LIMITS, write_guard=guard if campaign is not None else None,
            receipt_sink=record if campaign is not None else None, request_counts=requests)
        yield session, expect


def recover(session):
    result = session.recover()
    if not result['reconciled'] or result['unknown']:
        raise SafetyBlocked('Demo state requires GET reconciliation')
    return result


def run_operation(session, operation):
    try:
        return operation()
    except OPERATING_ERRORS as error:
        return dict(status='DEMO_BLOCKED', reason=str(error), account=session.status())


def equity(report):
    funds = report['balances']
    return funds['walletBalance'] + funds['unrealizedProfit']


def submit(session, expect, client_id, side, quantity):
    expect(client_id, side, quantity)
    session.submit_limit(client_id, side, quantity, reduce_only=side is Side.SELL)
    account = recover(session)
    if session.receipt_error:
        raise SafetyBlocked('Demo write receipt persistence failed')
    return account


def smoke_lifecycle(session, expect, campaign):
    entry_id = campaign['prefix'] + '-01'
    exit_id = campaign['prefix'] + '-02'

    def settle(client_id, report):
        snapshot = session.adapter.order(client_id)
        if snapshot.status not in TERMINAL:
            session.cancel(client_id)
            report = recover(session)
            snapshot = session.adapter.order(client_id)
        if session.receipt_error:
            raise SafetyBlocked('Demo write receipt persistence failed')
        if report['open_orders'] or snapshot.status not in TERMINAL:
            raise SafetyBlocked('Demo order remains active after its single cancellation opportunity')
        return snapshot, report

    starting = recover(session)
    if starting['open_orders'] or starting['positions']:
        raise SafetyBlocked('the one-time Demo test requires an initially flat account with no open orders')
    report = submit(session, expect, entry_id, Side.BUY, MAX_QUANTITY)
    if session.write_uncertain:
        raise SafetyBlocked('Demo entry response was uncertain; stopped after GET recovery')
    entry, report = settle(entry_id, report)
    if entry.status is OrderStatus.REJECTED:
        return dict(status='DEMO_ORDER_REJECTED_DIAGNOSED', account=report)
    if not entry.cumulative_filled_quantity:
        return dict(status='DEMO_ORDER_LIFECYCLE_ACCEPTED', account=report)

    quantity = entry.cumulative_filled_quantity
    positions = report['positions']
    if len(positions) != 1 or (positions[0]['symbol'], positions[0]['quantity']) != (SYMBOL, quantity):
        raise SafetyBlocked('confirmed entry fill does not match the current Demo position')
    report = submit(session, expect, exit_id, Side.SELL, quantity)
    if session.write_uncertain:
        raise SafetyBlocked('Demo reduction response was uncertain; stopped after GET recovery')
    exit_order, report = settle(exit_id, report)
    if exit_order.status is OrderStatus.REJECTED:
        return dict(status='DEMO_ORDER_REJECTED_DIAGNOSED', account=report)
    return dict(status='DEMO_FILL_RECONCILED', account=report)


def run_command(args, root, store, runtime, requests, smoke_id):
    command = args.command
    if command == 'setup' or command == 'credentials-update':
        return configure(store, requests, replace=command == 'credentials-update')
    if command == 'credentials-delete':
        runtime.stop()
        store.delete()
        store.delete_config()
        return dict(status='DEMO_CREDENTIALS_DELETED')
    if command in {'status', 'recover', 'enable-demo', 'smoke-test'}:
        started_at = now()
        stop_epoch = runtime.stop_epoch()
        if command == 'smoke-test' and runtime.campaign(smoke_id):
            raise SafetyBlocked('the one-time Demo smoke budget was already started; use recover')
        with open_session(root, store, runtime, requests) as (session, _):
            result = run_operation(session, lambda: dict(status='DEMO_RECONCILED', account=recover(session)))
        if command in {'status', 'recover'} or result['status'] == 'DEMO_BLOCKED':
            return result
        binding = private_binding(store)
        smoke = command == 'smoke-test'
        if smoke and (result['account']['positions'] or result['account']['open_orders']):
            raise SafetyBlocked('the one-time Demo test requires an initially flat account with no open orders')
        if not result['account']['can_trade']:
            raise SafetyBlocked('Demo account canTrade is unavailable or false')
        campaign = runtime.start(binding, seconds=900 if smoke else args.seconds, orders=2 if smoke else args.orders,
            opening_equity=equity(result['account']), max_loss=Decimal('1') if smoke else args.max_loss,
            stop_epoch=stop_epoch, started_at=started_at if smoke else now(), smoke_id=smoke_id if smoke else None)
        if not smoke:
            return dict(status='DEMO_ENABLED', campaign=campaign['id'])
        try:
            with open_session(root, store, runtime, requests, campaign=campaign) as (session, expect):
                result = run_operation(session, lambda: smoke_lifecycle(session, expect, campaign))
                result['write_receipts'] = session.receipts
                return result
        finally:
            runtime.stop()

    campaign = runtime.active_campaign()
    if campaign is None or campaign['smoke']:
        raise SafetyBlocked('no persistent Demo authorization; run enable-demo')
    with open_session(root, store, runtime, requests, campaign=campaign) as (session, expect):
        def operate():
            recover(session)
            if command == 'cancel':
                session.cancel(args.client_order_id)
                return dict(status='DEMO_RECONCILED', account=recover(session))
            side = Side.BUY if command == 'buy-limit' else Side.SELL
            if not Decimal(0) < args.quantity <= MAX_QUANTITY:
                raise SafetyBlocked('Demo order quantity must be positive and at most 0.04')
            client_id = campaign['prefix'] + '-' + uuid4().hex[:12]
            account = submit(session, expect, client_id, side, args.quantity)
            if account['orders'][client_id]['status'] == OrderStatus.REJECTED:
                runtime.stop()
                return dict(status='DEMO_ORDER_REJECTED_DIAGNOSED', account=account)
            return dict(status='DEMO_ORDER_ACCEPTED', account=account)

        result = run_operation(session, operate)
        if (session.receipt_error or session.write_uncertain or any(receipt['http_status'] == 400 for receipt in session.receipts)
                or result['status'] == 'DEMO_BLOCKED' and result['account']['unknown']):
            runtime.stop()
        result['write_receipts'] = session.receipts
        return result


def parse_decimal(value):
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError('expected a finite decimal') from None
    if not parsed.is_finite():
        raise argparse.ArgumentTypeError('expected a finite decimal')
    return parsed


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('setup', 'credentials-update', 'credentials-delete', 'status', 'recover'):
        commands.add_parser(name)
    smoke = commands.add_parser('smoke-test')
    smoke.add_argument('--approval-id', type=UUID,
        help='record a separately approved one-time Demo test under this unique UUID')
    enable = commands.add_parser('enable-demo', help='enable a persistent, expiring Demo order budget')
    enable.add_argument('--seconds', type=int, required=True)
    enable.add_argument('--orders', type=int, required=True)
    enable.add_argument('--max-loss', type=parse_decimal, required=True)
    commands.add_parser('stop', aliases=['disable-demo', 'kill'])
    for name in ('buy-limit', 'reduce-limit'):
        commands.add_parser(name).add_argument('quantity', type=parse_decimal)
    commands.add_parser('cancel').add_argument('client_order_id')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    smoke_id = 'smoke-' + args.approval_id.hex if args.command == 'smoke-test' and args.approval_id else SMOKE_CAMPAIGN
    requests = dict(GET=0, POST=0, DELETE=0)
    result = dict(status='DEMO_BLOCKED')
    restart_smoke = False
    try:
        root = state_root()
        with closing(DemoRuntime(root)) as runtime:
            if args.command in {'stop', 'disable-demo', 'kill'}:
                runtime.stop()
                result = dict(status='DEMO_STOPPED')
            else:
                with operator_lock(root):
                    before = runtime.campaign(smoke_id)
                    try:
                        result = run_command(args, root, DemoCredentialStore(), runtime, requests, smoke_id)
                    finally:
                        restart_smoke = args.command == 'smoke-test' and before is None and runtime.campaign(smoke_id) is not None
            result['runtime'] = runtime.report(smoke_id)
            result['smoke_receipts'] = runtime.receipts(smoke_id)
    except (DemoCredentialStoreError, *OPERATING_ERRORS) as error:
        result['reason'] = str(error)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        result['reason'] = f'Demo command failed ({type(error).__name__})'

    if restart_smoke:
        # Reopen the same current-user credential and journals with GET-only recovery in a fresh process.
        run = subprocess.run([sys.executable, '-m', 'factorlab.integrations.binance_futures_trading.demo_cli', 'recover'],
                             capture_output=True, text=True, encoding='utf-8', check=False)
        if run.stdout:
            result['restart_recovery'] = json.loads(run.stdout)
            result['restart_recovery'].pop('smoke_receipts', None)
            for method, count in result['restart_recovery']['requests'].items():
                requests[method] += count
        else:
            result['restart_recovery'] = dict(status='DEMO_BLOCKED', reason='independent recovery process failed')
    result['requests'] = requests
    result['safety'] = 'NO LIVE MONEY'
    print(json.dumps(result, indent=2, default=str))
    return int(result['status'] == 'DEMO_BLOCKED')


if __name__ == '__main__':
    sys.exit(main())
