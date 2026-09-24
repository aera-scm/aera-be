"""FR-NEG-02 / FR-LNG-02: load question authority from case and SAP master data."""

from __future__ import annotations

from services.dialogue.policy import Language, SupplierFacts
from services.shared.cases import CaseStore
from services.shared.models import CaseStatus
from services.shared.partner import partner_emails
from services.shared.sap_client import SapClient
from services.shared.sap_values import results

PO_SERVICE = "API_PURCHASEORDER_PROCESS_SRV"
BP_SERVICE = "API_BUSINESS_PARTNER"


def load_facts(
    cases: CaseStore, sap: SapClient, case_id: str,
    *, expected_status: CaseStatus = CaseStatus.INVESTIGATING,
) -> SupplierFacts:
    case = cases.get(case_id)
    if case is None or case.status is not expected_status or not case.po_number:
        raise ValueError(
            f"supplier question requires an open PO in {expected_status.value} state"
        )
    po = sap.get(
        PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrder": case.po_number},
        expand="to_PurchaseOrderItem",
    )
    supplier = str(po.data.get("Supplier") or "")
    items = results(po.data.get("to_PurchaseOrderItem"))
    active = [
        item for item in items
        if (not case.po_item or str(item.get("PurchaseOrderItem")) == case.po_item)
        and not item.get("PurchasingDocumentDeletionCode")
        and item.get("IsCompletelyDelivered") is not True
    ]
    if not supplier or not active:
        raise ValueError("supplier question requires an open PO item")
    partner = sap.get(BP_SERVICE, "A_BusinessPartner", {"BusinessPartner": supplier})
    try:
        language = Language(str(partner.data["CorrespondenceLanguage"]).upper())
    except (KeyError, ValueError):
        raise ValueError("supplier correspondence language is missing or unsupported") from None
    addresses = partner_emails(sap, supplier)
    if len(addresses) != 1:
        raise ValueError("supplier must have one unambiguous master-data email address")
    return SupplierFacts(
        case_id, supplier, frozenset({case.po_number}), next(iter(addresses)), language,
        f"{po.source_ref}/Supplier + {partner.source_ref}/CorrespondenceLanguage",
    )
