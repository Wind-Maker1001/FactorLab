from contextlib import ExitStack, closing
from datetime import timedelta
from decimal import Decimal as D
import json
import sqlite3
import zipfile

import pytest

from factorlab.integrations.binance_futures_trading.strategy_package import (
    INTERFACE, build_package, install_package, load_package, digest,
)
from factorlab.integrations.binance_futures_trading.strategy_deployment import (
    DeploymentStore, DeploymentController, OfflineAdapter, LEGACY,
)
from factorlab.integrations.binance_futures_trading.strategy_worker import invoke
from test_binance_demo_grid_network import network_grid, writes, observe
from test_binance_demo_network import wired
from test_demo_grid_supervisor import Control, bar


@pytest.fixture
def package(tmp_path):
    source = tmp_path / 'code'
    source.mkdir()
    (source / 'custom.py').write_text('def evaluate(context, state):\n'
        '    return {"state": {"count": state.get("count", 0) + 1}, "intents": []}\n')
    manifest = dict(schema_version=1, interface=INTERFACE, environment='BINANCE_USDM_DEMO',
        symbol='NVDAUSDT', code_commit='a' * 40, state_version=1, entrypoint='custom:evaluate',
        files=['custom.py'], dependencies=[], configuration={}, migration='wait_flat',
        evidence=dict(validation='offline-fixture'))
    archive = tmp_path / 'package.zip'
    m = build_package(source, manifest, archive)
    return archive, m


def candidate(m, selection='test-1'):
    return dict(schema_version=1, decision='PROMOTE', qualified=True, strictly_better=True,
                selection_id=selection, package_id=m['package_id'], evidence=m['evidence'], expected_active=LEGACY)


def test_package_deterministic_and_invocable(tmp_path, package):
    archive, m = package
    path, installed = install_package(archive, tmp_path / 'releases')
    assert installed == m == load_package(path)
    assert install_package(archive, tmp_path / 'releases')[0] == path
    assert invoke(path, dict(event='probe'), {}) == dict(state=dict(count=1), intents=[])
    second = tmp_path / 'second.zip'
    build_package(tmp_path / 'code', m, second)
    assert archive.read_bytes() == second.read_bytes()


def test_package_rejects_changed_payload(tmp_path, package):
    archive, _ = package
    forged = tmp_path / 'forged.zip'
    with zipfile.ZipFile(archive) as z, zipfile.ZipFile(forged, 'w') as out:
        out.writestr('manifest.json', z.read('manifest.json'))
        out.writestr('custom.py', b'raise RuntimeError()')
    with pytest.raises(ValueError, match='source identity'):
        install_package(forged, tmp_path / 'releases')


@pytest.mark.parametrize('change', [dict(qualified=False), dict(strictly_better=False),
    dict(evidence=dict(other='unbound')), dict(package_id='unbound'), dict(decision='KEEP')])
def test_non_champions_cannot_deploy(tmp_path, package, change):
    _, m = package
    with closing(DeploymentStore(tmp_path / 'deploy.sqlite3')) as s:
        with pytest.raises(ValueError): s.request(m, candidate(m) | change)
        assert s.snapshot()['active'] == LEGACY and not s.pending()


def test_single_pending_owner_and_duplicate_delivery(tmp_path, package):
    _, m = package
    with closing(DeploymentStore(tmp_path / 'deploy.sqlite3')) as s:
        first = s.request(m, candidate(m))
        assert first == s.request(m, candidate(m))
        with pytest.raises(sqlite3.IntegrityError):
            s.request(m, candidate(m, 'other'))
        assert s.pending()['id'] == first


def test_stale_selector_baseline_cannot_replace_current_strategy(tmp_path, package):
    _, m = package
    with closing(DeploymentStore(tmp_path / 'deploy.sqlite3')) as s:
        with pytest.raises(ValueError, match='baseline'):
            s.request(m, candidate(m) | dict(expected_active='outdated-version'))
        assert not s.pending()


def test_current_s02_native_contract_retains_or_rejects_incompatible_champion(package):
    from factorlab.integrations.binance_futures_trading.champion_release_adapter import adapt_selection
    _, m = package
    native = dict(schema='FACTORLAB_CHAMPION_SELECTION_V1', promoted=False,
                  common=dict(symbol='600000', execution='a_share_daily_long_only_next_open_t1'))
    assert adapt_selection(native, m) is None
    with pytest.raises(ValueError, match='incompatible'):
        adapt_selection(native | dict(promoted=True), m)


