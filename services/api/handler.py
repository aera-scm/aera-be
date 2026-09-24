"""Console REST API, M1 routes (SRD 6.10, FR-TRI-01, FR-TRI-02, FR-UI-01, FR-UI-05, IR-11).

API Gateway authenticates with the Cognito authorizer; this handler authorises by Cognito
group (NFR-SEC-04). Errors are RFC 7807 problem+json. Mutating routes honour
`Idempotency-Key`: a repeated key returns the first response instead of acting twice.

Routes: GET /cases, GET /cases/{id}, GET /cases/{id}/trace, POST /signals, GET /signals,
POST /cases/{id}/fields/{fieldId}/confirm, POST /realtime/ticket, GET /metrics.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from services.rules.br_13 import board_key
from services.shared import http
from services.shared.audit import AuditWriter
from services.shared.case_state import TERMINAL
from services.shared.cases import CaseStore
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.intake import Attachment, Inbound, Intake
from services.shared.models import Case, CaseStatus, FieldStatus, SignalChannel, SignalStatus
from services.shared.runtime import emit
from services.shared.signals import SignalStore
from services.shared.trace import TraceStore

COMPONENT = "api"
READERS = frozenset({"planner", "approver", "admin"})
PLANNERS = frozenset({"planner", "admin"})
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


def _user(event: dict[str, Any]) -> User:
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    subject = claims.get("sub")
    if not subject:
        raise Problem(401, "Not signed in")
    raw = claims.get("cognito:groups") or ""
    groups = raw if isinstance(raw, list) else re.findall(r"[a-z]+", str(raw))
    return User(id=str(subject), groups=frozenset(str(g) for g in groups))


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

    def __post_init__(self) -> None:
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
            raise Problem(405, "Method not allowed")
        except Problem as problem:
            return problem.response
        except ValueError as error:  # an unknown status or type in the query
            return http.problem(400, "Bad request", str(error))

    # Routing ----------------------------------------------------------------------------

    def _get(
        self, resource: str, params: dict[str, str], event: dict[str, Any], user: User
    ) -> dict[str, Any]:
        _require(user, READERS)
        if resource == "/cases":
            board = self.board(http.query(event, "status"), http.query(event, "type"))
            return http.response(200, board)
        if resource == "/cases/{id}":
            return http.response(200, self.case_detail(params["id"]))
        if resource == "/cases/{id}/trace":
            self._case(params["id"])
            events = self.trace.events(params["id"], after=http.query(event, "after"))
            return http.response(200, [e.model_dump(mode="json", by_alias=True) for e in events])
        if resource == "/signals":
            return http.response(200, self.signal_list(http.query(event, "status")))
        if resource == "/metrics":
            return http.response(200, self.metrics())
        raise Problem(404, "Not found")

    def _post(
        self, resource: str, params: dict[str, str], event: dict[str, Any], user: User
    ) -> dict[str, Any]:
        if resource == "/realtime/ticket":
            _require(user, READERS)
            return http.response(201, self.ticket(user))
        _require(user, PLANNERS)
        body = _json_body(event)
        if resource == "/signals":
            return http.response(202, self.upload(body, user))
        if resource == "/cases/{id}/fields/{fieldId}/confirm":
            return http.response(200, self.confirm(params["id"], params["fieldId"], body, user))
        raise Problem(404, "Not found")

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
        return [_case_json(c) for c in found]

    def case_detail(self, case_id: str) -> dict[str, Any]:
        case = self._case(case_id)
        signals = self.signals.for_case(case_id)
        return {
            "case": _case_json(case),
            "signals": [s.model_dump(mode="json", by_alias=True) for s in signals],
        }

    def signal_list(self, status: str | None) -> list[dict[str, Any]]:
        """FR-UI-05: quarantined signals are shown with their reason; they have no case."""
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
        return {"mrp": mrp}

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
        _api = Api(
            dynamodb=dynamodb,
            intake=Intake(
                dynamodb=dynamodb,
                raw=RawStore(runtime.client("s3"), runtime.raw_bucket()),
                bus=bus,
                component=COMPONENT,
            ),
            bus=bus,
        )
    return _api.handle(event)
