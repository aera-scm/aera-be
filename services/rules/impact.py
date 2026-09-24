"""Stock-out time and revenue at risk: the deterministic inputs to BR-13 (SRD 2 glossary, FR-IMP).

RaR sums the net value of sales order items whose confirmed delivery falls on or after the
stock-out day and before the day the delayed supply recovers (ADR-0011)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class SalesExposure:
    sales_order: str
    item: str
    net_usd: Decimal
    confirmed: date
    source_ref: str


def hours_to_stockout(on_hand: Decimal, consumption_per_hour: Decimal) -> Decimal | None:
    if consumption_per_hour <= 0:
        return None
    if on_hand <= 0:
        return Decimal(0)
    return on_hand / consumption_per_hour


def stockout_at(now: datetime, on_hand: Decimal, consumption_per_hour: Decimal) -> datetime | None:
    hours = hours_to_stockout(on_hand, consumption_per_hour)
    if hours is None:
        return None
    return now + timedelta(seconds=int((hours * 3600).to_integral_value()))


def revenue_at_risk(
    items: Iterable[SalesExposure],
    *,
    stockout_at: datetime | None,
    recovered_on: date | None = None,
) -> tuple[Decimal, list[SalesExposure]]:
    if stockout_at is None:
        return Decimal(0), []
    first = stockout_at.date()
    threatened = [
        e
        for e in items
        if e.confirmed >= first and (recovered_on is None or e.confirmed < recovered_on)
    ]
    return sum((e.net_usd for e in threatened), Decimal(0)), threatened
