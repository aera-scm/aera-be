"""FR-EXE-02..08: ordered writes, undo first, verification and reverse compensation."""

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Protocol

from services.execution.journal import Journal, UncertainWrite, idempotency_key
from services.execution.ledger import Ledger
from services.shared.models import Action


class StalePlan(ValueError):
    pass


class KillSwitch(RuntimeError):
    pass


class VerificationFailed(RuntimeError):
    pass


class CompensationFailed(RuntimeError):
    pass


class RejectedWrite(RuntimeError):
    """The receiver definitively refused this write without changing its state."""


@dataclass(frozen=True)
class Transfer:
    material: str
    plant: str
    quantity: Decimal
    available: Decimal
    source_ref: str


@dataclass(frozen=True)
class Step:
    index: int
    action: Action
    target: str
    source_refs: tuple[str, ...]
    substep: int = 0


@dataclass(frozen=True)
class Execution:
    case_id: str
    version: int
    part_id: str
    version_hash: str
    steps: tuple[Step, ...]
    transfers: tuple[Transfer, ...] = ()


class Boundary(Protocol):
    def authorize(self, execution: Execution) -> bool: ...
    def authorize_rollback(self, execution: Execution) -> bool: ...
    def kill_switch(self) -> bool: ...
    def revalidate(self, execution: Execution) -> bool: ...
    def prepare_undo(self, step: Step) -> dict[str, Any]: ...
    def apply(self, step: Step) -> dict[str, Any]: ...
    def verify(self, step: Step, result: dict[str, Any]) -> bool: ...
    def undo(self, step: Step, undo: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]: ...
    def audit(self, kind: str, data: dict[str, Any]) -> None: ...


