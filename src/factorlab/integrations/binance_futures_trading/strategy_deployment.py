"""Binance-qualified compatibility entrypoint for the shared deployment saga."""
from factorlab.strategies.deployment import (
    DeploymentStore as _DeploymentStore, DeploymentController, OfflineAdapter, LEGACY, TERMINAL,
)
from .strategy_package import _qualification


class DeploymentStore(_DeploymentStore):
    def __init__(self, path, *, instance_id=LEGACY, qualify=None, event_writer_factory=None):
        self._market_qualifier = qualify
        super().__init__(path, instance_id=instance_id,
                         qualify=lambda manifest: _qualification(manifest, qualify),
                         event_writer_factory=event_writer_factory)

    def for_instance(self, instance_id):
        if self.path == ':memory:':
            raise ValueError('instance views require a persistent database')
        return type(self)(self.path, instance_id=instance_id, qualify=self._market_qualifier,
                          event_writer_factory=self.event_writer_factory)
