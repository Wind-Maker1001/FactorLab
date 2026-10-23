"""Explicit diagnostic sources; no network calls during imports."""

from abc import ABC, abstractmethod
from typing import Sequence

from ..contracts import SourceSnapshot


class CorporateActionSource(ABC):
    @abstractmethod
    def snapshot(self, security_ids: Sequence[str], start: str, end: str) -> SourceSnapshot:
        """Acquire evidence, retaining unknown coverage and financial eligibility."""
