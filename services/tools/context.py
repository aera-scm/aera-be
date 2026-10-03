"""What every agent tool gets: SAP, stores, bus, configuration, rate card, clock (SRD 6.3.2).

Tools return plain JSON (Decimal becomes a JSON number) and carry a `sourceRef` for every
figure (FR-IMP-03). A tool that cannot answer returns `{"error": ...}` so the model can
correct itself; it never invents a value.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from services.shared.cases import CaseStore
from services.shared.config import Config
from services.shared.ratecard import RateCard
from services.shared.sap_client import SapClient
from services.shared.signals import SignalStore


class ToolError(Exception):
    """A request the tool refuses; the message goes back to the model."""


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value


@dataclass
class ToolContext:
    sap: SapClient
    dynamodb: Any
    bus: Any
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    env: str | None = None
    run_id: str | None = None
    actor: str = "agent"

    def __post_init__(self) -> None:
        self.cases = CaseStore(self.dynamodb, self.env, clock=self.clock)
        self.signals = SignalStore(self.dynamodb, self.env)
        self.config = Config(self.dynamodb, self.env)
        self.rates = RateCard(self.dynamodb, self.env)

    def now(self) -> datetime:
        return self.clock()


def parse_time(value: str | None, *, default: datetime | None = None) -> datetime:
    if not value:
        if default is None:
            raise ToolError("a date or time is required")
        return default
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"{value!r} is not an ISO 8601 date or time") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def decimal(value: Any, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ToolError(f"{name} must be a number") from None
    if not number.is_finite() or number < 0:
        raise ToolError(f"{name} must be a non-negative number")
    return number


def json_dict(value: dict[str, Any]) -> dict[str, Any]:
    """`jsonable` for a tool result object."""
    result: dict[str, Any] = jsonable(value)
    return result
