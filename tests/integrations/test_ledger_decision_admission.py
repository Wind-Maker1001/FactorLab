from contextlib import closing
import sqlite3

import pytest

from factorlab.integrations.ledger_replication import BatchReceiver, EventAdmission, ReplicationCursor
from factorlab.integrations.ledger_replay import AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition
from factorlab.strategies.deployment import DeploymentStore, reduce_deployment_decision
from factorlab.strategies.identity import content_identity
from factorlab.strategies.protocol import DecisionV2, decision_payload
from test_strategy_management_v2 import activate, event, package


BASE = SnapshotPosition('account', 'g1', 0, None)


def test_original_decision_and_ack_recover_after_lost_acks_without_inference(tmp_path):
    manifest, release = package(tmp_path)
    path = tmp_path / 'deployment.sqlite3'
    with closing(DeploymentStore(path)) as root:
        view, _, _, _ = activate(root, manifest, release, 'one')
        view.close()
    receiver = BatchReceiver(tmp_path / 'offsite', stream=BASE.stream, generation=BASE.generation)
    lose_ack = True

    def send(payload):
        receiver.accept(payload)
        if lose_ack:
            raise TimeoutError('offsite committed; ACK lost')
        return ReplicationCursor(BASE.stream, BASE.generation, payload['sequence'], payload['digest'])

    with closing(sqlite3.connect(tmp_path / 'admission.sqlite3', isolation_level=None)) as journal:
        journal.row_factory = sqlite3.Row

        def bind(db):
            replay = AccountReplay({'deployment': DomainReplay(db, reduce_deployment_decision, baseline=BASE)},
                                   baseline=BASE)
            return AccountEventWriter(EventAdmission(journal, stream=BASE.stream,
                generation=BASE.generation, send=send), replay)

        view = DeploymentStore(path, instance_id='one', event_writer_factory=bind)
        with closing(sqlite3.connect(tmp_path / 'snapshot.sqlite3')) as target:
            view.db.backup(target)
        original = event('one', 'a')
        decision = decision_payload(DecisionV2('quantity_deltas', state={'decisions': 1}))
        with pytest.raises(TimeoutError):
            view.commit_decision(manifest['package_id'], 'a', content_identity(original), decision,
                                 event=original, expected_checkpoint=view.checkpoint())
        assert view.checkpoint() == {}
        assert view.outstanding(manifest['package_id']) == []
        view.close()
        lose_ack = False
        view = DeploymentStore(path, instance_id='one', event_writer_factory=bind)
        receipt, = view.outstanding(manifest['package_id'])
        assert receipt['decision'] == decision
        assert view.checkpoint() == {'decisions': 1}
        assert view.commit_decision(manifest['package_id'], 'a', content_identity(original), decision,
                                   event=original, expected_checkpoint={}) == receipt
        lose_ack = True
        with pytest.raises(TimeoutError):
            view.acknowledge(receipt)
        assert view.outstanding(manifest['package_id']) == [receipt]
        view.close()
        lose_ack = False
        view = DeploymentStore(path, instance_id='one', event_writer_factory=bind)
        assert view.outstanding(manifest['package_id']) == []
        view.acknowledge(receipt)
        assert receiver.cursor().sequence == 2
        view.close()

    path.unlink()
    with closing(DeploymentStore(tmp_path / 'snapshot.sqlite3', instance_id='one')) as restored:
        replay = AccountReplay({'deployment': DomainReplay(restored.db, reduce_deployment_decision,
                                                           baseline=BASE)}, baseline=BASE)
        assert replay.catch_up(receiver).sequence == 2
        assert restored.checkpoint() == {'decisions': 1}
        assert restored.snapshot()['watermark'] == 'a'
        assert restored.decision(manifest['package_id'], 'a', content_identity(original)) == receipt
        assert restored.outstanding(manifest['package_id']) == []
        with pytest.raises(PermissionError, match='account event writer'):
            restored.acknowledge(receipt)
    receiver.close()
