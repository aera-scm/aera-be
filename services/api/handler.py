"""Console REST API, M1 routes (SRD 6.10, FR-TRI-01, FR-TRI-02, FR-UI-01, FR-UI-05, IR-11).

API Gateway authenticates with the Cognito authorizer; this handler authorises by Cognito
group (NFR-SEC-04). Errors are RFC 7807 problem+json. Mutating routes honour
`Idempotency-Key`: a repeated key returns the first response instead of acting twice.

Routes: GET /cases, GET /cases/{id}, GET /cases/{id}/trace, POST /signals, GET /signals,
POST /cases/{id}/fields/{fieldId}/confirm, POST /cases/{id}/runs, POST /realtime/ticket,
GET /metrics.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from services.api import whatif
from services.api.admin import Admin, AdminError
from services.api.chat import Chat
from services.lab.service import Lab, LabError, mirror_patch_via
from services.reporting.decision import RecordUnavailable, collect, render_html, render_pdf
from services.reporting.metrics import kpis
from services.routing.store import ApprovalConflict, ControlStore
from services.rules.br_13 import board_key, rank_reason
from services.run_starter.handler import STARTABLE
from services.shared import http
from services.shared.audit import AuditWriter
from services.shared.case_state import TERMINAL
from services.shared.cases import CaseStore
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.intake import Attachment, Inbound, Intake
from services.shared.models import (
    Case,
    CaseStatus,
    FieldStatus,
    SignalChannel,
    SignalStatus,
    new_ulid,
)
from services.shared.runtime import emit
from services.shared.signals import SignalStore
from services.shared.trace import TraceStore
from services.tools.context import ToolContext

COMPONENT = "api"
READERS = frozenset({"planner", "approver", "admin"})
PLANNERS = frozenset({"planner", "admin"})
APPROVERS = frozenset({"approver"})
ADMINS = frozenset({"admin"})
TICKET_SECONDS = 60
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOAD_TYPES = {"application/pdf", "image/jpeg", "image/png", "text/plain"}


class Problem(Exception):
    def __init__(self, status: int, title: str, detail: str | None = None) -> None:
        super().__init__(title)
        self.response = http.problem(status, title, detail)


@dataclass(frozen=True)
class User:
    id: str
    groups: frozenset[str]
    email: str = ""  # DR-12 names approvers by e-mail address


def _user(event: dict[str, Any]) -> User:
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    subject = claims.get("sub")
    if not subject:
        raise Problem(401, "Not signed in")
    raw = claims.get("cognito:groups") or ""
    groups = raw if isinstance(raw, list) else re.findall(r"[a-z]+", str(raw))
    return User(
        id=str(subject),
        groups=frozenset(str(g) for g in groups),
        email=str(claims.get("email") or "").strip().lower(),
    )


def _require(user: User, allowed: frozenset[str]) -> None:
    if not user.groups & allowed:
        raise Problem(403, "Forbidden", "Your role does not allow this action.")


def _json_body(event: dict[str, Any]) -> dict[str, Any]:
    try:
        body = json.loads(http.body_bytes(event) or b"{}")
    except ValueError:
        raise Problem(400, "Body is not JSON") from None
    if not isinstance(body, dict):
        raise Problem(400, "Body must be a JSON object")
    return body


def _case_json(case: Case) -> dict[str, Any]:
    return case.model_dump(mode="json", by_alias=True)


@dataclass
class Api:
    dynamodb: Any
    intake: Intake
    bus: Any
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    env: str | None = None
    states: Any = None
    execution_arn: str = ""
    scan: Callable[[str], str | None] = lambda text: None
    admin: Admin | None = None
    sap: Any = None  # SAP reads for projections and what-if (FR-SIM)
    lab: Lab | None = None

    def __post_init__(self) -> None:
        self.chat = Chat(self.dynamodb, self.bus, scan=self.scan, clock=self.clock, env=self.env)
        self.cases = CaseStore(self.dynamodb, self.env)
        self.signals = SignalStore(self.dynamodb, self.env)
        self.trace = TraceStore(self.dynamodb, self.env)
        self.audit = AuditWriter(self.dynamodb, self.env)
        self._cases_table = table_name("cases", self.env)
        self._signals_table = table_name("signals", self.env)
        self._connections = table_name("connections", self.env)
        self._idempotency = table_name("idempotency", self.env)

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        try:
            user = _user(event)
            method = str(event.get("httpMethod"))
            resource = str(event.get("resource") or "")
            params = event.get("pathParameters") or {}
            if method == "POST":
                return self._idempotent(
                    event, user, lambda: self._post(resource, params, event, user)
                )
            if method == "GET":
                return self._get(resource, params, event, user)
            if method == "PUT" and resource.startswith("/admin/"):
                _require(user, ADMINS)
                payload = _json_body(event)
                actor = f"user:{user.id}"
                if resource == "/admin/config/{key}":
                    return self._admin(
                        lambda admin: admin.set_config(params["key"], payload.get("value"), actor)
                    )
                if resource == "/admin/rate-card/{id}":
                    return self._admin(lambda admin: admin.set_rate(params["id"], payload, actor))
                if resource == "/admin/approvers/{id}":
                    return self._admin(
                        lambda admin: admin.set_approver(params["id"], payload, actor)
                    )
            raise Problem(405, "Method not allowed")
        except Problem as problem:
            return problem.response

    # Routing ----------------------------------------------------------------------------

    def _get(
        self, resource: str, params: dict[str, str], event: dict[str, Any], user: User
    ) -> dict[str, Any]:
        _require(user, READERS)
        if resource == "/cases":
            status = http.query(event, "status")
            if status is not None and status not in CaseStatus.__members__:
                raise Problem(400, "Unknown status", status)
            board = self.board(status, http.query(event, "type"))
            return http.response(200, board)
        if resource == "/cases/{id}":
            return http.response(200, self.case_detail(params["id"]))
        if resource == "/cases/{id}/trace":
            self._case(params["id"])
            events = self.trace.events(params["id"], after=http.query(event, "after"))
            return http.response(200, [e.model_dump(mode="json", by_alias=True) for e in events])
        if resource == "/cases/{id}/dialogue":
            return http.response(200, self.dialogue_thread(params["id"]))
        if resource == "/cases/{id}/decision-record":
            case_id = params["id"]
            self._case(case_id)
            try:
                record = collect(self.dynamodb, case_id, self.env or "dev")
            except RecordUnavailable as error:
                raise Problem(409, "Decision record unavailable", str(error)) from None
            output = http.query(event, "format") or "json"
            if output == "html":
                return http.response(
                    200, render_html(record), content_type="text/html; charset=utf-8"
                )
            if output == "pdf":
                return {
                    "statusCode": 200,
                    "headers": {
                        "Content-Type": "application/pdf",
                        "Content-Disposition": f'attachment; filename="{case_id}.pdf"',
                    },
                    "isBase64Encoded": True,
                    "body": base64.b64encode(render_pdf(record)).decode("ascii"),
                }
            if output == "json":
                return http.response(200, record)
            raise Problem(400, "Unknown decision record format")
        if resource == "/signals":
            return http.response(200, self.signal_list(http.query(event, "status")))
        if resource == "/metrics":
            return http.response(200, self.metrics())
        if resource == "/admin/settings":
            _require(user, ADMINS)
            return self._admin(lambda admin: admin.settings())
        if resource == "/lab/runs":
            _require(user, ADMINS)
            return http.response(200, self._lab(lambda lab: lab.list()))
        if resource == "/lab/runs/{id}":
            _require(user, ADMINS)
            return http.response(200, self._lab(lambda lab: lab.get(params["id"])))
        if resource == "/cases/{id}/projection":
            case = self._case(params["id"])
            return self._sim(lambda ctx: whatif.projection(ctx, case, http.query(event, "option")))
        raise Problem(404, "Not found")

    def _post(
        self, resource: str, params: dict[str, str], event: dict[str, Any], user: User
    ) -> dict[str, Any]:
        if resource == "/realtime/ticket":
            _require(user, READERS)
            return http.response(201, self.ticket(user))
        if resource == "/cases/{id}/rollback":
            _require(user, APPROVERS)
            return self.rollback(params["id"], user)
        if resource == "/cases/{id}/approval":
            _require(user, APPROVERS)
            return self.approve(params["id"], _json_body(event), user)
        if resource in ("/admin/killswitch", "/admin/reset"):
            _require(user, ADMINS)
            body = _json_body(event)
            actor = f"user:{user.id}"
            if resource == "/admin/killswitch":
                return self._admin(lambda admin: admin.kill_switch(body.get("on"), actor))
            return self._admin(lambda admin: admin.reset(body.get("confirm"), actor))
        if resource == "/lab/runs":
            _require(user, ADMINS)
            return http.response(
                202, self._lab(lambda lab: lab.start(_json_body(event), f"user:{user.id}"))
            )
        _require(user, PLANNERS)
        body = _json_body(event)
        if resource == "/signals":
            return http.response(202, self.upload(body, user))
        if resource == "/cases/{id}/fields/{fieldId}/confirm":
            return http.response(200, self.confirm(params["id"], params["fieldId"], body, user))
        if resource == "/cases/{id}/runs":
            return self.start_run(params["id"], user)
        if resource == "/cases/{id}/whatif":
            case = self._case(params["id"])
            option_id, changes = body.get("optionId"), body.get("params") or {}
            if not isinstance(option_id, str) or not isinstance(changes, dict):
                raise Problem(400, "optionId (string) and params (object) are required")
            return self._sim(lambda ctx: whatif.whatif(ctx, case, option_id, changes))
        if resource == "/cases/{id}/chat":
            message = body.get("message")
            if not isinstance(message, str) or not message.strip():
                raise Problem(400, "Missing message")
            return http.response(200, self.chat.handle(self._case(params["id"]), user.id, message))
        raise Problem(404, "Not found")

    def _sim(self, action: Callable[[ToolContext], dict[str, Any]]) -> dict[str, Any]:
        if self.sap is None:
            raise Problem(503, "SAP reads are not configured in this environment")
        ctx = ToolContext(
            sap=self.sap,
            dynamodb=self.dynamodb,
            bus=self.bus,
            clock=self.clock,
            env=self.env,
            actor="api",
        )
        try:
            return http.response(200, action(ctx))
        except whatif.WhatIfError as error:
            raise Problem(400, "What-if refused", str(error)) from None

    def _admin(self, action: Callable[[Admin], dict[str, Any]]) -> dict[str, Any]:
        if self.admin is None:
            raise Problem(503, "Administration is not configured in this environment")
        try:
            return http.response(200, action(self.admin))
        except AdminError as error:
            raise Problem(400, "Refused", str(error)) from None

    def _lab(self, action: Callable[[Lab], Any]) -> Any:
        if self.lab is None:
            raise Problem(503, "Scenario Lab is not configured")
        try:
            return action(self.lab)
        except LabError as error:
            status = (
                404
                if str(error) == "Lab run not found"
                else 503
                if "failed before signal delivery" in str(error)
                else 400
            )
            raise Problem(status, "Scenario Lab refused", str(error)) from None

    def approve(self, case_id: str, body: dict[str, Any], user: User) -> dict[str, Any]:
        """FR-RTE-03/04, AT-16: the decision binds to the plan version hash; a stale or
        repeated decision is refused with the current plan so the approver can refresh."""
        case = self._case(case_id)
        decision = str(body.get("decision") or "").upper()
        comment = body.get("comment") or ""
        version_hash = body.get("planVersionHash")
        if not isinstance(version_hash, str) or not isinstance(comment, str):
            raise Problem(400, "planVersionHash and comment must be strings")
        control = ControlStore(self.dynamodb, self.env)
        try:
            part = control.decide(
                case_id,
                actor=user.email or user.id,
                groups=user.groups,
                version_hash=version_hash,
                decision=decision,
                comment=comment,
                now=self.clock(),
            )
        except PermissionError as error:
            raise Problem(403, "Not allowed to decide", str(error)) from None
        except ApprovalConflict as error:
            current = control.get(case_id, f"ROUTE#{case.plan_version}")
            return http.response(
                409,
                {
                    "title": "Decision refused",
                    "detail": str(error),
                    "currentPlanVersion": case.plan_version,
                    "currentPlanVersionHash": current["versionHash"] if current else None,
                },
            )
        except ValueError as error:
            raise Problem(400, "Invalid decision", str(error)) from None
        if decision == "REJECTED" and case.status is CaseStatus.AWAITING_APPROVAL:
            self.cases.transition(
                case_id,
                CaseStatus.REJECTED,
                actor=f"user:{user.id}",
                reason=comment,
                expected=CaseStatus.AWAITING_APPROVAL,
            )
        return http.response(
            200, {"decision": part["decision"], "planPartId": part["id"], "caseId": case_id}
        )

    def rollback(self, case_id: str, user: User) -> dict[str, Any]:
        """FR-EXE-08, FR-MON-03: an approver rolls back a reopened case's executed plan. The
        request is audited; the execution workflow performs it; repeating it is harmless."""
        case = self._case(case_id)
        if case.status is not CaseStatus.REOPENED:
            raise Problem(409, "Rollback not possible", f"case is {case.status.value}")
        if self.states is None or not self.execution_arn:
            raise Problem(503, "Execution workflow is not configured in this environment")
        try:
            self.dynamodb.put_item(
                TableName=self._cases_table,
                Item=to_item(
                    {
                        "PK": f"CASE#{case_id}",
                        "SK": f"ROLLBACK#{case.plan_version}",
                        "status": "REQUESTED",
                        "actor": f"user:{user.id}",
                        "requestedAt": self.clock().isoformat(),
                    }
                ),
                ConditionExpression="attribute_not_exists(PK)",
            )
            self.audit.record(
                f"CASE#{case_id}",
                "ROLLBACK_REQUESTED",
                actor=f"user:{user.id}",
                case_id=case_id,
                payload={"planVersion": case.plan_version},
            )
        except self.dynamodb.exceptions.ConditionalCheckFailedException:
            pass  # already requested: start (or find) the same workflow execution
        name = f"{case_id}-rollback-v{case.plan_version}"
        try:
            self.states.start_execution(
                stateMachineArn=self.execution_arn,
                name=name,
                input=json.dumps({"mode": "rollback", "caseId": case_id}),
            )
        except self.states.exceptions.ExecutionAlreadyExists:
            pass
        return http.response(202, {"rollback": name})

    def start_run(self, case_id: str, user: User) -> dict[str, Any]:
        """NFR-REL-04: returns the run id at once; run-starter starts the run from the event
        under that id (the edge stack never calls the reasoning stack directly, SRD 6.17)."""
        case = self._case(case_id)
        if case.status not in STARTABLE:
            raise Problem(409, "Run not started", f"case is {case.status.value}")
        if case.active_run_id:
            raise Problem(409, "Run not started", "a run is already active")
        run_id = new_ulid()
        emit(
            self.bus,
            "CaseReadyForRun",
            {"caseId": case_id, "reason": "planner request", "runId": run_id},
            component=COMPONENT,
            case_id=case_id,
            actor=f"user:{user.id}",
            environment=self.env,
        )
        return http.response(202, {"runId": run_id})

    # Reads ------------------------------------------------------------------------------

    def board(self, status: str | None, case_type: str | None) -> list[dict[str, Any]]:
        """FR-TRI-01/02: open cases ranked by BR-13 (score, then fewer hours to stock-out)."""
        wanted = [CaseStatus(status)] if status else [s for s in CaseStatus if s not in TERMINAL]
        now = self.clock()
        found: list[Case] = []
        for value in wanted:
            arguments: dict[str, Any] = {
                "TableName": self._cases_table,
                "IndexName": "GSI1",
                "KeyConditionExpression": "#s = :s",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {":s": {"S": value.value}},
            }
            while True:
                page = self.dynamodb.query(**arguments)
                for item in page.get("Items", []):
                    if item.get("SK", {}).get("S") == "META":
                        case = self.cases.get(item["caseId"]["S"])
                        if case is not None and (case_type is None or case.type == case_type):
                            found.append(case)
                if "LastEvaluatedKey" not in page:
                    break
                arguments["ExclusiveStartKey"] = page["LastEvaluatedKey"]

        def hours(case: Case) -> Decimal | None:
            if case.stockout_at is None:
                return None
            return Decimal(str((case.stockout_at - now).total_seconds())) / 3600

        found.sort(key=lambda c: board_key(c.rar_usd or Decimal(0), hours(c)))
        rows = [_case_json(c) for c in found]
        for row, case, below in zip(rows, found, found[1:], strict=False):
            if below is not None:
                row["rankReason"] = rank_reason(
                    case.case_id,
                    case.rar_usd or Decimal(0),
                    hours(case),
                    below.case_id,
                    below.rar_usd or Decimal(0),
                    hours(below),
                )
        return rows

    def case_detail(self, case_id: str) -> dict[str, Any]:
        case = self._case(case_id)
        signals = self.signals.for_case(case_id)
        return {
            "case": _case_json(case),
            "signals": [s.model_dump(mode="json", by_alias=True) for s in signals],
        }

    def dialogue_thread(self, case_id: str) -> list[dict[str, Any]]:
        self._case(case_id)
        arguments: dict[str, Any] = {
            "TableName": table_name("dialogue", self.env),
            "KeyConditionExpression": "PK = :case AND begins_with(SK, :message)",
            "ExpressionAttributeValues": {
                ":case": {"S": f"CASE#{case_id}"},
                ":message": {"S": "MSG#"},
            },
        }
        messages: list[dict[str, Any]] = []
        while True:
            page = self.dynamodb.query(**arguments)
            for raw in page.get("Items", []):
                item = from_item(raw, keep_decimals=False)
                entry: dict[str, Any] = {
                    key: item[key]
                    for key in (
                        "messageId",
                        "templateId",
                        "language",
                        "renderedText",
                        "englishCopy",
                        "status",
                        "createdAt",
                        "sentAt",
                        "reminderSent",
                        "replySignalId",
                    )
                    if key in item
                }
                reply_id = item.get("replySignalId")
                if reply_id:
                    reply = self.signals.get(str(reply_id))
                    if reply and reply.case_id == case_id and reply.status is SignalStatus.ACCEPTED:
                        entry["reply"] = {
                            "receivedAt": reply.received_at.isoformat(),
                            "text": reply.normalized_text or "",
                        }
                messages.append(entry)
            if "LastEvaluatedKey" not in page:
                break
            arguments["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return sorted(messages, key=lambda message: str(message.get("createdAt", "")))

    def signal_list(self, status: str | None) -> list[dict[str, Any]]:
        """FR-UI-05: quarantined signals are shown with their reason; they have no case."""
        if status is not None and status not in SignalStatus.__members__:
            raise Problem(400, "Unknown status", status)
        wanted = SignalStatus(status) if status else SignalStatus.QUARANTINED
        arguments: dict[str, Any] = {
            "TableName": self._signals_table,
            "FilterExpression": "#s = :s",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":s": {"S": wanted.value}},
        }
        rows: list[dict[str, Any]] = []
        while True:
            page = self.dynamodb.scan(**arguments)
            rows += [
                SignalStore.parse(item).model_dump(mode="json", by_alias=True)
                for item in page.get("Items", [])
            ]
            if "LastEvaluatedKey" not in page:
                break
            arguments["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return sorted(rows, key=lambda r: str(r["receivedAt"]), reverse=True)

    def metrics(self) -> dict[str, Any]:
        item = self.dynamodb.get_item(
            TableName=self._cases_table, Key={"PK": {"S": "BOARD"}, "SK": {"S": "MRP"}}
        ).get("Item")
        mrp = from_item(item, keep_decimals=False) if item else {}
        for key in ("PK", "SK"):
            mrp.pop(key, None)
        return {"mrp": mrp, "kpis": kpis(self.dynamodb, self.env or "dev")}

    # Commands ---------------------------------------------------------------------------

    def upload(self, body: dict[str, Any], user: User) -> dict[str, Any]:
        """Manual upload (FR-ING-03): the planner names the real sender; BR-04 still applies."""
        sender = str(body.get("sender") or "").strip()
        text = str(body.get("text") or "").strip()
        if not sender:
            raise Problem(400, "Missing sender", "Name the supplier email or phone it came from.")
        attachments: tuple[Attachment, ...] = ()
        if body.get("contentBase64"):
            content_type = str(body.get("contentType") or "")
            if content_type not in UPLOAD_TYPES:
                raise Problem(415, "Unsupported file type")
            try:
                content = base64.b64decode(str(body["contentBase64"]), validate=True)
            except (binascii.Error, ValueError):
                raise Problem(400, "contentBase64 is not valid base64") from None
            if len(content) > MAX_UPLOAD_BYTES:
                raise Problem(413, "File too large", "The limit is 5 MB.")
            name = str(body.get("filename") or "upload")
            attachments = (Attachment(name, content, content_type),)
        if not text and not attachments:
            raise Problem(400, "Nothing to upload")
        signal = self.intake.receive(
            Inbound(
                channel=SignalChannel.MANUAL,
                sender_id=sender,
                body=json.dumps({"sender": sender, "text": text, "uploadedBy": user.id}).encode(),
                content_type="application/json",
                attachments=attachments,
                normalized_text=text or None,
                actor=f"user:{user.id}",
            )
        )
        return {"signalId": signal.signal_id}

    def confirm(
        self, case_id: str, field_id: str, body: dict[str, Any], user: User
    ) -> dict[str, Any]:
        """BR-02: a planner confirms an UNCONFIRMED critical field, optionally correcting it."""
        self._case(case_id)
        for signal in self.signals.for_case(case_id):
            for index, extracted in enumerate(signal.fields):
                if extracted.field_id != field_id:
                    continue
                value = str(body.get("value") or extracted.value)
                confirmed = extracted.model_copy(
                    update={
                        "status": FieldStatus.CONFIRMED,
                        "value": value,
                        "confirmed_by": f"user:{user.id}",
                    }
                )
                fields = [*signal.fields[:index], confirmed, *signal.fields[index + 1 :]]
                self.signals.save(signal.model_copy(update={"fields": fields}))
                self.audit.record(
                    f"CASE#{case_id}",
                    "FIELD_CONFIRMED",
                    actor=f"user:{user.id}",
                    case_id=case_id,
                    payload={
                        "fieldId": field_id,
                        "name": extracted.name,
                        "extracted": extracted.value,
                        "confidence": str(extracted.confidence),
                        "confirmed": value,
                    },
                )
                emit(
                    self.bus,
                    "CaseUpdated",
                    {"caseId": case_id, "reason": "field confirmed", "fieldId": field_id},
                    component=COMPONENT,
                    case_id=case_id,
                    actor=f"user:{user.id}",
                    environment=self.env,
                )
                self._answer_questions(case_id, field_id, user)
                return confirmed.model_dump(mode="json", by_alias=True)
        raise Problem(404, "Field not found")

    def ticket(self, user: User) -> dict[str, Any]:
        """Single-use WebSocket ticket, valid 60 s (SRD 6.10); the socket never sees a JWT."""
        value = secrets.token_urlsafe(32)
        self.dynamodb.put_item(
            TableName=self._connections,
            Item=to_item(
                {
                    "PK": f"TICKET#{value}",
                    "userId": user.id,
                    "groups": sorted(user.groups),
                    "expiresAt": int(time.time()) + TICKET_SECONDS,
                    "ttl": int(time.time()) + TICKET_SECONDS,
                }
            ),
        )
        return {"ticket": value, "expiresIn": TICKET_SECONDS}

    def _answer_questions(self, case_id: str, field_id: str, user: User) -> None:
        """UC-05: the answer closes the agent's question; with none left open, a new run
        continues the case (SRD 6.3.1)."""
        page = self.dynamodb.query(
            TableName=self._cases_table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :q)",
            ExpressionAttributeValues={
                ":pk": {"S": f"CASE#{case_id}"},
                ":q": {"S": "QUESTION#"},
            },
        )
        open_questions = 0
        for item in page.get("Items", []):
            if item.get("status", {}).get("S") != "OPEN":
                continue
            if item.get("fieldId", {}).get("S") == field_id:
                self.dynamodb.update_item(
                    TableName=self._cases_table,
                    Key={"PK": item["PK"], "SK": item["SK"]},
                    UpdateExpression="SET #s = :answered, answeredBy = :by, answeredAt = :at",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":answered": {"S": "ANSWERED"},
                        ":by": {"S": f"user:{user.id}"},
                        ":at": {"S": self.clock().isoformat()},
                    },
                )
            else:
                open_questions += 1
        case = self.cases.get(case_id)
        if open_questions == 0 and case is not None and case.status is CaseStatus.WAITING_PLANNER:
            emit(
                self.bus,
                "CaseReadyForRun",
                {"caseId": case_id, "reason": "planner answered"},
                component=COMPONENT,
                case_id=case_id,
                actor=f"user:{user.id}",
                environment=self.env,
            )

    # Helpers ----------------------------------------------------------------------------

    def _case(self, case_id: str) -> Case:
        case = self.cases.get(case_id) if re.fullmatch(r"EXC-\d{4}-\d{4,}", case_id) else None
        if case is None:
            raise Problem(404, "Case not found")
        return case

    def _idempotent(
        self, event: dict[str, Any], user: User, action: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        key = http.header(event, "Idempotency-Key")
        if not key:
            return action()
        item_key = {"PK": {"S": f"API#{user.id}#{key}"}}
        stored = self.dynamodb.get_item(
            TableName=self._idempotency, Key=item_key, ConsistentRead=True
        ).get("Item")
        if stored is not None:
            return dict(json.loads(stored["response"]["S"]))
        result = action()
        if result["statusCode"] < 500:
            self.dynamodb.put_item(
                TableName=self._idempotency,
                Item={
                    **item_key,
                    "response": {"S": json.dumps(result)},
                    "ttl": {"N": str(int(time.time()) + 30 * 86400)},
                },
            )
        return result


_api: Api | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _api
    if _api is None:
        from services.shared import runtime
        from services.shared.signals import RawStore

        dynamodb = runtime.client("dynamodb")
        bus = runtime.client("events")
        intake = Intake(
            dynamodb=dynamodb,
            raw=RawStore(runtime.client("s3"), runtime.raw_bucket()),
            bus=bus,
            component=COMPONENT,
        )
        sap = runtime.sap_client()
        lab_endpoint = sap.write
        auth = lab_endpoint.auth if lab_endpoint else None
        _api = Api(
            dynamodb=dynamodb,
            intake=intake,
            bus=bus,
            states=runtime.client("stepfunctions"),
            execution_arn=os.environ.get("AERA_EXECUTION_STATE_MACHINE_ARN", ""),
            scan=_guardrail_scan(runtime),
            admin=_admin_service(runtime, dynamodb),
            sap=sap,
            lab=Lab(
                dynamodb=dynamodb,
                intake=intake,
                mirror_patch=mirror_patch_via(
                    lab_endpoint.base_url, auth.apply if auth else (lambda headers: None)
                ),
                env=runtime.env(),
            )
            if lab_endpoint
            else None,
        )
    return _api.handle(event)


def _guardrail_scan(runtime: Any) -> Callable[[str], str | None]:
    from services.gatekeeper.handler import Guardrail

    guardrail = Guardrail(
        runtime.client("bedrock-runtime"),
        lambda: runtime.parameter("GUARDRAIL_ID"),
        lambda: runtime.parameter("GUARDRAIL_VERSION"),
    )

    def scan(text: str) -> str | None:  # FR-CHT-04
        result = guardrail.scan(text)
        return result.reason if result.blocked else None

    return scan


def _admin_service(runtime: Any, dynamodb: Any) -> Admin:
    from services.api.admin import mirror_reset_via

    sap = runtime.sap_client()
    auth = sap.read.auth
    return Admin(
        dynamodb=dynamodb,
        mirror_reset=mirror_reset_via(
            sap.read.base_url, auth.apply if auth else (lambda headers: None)
        ),
        env=runtime.env(),
    )
