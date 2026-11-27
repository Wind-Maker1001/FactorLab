"""Restore the simulated execution ledgers and publish a fenced handoff."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from time import monotonic, process_time

from .binance_futures_trading import demo_cloud_storage as storage
from .binance_futures_trading.protocol import SafetyBlocked


MARKETS = ('binance', 'alpaca')
RESULT = 'disaster-recovery-result.json'
ALPACA_UNIT = 'factorlab-alpaca-paper.service'
MODULE = 'factorlab.integrations.disaster_recovery'


def _time(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError('recovery watermarks must include a UTC offset')
    return result


def _configuration_digest(path):
    return None if path is None else storage.file_sha256(path)


def _target(machine, state):
    state = Path(state)
    if (not re.fullmatch('[0-9a-f]{32}', machine) or not state.is_absolute()
            or '..' in state.parts or not state.name):
        raise SafetyBlocked('the target must name a unique machine ID and absolute state directory')
    return dict(machine_id=machine, state_root=str(state))


def _assert_fence(path, manifest, snapshot_digest, target):
    # The execution UID must not be able to issue or replace a fence attestation.
    path = path.absolute()
    storage._no_symlinks(path)
    for directory in path.parents:
        info = directory.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SafetyBlocked('fence evidence must be under an administrator-owned directory')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SafetyBlocked('fence evidence must be an administrator-owned file')
    proof = json.loads(path.read_bytes())
    expected = dict(market=manifest['market'], source_machine=manifest['source_machine'],
                    target=target, snapshot_sha256=snapshot_digest)
    if any(proof[key] != value for key, value in expected.items()):
        raise SafetyBlocked('fence evidence is for a different recovery or target')
    mechanisms = {'source-sealed-and-units-masked', 'azure-deallocated-and-restart-denied'}
    if manifest['market'] == 'alpaca':
        mechanisms.add('source-paper-credentials-revoked')
    if proof['mechanism'] not in mechanisms or not proof['reference'].strip():
        raise SafetyBlocked('a durable source fence and its evidence reference are required')
    observed = _time(proof['observed_at'])
    now = datetime.now(timezone.utc)
    if not max(_time(manifest['captured_at']), now - timedelta(minutes=30)) <= observed <= now:
        raise SafetyBlocked('confirm the source fence after sealing and within 30 minutes of takeover')
    return proof


def _catch_up(market, root, since):
    if market == 'binance':
        from .binance_futures_trading.demo_disaster_recovery import catch_up
    else:
        from .alpaca.disaster_recovery import catch_up
    return catch_up(root, since=since)


def _prepare(root, manifest, *, source_digest, timer, proof=None):
    ledger = _catch_up(manifest['market'], root, _time(manifest['captured_at']))
    uncovered = []
    if not manifest['final_target']:
        uncovered = [
            'allocation and capital transfers',
            'cumulative order and loss budget changes',
            'stop, pause and halt changes',
            'strategy deployment, decisions, cursors and exit obligations',
            'TaskMailbox producers and acknowledgements',
        ]
    write_eligible = bool(proof and not uncovered and ledger['reconciled'] and ledger['budget']['verified'])
    result = dict(
        market=manifest['market'], snapshot_sha256=source_digest, revision=manifest['revision'],
        captured_at=manifest['captured_at'], ledger=ledger, missing_internal_coverage=uncovered,
        fence=proof, published_for_execution=write_eligible, elapsed_seconds=monotonic() - timer,
    )
    storage.atomic_json(root / RESULT, result)
    return result


def recover(backup, destination, *, legacy_since=None):
    """Publish a recovered investigation directory that remains quarantined."""
    timer = monotonic()
    backup = storage._root(backup)
    if legacy_since is None:
        manifest = storage.verify_snapshot(backup)
        digest = storage.file_sha256(backup / storage._RECOVERY_MANIFEST)
        root = storage.restore_verified_state(backup, destination, manifest)
    else:
        from .binance_futures_trading.demo_cloud_backup import verify_backup
        verify_backup(backup)
        manifest = dict(market='binance', revision=None, captured_at=_time(legacy_since).isoformat(),
                        final_target=None)
        digest = None
        root = storage.restore_state(backup, destination)
    return _prepare(root, manifest, source_digest=digest, timer=timer)


def take_over(backup, destination, *, fence_file, revision, configuration):
    """Publish the recovered execution tree only after evidence and ledger checks."""
    timer = monotonic()
    backup = storage._root(backup)
    destination = storage._destination(backup, destination)
    manifest = storage.verify_snapshot(backup)
    target = _target(storage.machine_id(), destination)
    if ((backup / storage._QUARANTINE).exists()
            or manifest['final_target'] != target or manifest['source_machine'] == target['machine_id']):
        raise SafetyBlocked('execution requires a final source seal for this machine and state directory')
    if (manifest['revision'] != revision or manifest['configuration_sha256'] is None
            or manifest['configuration_sha256'] != _configuration_digest(configuration)):
        raise SafetyBlocked('target revision and runtime configuration must match the sealed source')
    digest = storage.file_sha256(backup / storage._RECOVERY_MANIFEST)
    proof = _assert_fence(fence_file, manifest, digest, target)
    with tempfile.TemporaryDirectory(prefix='.takeover-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'state'
        storage.restore_verified_state(backup, staging, manifest)
        result = _prepare(staging, manifest, source_digest=digest, timer=timer, proof=proof)
        if not result['published_for_execution']:
            return result
        (staging / storage._QUARANTINE).unlink()
        storage._sync_directory(staging)
        storage._publish(staging, destination)
    return result


def finish_alpaca_backup(recovery_file, state_root, runner):
    if recovery_file.exists():
        if not (state_root / storage._QUARANTINE).exists():
            runner.start_unit(ALPACA_UNIT)
        recovery_file.unlink()
        storage._sync_directory(recovery_file.parent)


def scheduled_alpaca_backup(state_root, backup_root, release, recovery_file, keep, runner):
    from .binance_futures_trading.demo_cloud_backup import generate_backup_name, parse_backup_timestamp

    if keep < 1:
        raise ValueError('at least one verified backup must be kept')
    revision = release.resolve(strict=True).name
    destination = backup_root / generate_backup_name(backup_root)
    interrupted = runner.is_unit_active(ALPACA_UNIT)
    downtime = 0.0
    started = monotonic()
    if interrupted:
        storage.atomic_json(recovery_file, {'unit': ALPACA_UNIT})
    try:
        if interrupted:
            runner.stop_unit(ALPACA_UNIT)
        snapshot = runner.run_python_module(MODULE, [
                'snapshot', '--market', 'alpaca', '--state-root', str(state_root), '--destination',
                str(destination), '--revision', revision,
            ])
        snapshot.check_returncode()
    finally:
        if interrupted:
            finish_alpaca_backup(recovery_file, state_root, runner)
            downtime = monotonic() - started
    verification = runner.run_python_module(MODULE, ['describe', '--backup', str(destination)])
    verification.check_returncode()
    backups = [(parsed, path) for path in backup_root.iterdir()
               if (parsed := parse_backup_timestamp(path.name)) is not None and path.is_dir()]
    # The copy just verified survives retention even if the source clock moved backwards.
    previous = (path for _, path in sorted(backups, reverse=True) if path != destination)
    for index, path in enumerate(previous):
        if index >= keep - 1:
            shutil.rmtree(path)
    storage._sync_directory(backup_root)
    return dict(json.loads(snapshot.stdout), verified=True, maintenance_downtime_seconds=downtime)


def main(argv=None):
    if os.name != 'nt':
        os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    snapshot = commands.add_parser('snapshot')
    snapshot.add_argument('--market', choices=MARKETS, required=True)
    snapshot.add_argument('--state-root', type=Path, required=True)
    snapshot.add_argument('--destination', type=Path, required=True)
    snapshot.add_argument('--revision', required=True)
    snapshot.add_argument('--configuration', type=Path)
    snapshot.add_argument('--task-mailbox', type=Path)
    snapshot.add_argument('--target-machine')
    snapshot.add_argument('--target-state')
    for command in ('schedule-alpaca', 'finish-alpaca-backup'):
        sub = commands.add_parser(command)
        sub.add_argument('--state-root', type=Path, required=True)
        sub.add_argument('--recovery-file', type=Path, required=True)
        if command == 'schedule-alpaca':
            sub.add_argument('--backup-root', type=Path, required=True)
            sub.add_argument('--release', type=Path, required=True)
            sub.add_argument('--keep', type=int, default=14)
    for command in ('describe', 'recover', 'take-over'):
        sub = commands.add_parser(command)
        sub.add_argument('--backup', type=Path, required=True)
        if command != 'describe':
            sub.add_argument('--destination', type=Path, required=True)
        if command == 'recover':
            sub.add_argument('--legacy-since')
        if command == 'take-over':
            sub.add_argument('--fence-file', type=Path, required=True)
            sub.add_argument('--revision', required=True)
            sub.add_argument('--configuration', type=Path, required=True)

    args = parser.parse_args(argv)
    timer, cpu = monotonic(), process_time()
    try:
        if args.command == 'snapshot':
            final = None
            if args.target_machine or args.target_state:
                if not (args.target_machine and args.target_state and args.configuration):
                    parser.error('a final snapshot requires --target-machine, --target-state and --configuration')
                final = _target(args.target_machine, args.target_state)
                if final['machine_id'] == storage.machine_id():
                    raise SafetyBlocked('a final snapshot must target a different machine')
            root = storage.recovery_snapshot(
                args.state_root, args.destination, args.market, args.revision,
                task_mailbox=args.task_mailbox, final_target=final,
                configuration_sha256=_configuration_digest(args.configuration),
            )
            result = dict(path=str(root), backup_bytes=sum(path.stat().st_size
                          for path in root.rglob('*') if path.is_file()))
        elif args.command in ('schedule-alpaca', 'finish-alpaca-backup'):
            from .binance_futures_trading.demo_cloud_backup import SystemdRunner

            runner = SystemdRunner(service_user='factorlab-alpaca')
            if args.command == 'finish-alpaca-backup':
                finish_alpaca_backup(args.recovery_file, args.state_root, runner)
                return 0
            result = scheduled_alpaca_backup(args.state_root, args.backup_root, args.release,
                                            args.recovery_file, args.keep, runner)
        elif args.command == 'describe':
            result = storage.verify_snapshot(args.backup)
            result['snapshot_sha256'] = storage.file_sha256(args.backup / storage._RECOVERY_MANIFEST)
        elif args.command == 'recover':
            result = recover(args.backup, args.destination, legacy_since=args.legacy_since)
        else:
            result = take_over(args.backup, args.destination, fence_file=args.fence_file,
                               revision=args.revision, configuration=args.configuration)
        result.update(command_elapsed_seconds=monotonic() - timer, cpu_seconds=process_time() - cpu)
        print(json.dumps(result, default=str, sort_keys=True))
        return 1 if args.command == 'take-over' and not result['published_for_execution'] else 0
    except (OSError, ValueError, KeyError, SafetyBlocked) as error:
        parser.exit(1, f'{type(error).__name__}: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
