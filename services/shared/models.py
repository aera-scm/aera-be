"""Shared data model: DR-01..DR-15, the Plan schema (SRD 6.5) and the event envelope (6.17).

Defined once here and exported as JSON Schema for tool definitions and the console's
generated TypeScript types (SRD 7.2). Field names are snake_case in Python and camelCase
on the wire. Money and quantities are Decimals serialised as JSON numbers.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    computed_field,
    model_validator,
)
from pydantic.alias_generators import to_camel
from ulid import ULID


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


Money = Annotated[Decimal, PlainSerializer(_number, return_type=int | float, when_used="json")]
Quantity = Annotated[Decimal, PlainSerializer(_number, return_type=int | float, when_used="json")]
Ratio = Annotated[
    Decimal, Field(ge=0, le=1), PlainSerializer(_number, return_type=int | float, when_used="json")
]

# FR-IMP-03: where a figure came from. SAP entity (optionally a field), rate card entry,
# planner confirmation, signal field (corroboration only) or configuration key.
_SOURCE_REF = re.compile(
    r"SAP:[A-Z0-9_]+/[A-Za-z0-9_]+(\([^)]*\))?(/[A-Za-z0-9_]+)*"
    r"|ratecard:[A-Za-z0-9-]+"
    r"|planner:\S+"
    r"|signal:[A-Za-z0-9]+/[A-Z_]+"
    r"|config:[A-Z0-9_]+"
)


def _source_ref(value: str) -> str:
    if not _SOURCE_REF.fullmatch(value):
        raise ValueError(
            f"sourceRef {value!r} is not a SAP, ratecard, planner, signal or config reference"
        )
    return value


def _rate_card_ref(value: str) -> str:
    if not value.startswith("ratecard:"):
        raise ValueError("costSourceRef must reference the rate card (BR-01)")
    return _source_ref(value)


SourceRef = Annotated[str, AfterValidator(_source_ref)]
RateCardRef = Annotated[str, AfterValidator(_rate_card_ref)]
Ulid = Annotated[str, Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")]
CaseId = Annotated[str, Field(pattern=r"^EXC-\d{4}-\d{4,}$")]


def new_ulid() -> str:
    return str(ULID())


class Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=False
    )


# DR-04 ---------------------------------------------------------------------------------


class Figure(Model):
    name: str
    value: Quantity | str
    unit: str | None = None
    source_ref: SourceRef
    read_at: datetime | None = None


# DR-01 ---------------------------------------------------------------------------------


class CaseStatus(StrEnum):
    RECEIVED = "RECEIVED"
    TRIAGED = "TRIAGED"
    INVESTIGATING = "INVESTIGATING"
    WAITING_PLANNER = "WAITING_PLANNER"
    WAITING_SUPPLIER = "WAITING_SUPPLIER"
    PLAN_PROPOSED = "PLAN_PROPOSED"
    VERIFIED = "VERIFIED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AUTO_APPROVED = "AUTO_APPROVED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"
    EXECUTING = "EXECUTING"
    MONITORING = "MONITORING"
    REOPENED = "REOPENED"
    CLOSED = "CLOSED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED_ROLLED_BACK = "FAILED_ROLLED_BACK"


Stage = Literal["SIGNAL", "TRIAGE", "IMPACT", "OPTIONS", "APPROVE", "EXECUTE"]

_STAGES: dict[CaseStatus, Stage] = {
    CaseStatus.RECEIVED: "SIGNAL",
    CaseStatus.TRIAGED: "TRIAGE",
    CaseStatus.INVESTIGATING: "IMPACT",
    CaseStatus.WAITING_PLANNER: "IMPACT",
    CaseStatus.WAITING_SUPPLIER: "IMPACT",
    CaseStatus.PLAN_PROPOSED: "OPTIONS",
    CaseStatus.VERIFIED: "OPTIONS",
    CaseStatus.AWAITING_APPROVAL: "APPROVE",
    CaseStatus.AUTO_APPROVED: "APPROVE",
    CaseStatus.APPROVED: "APPROVE",
    CaseStatus.REJECTED: "APPROVE",
    CaseStatus.ESCALATED: "APPROVE",
    CaseStatus.EXECUTING: "EXECUTE",
    CaseStatus.MONITORING: "EXECUTE",
    CaseStatus.REOPENED: "EXECUTE",
    CaseStatus.CLOSED: "EXECUTE",
    CaseStatus.ROLLED_BACK: "EXECUTE",
    CaseStatus.FAILED_ROLLED_BACK: "EXECUTE",
}

CaseType = Literal[
    "MRP_EXCEPTION", "SUPPLIER_DELAY", "CARRIER_DELAY", "QUANTITY_SHORTFALL", "OTHER"
]


class Case(Model):
    case_id: CaseId
    type: CaseType
    material: str
    material_description: str | None = None
    plant: str
    po_number: str | None = None
    po_item: str | None = None
    status: CaseStatus
    tier: Literal[1, 2, 3] | None = None
    priority_score: Money | None = None
    rar_usd: Money | None = None
    stockout_at: datetime | None = None
    days_late: Quantity | None = None
    confidence: Ratio | None = None
    plan_version: int = 0
    active_run_id: str | None = None
    signal_ids: list[str] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stage(self) -> Stage:
        return _STAGES[self.status]


# DR-02 / DR-03 --------------------------------------------------------------------------


class SignalChannel(StrEnum):
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"
    CARRIER = "CARRIER"
    MANUAL = "MANUAL"
    AGENT = "AGENT"
    LAB = "LAB"


class SignalStatus(StrEnum):
    RECEIVED = "RECEIVED"
    ACCEPTED = "ACCEPTED"
    QUARANTINED = "QUARANTINED"


class FieldStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNCONFIRMED = "UNCONFIRMED"
    SAP_MATCHED = "SAP_MATCHED"


CriticalField = Literal["PO_NUMBER", "MATERIAL", "QUANTITY", "DELIVERY_DATE", "PRICE"]
FieldName = CriticalField | Literal["TRACKING_NUMBER", "CARRIER_STATUS", "ETA", "PO_ITEM"]


class ExtractedField(Model):
    field_id: str
    signal_id: str
    name: FieldName
    value: str
    confidence: Annotated[float, Field(ge=0, le=1)]
    status: FieldStatus
    confirmed_by: str | None = None


class Signal(Model):
    signal_id: Ulid
    channel: SignalChannel
    sender_id: str
    sender_verified: bool = False
    supplier_id: str | None = None
    received_at: datetime
    raw_s3_key: str
    raw_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    # S3 keys of attachments and images stored next to the raw payload (FR-ING-06, FR-ING-09).
    attachments: list[str] = Field(default_factory=list)
    normalized_text: str | None = None
    guardrail_result: Literal["NOT_SCANNED", "PASSED", "BLOCKED"] = "NOT_SCANNED"
    quarantine_reason: str | None = None
    language: str | None = None
    po_number: str | None = None
    material: str | None = None
    case_id: CaseId | None = None
    status: SignalStatus = SignalStatus.RECEIVED
    fields: list[ExtractedField] = Field(default_factory=list)


# DR-05 Plan (SRD 6.5) -------------------------------------------------------------------


class CreateSto(Model):
    type: Literal["CREATE_STO"]
    from_plant: str
    to_plant: str
    material: str
    qty: Quantity
    delivery_date: date


class ChangePoDate(Model):
    type: Literal["CHANGE_PO_DATE"]
    po_number: str
    po_item: str
    schedule_line: str
    new_date: date


class SplitPoScheduleLine(Model):
    class Part(Model):
        qty: Quantity
        delivery_date: date

    type: Literal["SPLIT_PO_SCHEDULE_LINE"]
    po_number: str
    po_item: str
    schedule_line: str
    parts: Annotated[list[Part], Field(min_length=2)]


class BookAirFreight(Model):
    type: Literal["BOOK_AIR_FREIGHT"]
    supplier_id: str
    po_number: str
    po_item: str
    qty: Quantity
    arrival: datetime


class CreatePoAlternate(Model):
    type: Literal["CREATE_PO_ALTERNATE"]
    supplier_id: str
    material: str
    plant: str
    qty: Quantity
    delivery_date: date


Action = Annotated[
    CreateSto | ChangePoDate | SplitPoScheduleLine | BookAirFreight | CreatePoAlternate,
    Field(discriminator="type"),
]


class Option(Model):
    id: Annotated[str, Field(pattern=r"^[A-Z]$")]
    name: str
    actions: Annotated[list[Action], Field(min_length=1)]
    coverage_units: Quantity
    arrival: datetime
    cost_usd: Money
    cost_source_ref: RateCardRef
    figures: list[Figure] = Field(default_factory=list)
    rationale: str


class ProposedPlan(Model):
    """What the agent may propose; no confidence and no checks (BR-18, 6.5)."""

    case_id: CaseId
    plan_version: Annotated[int, Field(ge=1)]
    options: Annotated[list[Option], Field(min_length=1)]
    chosen: Annotated[list[str], Field(min_length=1)]
    total_cost_usd: Money
    coverage_units: Quantity
    rationale: str

    @model_validator(mode="after")
    def _chosen_exist(self) -> ProposedPlan:
        ids = {option.id for option in self.options}
        if len(ids) != len(self.options):
            raise ValueError("option ids must be unique")
        missing = [choice for choice in self.chosen if choice not in ids]
        if missing:
            raise ValueError(f"chosen refers to unknown options {missing}")
        return self


class CheckResult(Model):
    check_id: Annotated[str, Field(pattern=r"^V-\d{2}$")]
    passed: bool
    blocking: bool
    option_id: str | None = None
    detail: str


class PlanRecord(Model):
    """A stored plan: the proposal plus what the Verifier computed."""

    plan: ProposedPlan
    confidence: Ratio | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    proposed_at: datetime
    verified_at: datetime | None = None


# DR-06 .. DR-15 -------------------------------------------------------------------------


class Approval(Model):
    case_id: CaseId
    plan_part_id: str
    plan_version_hash: str
    approver_id: str
    backup_approver_id: str | None = None
    limit_usd: Money
    deadline_at: datetime
    reminded_at: datetime | None = None
    decision: Literal["APPROVED", "REJECTED"] | None = None
    comment: str | None = None
    decided_at: datetime | None = None


class Reservation(Model):
    material_plant: str
    reservation_id: str
    case_id: CaseId
    qty: Quantity
    status: Literal["HELD", "COMMITTED", "RELEASED"]
    version: int = 0


class ExecutionRecord(Model):
    idempotency_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    action: str
    request: dict[str, Any]
    response: dict[str, Any] | None = None
    sap_doc_number: str | None = None
    status: Literal["PENDING", "SUCCEEDED", "FAILED", "UNDONE"]
    undo_action: dict[str, Any] | None = None


class AuditEvent(Model):
    event_id: Ulid
    chain_key: str
    case_id: CaseId | None = None
    ts: datetime
    actor: Annotated[str, Field(pattern=r"^(system|agent|user:\S+)$")]
    type: str
    payload: dict[str, Any]
    prev_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ConfigItem(Model):
    key: str
    value: Money | str
    changed_by: str
    changed_at: datetime


class RateCard(Model):
    entry_id: str
    action_type: Literal["STO", "AIR_FREIGHT", "ALTERNATE_SUPPLIER", "EXPEDITE"]
    from_plant: str | None = None
    to_plant: str | None = None
    supplier_id: str | None = None
    lane: str | None = None
    unit_cost_usd: Money
    fixed_cost_usd: Money
    lead_time_hours: Quantity
    valid_from: date
    valid_to: date
    changed_by: str | None = None


class ApproverLimit(Model):
    user_id: str
    role: Literal["approver"]
    plant: str
    limit_usd: Money
    valid_from: date
    valid_to: date
    granted_by: str | None = None


class SupplierReliability(Model):
    supplier_id: str
    material: str
    window_days: int
    sample_size: int
    on_time_rate: Ratio
    mean_delay_days: Quantity
    p90_delay_days: Quantity
    partial_rate: Ratio
    computed_at: datetime


class DialogueMessage(Model):
    message_id: Ulid
    case_id: CaseId
    direction: Literal["OUTBOUND", "INBOUND"]
    supplier_id: str
    language: str
    template_id: str | None = None
    rendered_text: str
    english_copy: str
    reference_token: str
    sent_at: datetime | None = None
    reply_signal_id: str | None = None
    status: Literal["DRAFT", "SENT", "REPLIED", "TIMED_OUT", "BLOCKED"]


class Portfolio(Model):
    portfolio_id: str
    case_ids: list[CaseId]
    candidate_actions: list[dict[str, Any]]
    solver_status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "TIMEOUT"]
    objective: Money
    saving_vs_single: Money


class ScenarioRun(Model):
    scenario_id: str
    parameters: dict[str, Any]
    created_by: str
    outcome: str | None = None
    synthetic: Literal[True] = True


# Events and trace (SRD 6.17, FR-AUD-03) ------------------------------------------------

EventType = Literal[
    "SignalReceived",
    "SignalAccepted",
    "SignalQuarantined",
    "SignalExtracted",
    "MrpExceptionsPolled",
    "CaseOpened",
    "CaseUpdated",
    "CaseReadyForRun",
    "RunStarted",
    "RunEnded",
    "PlanProposed",
    "PlanVerified",
    "PlanRouted",
    "PlanApproved",
    "PlanRejected",
    "ExecutionStarted",
    "ExecutionCompleted",
    "ExecutionFailed",
    "NotificationRequested",
    "SupplierInfoRequested",
    "SupplierReplyMatched",
    "GoodsReceiptDue",
    "CaseClosed",
    "CaseReopened",
    "PortfolioSolved",
    "LabScenarioCreated",
]


class EventEnvelope(Model):
    id: Ulid = Field(default_factory=new_ulid)
    type: EventType
    time: datetime
    env: str
    case_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    actor: Annotated[str, Field(pattern=r"^(system|agent|user:\S+)$")]
    data: dict[str, Any]


class TraceEvent(Model):
    case_id: CaseId
    event_id: Ulid = Field(default_factory=new_ulid)
    run_id: str | None = None
    kind: Literal["AGENT", "TOOL_CALL", "TOOL_RESULT", "CHECK", "GUARD", "SYSTEM"]
    title: str
    detail: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    ts: datetime


EXPORTED: tuple[type[BaseModel], ...] = (
    Case,
    Signal,
    ExtractedField,
    Figure,
    ProposedPlan,
    PlanRecord,
    CheckResult,
    Approval,
    Reservation,
    ExecutionRecord,
    AuditEvent,
    ConfigItem,
    RateCard,
    ApproverLimit,
    SupplierReliability,
    DialogueMessage,
    Portfolio,
    ScenarioRun,
    EventEnvelope,
    TraceEvent,
)


def json_schemas() -> dict[str, dict[str, Any]]:
    """JSON Schema per exported model, camelCase, in serialisation mode."""
    return {
        model.__name__: model.model_json_schema(by_alias=True, mode="serialization")
        for model in EXPORTED
    }