def test_native_retain_does_not_need_a_release_archive(tmp_path):
    from factorlab.integrations.binance_futures_trading.azure_strategy_deploy import cycle
    (tmp_path / 'retain.candidate.json').write_text(json.dumps(dict(selection=dict(
        schema='FACTORLAB_CHAMPION_SELECTION_V1', promoted=False))))
    assert cycle(tmp_path, tmp_path) == [dict(file='retain.candidate.json', accepted=False, reason='RETAIN_CHAMPION')]
    assert not (tmp_path / 'strategy-deployment.sqlite3').exists()


def test_inbox_builds_automatically_and_bad_envelope_does_not_starve_valid_candidate(tmp_path, package):
    from factorlab.integrations.binance_futures_trading.azure_strategy_deploy import cycle
    import shutil
    _, m = package
    inbox, root = tmp_path / 'inbox', tmp_path / 'state'
    inbox.mkdir()
    root.mkdir()
    shutil.copytree(tmp_path / 'code', inbox / 'source')
    (inbox / '0.candidate.json').write_text('{broken')
    (inbox / '1.candidate.json').write_text(json.dumps(dict(archive='built.zip', source='source',
        manifest=m, candidate=candidate(m))))
    results = cycle(root, inbox)
    assert not results[0]['accepted'] and results[1]['accepted']
    assert (inbox / 'built.zip').exists()
    with closing(DeploymentStore(root / 'strategy-deployment.sqlite3')) as s:
        assert s.pending()['new'] == m['package_id']


@pytest.mark.parametrize('source', ['import os; os._exit(23)', 'import time; time.sleep(2)'])
def test_worker_crash_or_timeout_does_not_return_order_intents(tmp_path, package, source):
    import subprocess
    archive, m = package
    (tmp_path / 'code/custom.py').write_text(source)
    changed = tmp_path / 'crash.zip'
    build_package(tmp_path / 'code', m, changed)
    release, _ = install_package(changed, tmp_path / 'releases')
    with pytest.raises((ValueError, subprocess.TimeoutExpired)):
        invoke(release, dict(event='probe'), {}, timeout=.3)


def test_restart_switch_requires_new_boot_and_retains_database(tmp_path, package):
    _, m = package
    path = tmp_path / 'deploy.sqlite3'
    s = DeploymentStore(path)
    a = OfflineAdapter()
    s.request(m, candidate(m), mode='restart')
    DeploymentController(s, a).step()
    assert s.pending()['phase'] == 'READY_RESTART'
    s.close()  # abrupt owner loss: no compensation or SQLite restore
    with closing(DeploymentStore(path)) as s:
        assert s.snapshot()['active'] == LEGACY
        a.boot_id = 'offline-2'
        DeploymentController(s, a).step()
        assert s.snapshot()['active'] == m['package_id']
        assert s.snapshot()['switch']['phase'] == 'SUCCEEDED'


@pytest.mark.parametrize('failure', ['network', 'unknown', 'unreconciled', 'foreign'])
def test_uncertain_account_never_activates(tmp_path, package, failure):
    _, m = package
    with closing(DeploymentStore(tmp_path / 'deploy.sqlite3')) as s:
        a = OfflineAdapter()
        s.request(m, candidate(m))
        if failure == 'network': a.failure = failure
        elif failure == 'unknown': a.facts['unknown'] = ['original-id']
        elif failure == 'foreign': a.facts['untracked_orders'] = ['foreign-order']
        else: a.facts['reconciled'] = False
        DeploymentController(s, a).step()
        assert s.snapshot()['active'] == LEGACY
        assert not any(x[0] in {'prepare', 'activate', 'cancel', 'reduce'} for x in a.actions)


def test_partial_fill_cancel_then_wait_real_position(tmp_path, package):
    _, m = package
    with closing(DeploymentStore(tmp_path / 'deploy.sqlite3')) as s:
        a = OfflineAdapter()
        a.facts.update(orders=[dict(client_id='original', status='PARTIALLY_FILLED', reduce_only=False)],
                       positions=[dict(quantity='0.02')])
        s.request(m, candidate(m))
        c = DeploymentController(s, a)
        c.step()
        assert ('cancel', 'original') in a.actions
        a.facts['orders'][0]['status'] = 'CANCELED'
        c.step()
        assert s.pending()['reason'] == 'WAIT_OLD_POSITIONS'
        assert not any(x[0] == 'activate' for x in a.actions)
        a.facts['positions'] = []
        c.step()
        assert s.snapshot()['active'] == m['package_id']


