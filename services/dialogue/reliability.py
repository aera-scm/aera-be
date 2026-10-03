"""FR-LRN-01/03: supplier reliability from SAP schedule lines and goods receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import ceil


@dataclass(frozen=True)
class Receipt:
    posted_on: date
    quantity: Decimal
    source_ref: str


@dataclass(frozen=True)
class Schedule:
    supplier_id: str
    material: str
    due_on: date
    quantity: Decimal
    receipts: tuple[Receipt, ...]
    source_ref: str


@dataclass(frozen=True)
class Reliability:
    supplier_id: str
    material: str
    window_days: int
    sample_size: int
    on_time_rate: Decimal
    mean_delay_days: Decimal
    p90_delay_days: int
    partial_rate: Decimal
    computed_at: datetime
    source_refs: tuple[str, ...]


def compute(
    supplier_id: str,
    material: str,
    schedules: list[Schedule],
    computed_at: datetime,
    *,
    window_days: int = 180,
) -> Reliability:
    if not supplier_id or not material or computed_at.tzinfo is None or window_days <= 0:
        raise ValueError("reliability requires supplier, material and an aware observation time")
    first_day = computed_at.date() - timedelta(days=window_days)
    selected = [
        line
        for line in schedules
        if line.supplier_id == supplier_id
        and line.material == material
        and first_day <= line.due_on <= computed_at.date()
    ]
    delays: list[int] = []
    on_time = partial = 0
    refs: set[str] = set()
    for line in selected:
        if line.quantity <= 0 or not line.source_ref.startswith("SAP:"):
            raise ValueError("reliability requires sourced positive SAP schedule quantities")
        refs.add(line.source_ref)
        received = Decimal(0)
        completed_on: date | None = None
        for receipt in sorted(line.receipts, key=lambda r: (r.posted_on, r.source_ref)):
            if (
                receipt.quantity <= 0
                or not receipt.source_ref.startswith("SAP:")
                or receipt.posted_on > computed_at.date()
            ):
                raise ValueError("reliability requires sourced positive SAP goods receipts")
            refs.add(receipt.source_ref)
            received += receipt.quantity
            if received >= line.quantity and completed_on is None:
                completed_on = receipt.posted_on
        if completed_on is not None and completed_on <= line.due_on:
            on_time += 1
        if len(line.receipts) > 1 or received < line.quantity:
            partial += 1
        # Incomplete orders are censored at observation date and remain late.
        delays.append(max(0, ((completed_on or computed_at.date()) - line.due_on).days))
    count = len(selected)
    if not count:
        raise ValueError("no SAP schedule lines in reliability window")
    delays.sort()
    return Reliability(
        supplier_id,
        material,
        window_days,
        count,
        Decimal(on_time) / count,
        Decimal(sum(delays)) / count,
        delays[ceil(count * 0.9) - 1],
        Decimal(partial) / count,
        computed_at,
        tuple(sorted(refs)),
    )
