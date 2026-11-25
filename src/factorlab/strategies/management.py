"""Market adapter interfaces; account execution ownership remains with the host."""
from typing import Protocol

from .protocol import StrategySpecV2


class DecisionExecutionAdapter(Protocol):
    def qualify(self, spec: StrategySpecV2, binding: dict, allocation: dict) -> None:
        """Reject unmet requirements without allocating funds or granting authority."""
        ...

    def execute(self, receipt: dict) -> None:
        """Durably accept all child intents through the existing account owner.

        receipt['decision'] is a versioned payload map. Its identity is the
        (binding.instance_id, package_id, event_id) tuple. Return only when all
        children are in the owner's journal/outbox, not when fills complete.
        Raise on incomplete persistence; recovery redelivers the same receipt.
        Stable child IDs must prevent repeated quantity deltas after a partial
        dispatch. Unconfirmed orders remain with the account owner's recovery.
        """
        ...


class DeploymentAdapter(Protocol):
    boot_id: str

    def quiesce(self) -> None:
        """Pause only the bound instance, not the shared account owner."""
        ...

    def reconcile(self) -> dict:
        """Return owned orders/positions plus account-level uncertainty blockers."""
        ...

    def watermark(self) -> str: ...
    def cancel(self, client_id: str) -> None: ...
    def reduce(self, switch_id: int, version: str) -> None: ...
    def prepare(self, version: str) -> None: ...
    def activated(self, version: str) -> None: ...
