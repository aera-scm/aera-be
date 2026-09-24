"""Contract: the shared SAP client against the running SAP Mirror (IR-02, IR-03, SRD 8.1).

Starts the real Mirror (Node, SQLite in memory, reference scenario at a fixed T0) and
drives it through the same client every AERA component uses. Skipped with a reason when
the Mirror's dependencies are not installed.
"""

import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from services.shared.sap_client import (
    Endpoint,
    SapClient,
    SapPreconditionRequiredError,
    SapStaleEtagError,
    Target,
)

MIRROR = Path(__file__).resolve().parents[1] / "sap-mirror"
SERVE = MIRROR / "node_modules" / "@sap" / "cds" / "bin" / "serve.js"
NODE = shutil.which("node")
PO = "API_PURCHASEORDER_PROCESS_SRV"

pytestmark = pytest.mark.skipif(
    NODE is None or not SERVE.exists(), reason="Mirror dependencies not installed (make setup)"
)


@pytest.fixture(scope="module")
def mirror() -> Iterator[str]:
    assert NODE is not None
    process = subprocess.Popen(
        [NODE, str(SERVE)],
        cwd=MIRROR,
        env={
            **os.environ,
            "PORT": "0",
            "NODE_ENV": "development",
            "SCENARIO_T0": "2026-10-05T08:00:00Z",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    url = None
    for line in process.stdout:
        match = re.search(r"server listening on \{ url: '([^']+)'", line)
        if match:
            url = match.group(1)
            break
    if url is None:
        process.kill()
        pytest.fail("Mirror did not start")
    # Keep draining the log so the server never blocks on a full pipe.
    threading.Thread(target=lambda: [None for _ in process.stdout or []], daemon=True).start()
    try:
        yield url
    finally:
        process.kill()
        process.wait()


@pytest.fixture
def sap(mirror: str) -> SapClient:
    endpoint = Endpoint(base_url=mirror, target=Target.MIRROR, auth=None)
    return SapClient(read=endpoint, write=endpoint)


def test_ir_02_reference_po_is_read_with_its_source_reference(sap: SapClient) -> None:
    record = sap.get(
        PO, "A_PurchaseOrder", {"PurchaseOrder": "4500001234"}, expand="to_PurchaseOrderItem"
    )

    item = record.data["to_PurchaseOrderItem"]["results"][0]
    assert record.source_ref == f"SAP:{PO}/A_PurchaseOrder('4500001234')"
    assert Decimal(item["OrderQuantity"]) == 1600
    assert record.target is Target.MIRROR


def test_ir_02_stock_query_rows_reference_their_entity(sap: SapClient) -> None:
    rows = sap.query(
        "API_MATERIAL_STOCK_SRV",
        "A_MatlStkInAcctMod",
        filter="Material eq 'MAT-48219' and Plant eq '1010'",
    )

    assert sum(Decimal(r.data["MatlWrhsStkQtyInMatlBaseUnit"]) for r in rows) == 310
    assert rows[0].source_ref.startswith("SAP:API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod(")
    assert "Material='MAT-48219'" in rows[0].source_ref


def test_ir_02_client_creates_an_sto_through_csrf_and_deep_insert(sap: SapClient) -> None:
    created = sap.create(
        PO,
        "A_PurchaseOrder",
        {
            "PurchaseOrderType": "UB",
            "CompanyCode": "1010",
            "SupplyingPlant": "1020",
            "to_PurchaseOrderItem": [
                {
                    "PurchaseOrderItem": "10",
                    "Material": "MAT-48219",
                    "Plant": "1010",
                    "OrderQuantity": "600",
                    "PurchaseOrderQuantityUnit": "PC",
                }
            ],
        },
    )

    assert re.fullmatch(r"45\d{8}", created.data["PurchaseOrder"])
    assert created.source_ref.startswith(f"SAP:{PO}/A_PurchaseOrder(")


def test_ir_02_client_patches_with_if_match_and_detects_a_stale_etag(
    sap: SapClient, mirror: str
) -> None:
    keys = {"PurchasingDocument": "4500001234", "PurchasingDocumentItem": "10", "ScheduleLine": "1"}
    line = sap.get(PO, "A_PurchaseOrderScheduleLine", keys)
    assert line.etag

    sap.update(
        PO,
        "A_PurchaseOrderScheduleLine",
        keys,
        {"ScheduleLineDeliveryDate": "/Date(1791936000000)/"},
        etag=line.etag,
    )
    with pytest.raises(SapStaleEtagError):
        sap.update(
            PO,
            "A_PurchaseOrderScheduleLine",
            keys,
            {"ScheduleLineDeliveryDate": "/Date(1791936000000)/"},
            etag=line.etag,
        )
    with pytest.raises(SapPreconditionRequiredError):
        sap.update(PO, "A_PurchaseOrderScheduleLine", keys, {}, etag="")

    with httpx.Client(base_url=mirror) as http:
        token = http.get(f"/sap/opu/odata/sap/{PO}/", headers={"x-csrf-token": "Fetch"}).headers[
            "x-csrf-token"
        ]
        assert (
            http.post("/admin/reset", json={}, headers={"x-csrf-token": token}).status_code == 200
        )
