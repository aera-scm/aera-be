"""OData V2 JSON value formats as S/4HANA sends them: `/Date(ms)/` and `PThhHmmMssS`."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

_DATE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")
_TIME = re.compile(r"PT(\d+)H(\d+)M(\d+)S")


def edm_datetime(value: Any) -> datetime | None:
    match = _DATE.fullmatch(str(value or ""))
    if match is None:
        return None
    return datetime.fromtimestamp(int(match.group(1)) / 1000, UTC)


def edm_date(value: Any) -> date | None:
    moment = edm_datetime(value)
    return None if moment is None else moment.date()


def edm_duration(value: Any) -> timedelta:
    match = _TIME.fullmatch(str(value or ""))
    if match is None:
        return timedelta(0)
    hours, minutes, seconds = (int(part) for part in match.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def at(date_value: Any, time_value: Any = None) -> datetime | None:
    """A date field plus its companion time field as one UTC moment."""
    moment = edm_datetime(date_value)
    return None if moment is None else moment + edm_duration(time_value)


def number(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def results(value: Any) -> list[dict[str, Any]]:
    """Rows of an expanded navigation (`{"results": [...]}`) or an empty list."""
    if isinstance(value, dict):
        rows = value.get("results")
        return list(rows) if isinstance(rows, list) else []
    return list(value) if isinstance(value, list) else []
