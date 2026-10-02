"""Case service and MRP poller (FR-ING-01, FR-ING-02, FR-ING-08, FR-TRI-01, AT-01 offline)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from services.case_service.handler import CaseService
from services.conftest import RAW_BUCKET, RecordingBus
from services.mrp_poller.handler import MrpPoller
from services.rules.br_13 import board_key
from services.shared.cases import CaseStore
from services.shared.intake import Inbound, Intake
from services.shared.models import CaseStatus, SignalChannel, SignalStatus
from services.shared.sap_client import SapClient
from services.shared.signals import RawStore, SignalStore

ENV = "test"
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
ACTIONABLE = {"MAT-48219", "MAT-51002", "MAT-33871", "MAT-20114", "MAT-60417", "MAT-72055"}


@pytest.fixture
def service(dynamodb: Any, sap: SapClient, bus: RecordingBus) -> CaseService:
    CaseStore(dynamodb, ENV).seed_counter(2026, 913)
    return CaseService(dynamodb=dynamodb, sap=sap, bus=bus, clock=lambda: T0, env=ENV)


def poll(sap: SapClient, bus: RecordingBus) -> dict[str, Any]:
    MrpPoller(sap=sap, bus=bus, env=ENV).poll()
    [event] = bus.details("MrpExceptionsPolled")
    return dict(event["data"])


def test_fr_ing_02_poller_forwards_six_of_214_and_counts_the_rest(
    sap: SapClient, bus: RecordingBus
) -> None:
    data = poll(sap, bus)

    assert (data["total"], data["actionableCount"], data["suppressedCount"]) == (214, 6, 208)
    assert {m["material"] for m in data["messages"]} == ACTIONABLE
    reference = next(m for m in data["messages"] if m["material"] == "MAT-48219")
    assert reference["element"] == "4500001234"
    assert reference["sourceRef"].startswith("SAP:ZAERA_MIRROR_SRV/MRPExceptionMessage(")


def test_fr_ing_02_tolerance_comes_from_configuration(sap: SapClient, bus: RecordingBus) -> None:
    counts = MrpPoller(sap=sap, bus=bus, tolerance=lambda: (0, 0), env=ENV).poll()
    assert counts["actionableCount"] == 214  # zero tolerance: every proposed shift counts


def test_at_01_six_cases_open_and_the_reference_case_ranks_first(
    service: CaseService, sap: SapClient, bus: RecordingBus, dynamodb: Any
) -> None:
    opened = service.on_mrp(poll(sap, bus))

    store = CaseStore(dynamodb, ENV)
    cases = [store.get(case_id) for case_id in opened]
    assert len(cases) == 6 and all(c is not None for c in cases)
    board = sorted(
        (c for c in cases if c is not None),
        key=lambda c: board_key(c.rar_usd or Decimal(0), _hours(c.stockout_at)),
    )
    first = board[0]
    assert (first.case_id, first.material, first.po_number) == (
        "EXC-2026-0914",
        "MAT-48219",
        "4500001234",
    )
    assert first.status is CaseStatus.TRIAGED
    assert (first.rar_usd, first.priority_score) == (Decimal(4_720_000), Decimal(14_160_000))
    assert {c.material for c in board} == ACTIONABLE
    assert bus.types().count("CaseOpened") == 6
    assert bus.types().count("CaseReadyForRun") == 6
    header = dynamodb.get_item(
        TableName="aera-test-cases", Key={"PK": {"S": "BOARD"}, "SK": {"S": "MRP"}}
    )["Item"]
    assert (header["total"]["N"], header["actionable"]["N"], header["suppressed"]["N"]) == (
        "214",
        "6",
        "208",
    )


def _hours(stockout: datetime | None) -> Decimal | None:
    return None if stockout is None else Decimal((stockout - T0).total_seconds()) / 3600


def test_fr_ing_01_a_second_poll_updates_instead_of_duplicating(
    service: CaseService, sap: SapClient, bus: RecordingBus
) -> None:
    data = poll(sap, bus)
    first = service.on_mrp(data)
    again = service.on_mrp(data)

    assert len(first) == 6 and again == []
    assert bus.types().count("CaseUpdated") == 6


def accepted_signal(
    dynamodb: Any, s3: Any, bus: RecordingBus, *, po: str, received: datetime, key: str
) -> str:
    intake = Intake(
        dynamodb=dynamodb, raw=RawStore(s3, RAW_BUCKET), bus=bus, component="t", env=ENV
    )
    signal = intake.receive(
        Inbound(
            channel=SignalChannel.WHATSAPP,
            sender_id="+447700900234",
            body=json.dumps({"k": key}).encode(),
            content_type="application/json",
            dedup_key=key,
            received_at=received,
        )
    )
    SignalStore(dynamodb, ENV).save(
        signal.model_copy(
            update={
                "status": SignalStatus.ACCEPTED,
                "sender_verified": True,
                "po_number": po,
                "material": "MAT-48219",
            }
        )
    )
    return signal.signal_id


def test_fr_ing_08_whatsapp_and_email_about_the_same_po_join_the_reference_case(
    service: CaseService, sap: SapClient, bus: RecordingBus, dynamodb: Any, s3: Any
) -> None:
    service.on_mrp(poll(sap, bus))
    photo = accepted_signal(dynamodb, s3, bus, po="4500001234", received=T0, key="wa")
    mail = accepted_signal(
        dynamodb, s3, bus, po="4500001234", received=T0 + timedelta(hours=1), key="em"
    )

    assert service.on_signal(photo) == "EXC-2026-0914"
    assert service.on_signal(mail) == "EXC-2026-0914"
    case = CaseStore(dynamodb, ENV).get("EXC-2026-0914")
    assert case is not None and case.signal_ids == [photo, mail]
    assert [s.signal_id for s in SignalStore(dynamodb, ENV).for_case("EXC-2026-0914")] == [
        photo,
        mail,
    ]


def test_br_15_a_signal_long_after_the_case_went_quiet_opens_a_new_case(
    service: CaseService, bus: RecordingBus, dynamodb: Any, s3: Any
) -> None:
    first = accepted_signal(dynamodb, s3, bus, po="4500001234", received=T0, key="a")
    assert service.on_signal(first) == "EXC-2026-0914"

    later = accepted_signal(
        dynamodb, s3, bus, po="4500001234", received=T0 + timedelta(hours=73), key="b"
    )
    assert service.on_signal(later) == "EXC-2026-0915"


def test_signals_without_a_known_po_open_no_case(
    service: CaseService, bus: RecordingBus, dynamodb: Any, s3: Any
) -> None:
    unknown = accepted_signal(dynamodb, s3, bus, po="4599999999", received=T0, key="x")
    assert service.on_signal(unknown) is None
    assert "CaseOpened" not in bus.types()


def test_redelivered_signal_event_is_harmless(
    service: CaseService, bus: RecordingBus, dynamodb: Any, s3: Any
) -> None:
    signal = accepted_signal(dynamodb, s3, bus, po="4500001234", received=T0, key="a")
    assert service.on_signal(signal) == service.on_signal(signal) == "EXC-2026-0914"
    assert bus.types().count("CaseOpened") == 1


@pytest.mark.parametrize("event_type", ["CaseOpened", "CaseReadyForRun"])
def test_signal_retry_recovers_failed_open_event(
    service: CaseService, dynamodb: Any, s3: Any, bus: RecordingBus, event_type: str
) -> None:
    signal_id = accepted_signal(dynamodb, s3, bus, po="4500001234", received=T0, key="retry")

    def reject(request: dict[str, Any]) -> None:
        if request["Entries"][0]["DetailType"] == event_type:
            raise ConnectionError("event transport interrupted")

    class InterruptedBus(RecordingBus):
        def put_events(self, Entries: list[dict[str, Any]]) -> dict[str, Any]:
            reject({"Entries": Entries})
            return super().put_events(Entries)

    service.bus = InterruptedBus()
    with pytest.raises(ConnectionError):
        service.on_signal(signal_id)
    service.bus = bus
    case_id = service.on_signal(signal_id)
    assert case_id == "EXC-2026-0914"
    assert bus.types().count("CaseReadyForRun") == 1
    stored = service.cases.get(case_id)
    assert stored is not None and stored.status is CaseStatus.TRIAGED
    assert stored.figures and all(figure.source_ref.startswith("SAP:") for figure in stored.figures)


def test_concurrent_signal_open_has_one_case_and_no_orphan(
    service: CaseService, dynamodb: Any, s3: Any, bus: RecordingBus
) -> None:
    first = accepted_signal(dynamodb, s3, bus, po="4500001234", received=T0, key="first")
    second = accepted_signal(dynamodb, s3, bus, po="4500001234", received=T0, key="second")
    winner: list[str | None] = []

    def overlap(**kwargs: Any) -> None:
        dynamodb.meta.events.unregister("before-call.dynamodb.TransactWriteItems", overlap)
        winner.append(service.on_signal(second))

    dynamodb.meta.events.register("before-call.dynamodb.TransactWriteItems", overlap)
    assert service.on_signal(first) == winner[0]
    items = dynamodb.scan(TableName="aera-test-cases")["Items"]
    cases = [
        item for item in items if item["PK"]["S"].startswith("CASE#") and item["SK"]["S"] == "META"
    ]
    assert len(cases) == 1
    stored = service.cases.get(str(winner[0]))
    assert stored is not None and set(stored.signal_ids) == {first, second}
    assert bus.types().count("CaseOpened") == 1


def test_mrp_retry_recovers_received_case(
    service: CaseService, sap: SapClient, bus: RecordingBus, dynamodb: Any
) -> None:
    data = poll(sap, bus)
    data["messages"] = data["messages"][:1]

    def interrupt(**kwargs: Any) -> None:
        dynamodb.meta.events.unregister("before-call.dynamodb.UpdateItem", interrupt)
        raise ConnectionError("triage write interrupted")

    def after_creation(**kwargs: Any) -> None:
        dynamodb.meta.events.unregister("after-call.dynamodb.TransactWriteItems", after_creation)
        dynamodb.meta.events.register("before-call.dynamodb.UpdateItem", interrupt)

    dynamodb.meta.events.register("after-call.dynamodb.TransactWriteItems", after_creation)
    with pytest.raises(ConnectionError):
        service.on_mrp(data)
    service.on_mrp(data)
    stored = service.cases.get("EXC-2026-0914")
    assert stored is not None and stored.status is CaseStatus.TRIAGED
    assert bus.types().count("CaseReadyForRun") == 1


@pytest.mark.parametrize("event_type", ["CaseUpdated", "CaseReadyForRun"])
def test_assigned_signal_retry_recovers_failed_update_event(
    service: CaseService, dynamodb: Any, s3: Any, bus: RecordingBus, event_type: str
) -> None:
    first = accepted_signal(dynamodb, s3, bus, po="4500001234", received=T0, key="open")
    service.on_signal(first)
    second = accepted_signal(dynamodb, s3, bus, po="4500001234", received=T0, key="update")

    class InterruptedBus(RecordingBus):
        def put_events(self, Entries: list[dict[str, Any]]) -> dict[str, Any]:
            if Entries[0]["DetailType"] == event_type:
                raise ConnectionError("event transport interrupted")
            return super().put_events(Entries)

    service.bus = InterruptedBus()
    with pytest.raises(ConnectionError):
        service.on_signal(second)
    service.bus = bus
    before = len(bus.details("CaseReadyForRun"))
    assert service.on_signal(second) == "EXC-2026-0914"
    assert len(bus.details("CaseReadyForRun")) == before + 1
    assert service.on_signal(second) == "EXC-2026-0914"
    assert len(bus.details("CaseReadyForRun")) == before + 1
    stored = service.cases.get("EXC-2026-0914")
    assert stored is not None and stored.signal_ids == [first, second]


def test_concurrent_mrp_open_has_one_case(
    service: CaseService, dynamodb: Any, sap: SapClient, bus: RecordingBus
) -> None:
    data = poll(sap, bus)
    data["messages"] = data["messages"][:1]

    def overlap(**kwargs: Any) -> None:
        dynamodb.meta.events.unregister("before-call.dynamodb.TransactWriteItems", overlap)
        service.on_mrp(data)

    dynamodb.meta.events.register("before-call.dynamodb.TransactWriteItems", overlap)
    service.on_mrp(data)
    items = dynamodb.scan(TableName="aera-test-cases")["Items"]
    assert len([item for item in items if item["SK"]["S"] == "META"]) == 1
    assert bus.types().count("CaseOpened") == 1


def test_br_15_exact_72_hours_joins_existing_case(
    service: CaseService, dynamodb: Any, s3: Any, bus: RecordingBus
) -> None:
    first = accepted_signal(dynamodb, s3, bus, po="4500001234", received=T0, key="boundary-open")
    later = accepted_signal(
        dynamodb, s3, bus, po="4500001234", received=T0 + timedelta(hours=72), key="boundary-join"
    )
    assert service.on_signal(first) == service.on_signal(later)
    assert bus.types().count("CaseOpened") == 1


def test_closed_lookup_can_be_replaced_without_duplicate_open_case(
    service: CaseService, dynamodb: Any, s3: Any, bus: RecordingBus
) -> None:
    first = accepted_signal(dynamodb, s3, bus, po="4500001234", received=T0, key="closed-open")
    case_id = service.on_signal(first)
    assert case_id is not None
    for status in (CaseStatus.INVESTIGATING, CaseStatus.ESCALATED, CaseStatus.CLOSED):
        service.cases.transition(case_id, status, actor="system")
    second = accepted_signal(dynamodb, s3, bus, po="4500001234", received=T0, key="closed-new")
    assert service.on_signal(second) == "EXC-2026-0915"
    assert service.on_signal(second) == "EXC-2026-0915"
    assert bus.types().count("CaseOpened") == 2
