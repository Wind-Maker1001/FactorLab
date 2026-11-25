"""Binance-qualified compatibility entrypoint for the shared deployment saga."""
from factorlab.strategies.deployment import (
    DeploymentStore as _DeploymentStore, DeploymentController, OfflineAdapter, LEGACY, TERMINAL,
)
from .strategy_package import qualify_manifest


class DeploymentStore(_DeploymentStore):
    def __init__(self, path, *, instance_id=LEGACY, qualify=qualify_manifest):
        super().__init__(path, instance_id=instance_id, qualify=qualify)
