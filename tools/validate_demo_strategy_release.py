"""On-host official Demo conformance run. Hold-only packages; existing owner does GET/DELETE.

Run as root after the pinned runtime installer. All SQLite writes are delegated to
the service user. This is software evidence, never a research qualification claim.
"""
import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time


ROOT = Path('/var/lib/factorlab-demo/execute-state')
INBOX = Path('/var/lib/factorlab-demo/champions')
MODULE = 'factorlab.integrations.binance_futures_trading.azure_strategy_deploy'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--run-id', default=str(int(time.time())))
    args = p.parse_args()
    python = str(args.release / 'venv/bin/python')
    sys.path.insert(0, str(args.release / 'source/src'))
    from factorlab.integrations.binance_futures_trading.strategy_package import INTERFACE, build_package

    def cli(*argv):
        command = ['runuser', '-u', 'factorlab-demo', '--', python, '-m', MODULE,
                   *argv, '--state-root', str(ROOT)]
        if argv[0] == 'rollback':
            command = ['runuser', '-u', 'factorlab-demo', '--', python, '-c',
                'import json,sys; from pathlib import Path; from ' +
                'factorlab.integrations.binance_futures_trading.strategy_deployment import DeploymentStore; '+
                's=DeploymentStore(Path(sys.argv[1])/"strategy-deployment.sqlite3"); '+
                's.rollback("CONFORMANCE_RESTORE_PREVIOUS_STABLE"); print(json.dumps(s.snapshot())); s.close()', str(ROOT)]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def health():
        raw = json.loads((ROOT / 'cloud-health.json').read_bytes())
        return {k: raw.get(k) for k in ('pid', 'heartbeat_at', 'status', 'unknown_count',
            'account_recovered', 'global_budget', 'positions', 'open_orders', 'strategy_release')}

    def wait_for(predicate, timeout=100):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            state = cli('status')
            if predicate(state):
                return dict(state=state, health=health())
            time.sleep(2)
        raise TimeoutError('conformance switch did not complete; existing owner retains safe pending state')

    before = health()
    results = dict(before=before, conformance_only=True, phases=[])
    args.output.write_text(json.dumps(results, indent=2))
    owner = pwd.getpwnam('factorlab-demo')
    for index, mode in [(1, 'restart'), (2, 'hot'), (3, 'hot')]:
        archive = INBOX / f's03-conformance-{args.run_id}-{index}.zip'
        spec = dict(schema_version=1, interface=INTERFACE, environment='BINANCE_USDM_DEMO', symbol='NVDAUSDT',
            code_commit=args.release.name, state_version=1, entrypoint='hold:evaluate', files=['hold.py'],
            dependencies=[], configuration=dict(conformance_version=index, run_id=args.run_id, inject_failure=index == 3),
            migration='wait_flat', evidence=dict(kind='SOFTWARE_CONFORMANCE_ONLY',
            research_qualification=False, source_commit=args.release.name))
        if not archive.exists():
            manifest = build_package(args.release / 'source/examples/strategy_release', spec, archive)
        else:
            import zipfile
            with zipfile.ZipFile(archive) as z: manifest = json.loads(z.read('manifest.json'))
        candidate = dict(schema_version=1, decision='PROMOTE', qualified=True, strictly_better=True,
            selection_id='s03-official-conformance-' + args.run_id + '-' + str(index),
            expected_active=cli('status')['active'],
            package_id=manifest['package_id'], evidence=manifest['evidence'])
        candidate_path = INBOX / f's03-conformance-{args.run_id}-{index}.json'
        candidate_path.write_text(json.dumps(candidate))
        for path in (archive, candidate_path):
            os.chown(path, owner.pw_uid, owner.pw_gid)
            path.chmod(0o600)
        prior_pid = health()['pid']
        cli('accept', '--archive', str(archive), '--candidate', str(candidate_path), '--mode', mode)
        phase = wait_for(lambda s: s['active'] == manifest['package_id'] and s['switch']['phase'] == 'SUCCEEDED')
        # Wait for a fresh health publication from the same active owner.
        time.sleep(3)
        phase['health'] = health()
        phase.update(mode=mode, version=index, old_pid=prior_pid,
                     expected_pid_change=mode == 'restart')
        results['phases'].append(phase)
        args.output.write_text(json.dumps(results, indent=2))
        if index == 3:
            recovered = wait_for(lambda s: s['active'] != manifest['package_id'] and s['switch']['phase'] == 'ROLLED_BACK')
            results['automatic_strategy_rollback'] = recovered
            args.output.write_text(json.dumps(results, indent=2))
    # Return through the stable history to the original Grid to keep natural trading running.
    for _ in range(3):
        if cli('status')['active'] == 'legacy-grid': break
        cli('rollback')
        wait_for(lambda s: s['switch']['phase'] == 'ROLLED_BACK')
    results['after'] = health()
    results['final_state'] = cli('status')
    results['complete'] = results['final_state']['active'] == 'legacy-grid'
    args.output.write_text(json.dumps(results, indent=2))
    print(json.dumps(dict(complete=results['complete'], output=str(args.output))))


if __name__ == '__main__':
    main()
