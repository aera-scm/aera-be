"""Administration (FR-ADM-01..03, UC-14): thresholds, kill switch, environment reset.

Every change is audited on the `ADMIN` chain with the old and new value. Only keys of the
configuration catalogue (SRD 6.23) can be set, with their value ranges. The kill switch
drops the system to advise-only at once: routing and the execution workflow read it before
any write (FR-ADM-02). Reset restores the Mirror scenario and clears operational records;
the audit trail is never deleted (FR-AUD-02).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from services.shared.audit import AuditWriter
from services.shared.defaults import CONFIG_DEFAULTS
from services.shared.dynamo import from_item, table_name, to_item

RATIOS = {
    "TIER1_MIN_CONFIDENCE",
    "CRITICAL_FIELD_MIN_CONF",
    "GROUNDING_MIN",
    "AUDIT_SAMPLE_RATE",
    "APPROVAL_REMINDER_AT",
}
NOT_EDITABLE = {"KILL_SWITCH", "SCENARIO_T0"}  # own endpoints
CLEARED_TABLES = ("signals", "trace", "ledger", "idempotency", "connections", "dialogue")
REFERENCE_CASE_SEQUENCE = 913


class AdminError(ValueError):
    pass


def mirror_reset_via(
    base_url: str, apply_auth: Callable[[dict[str, str]], None], http: Any = None
) -> Callable[[datetime], dict[str, Any]]:
    """POST /admin/reset on the Mirror (needs the MirrorAdmin scope), with a CSRF token."""
    import httpx

    client = http or httpx.Client(timeout=30.0)

    def reset(t0: datetime) -> dict[str, Any]:
        headers = {"x-csrf-token": "Fetch", "Accept": "application/json"}
        apply_auth(headers)
        token = client.get(
            f"{base_url.rstrip('/')}/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/",
            headers=headers,
        )
        headers["x-csrf-token"] = token.headers.get("x-csrf-token", "")
        response = client.post(
            f"{base_url.rstrip('/')}/admin/reset",
            json={"t0": t0.astimezone(UTC).isoformat().replace("+00:00", "Z")},
            headers=headers,
            cookies=token.cookies,
        )
        if response.status_code != 200:
            raise AdminError(f"Mirror reset failed with HTTP {response.status_code}")
        body: dict[str, Any] = response.json()
        return body

    return reset


@dataclass
class Admin:
    dynamodb: Any
    mirror_reset: Callable[[datetime], dict[str, Any]] | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    env: str = "dev"

    def __post_init__(self) -> None:
        self.audit = AuditWriter(self.dynamodb, self.env)
        self._config = table_name("config", self.env)

    def _current(self, key: str) -> Any:
        item = self.dynamodb.get_item(
            TableName=self._config, Key={"PK": {"S": f"CFG#{key}"}}, ConsistentRead=True
        ).get("Item")
        return from_item(item)["value"] if item else CONFIG_DEFAULTS.get(key)

    def _write(self, key: str, value: Any, actor: str, kind: str) -> dict[str, Any]:
        old = self._current(key)
        self.dynamodb.put_item(
            TableName=self._config,
            Item=to_item(
                {
                    "PK": f"CFG#{key}",
                    "key": key,
                    "value": value,
                    "changedBy": actor,
                    "changedAt": self.clock().isoformat(),
                }
            ),
        )
        self.audit.record(
            "ADMIN", kind, actor=actor, payload={"key": key, "old": str(old), "new": str(value)}
        )
        return {"key": key, "old": str(old), "new": str(value)}

    def set_config(self, key: str, raw: Any, actor: str) -> dict[str, Any]:
        """FR-ADM-01: thresholds of BR-05, BR-02, BR-11 and the rest of the catalogue."""
        if key not in CONFIG_DEFAULTS or key in NOT_EDITABLE:
            raise AdminError(f"{key} is not an editable configuration key (SRD 6.23)")
        default = CONFIG_DEFAULTS[key]
        if isinstance(default, Decimal):
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                raise AdminError(f"{key} must be a number") from None
            if not value.is_finite() or value < 0:
                raise AdminError(f"{key} must be a non-negative number")
            if key in RATIOS and value > 1:
                raise AdminError(f"{key} is a ratio between 0 and 1")
            return self._write(key, value, actor, "CONFIG_CHANGED")
        if not isinstance(raw, str) or not raw.strip():
            raise AdminError(f"{key} must be text")
        return self._write(key, raw.strip(), actor, "CONFIG_CHANGED")

    def kill_switch(self, on: Any, actor: str) -> dict[str, Any]:
        """FR-ADM-02: advise-only at once; running workflows halt between writes."""
        if not isinstance(on, bool):
            raise AdminError("on must be true or false")
        return self._write("KILL_SWITCH", "on" if on else "off", actor, "KILL_SWITCH_SET")

    def reset(self, confirm: Any, actor: str) -> dict[str, Any]:
        """FR-ADM-03: Mirror seed data, cases, ledger and signals back to the scenario start."""
        if confirm != f"RESET {self.env}":
            raise AdminError(f'type "RESET {self.env}" to confirm')
        if self.mirror_reset is None:
            raise AdminError("the Mirror reset is not configured in this environment")
        t0 = self.clock()
        mirror = self.mirror_reset(t0)
        removed = {name: self._clear(name) for name in CLEARED_TABLES}
        removed["cases"] = self._clear("cases", keep_prefixes=("AUDIT#",))
        self.dynamodb.put_item(
            TableName=table_name("cases", self.env),
            Item={
                "PK": {"S": f"COUNTER#CASE#{t0.year}"},
                "SK": {"S": "COUNTER"},
                "seq": {"N": str(REFERENCE_CASE_SEQUENCE)},
            },
        )
        self._write("SCENARIO_T0", t0.isoformat(), actor, "ENVIRONMENT_RESET")
        return {"t0": t0.isoformat(), "mirror": mirror, "removed": removed}

    def _clear(self, name: str, keep_prefixes: tuple[str, ...] = ()) -> int:
        table = table_name(name, self.env)
        removed = 0
        arguments: dict[str, Any] = {"TableName": table}
        while True:
            page = self.dynamodb.scan(**arguments)
            for item in page.get("Items", []):
                if item["PK"]["S"].startswith(keep_prefixes) if keep_prefixes else False:
                    continue
                key = {"PK": item["PK"]}
                if "SK" in item:
                    key["SK"] = item["SK"]
                self.dynamodb.delete_item(TableName=table, Key=key)
                removed += 1
            if "LastEvaluatedKey" not in page:
                return removed
            arguments["ExclusiveStartKey"] = page["LastEvaluatedKey"]
