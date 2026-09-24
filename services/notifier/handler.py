"""Notifier: the only component that sends messages (SRD 6.15; FR-COM-01..04, BR-03, IR-06).

On `NotificationRequested`:

- Recipients are resolved by role from SAP master data only: the case supplier's addresses,
  the internal role partners (customer service, production planning) and DR-12 approvers
  for approval reminders. Any other recipient named in a request is refused and audited
  (BR-03), whatever asked for it.
- Messages are rendered from templates and case facts (numbers, SAP document numbers);
  nothing is copied from inbound signals (FR-COM-03).
- In the hackathon environment each master-data address is delivered to a verified
  team-controlled stand-in (FR-COM-04); an address without a stand-in is recorded, not sent.
- Idempotent per case, template, plan part and recipient.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from services.dialogue.facts import load_facts
from services.dialogue.policy import Template, render_question
from services.dialogue.thread import DialogueStatus, Thread, TimeoutAction, start, tick
from services.shared.audit import AuditWriter
from services.shared.cases import CaseStore, ConcurrentUpdateError
from services.shared.config import Config
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.models import Case, CaseStatus
from services.shared.partner import partner_emails
from services.shared.sap_client import SapClient, SapNotFoundError

PO = "API_PURCHASEORDER_PROCESS_SRV"
# Internal roles as business partners in SAP master data (grouping INTL, ADR-0017).
INTERNAL_ROLES = {"customer_service": "9000001", "production_planning": "9000002"}

TEMPLATES: dict[str, tuple[str, str]] = {
    "SUPPLIER_PLAN_CONFIRMATION": (
        "{case}: delivery plan for PO {po}",
        "Dear supplier,\n\nfor purchase order {po} ({material}) Meridian Motors has recorded "
        "the following in SAP: {documents}.\nPlease confirm by reply.\n\nMeridian Motors "
        "supply planning",
    ),
    "CUSTOMER_SERVICE_UPDATE": (
        "{case}: supply recovery for {material}",
        "Supply of {material} at plant {plant} is being recovered. SAP documents: "
        "{documents}. Revenue at risk before action: USD {rar}.",
    ),
    "PRODUCTION_PLANNING_UPDATE": (
        "{case}: parts for Line 2 ({material})",
        "Recovery for {material} at plant {plant} is executed. SAP documents: {documents}. "
        "Projected stock-out before action: {stockout}.",
    ),
    "APPROVAL_REQUEST": (
        "{case}: plan waits for your approval",
        "A verified plan for {case} ({material}, plant {plant}) is routed to you for "
        "approval. Open the AERA console to review it before its deadline.",
    ),
    "APPROVAL_REMINDER": (
        "{case}: approval needed",
        "A plan for {case} ({material}, plant {plant}) waits for your decision. "
        "Open the AERA console to review it before its deadline.",
    ),
}
ROLE_TEMPLATES = {
    "supplier": {"SUPPLIER_PLAN_CONFIRMATION"},
    "customer_service": {"CUSTOMER_SERVICE_UPDATE"},
    "production_planning": {"PRODUCTION_PLANNING_UPDATE"},
    "approver": {"APPROVAL_REQUEST", "APPROVAL_REMINDER"},
}


@dataclass
class Notifier:
    dynamodb: Any
    sap: SapClient
    ses: Any = None
    standins: Callable[[], dict[str, str]] = field(default=lambda: {})
    sender: Callable[[], str] = field(default=lambda: "")
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    env: str | None = None

    def __post_init__(self) -> None:
        self.cases = CaseStore(self.dynamodb, self.env)
        self.audit = AuditWriter(self.dynamodb, self.env)
        self._cases = table_name("cases", self.env)
        self._config = table_name("config", self.env)
        self.config = Config(self.dynamodb, self.env)
        self._dialogue = table_name("dialogue", self.env)

    def handle_dialogue(self, data: dict[str, Any]) -> list[dict[str, str]]:
        case_id, message_id = str(data["caseId"]), str(data["messageId"])
        key = {"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": f"MSG#{message_id}"}}
        item = self.dynamodb.get_item(
            TableName=self._dialogue, Key=key, ConsistentRead=True
        ).get("Item")
        if item is None:
            return []
        draft = from_item(item)
        if draft.get("status") != "DRAFT":
            return [{"status": "DUPLICATE"}]
        try:
            facts = load_facts(
                self.cases, self.sap, case_id, expected_status=CaseStatus.WAITING_SUPPLIER
            )
            expected = render_question(
                facts, str(draft["poNumber"]), Template(str(draft["templateId"])),
                str(draft["referenceToken"]),
            )
            if any((
                draft.get("supplierId") != expected.supplier_id,
                draft.get("recipient") != expected.recipient,
                draft.get("language") != expected.language.value,
                draft.get("renderedText") != expected.rendered_text,
                draft.get("englishCopy") != expected.english_copy,
                draft.get("sourceRef") != expected.source_ref,
            )):
                raise ValueError("V-14: draft no longer matches SAP master and template")
        except (KeyError, ValueError):
            self._block_dialogue(key, "V-14 or master-data check failed")
            return [{"status": "BLOCKED"}]
        recipient = expected.recipient
        case = self.cases.get(case_id)
        if case is None or case.stockout_at is None:
            self._block_dialogue(key, "stock-out time unavailable for supplier timeout")
            return [{"status": "BLOCKED"}]
        try:
            schedule = start(
                case_id, facts.supplier_id, expected.reference_token,
                self.clock(), case.stockout_at,
                timeout=timedelta(hours=float(self.config.decimal("SUPPLIER_REPLY_TIMEOUT_HOURS"))),
            )
        except ValueError:
            self._block_dialogue(key, "no safe supplier reply window")
            return [{"status": "BLOCKED"}]
        standin = self.standins().get(recipient)
        sender = self.sender()
        if not standin or not sender or self.ses is None:
            self._block_dialogue(key, "no verified delivery stand-in")
            return [{"status": "BLOCKED"}]
        try:
            self.dynamodb.update_item(
                TableName=self._dialogue, Key=key,
                UpdateExpression="SET sendClaim = :claim",
                ConditionExpression="#s = :draft AND attribute_not_exists(sendClaim)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":draft": {"S": "DRAFT"}, ":claim": {"S": self.clock().isoformat()},
                },
            )
        except self.dynamodb.exceptions.ConditionalCheckFailedException:
            return [{"status": "DUPLICATE"}]
        try:
            self.ses.send_email(
                Source=sender,
                Destination={"ToAddresses": [standin]},
                Message={
                    "Subject": {"Data": f"{case_id}: PO {expected.po_number} clarification"},
                    "Body": {"Text": {"Data": expected.rendered_text}},
                },
            )
        except Exception:  # noqa: BLE001 - ambiguous SES failure must not trigger a duplicate send
            self.dynamodb.update_item(
                TableName=self._dialogue, Key=key,
                UpdateExpression="SET #s = :blocked, blockReason = :reason",
                ConditionExpression="#s = :draft AND attribute_exists(sendClaim)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":draft": {"S": "DRAFT"}, ":blocked": {"S": "BLOCKED"},
                    ":reason": {"S": "SES send result unavailable"},
                },
            )
            self._ensure_dialogue_escalated(case_id, message_id, "supplier send unavailable")
            return [{"status": "BLOCKED"}]
        self.dynamodb.update_item(
            TableName=self._dialogue, Key=key,
            UpdateExpression=(
                "SET #s = :sent, sentAt = :now, remindAt = :remind, "
                "deadline = :deadline, reminderSent = :no"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":sent": {"S": "SENT"}, ":now": {"S": schedule.sent_at.isoformat()},
                ":remind": {"S": schedule.remind_at.isoformat()},
                ":deadline": {"S": schedule.deadline.isoformat()}, ":no": {"BOOL": False},
            },
        )
        self.audit.record(
            f"CASE#{case_id}", "SUPPLIER_QUESTION_SENT", actor="system", case_id=case_id,
            payload={"messageId": message_id, "templateId": expected.template.value,
                     "recipient": recipient, "sourceRef": expected.source_ref},
        )
        return [{"recipient": recipient, "status": "SENT"}]

    def sweep_dialogue(self) -> list[dict[str, str]]:
        outcomes: list[dict[str, str]] = []
        cursor: dict[str, Any] | None = None
        while True:
            args: dict[str, Any] = {
                "TableName": self._dialogue,
                "FilterExpression": (
                    "#s = :sent OR (#s = :draft AND attribute_exists(sendClaim)) "
                    "OR ((#s = :blocked OR #s = :timedout) "
                    "AND attribute_not_exists(escalationDone))"
                ),
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":sent": {"S": "SENT"}, ":blocked": {"S": "BLOCKED"},
                    ":timedout": {"S": "TIMED_OUT"}, ":draft": {"S": "DRAFT"},
                },
            }
            if cursor is not None:
                args["ExclusiveStartKey"] = cursor
            page = self.dynamodb.scan(**args)
            for item in page.get("Items", []):
                record = from_item(item)
                outcomes.append(self.tick_dialogue(str(record["caseId"]), str(record["messageId"])))
            cursor = page.get("LastEvaluatedKey")
            if cursor is None:
                return outcomes

    def tick_dialogue(self, case_id: str, message_id: str) -> dict[str, str]:
        key = {"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": f"MSG#{message_id}"}}
        item = self.dynamodb.get_item(TableName=self._dialogue, Key=key).get("Item")
        if item is None:
            return {"status": "MISSING"}
        record = from_item(item)
        if record.get("status") == "DRAFT" and record.get("sendClaim"):
            claimed_at = datetime.fromisoformat(str(record["sendClaim"]))
            if self.clock() < claimed_at + timedelta(minutes=5):
                return {"status": "UNCHANGED"}
            try:
                self.dynamodb.update_item(
                    TableName=self._dialogue, Key=key,
                    UpdateExpression="SET #s = :blocked, blockReason = :reason",
                    ConditionExpression="#s = :draft AND sendClaim = :claim",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":draft": {"S": "DRAFT"}, ":blocked": {"S": "BLOCKED"},
                        ":claim": {"S": str(record["sendClaim"])},
                        ":reason": {"S": "SES send outcome unavailable"},
                    },
                )
            except self.dynamodb.exceptions.ConditionalCheckFailedException:
                return {"status": "UNCHANGED"}
            done = self._ensure_dialogue_escalated(
                case_id, message_id, "supplier send outcome unavailable"
            )
            return {"status": "ESCALATED" if done else "UNCHANGED"}
        if record.get("status") in {"BLOCKED", "TIMED_OUT"}:
            done = self._ensure_dialogue_escalated(
                case_id, message_id, "supplier question blocked or timed out"
            )
            return {"status": "ESCALATED" if done else "UNCHANGED"}
        if record.get("status") != "SENT":
            return {"status": "UNCHANGED"}
        thread = Thread(
            case_id, str(record["supplierId"]), str(record["referenceToken"]),
            datetime.fromisoformat(str(record["sentAt"])),
            datetime.fromisoformat(str(record["remindAt"])),
            datetime.fromisoformat(str(record["deadline"])),
            DialogueStatus.WAITING, bool(record.get("reminderSent")),
        )
        _, action = tick(thread, self.clock())
        if action is TimeoutAction.NONE:
            return {"status": "UNCHANGED"}
        if action is TimeoutAction.ESCALATE:
            try:
                self.dynamodb.update_item(
                    TableName=self._dialogue, Key=key,
                    UpdateExpression="SET #s = :timedout",
                    ConditionExpression="#s = :sent",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":sent": {"S": "SENT"}, ":timedout": {"S": "TIMED_OUT"},
                    },
                )
            except self.dynamodb.exceptions.ConditionalCheckFailedException:
                return {"status": "UNCHANGED"}
            done = self._ensure_dialogue_escalated(case_id, message_id, "supplier did not reply")
            return {"status": "ESCALATED" if done else "UNCHANGED"}
        try:
            self.dynamodb.update_item(
                TableName=self._dialogue, Key=key,
                UpdateExpression="SET reminderSent = :yes",
                ConditionExpression="#s = :sent AND reminderSent = :no",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":sent": {"S": "SENT"}, ":no": {"BOOL": False},
                    ":yes": {"BOOL": True},
                },
            )
        except self.dynamodb.exceptions.ConditionalCheckFailedException:
            return {"status": "UNCHANGED"}
        try:
            facts = load_facts(
                self.cases, self.sap, case_id, expected_status=CaseStatus.WAITING_SUPPLIER
            )
            question = render_question(
                facts, str(record["poNumber"]), Template(str(record["templateId"])),
                str(record["referenceToken"]),
            )
            standin = self.standins().get(question.recipient)
            if not standin or question.rendered_text != record["renderedText"]:
                return {"status": "BLOCKED"}
            self.ses.send_email(
                Source=self.sender(), Destination={"ToAddresses": [standin]},
                Message={
                    "Subject": {"Data": f"{case_id}: PO {question.po_number} clarification"},
                    "Body": {"Text": {"Data": question.rendered_text}},
                },
            )
        except (KeyError, ValueError):
            return {"status": "BLOCKED"}
        except Exception:  # noqa: BLE001 - claim prevents duplicate reminder on ambiguous failure
            return {"status": "BLOCKED"}
        self.audit.record(
            f"CASE#{case_id}", "SUPPLIER_REMINDER_SENT", actor="system", case_id=case_id,
            payload={"messageId": message_id, "recipient": question.recipient},
        )
        return {"status": "REMINDED"}

    def _block_dialogue(self, key: dict[str, Any], reason: str) -> None:
        self.dynamodb.update_item(
            TableName=self._dialogue, Key=key,
            UpdateExpression="SET #s = :blocked, blockReason = :reason",
            ConditionExpression="#s = :draft AND attribute_not_exists(sendClaim)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":draft": {"S": "DRAFT"}, ":blocked": {"S": "BLOCKED"},
                ":reason": {"S": reason},
            },
        )
        case_id = str(key["PK"]["S"]).removeprefix("CASE#")
        message_id = str(key["SK"]["S"]).removeprefix("MSG#")
        self._ensure_dialogue_escalated(case_id, message_id, reason)

    def _ensure_dialogue_escalated(self, case_id: str, message_id: str, reason: str) -> bool:
        case = self.cases.get(case_id)
        if case is None:
            return False
        try:
            if case.status is CaseStatus.WAITING_SUPPLIER:
                self.cases.transition(
                    case_id, CaseStatus.INVESTIGATING, actor="system", reason=reason,
                    expected=CaseStatus.WAITING_SUPPLIER,
                )
                case = self.cases.get(case_id)
            if case is not None and case.status is CaseStatus.INVESTIGATING:
                self.dynamodb.update_item(
                    TableName=self._cases,
                    Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": "META"}},
                    UpdateExpression="SET tier = :three",
                    ExpressionAttributeValues={":three": {"N": "3"}},
                )
                self.cases.transition(
                    case_id, CaseStatus.ESCALATED, actor="system", reason=reason,
                    expected=CaseStatus.INVESTIGATING,
                )
                case = self.cases.get(case_id)
        except ConcurrentUpdateError:
            return False
        if case is None or case.status is not CaseStatus.ESCALATED:
            return False
        self.dynamodb.update_item(
            TableName=self._dialogue,
            Key={"PK": {"S": f"CASE#{case_id}"}, "SK": {"S": f"MSG#{message_id}"}},
            UpdateExpression="SET escalationDone = :yes",
            ExpressionAttributeValues={":yes": {"BOOL": True}},
        )
        return True

    # Allowlist (BR-03) ------------------------------------------------------------------

    def supplier_of(self, case: Case) -> str | None:
        if not case.po_number:
            return None
        try:
            header = self.sap.get(PO, "A_PurchaseOrder", {"PurchaseOrder": case.po_number})
        except SapNotFoundError:
            return None
        return str(header.data.get("Supplier") or "") or None

    def approvers(self) -> set[str]:
        page = self.dynamodb.scan(
            TableName=self._config,
            FilterExpression="begins_with(PK, :p)",
            ExpressionAttributeValues={":p": {"S": "APPR#"}},
        )
        return {str(from_item(i)["userId"]).lower() for i in page.get("Items", [])}

    def by_role(self, case: Case, role: str) -> set[str]:
        if role == "supplier":
            supplier = self.supplier_of(case)
            return partner_emails(self.sap, supplier) if supplier else set()
        if role in INTERNAL_ROLES:
            return partner_emails(self.sap, INTERNAL_ROLES[role])
        if role == "approver":
            return self.approvers()
        return set()

    def allowlist(self, case: Case) -> set[str]:
        return set().union(*(self.by_role(case, role) for role in ROLE_TEMPLATES))

    # Sending ----------------------------------------------------------------------------

    def handle(self, data: dict[str, Any]) -> list[dict[str, str]]:
        case_id = str(data["caseId"])
        case = self.cases.get(case_id)
        if case is None:
            return []
        role = str(data.get("recipientRole") or "")
        template = str(data.get("templateId") or "")
        if template not in TEMPLATES or template not in ROLE_TEMPLATES.get(role, set()):
            self._refuse(case_id, role, template, "unknown template for this role")
            return [{"status": "REFUSED", "reason": "template"}]
        allowed = self.allowlist(case)
        named = data.get("recipients")
        wanted = (
            {str(r).strip().lower() for r in named}
            if isinstance(named, list)
            else self.by_role(case, role)
        )
        if role == "approver" and data.get("approverId"):
            wanted = {str(data["approverId"]).lower()}
        outcomes = []
        for recipient in sorted(wanted):
            if recipient not in allowed:
                self._refuse(case_id, role, template, "recipient not in SAP master data", recipient)
                outcomes.append({"recipient": recipient, "status": "REFUSED"})
                continue
            outcomes.append(self._send(case, role, template, recipient, data))
        return outcomes

    def _refuse(
        self, case_id: str, role: str, template: str, reason: str, recipient: str = ""
    ) -> None:
        self.audit.record(
            f"CASE#{case_id}",
            "NOTIFICATION_REFUSED",
            actor="system",
            case_id=case_id,
            payload={
                "role": role,
                "templateId": template,
                "recipient": recipient,
                "reason": reason,
            },
        )

    def _render(self, case: Case, template: str, data: dict[str, Any]) -> tuple[str, str]:
        subject, body = TEMPLATES[template]
        documents = ", ".join(str(d) for d in data.get("documents") or []) or "see AERA console"
        facts = {
            "case": case.case_id,
            "po": case.po_number or "-",
            "material": case.material,
            "plant": case.plant,
            "documents": documents,
            "rar": f"{case.rar_usd:,.0f}" if case.rar_usd is not None else "-",
            "stockout": case.stockout_at.isoformat() if case.stockout_at else "-",
        }
        return subject.format(**facts), body.format(**facts)

    def _send(
        self, case: Case, role: str, template: str, recipient: str, data: dict[str, Any]
    ) -> dict[str, str]:
        part = str(data.get("planPartId") or "")
        key = hashlib.sha256(f"{case.case_id}|{template}|{part}|{recipient}".encode()).hexdigest()
        subject, body = self._render(case, template, data)
        standin = self.standins().get(recipient)
        status = "SENT" if standin and self.ses is not None and self.sender() else "RECORDED"
        try:
            self.dynamodb.put_item(
                TableName=self._cases,
                Item=to_item(
                    {
                        "PK": f"CASE#{case.case_id}",
                        "SK": f"NOTIFY#{key}",
                        "role": role,
                        "templateId": template,
                        "recipient": recipient,
                        "deliveredTo": standin,
                        "subject": subject,
                        "body": body,
                        "status": "PENDING" if status == "SENT" else status,
                        "createdAt": self.clock().isoformat(),
                    }
                ),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except self.dynamodb.exceptions.ConditionalCheckFailedException:
            return {"recipient": recipient, "status": "DUPLICATE"}
        if status == "SENT":
            self.ses.send_email(
                Source=self.sender(),
                Destination={"ToAddresses": [standin]},
                Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
            )
            self.dynamodb.update_item(
                TableName=self._cases,
                Key={"PK": {"S": f"CASE#{case.case_id}"}, "SK": {"S": f"NOTIFY#{key}"}},
                UpdateExpression="SET #s = :sent",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":sent": {"S": "SENT"}},
            )
        self.audit.record(
            f"CASE#{case.case_id}",
            "NOTIFICATION_" + status,
            actor="system",
            case_id=case.case_id,
            payload={"role": role, "templateId": template, "recipient": recipient},
        )
        return {"recipient": recipient, "status": status}


_notifier: Notifier | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _notifier
    if _notifier is None:
        from services.shared import runtime

        def standins() -> dict[str, str]:
            try:
                value = runtime.secret("channels/email-standins")
            except Exception:  # noqa: BLE001 - no stand-ins: record only
                return {}
            return {str(k).lower(): str(v) for k, v in value.get("standins", {}).items()}

        def sender() -> str:
            try:
                return str(runtime.secret("channels/email-standins").get("sender", ""))
            except Exception:  # noqa: BLE001
                return ""

        _notifier = Notifier(
            dynamodb=runtime.client("dynamodb"),
            sap=runtime.sap_client(),
            ses=runtime.client("ses"),
            standins=standins,
            sender=sender,
        )
    data = dict((event.get("detail") or {}).get("data") or {})
    if event.get("task") == "dialogueSweep":
        outcomes = _notifier.sweep_dialogue()
    elif (event.get("detail-type") or "") == "SupplierInfoRequested":
        outcomes = _notifier.handle_dialogue(data)
    else:
        outcomes = _notifier.handle(data)
    return {"outcomes": outcomes}
