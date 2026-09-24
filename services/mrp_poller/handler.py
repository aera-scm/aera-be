"""MRP poller: read the MRP exception feed, keep what is outside tolerance (FR-ING-01,
FR-ING-02, UC-02, OI-07).

Runs every 15 minutes (EventBridge Scheduler) and on demand. The feed is the Mirror's custom
`ZAERA_MIRROR_SRV/MRPExceptionMessage` entity (OI-07: the sandbox APIs do not expose MRP
messages). Messages inside the working-day tolerance are counted, not forwarded; the rest
go to the case service in `MrpExceptionsPolled` events of at most 50 messages.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from services.rules.fr_ing_02 import IN_DAYS, OUT_DAYS, mrp_actionable
from services.shared.runtime import emit
from services.shared.sap_client import SapClient
from services.shared.sap_values import edm_date

COMPONENT = "mrp-poller"
SERVICE = "ZAERA_MIRROR_SRV"
BATCH = 50


@dataclass
class MrpPoller:
    sap: SapClient
    bus: Any
    tolerance: Callable[[], tuple[int, int]] = lambda: (IN_DAYS, OUT_DAYS)
    env: str | None = None

    def poll(self) -> dict[str, int]:
        rows = self.sap.query(SERVICE, "MRPExceptionMessage", top=5000)
        in_days, out_days = self.tolerance()
        actionable: list[dict[str, Any]] = []
        for row in rows:
            element = edm_date(row.data.get("MRPElementDate"))
            proposed = edm_date(row.data.get("MRPReschedulingDate"))
            if element is None:
                continue
            if mrp_actionable(element, proposed, in_days=in_days, out_days=out_days):
                actionable.append(
                    {
                        "id": str(row.data["MRPExceptionMessageID"]),
                        "material": str(row.data["Material"]),
                        "plant": str(row.data["Plant"]),
                        "element": str(row.data.get("MRPElement") or ""),
                        "elementItem": str(row.data.get("MRPElementItem") or ""),
                        "number": str(row.data.get("MRPExceptionNumber") or ""),
                        "text": str(row.data.get("MRPExceptionText") or ""),
                        "elementDate": element.isoformat(),
                        "reschedulingDate": proposed.isoformat() if proposed else None,
                        "sourceRef": row.source_ref,
                    }
                )
        counts = {
            "total": len(rows),
            "actionableCount": len(actionable),
            "suppressedCount": len(rows) - len(actionable),
        }
        for start in range(0, max(len(actionable), 1), BATCH):
            emit(
                self.bus,
                "MrpExceptionsPolled",
                {**counts, "messages": actionable[start : start + BATCH]},
                component=COMPONENT,
                environment=self.env,
            )
        return counts


_poller: MrpPoller | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    global _poller
    if _poller is None:
        from services.shared import runtime
        from services.shared.config import Config

        config = Config(runtime.client("dynamodb"))
        _poller = MrpPoller(
            sap=runtime.sap_client(),
            bus=runtime.client("events"),
            tolerance=lambda: (
                int(config.decimal("MRP_TOLERANCE_IN_DAYS")),
                int(config.decimal("MRP_TOLERANCE_OUT_DAYS")),
            ),
        )
    return _poller.poll()
