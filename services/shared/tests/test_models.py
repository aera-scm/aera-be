"""Shared data model (DR-01..DR-15, SRD 6.5 Plan schema, FR-IMP-03)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from services.shared.models import (
    AuditEvent,
    Case,
    CaseStatus,
    EventEnvelope,
    ExtractedField,
    FieldStatus,
    Figure,
    ProposedPlan,
    Signal,
    SignalChannel,
    TraceEvent,
    json_schemas,
)

NOW = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
STOCK_REF = (
    "SAP:API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod(Material='MAT-48219',Plant='1020')"
    "/MatlWrhsStkQtyInMatlBaseUnit"
)


def reference_plan(**overrides: object) -> dict[str, object]:
    plan: dict[str, object] = {
        "caseId": "EXC-2026-0914",
        "planVersion": 2,
        "options": [
            {
                "id": "C",
                "name": "Move stock from plant 1020",
                "actions": [
                    {
                        "type": "CREATE_STO",
                        "fromPlant": "1020",
                        "toPlant": "1010",
                        "material": "MAT-48219",
                        "qty": 600,
                        "deliveryDate": "2026-10-05",
                    }
                ],
                "coverageUnits": 600,
                "arrival": "2026-10-05T13:00:00Z",
                "costUsd": 4100,
                "costSourceRef": "ratecard:RC-STO-1020-1010",
                "figures": [{"name": "freeStock1020", "value": 600, "sourceRef": STOCK_REF}],
                "rationale": "Plant 1020 holds 600 units above two days of cover.",
            }
        ],
        "chosen": ["C"],
        "totalCostUsd": 4100,
        "coverageUnits": 600,
        "rationale": "Cheapest reversible cover before stock-out.",
    }
    plan.update(overrides)
    return plan


def test_srd_6_5_reference_plan_is_valid() -> None:
    plan = ProposedPlan.model_validate(reference_plan())

    assert plan.options[0].actions[0].type == "CREATE_STO"
    assert plan.total_cost_usd == Decimal("4100")


def test_br_18_proposed_plan_carries_no_confidence() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        ProposedPlan.model_validate(reference_plan(confidence=0.99))


def test_fr_imp_03_every_figure_needs_a_source_reference() -> None:
    with pytest.raises(ValidationError, match="sourceRef"):
        Figure.model_validate({"name": "freeStock1020", "value": 600})
    with pytest.raises(ValidationError, match="sourceRef"):
        Figure.model_validate({"name": "freeStock1020", "value": 600, "sourceRef": "trust me"})


@pytest.mark.parametrize(
    "reference",
    [
        STOCK_REF,
        "ratecard:RC-STO-1020-1010",
        "planner:planner@meridian-motors.example",
        "signal:01J9ZQ3F2W8XK7Y6V5T4S3R2Q1/QUANTITY",
        "config:TIER1_MAX_USD",
    ],
)
def test_fr_imp_03_known_source_kinds_are_accepted(reference: str) -> None:
    assert Figure(name="x", value=Decimal("1"), source_ref=reference).source_ref == reference


def test_br_01_option_cost_must_come_from_the_rate_card() -> None:
    with pytest.raises(ValidationError, match="costSourceRef"):
        ProposedPlan.model_validate(
            reference_plan(
                options=[{**reference_plan()["options"][0], "costSourceRef": STOCK_REF}]  # type: ignore[index]
            )
        )


def test_srd_6_5_chosen_options_must_exist() -> None:
    with pytest.raises(ValidationError, match="chosen"):
        ProposedPlan.model_validate(reference_plan(chosen=["C", "Z"]))


def test_srd_6_5_unknown_action_type_is_rejected() -> None:
    option = {**reference_plan()["options"][0], "actions": [{"type": "WIRE_MONEY"}]}  # type: ignore[index]
    with pytest.raises(ValidationError):
        ProposedPlan.model_validate(reference_plan(options=[option]))


def test_dr_01_case_ids_follow_the_srd_format() -> None:
    case = Case(
        case_id="EXC-2026-0914",
        type="SUPPLIER_DELAY",
        material="MAT-48219",
        plant="1010",
        status=CaseStatus.RECEIVED,
        created_at=NOW,
        updated_at=NOW,
    )
    assert case.stage == "SIGNAL"
    with pytest.raises(ValidationError):
        Case.model_validate({**case.model_dump(by_alias=True), "caseId": "CASE-1"})


def test_dr_03_extracted_field_confidence_is_a_probability() -> None:
    with pytest.raises(ValidationError):
        ExtractedField(
            field_id="f1",
            signal_id="s1",
            name="QUANTITY",
            value="640",
            confidence=1.4,
            status=FieldStatus.UNCONFIRMED,
        )


def test_dr_02_signal_round_trips_through_json() -> None:
    signal = Signal(
        signal_id="01J9ZQ3F2W8XK7Y6V5T4S3R2Q1",
        channel=SignalChannel.WHATSAPP,
        sender_id="+447700900234",
        received_at=NOW,
        raw_s3_key="raw/whatsapp/01J9.json",
        raw_sha256="a" * 64,
    )

    assert Signal.model_validate_json(signal.model_dump_json(by_alias=True)) == signal


def test_money_and_quantities_serialise_as_json_numbers() -> None:
    plan = ProposedPlan.model_validate(reference_plan())
    data = plan.model_dump(mode="json", by_alias=True)

    assert data["totalCostUsd"] == 4100
    assert data["options"][0]["figures"][0]["value"] == 600


def test_srd_6_17_event_envelope_requires_ulid_and_known_type() -> None:
    envelope = EventEnvelope(
        type="SignalReceived", env="dev", actor="system", data={"signalId": "s1"}, time=NOW
    )
    assert len(envelope.id) == 26
    with pytest.raises(ValidationError):
        EventEnvelope(type="Whatever", env="dev", actor="system", data={}, time=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        EventEnvelope(type="SignalReceived", env="dev", actor="root", data={}, time=NOW)


def test_dr_09_audit_event_and_trace_event_models_exist() -> None:
    event = AuditEvent(
        event_id="01J9ZQ3F2W8XK7Y6V5T4S3R2Q1",
        chain_key="CASE#EXC-2026-0914",
        ts=NOW,
        actor="system",
        type="STATE_TRANSITION",
        payload={"from": "RECEIVED", "to": "TRIAGED"},
        prev_hash="0" * 64,
        hash="1" * 64,
    )
    trace = TraceEvent(
        case_id="EXC-2026-0914", kind="TOOL_CALL", title="sap_get_purchase_order", ts=NOW
    )
    assert event.chain_key.startswith("CASE#")
    assert len(trace.event_id) == 26


def test_srd_7_2_json_schemas_cover_the_contract() -> None:
    schemas = json_schemas()

    for name in ("Case", "Signal", "ProposedPlan", "TraceEvent", "EventEnvelope", "AuditEvent"):
        assert name in schemas
    assert "confidence" not in schemas["ProposedPlan"]["properties"]
    assert schemas["ProposedPlan"]["additionalProperties"] is False
