"""Market adapter interfaces; account execution ownership remains with the host."""
from typing import Protocol

from .protocol import StrategySpecV2


class DecisionExecutionAdapter(Protocol):
    def qualify(self, spec: StrategySpecV2, binding: dict, allocation: dict) -> None:
        """Reject unmet requirements without allocating funds or granting authority."""
        ...

    def execute(self, receipt: dict) -> None:
        """Resume durable instance/allocation-owned intents through the account owner.

        A repeated receipt must not apply quantity deltas twice. A partially
        dispatched portfolio resumes remaining intents using stable identities.
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
