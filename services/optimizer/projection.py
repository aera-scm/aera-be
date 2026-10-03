"""Time-phased stock projection (SRD 6.11 inputs; FR-SIM-01, FR-SIM-02).

Available stock per plant is simulated from SAP facts only: on-hand stock, the consumption
rate, open purchase-order receipts and the receipts or issues an option adds. Stock falls
at the consumption rate while it lasts; at zero the line stops and demand goes unmet until
the next receipt. Output: hourly points for 72 h and daily points to 30 days, stock-out
times, and line-stop windows with the production orders whose requirement falls inside.

The core (`project`) is pure; `PlantInputs` are read from SAP by services/optimizer/inputs.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

HOURLY = 72
DAYS = 30
ZERO = Decimal(0)


@dataclass(frozen=True)
class Movement:
    """A receipt (qty > 0) or an issue (qty < 0) at a plant."""

    at: datetime
    qty: Decimal
    label: str
    source_ref: str


@dataclass(frozen=True)
class Requirement:
    order: str
    at: datetime
    qty: Decimal
    source_ref: str


@dataclass(frozen=True)
class PlantInputs:
    plant: str
    on_hand: Decimal
    rate: Decimal  # consumption per hour
    movements: tuple[Movement, ...] = ()
    requirements: tuple[Requirement, ...] = ()
    source_refs: tuple[str, ...] = ()

    def plus(self, *movements: Movement) -> PlantInputs:
        return PlantInputs(
            self.plant,
            self.on_hand,
            self.rate,
            self.movements + movements,
            self.requirements,
            self.source_refs + tuple(m.source_ref for m in movements),
        )


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime | None  # None: still stopped at the end of the horizon
    orders: tuple[str, ...]
    units_short: Decimal


@dataclass(frozen=True)
class Projection:
    plant: str
    points: tuple[tuple[datetime, Decimal], ...]
    stockouts: tuple[datetime, ...]
    windows: tuple[Window, ...]
    source_refs: tuple[str, ...] = field(default=())

    def stock_at(self, moment: datetime) -> Decimal:
        return next((s for t, s in reversed(self.points) if t <= moment), self.points[0][1])

    @property
    def first_stockout(self) -> datetime | None:
        return self.stockouts[0] if self.stockouts else None

    @property
    def units_short(self) -> Decimal:
        return sum((w.units_short for w in self.windows), ZERO)

    def json(self) -> dict[str, Any]:
        return {
            "plant": self.plant,
            "points": [{"at": t, "stock": s} for t, s in self.points],
            "stockouts": list(self.stockouts),
            "lineStops": [
                {
                    "start": w.start,
                    "end": w.end,
                    "ordersAffected": list(w.orders),
                    "unitsShort": w.units_short,
                }
                for w in self.windows
            ],
            "unitsShort": self.units_short,
            "sourceRefs": sorted(set(self.source_refs)),
        }


def _hours(delta: timedelta) -> Decimal:
    return Decimal(int(delta.total_seconds())) / 3600


def _after(hours: Decimal) -> timedelta:
    return timedelta(seconds=int((hours * 3600).to_integral_value()))


def project(inputs: PlantInputs, now: datetime, *, days: int = DAYS) -> Projection:
    end = now + timedelta(days=days)
    rate = max(inputs.rate, ZERO)
    events = sorted(
        (Movement(max(m.at, now), m.qty, m.label, m.source_ref) for m in inputs.movements),
        key=lambda m: m.at,
    )
    events = [m for m in events if m.at <= end]
    # Breakpoints: (time, stock right after it); stock falls at `rate` until the next one.
    breaks: list[tuple[datetime, Decimal]] = []
    stockouts: list[datetime] = []
    windows: list[tuple[datetime, datetime | None]] = []
    t, stock = now, max(inputs.on_hand, ZERO)
    stopped_since: datetime | None = now if stock == 0 and rate > 0 else None
    if stopped_since is not None:
        stockouts.append(now)
    breaks.append((t, stock))
    for event in [*events, None]:
        until = end if event is None else event.at
        if stock > 0 and rate > 0:
            zero = t + _after(stock / rate)
            if zero <= until:
                stock = ZERO
                breaks.append((zero, ZERO))
                stockouts.append(zero)
                stopped_since = zero
            else:
                stock -= rate * _hours(until - t)
        t = until
        if event is None:
            break
        stock += event.qty
        if stock <= 0:
            stock = ZERO
            if stopped_since is None and rate > 0:
                stopped_since = t
                stockouts.append(t)
        elif stopped_since is not None:
            windows.append((stopped_since, t))
            stopped_since = None
        breaks.append((t, stock))
    if stopped_since is not None:
        windows.append((stopped_since, None))

    def value(moment: datetime) -> Decimal:
        start, level = next((b for b in reversed(breaks) if b[0] <= moment), breaks[0])
        return max(level - rate * _hours(moment - start), ZERO)

    samples = [now + timedelta(hours=h) for h in range(HOURLY + 1)]
    samples += [now + timedelta(days=d) for d in range(HOURLY // 24 + 1, days + 1)]
    return Projection(
        plant=inputs.plant,
        points=tuple((moment, value(moment)) for moment in samples),
        stockouts=tuple(stockouts),
        windows=tuple(
            Window(
                start,
                stop,
                tuple(
                    r.order
                    for r in sorted(inputs.requirements, key=lambda r: r.at)
                    if start <= r.at < (stop or end)
                ),
                rate * _hours((stop or end) - start),
            )
            for start, stop in windows
        ),
        source_refs=inputs.source_refs,
    )