class Workflow:
    def __init__(self, journal: Journal, ledger: Ledger, boundary: Boundary) -> None:
        self.journal = journal
        self.ledger = ledger
        self.boundary = boundary

    @staticmethod
    def key(execution: Execution, step: Step) -> str:
        # Index is the original plan action index, never renumbered inside a split part.
        return idempotency_key(
            execution.case_id, execution.version, step.index, step.action.type, step.target
        )

    def run(self, execution: Execution) -> list[dict[str, Any]]:
        if not self.boundary.authorize(execution):
            raise PermissionError("persisted approval for exact plan part required")
        execution = self.expand(execution)
        if not execution.steps or len({(s.index, s.target) for s in execution.steps}) != len(
            execution.steps
        ):
            raise ValueError("nonempty uniquely indexed steps required")
        if any(not step.source_refs for step in execution.steps):
            raise ValueError("every write requires source references")
        if self.boundary.kill_switch():
            raise KillSwitch("advise-only: no execution may start")
        execution_key = f"EXEC#{execution.case_id}#{execution.version}#{execution.part_id}"
        self.journal.prepare(
            execution_key,
            {
                "versionHash": execution.version_hash,
                "steps": [
                    {
                        "index": s.index,
                        "target": s.target,
                        "action": s.action.model_dump(mode="json", by_alias=True),
                        "sourceRefs": list(s.source_refs),
                    }
                    for s in execution.steps
                ],
            },
            {"type": "REVERSE_COMPLETED_ACTIONS"},
        )
        replay = self.journal.claim(execution_key)
        if replay is not None:
            self.boundary.audit("EXECUTION_REPLAY", {"partId": execution.part_id})
            return [dict(result) for result in replay["results"]]
        keys = [self.key(execution, step) for step in execution.steps]
        records = [self.journal.read(key) for key in keys]
        if all(record is not None and record["status"] == "SUCCEEDED" for record in records):
            self.boundary.audit("EXECUTION_REPLAY", {"partId": execution.part_id})
            return [dict(record["result"]) for record in records if record is not None]
        if any(
            record is not None and record["status"] in {"PENDING", "UNDONE"} for record in records
        ):
            raise UncertainWrite("partial or compensated execution requires reconciliation")
        if not self.boundary.revalidate(execution):
            self.boundary.audit("RETURN_TO_PLANNING", {"reason": "STALE_PLAN"})
            raise StalePlan("dependent SAP values changed")
        held: list[Transfer] = []
        try:
            for transfer in execution.transfers:
                self.ledger.reserve(
                    material=transfer.material,
                    plant=transfer.plant,
                    reservation_id=execution.part_id,
                    case_id=execution.case_id,
                    quantity=transfer.quantity,
                    available=transfer.available,
                    source_ref=transfer.source_ref,
                )
                held.append(transfer)
            for step, key, record in zip(execution.steps, keys, records, strict=True):
                undo = record["undo"] if record is not None else self.boundary.prepare_undo(step)
                self.journal.prepare(
                    key,
                    {
                        "action": step.action.model_dump(mode="json", by_alias=True),
                        "target": step.target,
                        "sourceRefs": list(step.source_refs),
                        "versionHash": execution.version_hash,
                        "partId": execution.part_id,
                    },
                    undo,
                )
        except Exception:
            for transfer in reversed(held):
                self._release(execution, transfer)
            raise
        self.boundary.audit("UNDO_SAVED", {"partId": execution.part_id, "keys": keys})
        results = []
        completed: list[tuple[Step, str, dict[str, Any]]] = []
        try:
            for step, key in zip(execution.steps, keys, strict=True):
                if self.boundary.kill_switch():
                    raise KillSwitch("execution halted between writes")
                previous = self.journal.claim(key)
                if previous is None:
                    try:
                        result = self.boundary.apply(step)
                    except RejectedWrite:
                        self.journal.rejected(key)
                        raise
                    self.journal.complete(key, result)
                else:
                    result = previous
                completed.append((step, key, result))
                if not self.boundary.verify(step, result):
                    raise VerificationFailed("SAP post-write values differ")
                results.append(result)
        except KillSwitch:
            self.boundary.audit("EXECUTION_HALTED", {"partId": execution.part_id})
            raise
        except Exception:
            self._compensate(execution, completed)
            # Never release stock held for an unresolved SAP effect.
            uncertain = any(
                (record := self.journal.read(key)) is not None and record["status"] == "PENDING"
                for key in keys
            )
            if not uncertain:
                for transfer in held:
                    self._release(execution, transfer)
            self.boundary.audit(
                "EXECUTION_UNCERTAIN" if uncertain else "FAILED_ROLLED_BACK",
                {"partId": execution.part_id},
            )
            raise
        self.boundary.audit(
            "EXECUTION_COMPLETED", {"partId": execution.part_id, "results": results}
        )
        self.journal.complete(execution_key, {"results": results})
        return results

    def _release(self, execution: Execution, transfer: Transfer) -> None:
        self.ledger.release(
            material=transfer.material, plant=transfer.plant, reservation_id=execution.part_id
        )

    def _compensate(
        self, execution: Execution, completed: list[tuple[Step, str, dict[str, Any]]]
    ) -> None:
        try:
            for step, key, result in reversed(completed):
                record = self.journal.read(key)
                if record is None:
                    raise CompensationFailed("undo record missing")
                if record["status"] == "UNDONE":
                    continue
                undo_key = f"UNDO#{key}"
                self.journal.prepare(
                    undo_key,
                    {"undo": record["undo"], "result": result},
                    {"type": "MANUAL_RECONCILIATION"},
                )
                prior = self.journal.claim(undo_key)
                if prior is None:
                    undo_result = self.boundary.undo(step, record["undo"], result)
                    self.journal.complete(undo_key, undo_result)
                self.journal.mark_undone(key)
                self.boundary.audit("ACTION_COMPENSATED", {"key": key})
        except Exception as error:
            self.boundary.audit("COMPENSATION_FAILED", {"partId": execution.part_id})
            raise CompensationFailed(
                "compensation incomplete; manual reconciliation required"
            ) from error

    def rollback(self, execution: Execution) -> None:
        if not self.boundary.authorize_rollback(execution):
            raise PermissionError("named rollback authority required")
        execution = self.expand(execution)
        execution_key = f"EXEC#{execution.case_id}#{execution.version}#{execution.part_id}"
        record = self.journal.read(execution_key)
        if record is None or record["status"] != "SUCCEEDED":
            raise UncertainWrite("execution must finish before rollback")
        rollback_key = f"ROLLBACK#{execution_key}"
        self.journal.prepare(
            rollback_key, {"versionHash": execution.version_hash}, {"type": "MANUAL_RECONCILIATION"}
        )
        if self.journal.claim(rollback_key) is not None:
            return
        completed = []
        for step in execution.steps:
            key = self.key(execution, step)
            record = self.journal.read(key)
            if record is not None and record["status"] == "PENDING":
                raise UncertainWrite("unresolved SAP write blocks rollback")
            if record is not None and record["status"] == "SUCCEEDED":
                completed.append((step, key, dict(record["result"])))
        self._compensate(execution, completed)
        for transfer in execution.transfers:
            self._release(execution, transfer)
        self.boundary.audit("ROLLED_BACK", {"partId": execution.part_id})
        self.journal.complete(rollback_key, {"status": "ROLLED_BACK"})

    @staticmethod
    def expand(execution: Execution) -> Execution:
        steps: list[Step] = []
        for step in execution.steps:
            if step.action.type == "SPLIT_PO_SCHEDULE_LINE":
                steps.extend(
                    replace(step, target=f"{step.target}#part#{index}", substep=index)
                    for index in range(len(step.action.parts))
                )
            else:
                steps.append(step)
        return replace(execution, steps=tuple(steps))
