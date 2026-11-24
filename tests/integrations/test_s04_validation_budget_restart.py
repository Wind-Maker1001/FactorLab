"""Exit reservations survive restart until terminal zero-fill reconciliation."""
from test_s04_validation_budget import install_cycle, seed_posts
from test_unified_cloud_strategy_s04 import harness, promote_candidate
from test_binance_demo_grid_network import network_grid, writes
from test_binance_demo_network import wired


def test_exit_reservation_persists_across_restart(tmp_path, harness):
    manifest = install_cycle(tmp_path, harness)
    with harness.open() as router:
        seed_posts(router.bridge.runtime, 98)
        router.store.request(manifest, promote_candidate(manifest, 'restart-budget'))
        router.before_poll()
        harness.set_time(1, 1)
        harness.quote(router, '150')
        reservation = router.bridge.runtime.db.execute('SELECT * FROM exit_budget_reservations').fetchone()
        assert reservation is not None
    harness.set_time(1, 2)
    with harness.open() as router:
        router.before_poll()
        assert router.bridge.runtime.db.execute('SELECT * FROM exit_budget_reservations').fetchone() == reservation
        assert len(writes(harness.requests)) == 1
        entry_id = writes(harness.requests)[0]['newClientOrderId'][0]
        router.bridge.session.cancel(entry_id)
        harness.set_time(1, 3)
        router.before_poll()
        assert router.bridge.runtime.db.execute('SELECT count(*) FROM exit_budget_reservations').fetchone()[0] == 0
        assert len(writes(harness.requests)) == 1


def test_below_minimum_partial_inventory_remains_recoverable_not_flat(tmp_path, harness):
    from decimal import Decimal as D
    manifest = install_cycle(tmp_path, harness)
    with harness.open() as router:
        seed_posts(router.bridge.runtime, 98)
        router.store.request(manifest, promote_candidate(manifest, 'dust-budget'))
        router.before_poll()
        harness.set_time(1, 1)
        harness.quote(router, '150')
        key = writes(harness.requests)[0]['newClientOrderId'][0]
        harness.fake.trade_update(key, '.01', '148')
        for second in (2, 3, 4):
            harness.set_time(1, second)
            router.before_poll()
            harness.quote(router, '148')
        assert router.bridge.session.status()['positions'][0]['quantity'] == D('.01')
        runner = router._load_decision_runner(manifest['package_id'])[2]
        assert runner.journal._latest()['recovery_required'] == 'EXIT_PENDING'
        # Existing reduce-only admission legally accepts the dust exit; an
        # unfilled exit still does not establish a flat account.
        assert len(writes(harness.requests)) == 2
        assert writes(harness.requests)[1]['reduceOnly'] == ['true']
        assert router.bridge.runtime.db.execute('SELECT count(*) FROM exit_budget_reservations').fetchone()[0] == 0
        router.store.rollback('cannot-claim-flat')
        for second in (5, 6):
            harness.set_time(1, second)
            router.before_poll()
        assert router.store.pending()['reason'] in {'WAIT_TERMINAL_ORDERS', 'WAIT_OLD_POSITIONS'}
        assert router.bridge.session.status()['positions'][0]['quantity'] == D('.01')
