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
