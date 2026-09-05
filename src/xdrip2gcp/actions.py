"""Result type shared by the idempotent operations in this package."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionResult:
    """What an idempotent operation actually did."""

    changed: bool
    detail: str

    def __str__(self) -> str:
        return self.detail

    @property
    def marker(self) -> str:
        """Fixed-width tag for aligned script output."""
        return "changed" if self.changed else "no-op  "
