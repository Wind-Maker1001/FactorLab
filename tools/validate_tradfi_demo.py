"""Rebuild V69-F01 market-discovery summaries offline from frozen evidence.

No credentials, network requests, execution-layer changes, or account writes.
"""
import hashlib
import json
from collections import Counter
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'docs/validation/v69-f01'
TARGETS = ('AAPL', 'SNDK', 'BA', 'C', 'JPM', 'GE', 'TQQQ', 'QQQ')


def equity(row):
    return row.get('underlyingType') == 'EQUITY' and 'TradFi' in row.get('underlyingSubType', [])


def build(root=ROOT):
    def read(name):
        return json.loads((root / (name + '.json')).read_text(encoding='utf-8'))

    def save(name, value):
        (root / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')

    manifest = read('evidence_manifest')
    snapshots = {}
    for name, source in manifest['sources'].items():
        raw = (root / source['retained_evidence']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source['retained_sha256']:
            raise ValueError('Retained evidence hash mismatch: ' + name)
        snapshot = json.loads(raw)
        index = snapshot['identity_index']
        if len(index) != source['symbol_count'] or len({r['symbol'] for r in index}) != len(index):
            raise ValueError('Incomplete or duplicate symbol index: ' + name)
        snapshots[name] = snapshot
    demo = snapshots['demo']
    listings = [r for r in demo['raw_selected_symbols'] if equity(r)]
    if {r['symbol'] for r in listings} != {r['symbol'] for r in demo['identity_index'] if equity(r)}:
        raise ValueError('Demo equity rows do not match complete index')
    grouped = {status: [r['symbol'] for r in listings if r['status'] == status]
               for status in sorted({r['status'] for r in listings})}
    markets = read('qqq_market_evidence')
    coverage = {k: bool(markets.get(k)) and not (isinstance(markets[k], dict) and 'error_type' in markets[k])
                for k in ('book', 'depth', 'klines', 'mark', 'funding')}
    mapping = []
    for ticker in TARGETS:
        symbol = ticker + 'USDT'
        rows = {name: next((r for r in snapshot['raw_selected_symbols'] if r['symbol'] == symbol and r.get('baseAsset') == ticker and equity(r)), None)
                for name, snapshot in snapshots.items()}
        status = 'DEMO_LISTED_NOT_EXECUTED' if rows['demo'] else 'MAINNET_ONLY' if rows['mainnet'] else 'NOT_LISTED'
        mapping.append(dict(stock=ticker, candidate_contract=symbol if any(rows.values()) else None,
            identity_status='EXCHANGE_METADATA_ONLY' if any(rows.values()) else 'NO_EQUITY_CONTRACT_FOUND',
            identity_basis='EQUITY/TradFi exchange metadata; issuer, legal underlying and ETF share class not independently confirmed',
            issuer_identity_verified=False, mainnet=rows['mainnet'], demo=rows['demo'], classification=status,
            market_coverage=coverage if rows['demo'] else {k: 'NOT_REQUESTED_NO_DEMO_CONTRACT' for k in coverage},
            execution_status='NOT_EXECUTED', fees=None,
            funding_rate=markets['mark'].get('lastFundingRate') if rows['demo'] and coverage['mark'] else None,
            reason='Listed in Demo; execution acceptance owned by F03 PR #239' if rows['demo'] else
                'No corresponding EQUITY/TradFi contract in complete Demo identity index' if rows['mainnet'] else
                'No corresponding EQUITY/TradFi contract in either complete identity index',
            rejected_collision='CUSDT is COIN, not Citigroup' if ticker == 'C' else None))
    qqq = next(r['demo'] for r in mapping if r['stock'] == 'QQQ')
    filters = {r['filterType']: r for r in qqq['filters']}
    ask = Decimal(markets['book']['askPrice'])
    step = Decimal(filters['LOT_SIZE']['stepSize'])
    quantity = max(Decimal(filters['LOT_SIZE']['minQty']),
                   (Decimal(filters['MIN_NOTIONAL']['notional']) / ask / step).to_integral_value(rounding=ROUND_CEILING) * step)
    save('instrument_mapping', mapping)
    save('demo_equity_tradfi', dict(identity_status='EXCHANGE_METADATA_ONLY', total=len(listings),
        status_counts=dict(Counter(r['status'] for r in listings)), by_status=grouped,
        note='TRADING is an exchange status, not executed acceptance or independent issuer verification.', contracts=listings))
    save('validation', dict(schema_version=2,
        market_discovery=dict(status='COMPLETE', scope='Frozen official listing coverage and QQQ public market acquisition',
            observed_at=markets['observed_at'], listing_acquired_at=None, identity_confirmation='EXCHANGE_METADATA_ONLY',
            issuer_identity_verified=False, classifications={r['stock']: r['classification'] for r in mapping},
            demo_status_counts=dict(Counter(r['status'] for r in listings)), market_coverage=coverage,
            kline_count=len(markets['klines']), funding_history_count=len(markets['funding']) if coverage['funding'] else None,
            qqq_rules=dict(contract_type=qqq['contractType'], margin_asset=qqq['marginAsset'],
                quote_asset=qqq['quoteAsset'], price_precision=qqq['pricePrecision'], quantity_precision=qqq['quantityPrecision'],
                tick_size=filters['PRICE_FILTER']['tickSize'], quantity_step=str(step),
                min_notional_filter=filters['MIN_NOTIONAL']['notional'],
                snapshot_ask=str(ask), indicative_limit_buy_quantity=str(quantity),
                indicative_limit_buy_notional=str(quantity * ask),
                calculation_scope='Historical ask and LOT_SIZE/MIN_NOTIONAL only; not live order admission'),
            evidence_manifest='evidence_manifest.json'),
        trading_execution=dict(status='NOT_EXECUTED', classification='DEMO_LISTED_NOT_EXECUTED',
            owner='F03 PR #239', dependency='https://github.com/Wind-Maker1001/FactorLab/pull/239',
            orders_submitted=0, execution_verified=False, fees=None, leverage_brackets=None,
            account_equity_reconciliation=None, position_reconciliation=None, restart_reconciliation=None,
            remaining=['Generic Demo execution admission (F03)', 'Azure account/order ownership coordination',
                       'Account leverage and current rule checks', 'Buy, fill, reduce-only close and restart reconciliation']),
        review_ready=True, shared_execution_layer_modified=False))
    return mapping, grouped


if __name__ == '__main__':
    mapping, grouped = build()
    print(json.dumps({'classifications': {r['stock']: r['classification'] for r in mapping},
                      'demo_status_counts': {k: len(v) for k, v in grouped.items()}}, indent=2))
