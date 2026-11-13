"""Capture one independent GET-only Demo exact-order query against a private plan."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from factorlab.integrations.binance_futures_trading.network import DemoExecutionPlan
from factorlab.integrations.binance_futures_trading.order_evidence import exact_demo_order
from factorlab.integrations.binance_futures_trading.preflight import demo_credential_dialog
from factorlab.integrations.binance_futures_trading.protocol import (
    Environment, FuturesClient, ReadOnlyNetworkTransport,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-plan', required=True, type=Path)
    parser.add_argument('--private-output', required=True, type=Path)
    args = parser.parse_args()
    plan = DemoExecutionPlan.from_json(args.private_plan.read_text(encoding='utf-8'))
    if plan.quote_policy is None:
        parser.error('bounded policy with exact client ID required')
    # Reserve a new destination before any request; never overwrite prior evidence.
    with args.private_output.open('x', encoding='utf-8') as stream:
        credentials = demo_credential_dialog()
        if credentials is None:
            json.dump({'status': 'CANCELLED', 'GET': 0, 'POST': 0, 'DELETE': 0}, stream)
            return
        binding = 'DEMO:FactorLab-Demo:' + hashlib.sha256(credentials.key.encode()).hexdigest()
        if binding != plan.account_id:
            raise ValueError('full credential account binding mismatch')
        client = FuturesClient(credentials, environment=Environment.DEMO,
            transport=ReadOnlyNetworkTransport(environment=Environment.DEMO, permit_account_read=True))
        client.sync_time()
        evidence = exact_demo_order(client, plan.quote_policy.client_order_id)
        result = dict(observed_at=datetime.now(timezone.utc).isoformat(), policy_digest=plan.quote_policy.digest,
                      plan_digest=plan.digest, exact_order=evidence, GET=2, POST=0, DELETE=0,
                      account_configuration_writes=0, online_acceptance=False)
        json.dump(result, stream, indent=2)
        print(json.dumps({'state': evidence['state'], 'GET': 2, 'POST': 0, 'DELETE': 0}))


if __name__ == '__main__':
    main()
