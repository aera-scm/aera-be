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
from datetime import UTC, datetime
from typing import Any

from services.shared.audit import AuditWriter
from services.shared.cases import CaseStore
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.models import Case
from services.shared.sap_client import SapClient, SapNotFoundError

PARTNER = "API_BUSINESS_PARTNER"
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
    "approver": {"APPROVAL_REMINDER"},
}


def partner_emails(sap: SapClient, partner: str) -> set[str]:
    addresses = sap.query(
        PARTNER,
        "A_BusinessPartnerAddress",
        filter=f"BusinessPartner eq '{partner}'",
        select="AddressID",
    )
    emails: set[str] = set()
    for address in addresses:
        rows = sap.query(
            PARTNER,
            "A_AddressEmailAddress",
            filter=f"AddressID eq '{address.data['AddressID']}'",
            select="EmailAddress",
        )
        emails |= {str(r.data["EmailAddress"]).strip().lower() for r in rows}
    return emails


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
    return {"outcomes": _notifier.handle(dict((event.get("detail") or {}).get("data") or {}))}
