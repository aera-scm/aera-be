"""Audited conditional approval decisions and durable delivery outbox (FR-RTE-03/07)."""

from dataclasses import asdict
from datetime import datetime
from typing import Any

from services.routing.logic import Route, eligible
from services.shared.audit import AuditWriter, TransactionConflictError
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.models import ApproverLimit
from services.verifier.logic import Verification, plan_hash


class ApprovalConflict(ValueError):
    pass


class ControlStore:
    def __init__(self, client: Any, env: str | None = None) -> None:
        self.client = client
        self.table = table_name("cases", env)
        self.config = table_name("config", env)
        self.audit = AuditWriter(client, env)

    def get(self, case_id: str, sk: str) -> dict[str, Any] | None:
        item = self.client.get_item(
            TableName=self.table,
            Key=to_item({"PK": f"CASE#{case_id}", "SK": sk}),
            ConsistentRead=True,
        ).get("Item")
        return from_item(item) if item else None

    def _put(
        self,
        item: dict[str, Any],
        condition: str = "attribute_not_exists(PK)",
        values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        put: dict[str, Any] = {
            "TableName": self.table,
            "Item": to_item(item),
            "ConditionExpression": condition,
        }
        if values:
            put["ExpressionAttributeValues"] = to_item(values)
        return {"Put": put}

    def limits(self) -> list[ApproverLimit]:
        args: dict[str, Any] = {
            "TableName": self.config,
            "ConsistentRead": True,
            "FilterExpression": "begins_with(PK, :prefix)",
            "ExpressionAttributeValues": {":prefix": {"S": "APPR#"}},
        }
        limits = []
        while True:
            page = self.client.scan(**args)
            for item in page.get("Items", []):
                data = from_item(item)
                data.pop("PK", None)
                data.pop("grantedAt", None)
                limits.append(ApproverLimit.model_validate(data))
            if "LastEvaluatedKey" not in page:
                return limits
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def save(
        self, verification: Verification, result: Route, *, plant: str, now: datetime
    ) -> dict[str, Any]:
        plan = verification.record.plan
        if result.version_hash != plan_hash(plan):
            raise ApprovalConflict("stale route")
        case_id = plan.case_id
        sk = f"ROUTE#{plan.plan_version}"
        existing = self.get(case_id, sk)
        if existing:
            if existing["versionHash"] != result.version_hash:
                raise ApprovalConflict("plan version already routed with another hash")
            return existing
        item = {
            "PK": f"CASE#{case_id}",
            "SK": sk,
            "versionHash": result.version_hash,
            "version": plan.plan_version,
            "plant": plant,
            "routedAt": now.isoformat(),
            "tier": result.tier,
            "reason": result.reason,
            "parts": [asdict(part) for part in result.parts],
            "verifiedPlan": verification.record.model_dump(mode="python", by_alias=True),
        }
        writes = [
            self._put(item),
            {
                "ConditionCheck": {
                    "TableName": self.table,
                    "Key": to_item({"PK": f"CASE#{case_id}", "SK": "META"}),
                    "ConditionExpression": "planVersion = :v",
                    "ExpressionAttributeValues": to_item({":v": plan.plan_version}),
                }
            },
        ]
        for part in result.parts:
            writes.append(
                self._put(
                    {
                        "PK": f"CASE#{case_id}",
                        "SK": f"PART#{part.id}",
                        **asdict(part),
                        "versionHash": result.version_hash,
                        "version": plan.plan_version,
                        "plant": plant,
                        "revision": 0,
                        "decision": None,
                        "reminded": False,
                        "expired": False,
                        "routedAt": now.isoformat(),
                    }
                )
            )
            if part.tier == 1:
                writes.append(
                    self._outbox(
                        case_id,
                        part.id,
                        "PlanApproved",
                        {
                            "planPartId": part.id,
                            "planVersionHash": result.version_hash,
                            "optionIds": list(part.options),
                            "approvalKind": "AUTO",
                        },
                    )
                )
        writes.append(
            self._outbox(
                case_id,
                result.version_hash,
                "PlanRouted",
                {"tier": result.tier, "planVersionHash": result.version_hash},
            )
        )
        self.audit.record(
            f"CASE#{case_id}",
            "PLAN_ROUTED",
            actor="system",
            case_id=case_id,
            payload={"tier": result.tier, "versionHash": result.version_hash},
            extra=writes,
        )
        return item

    def _outbox(self, case_id: str, key: str, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._put(
            {
                "PK": f"CASE#{case_id}",
                "SK": f"OUTBOX#{kind}#{key}",
                "eventType": kind,
                "data": {"caseId": case_id, **data},
                "sent": False,
            }
        )

    def decide(
        self,
        case_id: str,
        *,
        actor: str,
        groups: frozenset[str],
        version_hash: str,
        decision: str,
        comment: str,
        now: datetime,
    ) -> dict[str, Any]:
        if "approver" not in groups:
            raise PermissionError("approver role required")
        if decision not in {"APPROVED", "REJECTED"} or len(comment) > 2000:
            raise ValueError("invalid approval decision or comment")
        if decision == "REJECTED" and not comment.strip():
            raise ValueError("rejection requires a reason")
        meta = self.get(case_id, "META")
        if meta is None:
            raise ApprovalConflict("case not found")
        record = self.get(case_id, f"ROUTE#{meta['planVersion']}")
        if record is None or record["versionHash"] != version_hash:
            raise ApprovalConflict("stale approval; refresh current plan")
        parts = [p for p in record["parts"] if p["tier"] == 2]
        if len(parts) != 1:
            raise ApprovalConflict("no single pending approval")
        part = self.get(case_id, f"PART#{parts[0]['id']}")
        if part is None:
            raise ApprovalConflict("approval missing")
        payload = {
            "actor": actor,
            "decision": decision,
            "comment": comment,
            "planVersionHash": version_hash,
        }
        if part.get("decision") is not None:
            if part.get("decisionPayload") == payload:
                return part
            raise ApprovalConflict("approval already decided")
        if part["approver"] != actor:
            raise PermissionError("approval assigned to another user")
        if part["expired"] or now >= datetime.fromisoformat(part["deadline"]):
            self.tick(case_id, part["id"], now=now)
            raise ApprovalConflict("approval expired; re-verification required")
        eligible_users = eligible(self.limits(), part["plant"], part["cost"], now)
        if actor not in {p.user_id for p in eligible_users}:
            raise PermissionError("current approval limit insufficient")
        current_limit = self.client.get_item(
            TableName=self.config, Key={"PK": {"S": f"APPR#{actor}"}}, ConsistentRead=True
        ).get("Item")
        if current_limit is None:
            raise PermissionError("current approval limit missing")
        limit_data = from_item(current_limit)
        limit_data.pop("PK", None)
        limit_data.pop("grantedAt", None)
        limit = ApproverLimit.model_validate(limit_data)
        if limit.user_id != actor or not eligible([limit], part["plant"], part["cost"], now):
            raise PermissionError("current approval limit insufficient")
        kill = self.client.get_item(
            TableName=self.config, Key={"PK": {"S": "CFG#KILL_SWITCH"}}, ConsistentRead=True
        ).get("Item")
        if kill is None or str(from_item(kill)["value"]).lower() not in {"off", "false", "0"}:
            raise ApprovalConflict("kill switch enabled or unavailable")
        revision = part["revision"]
        changed = {
            **part,
            "decision": decision,
            "decisionPayload": payload,
            "decidedAt": now.isoformat(),
            "revision": revision + 1,
        }
        writes = [
            self._put(
                changed, "revision = :revision AND attribute_exists(PK)", {":revision": revision}
            ),
            {
                "ConditionCheck": {
                    "TableName": self.table,
                    "Key": to_item({"PK": f"CASE#{case_id}", "SK": "META"}),
                    "ConditionExpression": "planVersion = :version",
                    "ExpressionAttributeValues": to_item({":version": part["version"]}),
                }
            },
            {
                "ConditionCheck": {
                    "TableName": self.config,
                    "Key": {"PK": {"S": "CFG#KILL_SWITCH"}},
                    "ConditionExpression": "#value = :value",
                    "ExpressionAttributeNames": {"#value": "value"},
                    "ExpressionAttributeValues": {":value": kill["value"]},
                }
            },
            self._outbox(
                case_id,
                part["id"],
                "PlanApproved" if decision == "APPROVED" else "PlanRejected",
                {**payload, "planPartId": part["id"], "optionIds": part["options"]},
            ),
        ]
        writes.append(
            {
                "ConditionCheck": {
                    "TableName": self.config,
                    "Key": {"PK": {"S": f"APPR#{actor}"}},
                    "ConditionExpression": (
                        "limitUsd = :limit AND plant = :plant AND validFrom = :start "
                        "AND validTo = :end AND #role = :role AND userId = :user"
                    ),
                    "ExpressionAttributeNames": {"#role": "role"},
                    "ExpressionAttributeValues": {
                        ":limit": current_limit["limitUsd"],
                        ":plant": current_limit["plant"],
                        ":start": current_limit["validFrom"],
                        ":end": current_limit["validTo"],
                        ":role": current_limit["role"],
                        ":user": current_limit["userId"],
                    },
                }
            }
        )
        try:
            self.audit.record(
                f"CASE#{case_id}",
                "APPROVAL_DECIDED",
                actor=f"user:{actor}",
                case_id=case_id,
                payload=payload,
                extra=writes,
            )
        except TransactionConflictError:
            raise ApprovalConflict("approval changed; refresh current plan") from None
        return changed

    def tick(self, case_id: str, part_id: str, *, now: datetime) -> None:
        part = self.get(case_id, f"PART#{part_id}")
        if part is None or part["tier"] != 2 or part.get("decision") or part["expired"]:
            return
        deadline = datetime.fromisoformat(part["deadline"])
        reminder = datetime.fromisoformat(part["reminder"])
        if now < reminder or (now < deadline and part["reminded"]):
            return
        changed = dict(part)
        if now >= deadline:
            backups = [
                p.user_id
                for p in eligible(self.limits(), part["plant"], part["cost"], now)
                if p.user_id != part["approver"]
            ]
            changed.update(expired=True, approver=backups[0] if backups else None)
            kind = "APPROVAL_EXPIRED"
            event_type = "CaseReadyForRun" if backups else "PlanRouted"
            data = {
                "reason": "APPROVAL_DEADLINE",
                "backupApproverId": changed["approver"],
                "tier": 3 if not backups else 2,
                "requiresReverification": True,
            }
        else:
            changed["reminded"] = True
            kind = "APPROVAL_REMINDER"
            event_type = "NotificationRequested"
            data = {
                "recipientRole": "approver",
                "approverId": part["approver"],
                "templateId": "APPROVAL_REMINDER",
                "planPartId": part_id,
            }
        changed["revision"] += 1
        try:
            self.audit.record(
                f"CASE#{case_id}",
                kind,
                actor="system",
                case_id=case_id,
                payload=data,
                extra=[
                    self._put(changed, "revision = :revision", {":revision": part["revision"]}),
                    self._outbox(case_id, f"{part_id}#{kind}", event_type, data),
                ],
            )
        except TransactionConflictError:
            return  # A concurrent timer or decision won; its durable state is authoritative.
