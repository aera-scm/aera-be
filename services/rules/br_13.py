"""BR-13 / FR-TRI-01: score = RaR_usd x U; U = 3 below 24 h to stock-out, 2 below 72 h, else 1."""

from __future__ import annotations

from decimal import Decimal

_NEVER = Decimal("Infinity")


def urgency(hours_to_stockout: Decimal | None) -> int:
    if hours_to_stockout is None:
        return 1
    if hours_to_stockout < 24:
        return 3
    if hours_to_stockout < 72:
        return 2
    return 1


def priority_score(rar_usd: Decimal, hours_to_stockout: Decimal | None) -> Decimal:
    return rar_usd * urgency(hours_to_stockout)


def board_key(rar_usd: Decimal, hours_to_stockout: Decimal | None) -> tuple[Decimal, Decimal]:
    """Ascending sort key: highest score first, ties broken by fewer hours to stock-out."""
    hours = _NEVER if hours_to_stockout is None else hours_to_stockout
    return (-priority_score(rar_usd, hours_to_stockout), hours)


def _hours(value: Decimal | None) -> str:
    if value is None:
        return "no stock-out"
    return f"{value.quantize(Decimal('0.1')).normalize():f} h"


def _usd(value: Decimal) -> str:
    return f"USD {value:,.0f}"


def rank_reason(
    top_id: str,
    top_rar: Decimal,
    top_hours: Decimal | None,
    next_id: str,
    next_rar: Decimal,
    next_hours: Decimal | None,
) -> str:
    """FR-TRI-03: a short, generated (deterministic) reason why one case outranks the next."""
    top_score = priority_score(top_rar, top_hours)
    next_score = priority_score(next_rar, next_hours)
    if top_score == next_score:
        return (
            f"{top_id} ranks above {next_id}: same priority score ({_usd(top_score)}); "
            f"stock runs out sooner ({_hours(top_hours)} vs {_hours(next_hours)})."
        )
    return (
        f"{top_id} ranks above {next_id}: stock runs out in {_hours(top_hours)} vs "
        f"{_hours(next_hours)} (urgency {urgency(top_hours)} vs {urgency(next_hours)}) and "
        f"{_usd(top_rar)} vs {_usd(next_rar)} is at risk."
    )
