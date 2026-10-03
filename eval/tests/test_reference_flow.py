"""The reference scenario end to end, offline, across every component boundary (SRD 6.25).

Signals -> gatekeeper -> extraction -> case -> tools -> propose_plan -> Verifier and routing
-> outbox relay -> `PlanApproved` -> execution steps (6.8) against the Mirror -> notifier.
Each hop consumes exactly what the previous one published, as EventBridge would deliver it.
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from mirror_process import AVAILABLE, MISSING, running_mirror
from runner import ENV, T0, CaseResult, Run, _plan, aws_fakes, load_cases, mirror_admin

pytestmark = pytest.mark.skipif(not AVAILABLE, reason=MISSING)

PO = "API_PURCHASEORDER_PROCESS_SRV"
STANDINS = {
    "orders@krieger-guss.example": "team+supplier@aera-demo.example",
    "customer.service@meridian-motors.example": "team+cs@aera-demo.example",
    "production.planning@meridian-motors.example": "team+pp@aera-demo.example",
    "approver@meridian-motors.example": "team+approver@aera-demo.example",
}


class Ses:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_email(self, **request: Any) -> dict[str, Any]:
        self.sent.append(request)
        return {"MessageId": f"m{len(self.sent)}"}


class Scheduler:
    class exceptions:  # noqa: N801 - mirrors the boto3 client shape
        ConflictException = type("ConflictException", (Exception,), {})

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_schedule(self, **request: Any) -> None:
        self.created.append(request)


@pytest.fixture(scope="module")
def mirror() -> Iterator[str]:
    with running_mirror(T0) as url:
        yield url


def test_reference_scenario_from_signals_to_sap_writes_and_messages(mirror: str) -> None:
    from services.execution.service import ExecutionService, dispatch
    from services.notifier.handler import Notifier
    from services.outbox.handler import OutboxRelay
    from services.shared.models import CaseStatus
    from services.shared.sap_client import Endpoint, SapClient, Target

    [case] = [c for c in load_cases("ci") if c.id == "lpc-01"]
    t0 = datetime.fromisoformat(T0.replace("Z", "+00:00"))
    mirror_admin(mirror, "/admin/reset", {"t0": T0})
    with aws_fakes():
        run = Run(case, mirror, t0)
        run.open_cases()
        for item in case.signals:
            run.deliver(item)
        assert case.plan is not None
        checks = CaseResult(case)
        _plan(run, case.plan, checks)
        assert checks.passed, [c for c in checks.checks if not c.ok]

        # Routing's outbox, as the DynamoDB stream hands it to the relay.
        outbox = run.dynamodb.query(
            TableName=f"aera-{ENV}-cases",
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": {"S": f"CASE#{case.case_id}"},
                ":sk": {"S": "OUTBOX#"},
            },
        )["Items"]
        OutboxRelay(run.dynamodb, run.bus, ENV).relay(
            {"Records": [{"eventName": "INSERT", "dynamodb": {"NewImage": i}} for i in outbox]}
        )
        [approved] = run.bus.details("PlanApproved")
        assert approved["data"]["optionIds"] == ["C"]  # BR-22: the STO part runs at once

        # The state machine's input (control stack rule) and its steps (SRD 6.8).
        endpoint = Endpoint(base_url=mirror, target=Target.MIRROR, auth=None)
        service = ExecutionService(
            dynamodb=run.dynamodb,
            sap=SapClient(read=endpoint, write=endpoint, sleep=lambda _: None),
            bus=run.bus,
            scheduler=Scheduler(),
            clock=lambda: t0,
            env=ENV,
            scheduler_role_arn="arn:aws:iam::000000000000:role/scheduler",
            bus_arn="arn:aws:events:us-east-1:000000000000:event-bus/aera-eval",
        )
        event = {"caseId": approved["data"]["caseId"], "planPartId": approved["data"]["planPartId"]}
        assert dispatch(service, {**event, "step": "CheckKillSwitch"})["killed"] is False
        result = dispatch(service, {**event, "step": "Execute"})
        assert result["outcome"] == "COMPLETED", result
        dispatch(service, {**event, "step": "Notify", "documents": result["documents"]})
        dispatch(
            service,
            {
                **event,
                "step": "ScheduleGoodsReceiptCheck",
                "expectedArrival": result["expectedArrival"],
            },
        )
        dispatch(service, {**event, "step": "MarkMonitoring", "documents": result["documents"]})
        [sto] = [d for d in result["documents"] if str(d).startswith("45")]
        header = run.sap.get(PO, "A_PurchaseOrder", {"PurchaseOrder": str(sto)})
        assert (header.data["PurchaseOrderType"], header.data["SupplyingPlant"]) == ("UB", "1020")
        record = run.store.get(case.case_id)
        assert record is not None and record.status is CaseStatus.AWAITING_APPROVAL  # A waits

        # Every requested message goes to a master-data address through its stand-in.
        ses = Ses()
        notifier = Notifier(
            dynamodb=run.dynamodb,
            sap=run.sap,
            ses=ses,
            standins=lambda: STANDINS,
            sender=lambda: "aera@aera-demo.example",
            clock=lambda: t0,
            env=ENV,
        )
        outcomes = [
            outcome
            for request in run.bus.details("NotificationRequested")
            for outcome in notifier.handle(request["data"])
        ]
        assert [o["status"] for o in outcomes] == ["SENT"] * 4, outcomes
        assert sorted(m["Destination"]["ToAddresses"][0] for m in ses.sent) == sorted(
            STANDINS.values()
        )
