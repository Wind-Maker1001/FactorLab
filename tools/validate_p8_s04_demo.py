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
import pwd
import sqlite3
import subprocess
import sys
import time


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


def main():
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
        budget_profile=('FULL_ROUNDTRIP_3_POST' if remaining_posts >= 3 else
                        'SINGLE_POST_RESTING_CANCEL' if remaining_posts >= 1 else
                        'ZERO_POST_SWITCH_ONLY'),
        real_fill_verified=False,
        real_fill_note=None,
        before=dict(health=before_health, pid=service_pid(), inodes=before_inodes, state=cli('status')),
        phases=[],
    )
    save()

    if before_health.get('unknown_count') != 0:
        raise RuntimeError('UNKNOWN orders present before validation; aborting')

    # --- Optional Phase 1 (only when >= 3 POSTs remain): real BUY FILL + reduce-only SELL close ---
    if remaining_posts >= 3:
        pid0 = service_pid()
        a1_man = publish_and_accept(
            'a1-buy-sell', version='1.0.1',
            configuration=dict(
                symbol='NVDAUSDT', interval_seconds=60, quantity='.04', tick='.01',
                buy_offset='.30', sell_offset='-.30', mode='buy_then_sell',
                version='1.0.1', strategy_id='limit_cycle',
            ),
            mode='hot',
        )
        p1_active = wait_for(
            lambda s: s['state']['active'] == a1_man['package_id'] and s['state']['switch']['phase'] == 'SUCCEEDED',
            timeout=120,
        )
        posts_at_activate = p1_active['health']['global_budget']['consumed']['posts']
        p1_done = wait_for(
            lambda s: (
                s['health']['global_budget']['consumed']['posts'] >= posts_at_activate + 2
                and not s['health']['open_orders']
                and not s['health']['positions']
                and s['health']['unknown_count'] == 0
                and s['health']['account_recovered']
            ),
            timeout=args.fill_timeout,
        )
        results['real_fill_verified'] = True
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
            f'Remaining existing global POST budget is {remaining_posts}/100; '
            'opening a fill without >=2 POSTs reserved for entry+exit would risk an uncloseable '
            'residual position under frozen risk limits. Using non-crossing resting LIMIT order '
            'lifecycle instead without expanding budget.'
        )

    # --- Resting order + restart recovery phase ---
    pid1 = service_pid()
    cur_health = health()
    cur_remaining = (cur_health.get('global_budget') or {}).get('remaining_posts', 0)
    a2_mode = 'buy_then_cancel' if cur_remaining >= 1 else 'hold'
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
    if cur_remaining >= 1:
        p2_order = wait_for(
            lambda s: (
                s['health']['global_budget']['consumed']['posts'] >= posts_at_a2 + 1
                and s['health']['unknown_count'] == 0
                and (len(s['health']['open_orders'] or []) == 1 or bool(s['health']['positions']))
            ),
            timeout=90,
        )
        resting_order = (p2_order['health']['open_orders'] or [None])[0]
        pre_restart_posts = p2_order['health']['global_budget']['consumed']['posts']
        pre_restart_pid = p2_order['pid']
    else:
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
