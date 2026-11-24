"""Invalid installed packages remain diagnosed faults, never legacy routing."""
import json
import pytest
from factorlab.integrations.binance_futures_trading.demo_strategy_release import PackageLoadError
from factorlab.integrations.binance_futures_trading.strategy_deployment import LEGACY
from test_s04_validation_budget import install_cycle
from test_unified_cloud_strategy_s04 import harness, promote_candidate, build_decision_package, EXAMPLES
from test_binance_demo_grid_network import network_grid, writes
from test_binance_demo_network import wired


@pytest.mark.parametrize('damage', ['missing', 'json', 'identity', 'source', 'filesystem'])
def test_broken_package_is_diagnosed_and_paused(tmp_path, harness, damage, monkeypatch):
    manifest = install_cycle(tmp_path, harness)
    with harness.open() as router:
        router.store.request(manifest, promote_candidate(manifest, 'installed'))
        router.before_poll()
        version = manifest['package_id']
        router._runners.clear()
        path = router.releases / version / 'manifest.json'
        if damage == 'missing':
            path.unlink()
        elif damage == 'json':
            path.write_text('{broken', encoding='utf-8')
        elif damage == 'identity':
            altered = dict(manifest, package_id='0' * 64)
            path.write_text(json.dumps(altered), encoding='utf-8')
        elif damage == 'source':
            (router.releases / version / 'limit_cycle.py').write_text('broken source', encoding='utf-8')
        else:
            from pathlib import Path
            original = Path.read_bytes
            def unreadable(self):
                if self == path:
                    raise PermissionError('offline injected permission failure')
                return original(self)
            monkeypatch.setattr(Path, 'read_bytes', unreadable)
        with pytest.raises(PackageLoadError):
            router._is_decision_package(version)
        assert router.reason.startswith('PACKAGE_LOAD_FAILED:')
        assert not router.live_bar
        assert router.store.pending()['phase'] == 'ROLLBACK'
    with harness.open() as router:
        for _ in range(3):
            router.before_poll()
        assert router.store.snapshot()['active'] == LEGACY
        assert router.store.snapshot()['switch']['phase'] == 'ROLLED_BACK'
        assert router.store.pending() is None
        assert not writes(harness.requests)
        assert not router._is_decision_package(LEGACY)


def test_missing_dependency_on_active_restart_becomes_controlled_fault(tmp_path, harness):
    archive, manifest = build_decision_package(tmp_path, 'dependency-fault', EXAMPLES,
        strategy_id='limit_cycle', strategy_version='1.0.0',
        entrypoint='limit_cycle:LimitCycle', files=['limit_cycle.py'],
        configuration=dict(mode='hold'), execution_mode='orders',
        dependencies=['nonexistent-factorlab-pkg==9.9.9'])
    harness.install(archive)
    with harness.open() as router:
        # Restore an installed active release whose pinned dependency is unavailable.
        with router.store.atomic():
            router.store.db.execute('UPDATE deployment SET active=? WHERE id=1', (manifest['package_id'],))
        router.before_poll()
        assert not router.live_bar
        assert router.store.pending() is not None
        assert router.reason.startswith('PACKAGE_LOAD_FAILED:')
        assert not writes(harness.requests)
    with harness.open() as router:
        for _ in range(3):
            router.before_poll()
        assert router.store.snapshot()['active'] == LEGACY
        assert router.store.snapshot()['switch']['phase'] == 'ROLLED_BACK'
        assert router.store.pending() is None
        assert not writes(harness.requests)
