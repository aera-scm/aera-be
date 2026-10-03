"""DynamoDB item conversion and table naming for the stores of SRD 6.20."""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def table_name(component: str, env: str | None = None) -> str:
    return f"aera-{env or os.environ['AERA_ENV']}-{component}"


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def to_item(data: dict[str, Any]) -> dict[str, Any]:
    return {key: _serializer.serialize(value) for key, value in _plain(data).items()}


def to_value(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], _serializer.serialize(_plain(value)))


def _python(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _python(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_python(v) for v in value]
    if isinstance(value, set):
        return sorted(_python(v) for v in value)
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return value


def from_item(item: dict[str, Any], *, keep_decimals: bool = True) -> dict[str, Any]:
    data = {key: _deserializer.deserialize(value) for key, value in item.items()}
    return data if keep_decimals else _python(data)
