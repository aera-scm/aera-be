"""BR-15 / FR-ING-08: signals about the same PO and material within 72 h belong to one case."""

from __future__ import annotations

from datetime import datetime, timedelta

WINDOW = timedelta(hours=72)


def same_case(
    po: str | None,
    material: str | None,
    at: datetime,
    other_po: str | None,
    other_material: str | None,
    other_at: datetime,
    window: timedelta = WINDOW,
) -> bool:
    if not (po and material and other_po and other_material):
        return False
    return po == other_po and material == other_material and abs(at - other_at) <= window
