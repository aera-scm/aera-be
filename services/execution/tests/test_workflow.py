from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from services.execution.journal import Journal, UncertainWrite
from services.execution.ledger import Ledger
from services.execution.workflow import (
    CompensationFailed,
    Execution,
    KillSwitch,
    RejectedWrite,
    StalePlan,
    Step,
    Transfer,
    VerificationFailed,
    Workflow,
)
from services.shared.models import ChangePoDate, CreateSto


class Mirror:
    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self.writes: list[int] = []
        self.undos: list[int] = []
        self.events: list[str] = []
        self.allowed = True
        self.rollback_allowed = True
        self.stale = False
        self.killed = False
        self.fail: int | None = None
        self.mismatch: int | None = None
        self.undo_fail = False
        self.ambiguous = False
        self.halt_after_write = False

    def authorize(self, execution: Execution) -> bool:
        return self.allowed

    def authorize_rollback(self, execution: Execution) -> bool:
        return self.rollback_allowed

    def kill_switch(self) -> bool:
        return self.killed

    def revalidate(self, execution: Execution) -> bool:
        return not self.stale

    def prepare_undo(self, step: Step) -> dict[str, Any]:
        return {"index": step.index, "oldDate": "2026-10-01"}

    def apply(self, step: Step) -> dict[str, Any]:
        execution = sample()
        assert all(
            self.journal.read(Workflow.key(execution, s)) is not None for s in execution.steps
        )
        if step.index == self.fail:
            if self.ambiguous:
                self.writes.append(step.index)
                raise TimeoutError("response lost after SAP committed")
            raise RejectedWrite("synthetic receiver rejected before mutation")
        self.writes.append(step.index)
        if self.halt_after_write:
            self.killed = True
        return {"document": f"450000000{step.index}", "sourceRef": "SAP:PO/A_PurchaseOrder('1')"}

    def verify(self, step: Step, result: dict[str, Any]) -> bool:
        return step.index != self.mismatch

    def undo(self, step: Step, undo: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        if self.undo_fail:
            raise RuntimeError("synthetic compensation failure")
        self.undos.append(step.index)
        return {"restored": True}

    def audit(self, kind: str, data: dict[str, Any]) -> None:
        self.events.append(kind)


def sample() -> Execution:
    return Execution(
        "EXC-2026-0914",
        1,
        "part-C",
        "verified-hash",
        (
            Step(
                0,
                CreateSto(
                    type="CREATE_STO",
                    from_plant="1020",
                    to_plant="1010",
                    material="MAT-48219",
                    qty=Decimal(600),
                    delivery_date=date(2026, 10, 1),
                ),
                "MAT-48219#1020",
                ("SAP:STOCK/A_Stock('1')",),
            ),
            Step(
                1,
                ChangePoDate(
                    type="CHANGE_PO_DATE",
                    po_number="4500001234",
                    po_item="10",
                    schedule_line="1",
                    new_date=date(2026, 10, 2),
                ),
                "4500001234#10#1",
                ("SAP:PO/A_Line('1')",),
            ),
        ),
        (Transfer("MAT-48219", "1020", Decimal(600), Decimal(600), "SAP:STOCK/A_Stock('1')"),),
    )


def setup(dynamodb: Any) -> tuple[Workflow, Mirror]:
    journal = Journal(dynamodb, "test")
    mirror = Mirror(journal)
    return Workflow(journal, Ledger(dynamodb, "test"), mirror), mirror


def test_BR_09_BR_10_AT_07_replay_has_no_new_writes(dynamodb: Any) -> None:
    workflow, mirror = setup(dynamodb)
    first = workflow.run(sample())
    assert workflow.run(sample()) == first
    assert mirror.writes == [0, 1]
    assert mirror.events.index("UNDO_SAVED") < mirror.events.index("EXECUTION_COMPLETED")


def test_AT_08_known_failure_compensates_completed_step(dynamodb: Any) -> None:
    workflow, mirror = setup(dynamodb)
    mirror.fail = 1
    with pytest.raises(RejectedWrite):
        workflow.run(sample())
    assert mirror.writes == [0] and mirror.undos == [0]
    assert "FAILED_ROLLED_BACK" in mirror.events
    balance = workflow.ledger._read("MAT#MAT-48219#PLANT#1020", "BAL")
    assert balance is not None and balance["allocated"] == 0


def test_FR_EXE_06_mismatch_compensates_in_reverse(dynamodb: Any) -> None:
    workflow, mirror = setup(dynamodb)
    mirror.mismatch = 1
    with pytest.raises(VerificationFailed):
        workflow.run(sample())
    assert mirror.undos == [1, 0]


def test_FR_EXE_07_failed_compensation_never_claims_rollback(dynamodb: Any) -> None:
    workflow, mirror = setup(dynamodb)
    mirror.fail, mirror.undo_fail = 1, True
    with pytest.raises(CompensationFailed):
        workflow.run(sample())
    assert "FAILED_ROLLED_BACK" not in mirror.events
    assert "COMPENSATION_FAILED" in mirror.events


def test_FR_EXE_02_AT_17_stale_plan_writes_nothing(dynamodb: Any) -> None:
    workflow, mirror = setup(dynamodb)
    mirror.stale = True
    with pytest.raises(StalePlan):
        workflow.run(sample())
    assert mirror.writes == []


def test_AT_15_kill_switch_halts_at_step_boundary(dynamodb: Any) -> None:
    workflow, mirror = setup(dynamodb)
    mirror.killed = True
    with pytest.raises(KillSwitch):
        workflow.run(sample())
    assert mirror.writes == []
    mirror.killed, mirror.halt_after_write = False, True
    with pytest.raises(KillSwitch):
        workflow.run(sample())
    assert mirror.writes == [0] and mirror.undos == []


def test_BR_09_ambiguous_response_is_not_retried(dynamodb: Any) -> None:
    workflow, mirror = setup(dynamodb)
    mirror.fail, mirror.ambiguous = 1, True
    with pytest.raises(TimeoutError):
        workflow.run(sample())
    assert "EXECUTION_UNCERTAIN" in mirror.events
    with pytest.raises(UncertainWrite):
        workflow.run(sample())
    assert mirror.writes == [0, 1]
    balance = workflow.ledger._read("MAT#MAT-48219#PLANT#1020", "BAL")
    assert balance is not None and balance["allocated"] == 600


def test_FR_EXE_08_rollback_authority_and_idempotency(dynamodb: Any) -> None:
    workflow, mirror = setup(dynamodb)
    workflow.run(sample())
    mirror.rollback_allowed = False
    with pytest.raises(PermissionError):
        workflow.rollback(sample())
    mirror.rollback_allowed = True
    workflow.rollback(sample())
    workflow.rollback(sample())
    assert mirror.undos == [1, 0]


def test_FR_EXE_01_unapproved_plan_never_writes(dynamodb: Any) -> None:
    workflow, mirror = setup(dynamodb)
    mirror.allowed = False
    with pytest.raises(PermissionError):
        workflow.run(sample())
    assert mirror.writes == []
