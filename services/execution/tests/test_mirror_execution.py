from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from services.execution.journal import Journal
from services.execution.ledger import Ledger
from services.execution.sap import LINE, PO, SapBoundary

# Writes go to a Mirror of this module's own, reset before each test; the session Mirror
# the read-only tests share must never see them.
from services.execution.tests.test_service import mirror, sap  # noqa: F401 - fixtures
from services.execution.tests.test_workflow import sample
from services.execution.workflow import Execution, Workflow
from services.shared.models import SplitPoScheduleLine
from services.shared.sap_client import SapClient


@pytest.mark.parametrize("split", [False, True])
def test_AT_06_AT_07_STO_date_change_and_rollback_on_local_mirror(
    sap: SapClient,  # noqa: F811
    dynamodb: Any,
    split: bool,
) -> None:
    writer = SapClient(read=sap.read, write=sap.read)
    header = writer.get(PO, "A_PurchaseOrder", {"PurchaseOrder": "4500001234"})
    keys = {"PurchasingDocument": "4500001234", "PurchasingDocumentItem": "10", "ScheduleLine": "1"}
    original = writer.get(PO, LINE, keys)
    execution = sample()
    change = execution.steps[1].action.model_copy(update={"new_date": date(2027, 1, 2)})
    if split:
        change = SplitPoScheduleLine.model_validate(
            {
                "type": "SPLIT_PO_SCHEDULE_LINE",
                "poNumber": "4500001234",
                "poItem": "10",
                "scheduleLine": "1",
                "parts": [
                    {"qty": 640, "deliveryDate": "2027-01-02"},
                    {"qty": 960, "deliveryDate": "2027-01-05"},
                ],
            }
        )
    execution = replace(
        execution, steps=(execution.steps[0], replace(execution.steps[1], action=change))
    )
    events: list[str] = []

    def revalidate(plan: Execution) -> bool:
        return bool(
            writer.get(PO, LINE, keys).data["ScheduleLineDeliveryDate"]
            == original.data["ScheduleLineDeliveryDate"]
        )

    boundary = SapBoundary(
        writer,
        authorize=lambda plan: plan == execution,
        authorize_rollback=lambda plan: plan == execution,
        kill_switch=lambda: False,
        revalidate=revalidate,
        audit=lambda kind, data: events.append(kind),
        sto_header={
            key: str(header.data[key])
            for key in (
                "CompanyCode",
                "PurchasingOrganization",
                "PurchasingGroup",
                "DocumentCurrency",
            )
        },
    )
    workflow = Workflow(Journal(dynamodb, "test"), Ledger(dynamodb, "test"), boundary)
    result = workflow.run(execution)
    assert len(result) == (3 if split else 2) and result[0]["document"] != "4500001234"
    assert workflow.run(execution) == result
    assert "EXECUTION_REPLAY" in events
    workflow.rollback(execution)
    workflow.rollback(execution)
    assert (
        writer.get(PO, LINE, keys).data["ScheduleLineDeliveryDate"]
        == original.data["ScheduleLineDeliveryDate"]
    )
