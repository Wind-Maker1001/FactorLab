"""Publish scheduled targets, never orders, to the account owner's mailbox."""
from datetime import timedelta
from decimal import Decimal

import pandas as pd

from .tradfi_schedule import utc_instant
from .tradfi_tasks import PortfolioTask, TEST_ALLOCATION, TEST_SYMBOLS


def dispatch(mailbox, config, window, quotes, now, *, acceptance_task_id=None):
    now = utc_instant(now)
    revision = config['revision']
    if not isinstance(revision, str) or not revision:
        raise ValueError('explicit target configuration revision required')
    task_id = acceptance_task_id or f'{TEST_ALLOCATION}:{window.session}:{revision}'
    previous = mailbox.status(task_id)
    if previous is not None:
        return {'task_id': task_id, 'status': previous}
    if acceptance_task_id is None and not window.opens_at <= now < window.closes_at:
        return {'task_id': task_id, 'status': 'WAITING_SESSION'}
    if not TEST_SYMBOLS <= set(quotes):
        return {'task_id': task_id, 'status': 'WAITING_QUOTES'}
    observations = [pd.Timestamp(quotes[s]['time'], unit='ms', tz='UTC') for s in sorted(TEST_SYMBOLS)]
    for observed in observations:
        if not 0 <= (now - observed).total_seconds() <= 5:
            return {'task_id': task_id, 'status': 'WAITING_FRESH_QUOTES'}
        if acceptance_task_id is None and not window.accepts(observed, now):
            return {'task_id': task_id, 'status': 'WAITING_SESSION_QUOTES'}
    expiry = now + timedelta(seconds=60)
    if acceptance_task_id is None:
        expiry = min(expiry, window.closes_at)
    session = window.session if acceptance_task_id is None else str(now.tz_convert('America/New_York').date())
    task = PortfolioTask(task_id, TEST_ALLOCATION, session,
                         max(observations).isoformat(), expiry.isoformat(),
                         Decimal(config['equity_usdt']),
                         {s: Decimal(v) for s, v in config['weights'].items()},
                         'SCHEDULED' if acceptance_task_id is None else 'ACCEPTANCE')
    return {'task_id': task_id, 'status': mailbox.submit(task)}
