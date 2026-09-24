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
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from services.shared.audit import AuditWriter
from services.shared.defaults import CONFIG_DEFAULTS
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.models import ApproverLimit

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
RATE_ACTIONS = {"STO", "AIR_FREIGHT", "ALTERNATE_SUPPLIER", "PO_DATE_CHANGE", "PO_SPLIT"}
RATE_FIELDS = {
    "entryId",
    "actionType",
    "fromPlant",
    "toPlant",
    "supplierId",
    "lane",
    "unitCostUsd",
    "fixedCostUsd",
    "leadTimeHours",
    "validFrom",
    "validTo",
}


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

    def settings(self) -> dict[str, Any]:
        """FR-UI-11: return current controls, including seeded rate card and limits."""
        rows: list[dict[str, Any]] = []
        args: dict[str, Any] = {"TableName": self._config, "ConsistentRead": True}
        while True:
            page = self.dynamodb.scan(**args)
            rows.extend(from_item(item, keep_decimals=False) for item in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        config = {
            key: next((row["value"] for row in rows if row["PK"] == f"CFG#{key}"), str(default))
            for key, default in CONFIG_DEFAULTS.items()
        }
        return {
            "config": config,
            "rateCard": sorted(
                (
                    {k: v for k, v in row.items() if k not in {"PK", "changedBy", "changedAt"}}
                    for row in rows
                    if row["PK"].startswith("RATE#")
                ),
                key=lambda row: str(row["entryId"]),
            ),
            "approverLimits": sorted(
                (
                    {k: v for k, v in row.items() if k not in {"PK", "grantedBy", "grantedAt"}}
                    for row in rows
                    if row["PK"].startswith("APPR#")
                ),
                key=lambda row: str(row["userId"]),
            ),
        }

    def _replace_catalogue(
        self, prefix: str, identifier: str, value: dict[str, Any], actor: str, kind: str
    ) -> dict[str, Any]:
        key = {"PK": {"S": f"{prefix}#{identifier}"}}
        previous = self.dynamodb.get_item(TableName=self._config, Key=key, ConsistentRead=True).get(
            "Item"
        )
        if previous is None:
            raise AdminError(f"{identifier} is not a configured {prefix} entry")
        old = from_item(previous, keep_decimals=False)
        self.dynamodb.put_item(
            TableName=self._config,
            Item=to_item(
                {
                    "PK": key["PK"]["S"],
                    **value,
                    "changedBy": actor,
                    "changedAt": self.clock().isoformat(),
                }
            ),
        )
        self.audit.record(
            "ADMIN",
            kind,
            actor=actor,
            payload={
                "id": identifier,
                "old": {k: v for k, v in old.items() if k != "PK"},
                "new": value,
            },
        )
        return value

    def set_rate(self, entry_id: str, raw: Any, actor: str) -> dict[str, Any]:
        if not isinstance(raw, dict) or set(raw) - RATE_FIELDS or raw.get("entryId") != entry_id:
            raise AdminError("rate card entry id or fields are invalid")
        if raw.get("actionType") not in RATE_ACTIONS:
            raise AdminError("unknown rate card action type")
        value = dict(raw)
        for key in ("unitCostUsd", "fixedCostUsd", "leadTimeHours"):
            try:
                number = Decimal(str(value[key]))
            except (KeyError, InvalidOperation, ValueError):
                raise AdminError(f"{key} must be a number") from None
            if not number.is_finite() or number < 0:
                raise AdminError(f"{key} must be non-negative and finite")
            value[key] = number
        try:
            start, end = (
                date.fromisoformat(value["validFrom"]),
                date.fromisoformat(value["validTo"]),
            )
        except (KeyError, TypeError, ValueError):
            raise AdminError("rate card validity dates must be ISO dates") from None
        if start > end:
            raise AdminError("rate card validity ends before it starts")
        for key in ("fromPlant", "toPlant", "supplierId", "lane"):
            if key in value and (not isinstance(value[key], str) or not value[key].strip()):
                raise AdminError(f"{key} must be non-empty text")
        return self._replace_catalogue("RATE", entry_id, value, actor, "RATE_CARD_CHANGED")

    def set_approver(self, user_id: str, raw: Any, actor: str) -> dict[str, Any]:
        if not isinstance(raw, dict) or raw.get("userId") != user_id:
            raise AdminError("approver user id is invalid")
        try:
            parsed = ApproverLimit.model_validate(raw)
        except ValueError as error:
            raise AdminError(f"invalid approver limit: {error}") from None
        if not parsed.limit_usd.is_finite() or parsed.limit_usd < 0:
            raise AdminError("approver limit must be non-negative and finite")
        if parsed.valid_from > parsed.valid_to:
            raise AdminError("approver validity ends before it starts")
        return self._replace_catalogue(
            "APPR",
            user_id,
            parsed.model_dump(mode="python", by_alias=True),
            actor,
            "APPROVER_LIMIT_CHANGED",
        )

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
