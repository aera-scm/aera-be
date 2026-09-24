"""FR-LAB-04: persist synthetic Lab runs and follow their real case outcomes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from services.lab.delivery import ChannelReplay
from services.lab.scenario import (
    CONTACTS,
    HOSTILE,
    SEED,
    Artifacts,
    Parameters,
    generate,
    mirror_changes,
)
from services.shared.audit import AuditWriter
from services.shared.cases import CaseStore
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.intake import Attachment, Inbound, Intake
from services.shared.models import CaseStatus, SignalChannel, SignalStatus, new_ulid
from services.shared.signals import SignalStore

TERMINAL = {
    CaseStatus.CLOSED,
    CaseStatus.ESCALATED,
    CaseStatus.REJECTED,
    CaseStatus.ROLLED_BACK,
    CaseStatus.FAILED_ROLLED_BACK,
}


class LabError(ValueError):
    pass


def mirror_patch_via(
    base_url: str, apply_auth: Callable[[dict[str, str]], None], http: Any = None
) -> Callable[[list[dict[str, object]]], None]:
    """Use MirrorAdmin CSRF/session and bounded mutations prepared by scenario.py."""
    import httpx

    client = http or httpx.Client(timeout=30.0)

    def patch(changes: list[dict[str, object]]) -> None:
        headers = {"x-csrf-token": "Fetch", "Accept": "application/json"}
        apply_auth(headers)
        base = base_url.rstrip("/")
        token = client.get(
            f"{base}/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/", headers=headers
        )
        token.raise_for_status()
        headers["x-csrf-token"] = token.headers.get("x-csrf-token", "")
        import json

        response = client.post(
            f"{base}/admin/patch",
            json={"changes": json.dumps(changes)},
            headers=headers,
            cookies=token.cookies,
        )
        if response.status_code != 200:
            raise LabError(f"Mirror patch failed with HTTP {response.status_code}")

    return patch


@dataclass
class Lab:
    dynamodb: Any
    intake: Intake
    mirror_patch: Callable[[list[dict[str, object]]], None]
    delivery: ChannelReplay | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    env: str = "dev"

    def __post_init__(self) -> None:
        self.table = table_name("cases", self.env)
        self.signals = SignalStore(self.dynamodb, self.env)
        self.cases = CaseStore(self.dynamodb, self.env)
        self.audit = AuditWriter(self.dynamodb, self.env)

    def start(self, raw: dict[str, Any], actor: str) -> dict[str, Any]:
        try:
            params = Parameters.model_validate(raw)
        except ValidationError as error:
            raise LabError(str(error)) from None
        run_id = new_ulid()
        now = self.clock().astimezone(UTC)
        po, _, supplier = SEED[params.material]
        record = {
            "PK": f"LAB#{run_id}",
            "SK": "META",
            "runId": run_id,
            "synthetic": True,
            "deliveryMode": "channel-replay" if self.delivery else "internal-replay",
            "parameters": params.model_dump(mode="json", by_alias=True),
            "poNumber": po,
            "createdAt": now.isoformat(),
            "status": "PREPARING",
            "outcome": "IN_PROGRESS",
            "signalIds": [],
            "receiptKeys": [],
            "caseId": None,
        }
        self.dynamodb.put_item(
            TableName=self.table,
            Item=to_item(record),
            ConditionExpression="attribute_not_exists(PK)",
        )
        self.audit.record(
            f"LAB#{run_id}",
            "LAB_STARTED",
            actor=actor,
            payload={"parameters": record["parameters"], "synthetic": True},
        )
        try:
            self.mirror_patch(mirror_changes(params, now))
            artifacts = generate(params, now, run_id)
            ids: list[str] = []
            receipts: list[dict[str, str]] = []
            if self.delivery:
                receipts = [
                    {"channel": channel, "dedupKey": dedup}
                    for channel, dedup in self.delivery.deliver(params, artifacts, run_id, now)
                ]
            else:
                inbound = self._inbound(params, artifacts, supplier, now, run_id)
                main = self.intake.receive(inbound)
                ids = [main.signal_id]
                if artifacts.hostile_email is not None:
                    hostile = self.intake.receive(
                        Inbound(
                            channel=SignalChannel.EMAIL,
                            sender_id=CONTACTS[supplier][0],
                            body=artifacts.hostile_email,
                            content_type="message/rfc822",
                            dedup_key=f"lab-{run_id}-hostile",
                            normalized_text=HOSTILE,
                            po_number=po,
                            material=params.material,
                            received_at=now,
                        )
                    )
                    ids.append(hostile.signal_id)
        except Exception:
            self._save(run_id, {"status": "FAILED", "outcome": "DELIVERY_FAILED"})
            self.audit.record(
                f"LAB#{run_id}",
                "LAB_FAILED",
                actor="system",
                payload={"reason": "mutation or signal submission failed"},
            )
            raise LabError(
                "Lab run failed before signal delivery; inspect services and reset Mirror"
            ) from None
        self._save(run_id, {"status": "SUBMITTED", "signalIds": ids, "receiptKeys": receipts})
        self.audit.record(
            f"LAB#{run_id}",
            "LAB_SUBMITTED",
            actor="system",
            payload={"signalIds": ids, "deliveryMode": record["deliveryMode"]},
        )
        return self.get(run_id)

    def _inbound(
        self, params: Parameters, artifacts: Artifacts, supplier: str, now: datetime, run_id: str
    ) -> Inbound:
        po, _, _ = SEED[params.material]
        sender, phone = CONTACTS[supplier]
        if params.channel == "EMAIL":
            return Inbound(
                channel=SignalChannel.EMAIL,
                sender_id=sender,
                body=artifacts.email,
                content_type="message/rfc822",
                attachments=(
                    Attachment(f"confirmation-{po}.pdf", artifacts.pdf, "application/pdf"),
                ),
                normalized_text=artifacts.text,
                po_number=po,
                material=params.material,
                dedup_key=f"lab-{run_id}-email",
                received_at=now,
            )
        if params.channel == "WHATSAPP":
            return Inbound(
                channel=SignalChannel.WHATSAPP,
                sender_id=phone,
                body=artifacts.photo,
                content_type="image/png",
                attachments=(Attachment(f"confirmation-{po}.png", artifacts.photo, "image/png"),),
                normalized_text=artifacts.text,
                po_number=po,
                material=params.material,
                dedup_key=f"lab-{run_id}-photo",
                received_at=now,
            )
        return Inbound(
            channel=SignalChannel.CARRIER,
            sender_id="1000950",
            body=artifacts.carrier_event,
            content_type="application/json",
            normalized_text=artifacts.carrier_event.decode(),
            po_number=po,
            material=params.material,
            dedup_key=f"lab-{run_id}-carrier",
            received_at=now,
        )

    def _save(self, run_id: str, data: dict[str, Any]) -> None:
        data = {key: value for key, value in data.items() if value is not None}
        if not data:
            return
        names = {f"#k{index}": key for index, key in enumerate(data)}
        values = {f":v{index}": value for index, value in enumerate(data.values())}
        self.dynamodb.update_item(
            TableName=self.table,
            Key=to_item({"PK": f"LAB#{run_id}", "SK": "META"}),
            UpdateExpression="SET "
            + ", ".join(f"#k{index} = :v{index}" for index in range(len(data))),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=to_item(values),
        )

    def get(self, run_id: str) -> dict[str, Any]:
        item = self.dynamodb.get_item(
            TableName=self.table,
            Key=to_item({"PK": f"LAB#{run_id}", "SK": "META"}),
            ConsistentRead=True,
        ).get("Item")
        if item is None:
            raise LabError("Lab run not found")
        row = from_item(item, keep_decimals=False)
        row.pop("PK", None)
        row.pop("SK", None)
        if row["status"] != "SUBMITTED":
            return row
        resolved = self._resolved_signals(row)
        ids = [signal.signal_id for signal in resolved if signal]
        main = resolved[0] if resolved else None
        case_id = main.case_id if main else None
        case = self.cases.get(case_id) if case_id else None
        hostile = resolved[1] if len(resolved) > 1 else None
        outcome = "IN_PROGRESS"
        if main and main.status is SignalStatus.QUARANTINED:
            outcome = "SIGNAL_BLOCKED"
        elif hostile and hostile.status is SignalStatus.ACCEPTED:
            outcome = "HOSTILE_NOT_BLOCKED"
        elif hostile and hostile.status is SignalStatus.RECEIVED:
            outcome = "IN_PROGRESS"
        elif case and case.status in TERMINAL:
            outcome = "RESOLVED" if case.status is CaseStatus.CLOSED else "ESCALATED"
        updates: dict[str, Any] = {
            "signalIds": ids,
            "caseId": case_id,
            "outcome": outcome,
            "hostileBlocked": hostile.status is SignalStatus.QUARANTINED if hostile else None,
        }
        if case_id and "verifiedAt" not in row:
            verified = next(
                (
                    event
                    for event in self.audit.events(f"CASE#{case_id}")
                    if event.type == "STATE_TRANSITION" and event.payload.get("to") == "VERIFIED"
                ),
                None,
            )
            if verified:
                updates["verifiedAt"] = verified.ts.isoformat()
                updates["timeToVerifiedSeconds"] = max(
                    0,
                    round(
                        (verified.ts - datetime.fromisoformat(row["createdAt"])).total_seconds(), 3
                    ),
                )
        if updates != {key: row.get(key) for key in updates}:
            changed_outcome = outcome != row.get("outcome")
            self._save(run_id, updates)
            row.update(updates)
            if outcome != "IN_PROGRESS" and changed_outcome:
                self.audit.record(
                    f"LAB#{run_id}",
                    "LAB_OUTCOME",
                    actor="system",
                    payload={"outcome": outcome, "caseId": case_id},
                )
        return row

    def _resolved_signals(self, row: dict[str, Any]) -> list[Any]:
        receipts = row.get("receiptKeys") or []
        if not receipts:
            return [self.signals.get(signal_id) for signal_id in row["signalIds"]]
        resolved: list[Any] = []
        for receipt in receipts:
            key = f"INTAKE#{receipt['channel']}#{receipt['dedupKey']}"
            item = self.dynamodb.get_item(
                TableName=table_name("idempotency", self.env),
                Key={"PK": {"S": key}},
                ConsistentRead=True,
            ).get("Item")
            resolved.append(self.signals.get(item["signalId"]["S"]) if item else None)
        return resolved

    def list(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        args: dict[str, Any] = {
            "TableName": self.table,
            "FilterExpression": "begins_with(PK, :lab) AND SK = :meta",
            "ExpressionAttributeValues": {":lab": {"S": "LAB#"}, ":meta": {"S": "META"}},
        }
        while True:
            page = self.dynamodb.scan(**args)
            rows.extend(from_item(item, keep_decimals=False) for item in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return [
            self.get(row["runId"])
            for row in sorted(rows, key=lambda row: row["createdAt"], reverse=True)[:50]
        ]
