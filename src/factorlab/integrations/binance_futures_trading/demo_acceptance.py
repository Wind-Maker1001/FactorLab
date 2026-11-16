"""Capture GET-only Demo admission evidence without enabling a trading campaign."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .demo_cli import load_bound_credential
from .demo_credentials import DemoCredentialStore
from .demo_preflight import inspect_demo
from .protocol import Environment, SafetyBlocked


def preflight(store):
    """Verify the persisted credential/account binding against signed Demo reads.

    The returned admission remains blocked: account read permission and canTrade
    do not establish API-key write permission or authorize a new experiment.
    """
    binding, credential = load_bound_credential(store)
    if credential.environment is not Environment.DEMO:
        raise SafetyBlocked('Demo credentials required')
    observation = inspect_demo(credential)
    account = observation['checks']['account_id']
    identity_verified = account['eligible'] is True and account['value'] == binding['account_id']
    if account['eligible'] is True and not identity_verified:
        raise SafetyBlocked('persisted Demo account binding mismatch')
    return dict(
        observed_at=datetime.now(timezone.utc),
        source='BINANCE_DEMO_GET_ONLY',
        statuses=(['DEMO_ENVIRONMENT_VERIFIED'] if identity_verified else [])
        + ['BLOCKED_AUTHORIZATION', 'REAL_DEMO_FILL_NOT_VERIFIED'],
        credential_binding_verified=identity_verified,
        execution_admission='BLOCKED',
        blockers=['EXPERIMENT_SPECIFIC_WRITE_AUTHORIZATION_NOT_ESTABLISHED',
                  'API_KEY_TRADE_PERMISSION_NOT_ESTABLISHED', 'REAL_TRADE_RECEIPTS_MISSING'],
        observation=observation,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path,
                        help='new evidence file; existing evidence is never overwritten')
    args = parser.parse_args(argv)
    with args.output.open('x', encoding='utf-8') as stream:
        try:
            result = preflight(DemoCredentialStore())
        except SafetyBlocked as error:
            result = dict(observed_at=datetime.now(timezone.utc), source='LOCAL_ADMISSION',
                          statuses=['BLOCKED_AUTHORIZATION', 'REAL_DEMO_FILL_NOT_VERIFIED'],
                          execution_admission='BLOCKED', reason=str(error), network_writes=0)
        json.dump(result, stream, indent=2, default=str)
        stream.write('\n')
    print(json.dumps(dict(statuses=result['statuses'], execution_admission='BLOCKED')))


if __name__ == '__main__':
    main()
