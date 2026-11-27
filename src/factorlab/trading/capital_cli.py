"""Operator commands submitted to the running account owner through its journal."""
from argparse import ArgumentParser
from contextlib import closing
from dataclasses import asdict, is_dataclass
from decimal import Decimal
import json
from pathlib import Path

from factorlab.errors import DataQualityError

from .portfolio_execution import PortfolioStore
from .storage import decode, dumps


def enqueue(store, account_id, command_id, request):
    with store.transaction():
        if not store.capital_policy.is_configured(account_id):
            raise ValueError('configured account required in this portfolio database')
        row = store.db.execute(
            'SELECT request FROM capital_commands WHERE account=? AND command=?',
            (account_id, command_id)).fetchone()
        if row is None:
            store.db.execute('INSERT INTO capital_commands VALUES (?,?,?,NULL)',
                             (account_id, command_id, dumps(request)))
        elif decode(json.loads(row[0])) != request:
            raise ValueError('capital command identity conflicts with original request')


def command_result(store, account_id, command_id):
    row = store.db.execute('SELECT result FROM capital_commands WHERE account=? AND command=?',
                           (account_id, command_id)).fetchone()
    if row is None:
        raise KeyError(command_id)
    return dict(status='PENDING') if row[0] is None else decode(json.loads(row[0]))


def process_command(manager, account_id, *, operating_errors=()):
    """Process one queued operation on the existing account owner's turn."""
    db = manager.store.db
    row = db.execute('SELECT command,request FROM capital_commands WHERE account=? AND result IS NULL '
                     'ORDER BY rowid LIMIT 1', (account_id,)).fetchone()
    if row is None:
        return
    command, payload = row
    request = decode(json.loads(payload))
    action = request.pop('action')
    try:
        if action not in {'snapshot', 'history', 'change', 'advance', 'release', 'performance'}:
            raise ValueError('unsupported capital command')
        if action in {'snapshot', 'history'}:
            data = getattr(manager, action)(account_id)
        else:
            data = getattr(manager, action)(account_id=account_id, **request)
    except (ValueError, KeyError, DataQualityError, PermissionError, TimeoutError, ConnectionError,
            *operating_errors) as error:
        result = dict(status='REJECTED', reason=str(error))
    else:
        if action == 'history':
            data = [dict(entry, result=asdict(entry['result'])) for entry in data]
        if action == 'performance':
            data['returns'] = asdict(data['returns'])
        result = dict(status='COMPLETE', data=asdict(data) if is_dataclass(data) else data)
    with manager.store.transaction():
        db.execute('UPDATE capital_commands SET result=? WHERE account=? AND command=?',
                   (dumps(result), account_id, command))


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path, help='existing portfolio SQLite database')
    parser.add_argument('account', help='bound simulation account identity')
    parser.add_argument('command', help='unique command identity')
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('result', 'snapshot', 'history'):
        commands.add_parser(action)
    change = commands.add_parser('change')
    change.add_argument('event_id')
    change.add_argument('amount', type=Decimal)
    change.add_argument('--source')
    change.add_argument('--destination')
    change.add_argument('--mode', choices=['EXACT', 'STAGED'], default='EXACT')
    advance = commands.add_parser('advance')
    advance.add_argument('event_id')
    advance.add_argument('step_id')
    advance.add_argument('--reduction-plan')
    release = commands.add_parser('release')
    release.add_argument('event_id')
    release.add_argument('strategy_id')
    performance = commands.add_parser('performance')
    performance.add_argument('strategy_id')
    values = vars(parser.parse_args(argv))
    database, account, command = (values.pop(key) for key in ('database', 'account', 'command'))
    if not database.is_file():
        parser.error('existing portfolio database required')
    with closing(PortfolioStore(database)) as store:
        if values['action'] != 'result':
            enqueue(store, account, command, values)
        print(json.dumps(command_result(store, account, command), ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