def test_strategy_exception_and_rollback_failure_remain_quiesced(tmp_path, package):
    _, m = package
    with closing(DeploymentStore(tmp_path / 'deploy.sqlite3')) as s:
        a = OfflineAdapter()
        s.request(m, candidate(m))
        a.failure = 'strategy'
        c = DeploymentController(s, a)
        c.step()
        assert s.pending()['phase'] == 'ROLLBACK'
        c.step()
        assert s.pending()['reason'] == 'ROLLBACK_FAILED:ValueError'
        assert s.snapshot()['active'] == LEGACY
        a.failure = None
        a.mark = 'current-ledger-999'
        c.step()
        assert s.snapshot()['watermark'] == 'current-ledger-999'
        assert s.snapshot()['switch']['phase'] == 'ROLLED_BACK'


def test_actual_demo_adapter_switch_and_recovery_without_new_authorization(network_grid, package):
    from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session, DemoRuntime
    from factorlab.integrations.binance_futures_trading.demo_strategy_release import ReleaseConsumer
    from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import GridConsumer
    from factorlab.integrations.binance_futures_trading.fake import NOW
    from types import SimpleNamespace

    root, credentials, factory, enable, fake, requests = network_grid
    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(credentials.read_config(), opening_equity=D('1000'),
                                 stop_epoch=runtime.stop_epoch())
    archive, m = package
    install_package(archive, root / 'strategy-releases')
    with ExitStack() as stack:
        b = stack.enter_context(open_grid_session(root, credentials, factory, continuous=True))
        b.recover()
        observe(b)
        original = next(iter(b.session.status()['orders']))
        base = GridConsumer(b, Control(), clock=lambda: NOW, ensure_campaign=b.ensure_campaign)
        base.supervisor = SimpleNamespace(collection=None, feed=SimpleNamespace())
        router = ReleaseConsumer(base, root, stack)
        before = b.runtime.report('demo-smoke')
        router.store.request(m, candidate(m))
        router.before_poll()
        assert len(writes(requests, 'DELETE')) == 1
        assert writes(requests, 'DELETE')[0]['origClientOrderId'] == [original]
        assert router.store.snapshot()['active'] == LEGACY
        router.before_poll()
        assert router.store.snapshot()['active'] == m['package_id']
        assert b.runtime.report('demo-smoke')['USER_DEMO_AUTHORIZATION'] == before['USER_DEMO_AUTHORIZATION']
        assert len(writes(requests)) == 1
        router.store.rollback('injected runtime fault')
        router.before_poll()
        assert router.store.snapshot()['active'] == LEGACY
        assert router.store.snapshot()['switch']['phase'] == 'ROLLED_BACK'
    with open_grid_session(root, credentials, factory, continuous=True) as b:
        b.recover()
        assert b.session.status()['orders'][original]['status'] == 'CANCELED'
        assert len(writes(requests)) == 1


@pytest.mark.parametrize('failure', ['partial', 'unknown'])
def test_custom_orders_keep_identity_and_budget_during_faults(network_grid, package, failure):
    from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session, DemoRuntime
    from factorlab.integrations.binance_futures_trading.demo_strategy_release import ReleaseConsumer
    from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import GridConsumer
    from factorlab.integrations.binance_futures_trading.fake import NOW
    from types import SimpleNamespace

    root, credentials, factory, _, fake, requests = network_grid
    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(credentials.read_config(), opening_equity=D('1000'),
                                 stop_epoch=runtime.stop_epoch())
    archive, m = package
    install_package(archive, root / 'strategy-releases')
    with ExitStack() as stack:
        b = stack.enter_context(open_grid_session(root, credentials, factory, continuous=True))
        b.recover()
        base = GridConsumer(b, Control(), clock=lambda: NOW, ensure_campaign=b.ensure_campaign)
        base.supervisor = SimpleNamespace(collection=None)
        r = ReleaseConsumer(base, root, stack)
        r.store.request(m, candidate(m))
        r.before_poll()
        base.live_bar = True
        base.latest_book = b.session._fresh_book()
        fake.timeout_next = failure == 'unknown'
        r.send(m['package_id'], 'bar-1', dict(side='BUY', quantity='.04'))
        original = writes(requests)[0]['newClientOrderId'][0]
        if failure == 'partial':
            fake.trade_update(original, '.02', '150')
        r.store.rollback('injected strategy exception')
        r.before_poll()
        r.before_poll()
        assert len(writes(requests)) == 1
        assert writes(requests, 'DELETE')[0]['origClientOrderId'] == [original]
        assert b.runtime.consumption(b.runtime.continuous_authorization())['posts'] == 1
        if failure == 'partial':
            assert r.store.pending()['reason'] == 'WAIT_OLD_POSITIONS'
            assert r.store.snapshot()['active'] == m['package_id']
            assert b.session.status()['positions'][0]['quantity'] == D('.02')
        else:
            assert r.store.snapshot()['active'] == LEGACY
