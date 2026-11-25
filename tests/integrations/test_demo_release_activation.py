from contextlib import ExitStack, closing
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from test_binance_demo_grid_network import network_grid
from test_binance_demo_network import wired
from test_demo_grid_supervisor import Control
from test_strategy_deployment_s03 import package, candidate
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, open_grid_session
from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import GridConsumer
from factorlab.integrations.binance_futures_trading.demo_strategy_release import ReleaseConsumer
from factorlab.integrations.binance_futures_trading.strategy_package import install_package
from factorlab.integrations.binance_futures_trading.strategy_deployment import LEGACY
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.integrations.binance_futures_trading.fake import NOW


def test_activation_failure_blocks_events_and_legacy_callback_recovers_after_restart(network_grid, package, monkeypatch):
    root, credentials, factory, enable, fake, requests = network_grid
    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(credentials.read_config(), opening_equity=D('1000'),
                                 stop_epoch=runtime.stop_epoch())
    archive, manifest = package
    install_package(archive, root / 'strategy-releases')
    with ExitStack() as stack:
        bridge = stack.enter_context(open_grid_session(root, credentials, factory, continuous=True))
        bridge.recover()
        base = GridConsumer(bridge, Control(), clock=lambda: NOW, ensure_campaign=bridge.ensure_campaign)
        base.supervisor = SimpleNamespace(collection=None, feed=SimpleNamespace())
        router = ReleaseConsumer(base, root, stack)
        activated = router.deployer.adapter.activated
        def fail(version):
            raise RuntimeError('activation interrupted')
        monkeypatch.setattr(router.deployer.adapter, 'activated', fail)
        router.store.request(manifest, candidate(manifest))
        router.before_poll()
        assert router.store.snapshot()['active'] == manifest['package_id']
        assert router.store.pending() is None
        assert router.store.activation_callback() is not None
        with pytest.raises(SafetyBlocked, match='activation recovery'):
            router.gate()
        with pytest.raises(SafetyBlocked, match='activation recovery'):
            router.write_guard('POST', {}, None, None, None, None, None)
        assert router.closed_bar(None) is False
        assert router.quote(None) is None
        monkeypatch.setattr(router.deployer.adapter, 'activated', activated)
        router.before_poll()
        assert router.store.activation_callback() is None
        monkeypatch.setattr(router.deployer.adapter, 'activated', fail)
        router.store.rollback('rollback fixture')
        router.before_poll()
        assert router.store.snapshot()['active'] == LEGACY
        assert router.store.activation_callback() is not None
    with ExitStack() as stack:
        bridge = stack.enter_context(open_grid_session(root, credentials, factory, continuous=True))
        bridge.recover()
        base = GridConsumer(bridge, Control(), clock=lambda: NOW, ensure_campaign=bridge.ensure_campaign)
        base.supervisor = SimpleNamespace(collection=None, feed=SimpleNamespace())
        router = ReleaseConsumer(base, root, stack)
        assert router.store.activation_callback() is not None
        router.before_poll()
        assert router.store.activation_callback() is None
        assert router.store.snapshot()['active'] == LEGACY
