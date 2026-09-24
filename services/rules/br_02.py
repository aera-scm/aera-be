"""BR-02 / FR-ING-07: a critical extracted field is usable only if confidence >= 0.95, it matches
SAP exactly, or a planner confirmed it."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import get_args

from services.shared.models import CriticalField, ExtractedField, FieldStatus

MIN_CONFIDENCE = 0.95
CRITICAL_FIELDS: frozenset[str] = frozenset(get_args(CriticalField))
_NUMERIC = {"QUANTITY", "PRICE"}


def _number(value: str) -> Decimal | None:
    try:
        return Decimal(value.strip().replace(",", ""))
    except InvalidOperation:
        return None


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def matches_sap(name: str, value: str, sap_value: str) -> bool:
    """Exact match after normalising representation only (separators, whitespace, case)."""
    if name in _NUMERIC:
        mine, theirs = _number(value), _number(sap_value)
        return mine is not None and mine == theirs
    if name == "DELIVERY_DATE":
        mine_date, theirs_date = _date(value), _date(sap_value)
        return mine_date is not None and mine_date == theirs_date
    return value.strip().upper() == sap_value.strip().upper()


def field_status(
    name: str,
    value: str,
    confidence: float,
    *,
    sap_value: str | None = None,
    confirmed_by: str | None = None,
) -> FieldStatus:
    if confirmed_by:
        return FieldStatus.CONFIRMED
    if sap_value is not None and matches_sap(name, value, sap_value):
        return FieldStatus.SAP_MATCHED
    return FieldStatus.CONFIRMED if confidence >= MIN_CONFIDENCE else FieldStatus.UNCONFIRMED


def usable(field: ExtractedField) -> bool:
    return field.name not in CRITICAL_FIELDS or field.status is not FieldStatus.UNCONFIRMED
