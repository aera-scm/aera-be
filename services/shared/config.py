"""Cached configuration accessor (SRD 6.23): Config table values over defaults, 60 s cache.

Administrators change values in the Config table; services see the change within a minute
without a redeploy. Keys outside the catalogue are refused, so a typo cannot silently fall
back to nothing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from services.shared.defaults import CONFIG_DEFAULTS
from services.shared.dynamo import from_item, table_name

CACHE_SECONDS = 60.0


class Config:
    def __init__(
        self,
        client: Any,
        env: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._table = table_name("config", env)
        self._clock = clock
        self._cache: dict[str, tuple[float, Decimal | str]] = {}

    def get(self, key: str) -> Decimal | str:
        if key not in CONFIG_DEFAULTS:
            raise KeyError(f"{key} is not in the configuration catalogue (SRD 6.23)")
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] < CACHE_SECONDS:
            return cached[1]
        item = self._client.get_item(TableName=self._table, Key={"PK": {"S": f"CFG#{key}"}}).get(
            "Item"
        )
        value: Decimal | str = from_item(item)["value"] if item else CONFIG_DEFAULTS[key]
        self._cache[key] = (self._clock(), value)
        return value

    def decimal(self, key: str) -> Decimal:
        return Decimal(str(self.get(key)))

    def kill_switch(self) -> bool:
        return str(self.get("KILL_SWITCH")).lower() in {"on", "true", "1"}
