"""FR-NEG-02 / FR-LNG-02: load question authority from case and SAP master data."""

from __future__ import annotations

from typing import Literal

from services.dialogue.policy import Language, SupplierFacts
from services.shared.cases import CaseStore
from services.shared.models import CaseStatus, SignalChannel, SignalStatus
from services.shared.partner import partner_emails, partner_phones
from services.shared.sap_client import SapClient
from services.shared.sap_values import results
from services.shared.signals import SignalStore

PO_SERVICE = "API_PURCHASEORDER_PROCESS_SRV"
BP_SERVICE = "API_BUSINESS_PARTNER"


def load_facts(
    cases: CaseStore,
    sap: SapClient,
    case_id: str,
    *,
    expected_status: CaseStatus = CaseStatus.INVESTIGATING,
    signals: SignalStore | None = None,
    selected_channel: Literal["EMAIL", "WHATSAPP"] | None = None,
) -> SupplierFacts:
    case = cases.get(case_id)
    if case is None or case.status is not expected_status or not case.po_number:
        raise ValueError(f"supplier question requires an open PO in {expected_status.value} state")
    po = sap.get(
        PO_SERVICE,
        "A_PurchaseOrder",
        {"PurchaseOrder": case.po_number},
        expand="to_PurchaseOrderItem",
    )
    supplier = str(po.data.get("Supplier") or "")
    items = results(po.data.get("to_PurchaseOrderItem"))
    active = [
        item
        for item in items
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
    if selected_channel is not None and selected_channel not in ("EMAIL", "WHATSAPP"):
        raise ValueError("unsupported supplier channel")
    channel = selected_channel or "EMAIL"
    if selected_channel is None and signals is not None:
        eligible = [
            signal
            for signal in signals.for_case(case_id)
            if signal.status is SignalStatus.ACCEPTED
            and signal.sender_verified
            and signal.supplier_id == supplier
            and signal.channel in (SignalChannel.EMAIL, SignalChannel.WHATSAPP)
        ]
        if eligible:
            newest = max(signal.received_at for signal in eligible)
            channels = {signal.channel.value for signal in eligible if signal.received_at == newest}
            if len(channels) != 1:
                raise ValueError("supplier channel is ambiguous")
            channel = "WHATSAPP" if "WHATSAPP" in channels else "EMAIL"
    addresses = (
        partner_phones(sap, supplier) if channel == "WHATSAPP" else partner_emails(sap, supplier)
    )
    if len(addresses) != 1:
        raise ValueError("supplier must have one unambiguous master-data destination")
    return SupplierFacts(
        case_id,
        supplier,
        frozenset({case.po_number}),
        next(iter(addresses)),
        language,
        f"{po.source_ref}/Supplier + {partner.source_ref}/CorrespondenceLanguage",
        channel,
    )
