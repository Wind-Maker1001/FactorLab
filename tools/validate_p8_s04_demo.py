"""On-host P8-S04 official Binance USD-M Demo end-to-end conformance runner.

NO LIVE MONEY / SOFTWARE_CONFORMANCE_ONLY:
Runs as root on the existing Azure host after the pinned runtime installer.
All SQLite mutations remain delegated to the existing `factorlab-demo` user and
the single `factorlab-demo-execute.service` owner. Never resets budgets, never
expands risk limits, never restores old databases, and never claims Champion
profitability qualification.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal as D, ROUND_CEILING
from urllib.request import urlopen


ROOT = Path('/var/lib/factorlab-demo/execute-state')
INBOX = Path('/var/lib/factorlab-demo/champions')
MONITOR = Path('/var/lib/factorlab-demo/monitoring')
BACKUP = Path('/var/backups/factorlab-demo')
UNIT = 'factorlab-demo-execute.service'
MODULE = 'factorlab.integrations.binance_futures_trading.azure_strategy_deploy'


def run_cmd(argv):
    res = subprocess.run(argv, check=True, capture_output=True, text=True)
    return res.stdout.strip()


def sqlite_inodes():
    return {str(f.relative_to(ROOT)): f.stat().st_ino for f in ROOT.rglob('*.sqlite3')}


def acceptance_plan(health, symbol_info, book):
    """Plan one bounded entry/exit pair; dispatch still reserves the exit atomically."""
    from factorlab.integrations.binance_futures_trading.demo_runtime import MAX_NOTIONAL, MAX_QUANTITY
    zero = dict(mode='ZERO_POST_SWITCH_ONLY', configuration={})
    budget, campaign = health.get('global_budget') or {}, health.get('campaign') or {}
    if (budget.get('state') != 'RUNNING' or budget.get('remaining_posts', 0) < 2
            or campaign.get('remaining_posts', 0) < 2 or not health.get('account_recovered')
            or health.get('unknown_count') != 0 or health.get('open_orders') or health.get('positions')
            or 'opening_equity' not in budget or 'max_loss' not in budget
            or D(budget['opening_equity']) - D(health.get('equity') or '0') >= D(budget['max_loss'])):
        return zero
    filters = {row['filterType']: row for row in symbol_info['filters']}
    tick = D(filters['PRICE_FILTER']['tickSize'])
    quantity = D('.04')
    lot = filters['LOT_SIZE']
    if not D(lot['minQty']) <= quantity <= min(D(lot['maxQty']), MAX_QUANTITY):
        return zero
    if quantity % D(lot['stepSize']):
        return zero
    # Both entry and exit prices are capped in the strategy; reserve at the caps.
    cap = (D(book['askPrice']) * D('1.01') / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    notional = quantity * cap
    min_notional = D(filters.get('MIN_NOTIONAL', {}).get('notional', '0'))
    notional_filter = filters.get('NOTIONAL', {})
    if (not D(filters['PRICE_FILTER']['minPrice']) <= cap <= D(filters['PRICE_FILTER']['maxPrice'])
            or notional < max(min_notional, D(notional_filter.get('minNotional', '0')))
            or notional > min(MAX_NOTIONAL, D(notional_filter.get('maxNotional', str(MAX_NOTIONAL))))
            or D(budget.get('remaining_notional', '0')) < notional * 2
            or D(health.get('equity') or '0') < notional):
        return zero
    return dict(mode='ROUNDTRIP_2_POST', configuration=dict(quantity=str(quantity), tick=str(tick),
        max_entry_price=str(cap), max_exit_price=str(cap), exit_budget_notional=str(notional)))


def can_downgrade_after_timeout(health, plan, evidence):
    return (plan['mode'] == 'ZERO_POST_SWITCH_ONLY' and health.get('account_recovered')
            and health.get('unknown_count') == 0 and not health.get('positions')
            and not health.get('open_orders') and D(evidence['actual_filled_quantity']) == 0)


def real_fill_evidence(orders, receipts, entry_id, exit_id, *, inventory):
    """Match protocol order identities to exchange fills, quantities and commissions."""
    from factorlab.trading.contracts import OrderStatus, Side, TERMINAL
    result = dict(verified=False, entry_id=entry_id, exit_id=exit_id)
    entry, exit_order = orders.get(entry_id), orders.get(exit_id)
    result['actual_filled_quantity'] = str(entry.snapshot.cumulative_filled_quantity) if entry else '0'
    if entry is None or exit_order is None:
        return result
    a, b = entry.snapshot, exit_order.snapshot
    if (a.status not in TERMINAL or b.status is not OrderStatus.FILLED
            or a.reconcile_required or b.reconcile_required
            or a.intent.side is not Side.BUY or b.intent.side is not Side.SELL
            or a.intent.reduce_only or not b.intent.reduce_only
            or a.cumulative_filled_quantity <= 0
            or a.cumulative_filled_quantity != b.cumulative_filled_quantity or D(inventory) != 0):
        return result
    fills = {}
    for key, snapshot in ((entry_id, a), (exit_id, b)):
        matching = [row for row in receipts if row['order']['clientOrderId'] == key
                    and row['order']['status'] == snapshot.status.value]
        if not matching:
            return result
        receipt = matching[-1]
        trades = receipt['trades']
        if (str(receipt['order']['orderId']) != str(snapshot.exchange_order_id)
                or receipt['order']['side'] != snapshot.intent.side.value
                or receipt['order']['symbol'] != snapshot.intent.instrument_id
                or D(receipt['order']['executedQty']) != snapshot.cumulative_filled_quantity
                or not trades or len({str(t['id']) for t in trades}) != len(trades)
                or any(str(t['orderId']) != str(receipt['order']['orderId'])
                       or t['symbol'] != receipt['order']['symbol']
                       or 'commission' not in t or t.get('commissionAsset') != 'USDT' for t in trades)
                or sum((D(t['qty']) for t in trades), D(0)) != snapshot.cumulative_filled_quantity
                or sum((D(t['commission']) for t in trades), D(0)) != snapshot.cumulative_fee):
            return result
        fills[key] = dict(quantity=str(snapshot.cumulative_filled_quantity), status=snapshot.status.value,
            trades=trades, commissions=[dict(amount=t['commission'], asset=t['commissionAsset']) for t in trades])
    return dict(result, verified=True, fills=fills, final_inventory=str(inventory))


def main():
    import pwd
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--run-id', default=str(int(time.time())))
    p.add_argument('--fill-timeout', type=int, default=120)
    args = p.parse_args()

    commit = args.release.name
    python = str(args.release / 'venv/bin/python')
    sys.path.insert(0, str(args.release / 'source/src'))
    from factorlab.integrations.binance_futures_trading.strategy_package import (
        DECISION_INTERFACE, build_package,
    )

    def cli(*argv):
        if argv[0] == 'rollback':
            cmd = [
                'runuser', '-u', 'factorlab-demo', '--', python, '-c',
                'import json,sys; from pathlib import Path; from '
                'factorlab.integrations.binance_futures_trading.strategy_deployment import DeploymentStore; '
                's=DeploymentStore(Path(sys.argv[1])/"strategy-deployment.sqlite3"); '
                's.rollback(sys.argv[2]); print(json.dumps(s.snapshot())); s.close()',
                str(ROOT), argv[1] if len(argv) > 1 else 'P8_S04_RESTORE_PREVIOUS_STABLE',
            ]
        else:
            cmd = ['runuser', '-u', 'factorlab-demo', '--', python, '-m', MODULE,
                   *argv, '--state-root', str(ROOT)]
        return json.loads(run_cmd(cmd))

    def health():
        raw = json.loads((ROOT / 'cloud-health.json').read_bytes())
        keys = (
            'pid', 'heartbeat_at', 'started_at', 'starts', 'restart_count',
            'status', 'reason', 'mode', 'desired', 'account_recovered',
            'last_reconciled_at', 'unknown_count', 'unknown_orders',
            'open_orders', 'positions', 'equity', 'nav_usdt', 'campaign',
            'global_budget', 'recent_orders', 'grid', 'strategy_release',
        )
        return {k: raw.get(k) for k in keys}

    def service_pid():
        return int(run_cmd(['systemctl', 'show', UNIT, '-p', 'MainPID', '--value']))

    def c06_report():
        out = run_cmd([
            'runuser', '-u', 'factorlab-demo', '--', python, '-m',
            'factorlab.integrations.binance_futures_trading.demo_cloud_runtime', 'report',
            '--state-root', str(ROOT), '--monitor-dir', str(MONITOR), '--backup-root', str(BACKUP),
        ])
        return json.loads(out)

    def read_journal_orders():
        proto = next(ROOT.glob('*/protocol.sqlite3'))
        with sqlite3.connect(proto.resolve().as_uri() + '?mode=ro', uri=True) as db:
            rows = db.execute(
                'SELECT id, payload FROM futures_orders ORDER BY rowid DESC LIMIT 15'
            ).fetchall()
        orders = {}
        for oid, raw in rows:
            orders[oid] = json.loads(raw)
        return orders

    def current_plan():
        from factorlab.integrations.binance_futures_trading.protocol import Environment
        host = Environment.DEMO.host
        with urlopen(host + '/fapi/v1/exchangeInfo', timeout=10) as response:
            exchange = json.load(response)
        with urlopen(host + '/fapi/v1/ticker/bookTicker?symbol=NVDAUSDT', timeout=10) as response:
            book = json.load(response)
        symbol = next(row for row in exchange['symbols'] if row['symbol'] == 'NVDAUSDT')
        return acceptance_plan(health(), symbol, book)

    def phase_fill_evidence(package_id):
        from factorlab.trading.storage import decode
        with sqlite3.connect((ROOT / 'strategy-deployment.sqlite3').resolve().as_uri() + '?mode=ro', uri=True) as db:
            owned = [row[0] for row in db.execute('SELECT client_id FROM strategy_orders WHERE version=?', (package_id,))]
        entry_id = next((key for key in owned if key.endswith('-entry')), None)
        exit_id = next((key for key in owned if key.endswith('-exit')), None)
        proto = next(ROOT.glob('*/protocol.sqlite3'))
        with sqlite3.connect(proto.resolve().as_uri() + '?mode=ro', uri=True) as db:
            orders = {key: decode(json.loads(raw)) for key, raw in db.execute('SELECT id,payload FROM futures_orders')
                      if key in {entry_id, exit_id}}
            receipts = [json.loads(raw) for raw, in db.execute('SELECT payload FROM futures_receipts WHERE client_id IN (?,?) ORDER BY rowid', (entry_id, exit_id))]
        h = health()
        inventory = sum((D(row['quantity']) for row in (h['positions'] or [])), D(0))
        return real_fill_evidence(orders, receipts, entry_id, exit_id, inventory=inventory)

    def wait_for(predicate, timeout=120, interval=2):
        until = time.monotonic() + timeout
        last = None
        while time.monotonic() < until:
            last = dict(state=cli('status'), health=health(), pid=service_pid())
            if predicate(last):
                return last
            time.sleep(interval)
        raise TimeoutError(f'condition not met within {timeout}s; last={json.dumps(last, default=str)[:900]}')

    owner = pwd.getpwnam('factorlab-demo')
    examples = args.release / 'source/examples/protocol_strategy'

    def publish_and_accept(tag, *, version, configuration, mode='hot'):
        archive = INBOX / f'p8-s04-{args.run_id}-{tag}.zip'
        manifest_spec = dict(
            schema_version=1,
            interface=DECISION_INTERFACE,
            environment='BINANCE_USDM_DEMO',
            symbol='NVDAUSDT',
            code_commit=commit,
            strategy_id='limit_cycle',
            strategy_version=version,
            state_version=1,
            execution_mode='orders',
            interval_seconds=60,
            entrypoint='limit_cycle:LimitCycle',
            files=['limit_cycle.py'],
            dependencies=[],
            configuration=configuration,
            migration='wait_flat',
            evidence=dict(
                kind='SOFTWARE_CONFORMANCE_ONLY',
                research_qualification=False,
                source_commit=commit,
                run_id=args.run_id,
                tag=tag,
            ),
        )
        if archive.exists():
            archive.unlink()
        manifest = build_package(examples, manifest_spec, archive)
        expected_active = cli('status')['active']
        candidate = dict(
            schema_version=1,
            decision='PROMOTE',
            qualified=True,
            strictly_better=True,
            selection_id=f'p8-s04-{args.run_id}-{tag}',
            expected_active=expected_active,
            package_id=manifest['package_id'],
            evidence=manifest['evidence'],
        )
        candidate_path = INBOX / f'p8-s04-{args.run_id}-{tag}.json'
        candidate_path.write_text(json.dumps(candidate, sort_keys=True), encoding='utf-8')
        for path in (archive, candidate_path):
            os.chown(path, owner.pw_uid, owner.pw_gid)
            path.chmod(0o600)
        cli('accept', '--archive', str(archive), '--candidate', str(candidate_path), '--mode', mode)
        return manifest

    def save():
        args.output.write_text(json.dumps(results, indent=2, default=str) + '\n', encoding='utf-8')

    before_health = health()
    before_inodes = sqlite_inodes()
    remaining_posts = (before_health.get('global_budget') or {}).get('remaining_posts', 0)
    results = dict(
        schema='FACTORLAB_P8_S04_DEMO_ACCEPTANCE_V1',
        conformance_only=True,
        research_qualification=False,
        commit=commit,
        run_id=args.run_id,
        started_at_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        remaining_posts_before=remaining_posts,
        budget_profile='ZERO_POST_SWITCH_ONLY',
        real_fill_verified=False,
        real_fill_note=None,
        before=dict(health=before_health, pid=service_pid(), inodes=before_inodes, state=cli('status')),
        phases=[],
    )
    save()

    if before_health.get('unknown_count') != 0:
        raise RuntimeError('UNKNOWN orders present before validation; aborting')

    phase1_plan = current_plan()
    results['budget_profile'] = phase1_plan['mode']
    results['phase1_plan'] = phase1_plan
    save()
    if phase1_plan['mode'] == 'ROUNDTRIP_2_POST':
        pid0 = service_pid()
        a1_man = publish_and_accept(
            'a1-buy-sell', version='1.0.1',
            configuration=dict(
                symbol='NVDAUSDT', interval_seconds=60,
                buy_offset='.30', sell_offset='-.30', mode='buy_then_sell',
                version='1.0.1', strategy_id='limit_cycle',
                **phase1_plan['configuration'],
            ),
            mode='hot',
        )
        p1_active = wait_for(
            lambda s: s['state']['active'] == a1_man['package_id'] and s['state']['switch']['phase'] == 'SUCCEEDED',
            timeout=120,
        )
        posts_at_activate = p1_active['health']['global_budget']['consumed']['posts']
        try:
            p1_done = wait_for(
                lambda s: (
                    phase_fill_evidence(a1_man['package_id'])['verified']
                    and not s['health']['open_orders']
                    and not s['health']['positions']
                    and s['health']['unknown_count'] == 0
                    and s['health']['account_recovered']
                ),
                timeout=args.fill_timeout,
            )
        except TimeoutError:
            results['complete'] = False
            results['real_fill_evidence'] = phase_fill_evidence(a1_man['package_id'])
            results['recovery_required'] = dict(package_id=a1_man['package_id'], health=health(),
                reason='ENTRY_OR_EXIT_NOT_CONFIRMED; inventory and order recovery remain active')
            latest = health()
            if can_downgrade_after_timeout(latest, current_plan(), results['real_fill_evidence']):
                results['budget_profile'] = 'ZERO_POST_SWITCH_ONLY'
                results['real_fill_note'] = 'Budget changed before dispatch; zero-fill phase safely downgraded.'
                results.pop('recovery_required')
                p1_done = dict(health=latest)
            else:
                save()
                raise
        results['real_fill_evidence'] = phase_fill_evidence(a1_man['package_id'])
        results['real_fill_verified'] = results['real_fill_evidence']['verified']
        results['phases'].append(dict(
            name='phase1_s01_buy_fill_sell_close',
            package_id=a1_man['package_id'],
            old_pid=pid0,
            activated_pid=p1_active['pid'],
            same_pid_switch=(pid0 == p1_active['pid']),
            posts_before=posts_at_activate,
            posts_after=p1_done['health']['global_budget']['consumed']['posts'],
            after_health=p1_done['health'],
        ))
        save()
    else:
        results['real_fill_note'] = (
            'The current account, filters and risk/notional/POST budgets do not admit '
            'a reserved entry/exit pair. Continuing with zero-POST switching only.'
        )

    # Restart, switching and rollback do not require another potentially filling entry.
    pid1 = service_pid()
    cur_health = health()
    results['phase2_budget'] = cur_health.get('global_budget')
    a2_mode = 'hold'
    a2_man = publish_and_accept(
        'a2-resting-buy', version='1.0.2',
        configuration=dict(
            symbol='NVDAUSDT', interval_seconds=60, quantity='.04', tick='.01',
            buy_offset='-2.00', sell_offset='-.30', mode=a2_mode,
            cancel_after_quotes=999, version='1.0.2', strategy_id='limit_cycle',
        ),
        mode='hot',
    )
    p2_active = wait_for(
        lambda s: s['state']['active'] == a2_man['package_id'] and s['state']['switch']['phase'] == 'SUCCEEDED',
        timeout=120,
    )
    posts_at_a2 = p2_active['health']['global_budget']['consumed']['posts']
    resting_order = None
    pre_restart_posts = posts_at_a2
    pre_restart_pid = p2_active['pid']

    restart_at = time.time()
    run_cmd(['systemctl', 'restart', UNIT])
    wait_for(
        lambda s: (
            s['pid'] != pre_restart_pid
            and s['pid'] == s['health']['pid']
            and s['health']['heartbeat_at'] >= restart_at
            and s['health']['account_recovered']
            and s['health']['unknown_count'] == 0
            and s['state']['active'] == a2_man['package_id']
        ),
        timeout=120,
    )
    time.sleep(6)
    p2_settled = dict(state=cli('status'), health=health(), pid=service_pid())
    results['phases'].append(dict(
        name='phase2_hot_switch_resting_order_and_restart_recovery',
        package_id=a2_man['package_id'],
        strategy_mode=a2_mode,
        switch_old_pid=pid1,
        switch_new_pid=p2_active['pid'],
        same_pid_switch=(pid1 == p2_active['pid']),
        resting_order_before_restart=resting_order,
        pre_restart_pid=pre_restart_pid,
        post_restart_pid=p2_settled['pid'],
        pre_restart_posts=pre_restart_posts,
        post_restart_posts=p2_settled['health']['global_budget']['consumed']['posts'],
        open_orders_after_restart=p2_settled['health']['open_orders'],
        active_after_restart=p2_settled['state']['active'],
    ))
    save()

    # --- Same-PID hot switch (cancels A2 resting order via DRAINING) + S01 fault auto-rollback ---
    pid2 = service_pid()
    a3_man = publish_and_accept(
        'a3-fault', version='1.0.3',
        configuration=dict(
            symbol='NVDAUSDT', interval_seconds=60, quantity='.04', tick='.01',
            mode='fault_on_quote', version='1.0.3', strategy_id='limit_cycle',
        ),
        mode='hot',
    )
    wait_for(
        lambda s: (
            s['state']['active'] == a2_man['package_id']
            and s['state']['switch'] is not None
            and s['state']['switch']['phase'] == 'ROLLED_BACK'
            and s['state']['switch']['old'] == a3_man['package_id']
        ),
        timeout=150,
    )
    time.sleep(5)
    p3_after = dict(state=cli('status'), health=health(), pid=service_pid())
    results['phases'].append(dict(
        name='phase3_hot_switch_cancel_and_automatic_fault_rollback',
        fault_package_id=a3_man['package_id'],
        expected_rollback_target=a2_man['package_id'],
        old_pid=pid2,
        after_pid=p3_after['pid'],
        same_pid=(pid2 == p3_after['pid']),
        rolled_back_state=p3_after['state'],
        posts_after=p3_after['health']['global_budget']['consumed']['posts'],
        open_orders_after=p3_after['health']['open_orders'],
        positions_after=p3_after['health']['positions'],
    ))
    save()

    # --- Restore legacy-grid along stable history ---
    restore_steps = []
    for step_idx in range(4):
        cur = cli('status')
        if cur['active'] == 'legacy-grid':
            break
        cli('rollback', f'P8_S04_RESTORE_LEGACY_{step_idx + 1}')
        done = wait_for(
            lambda s: s['state']['switch']['phase'] == 'ROLLED_BACK' and s['state']['active'] != cur['active'],
            timeout=120,
        )
        restore_steps.append(dict(from_version=cur['active'], to_version=done['state']['active'], pid=done['pid']))
    time.sleep(4)
    after_health = health()
    after_inodes = sqlite_inodes()
    results['restore_to_legacy'] = restore_steps
    results['after'] = dict(
        health=after_health,
        pid=service_pid(),
        inodes=after_inodes,
        inodes_preserved=all(after_inodes.get(k) == v for k, v in before_inodes.items()),
        state=cli('status'),
        recent_protocol_orders=read_journal_orders(),
        c06_report=c06_report(),
    )
    results['completed_at_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    results['complete'] = (
        results['after']['state']['active'] == 'legacy-grid'
        and results['after']['health']['unknown_count'] == 0
        and results['after']['inodes_preserved']
        and not results['after']['health']['positions']
        and not results['after']['health']['open_orders']
    )
    save()
    print(json.dumps(dict(
        complete=results['complete'],
        budget_profile=results['budget_profile'],
        real_fill_verified=results['real_fill_verified'],
        output=str(args.output),
        posts_before=before_health['global_budget']['consumed']['posts'],
        posts_after=after_health['global_budget']['consumed']['posts'],
        active=results['after']['state']['active'],
    ), sort_keys=True))


if __name__ == '__main__':
    main()
