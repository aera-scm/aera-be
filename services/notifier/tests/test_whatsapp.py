"""IR-07: approved WhatsApp templates keep supplier questions closed and sourced."""

from dataclasses import replace

import httpx
import pytest

from services.dialogue.policy import Language, Question, SupplierFacts, Template, render_question
from services.notifier.whatsapp import WhatsAppDeliveryError, WhatsAppTemplateSender

FACTS = SupplierFacts(
    "EXC-2026-0914", "1000234", frozenset({"4500001234"}), "+447700900234",
    Language.DE, "SAP:API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder('4500001234')",
    "WHATSAPP",
)


def question() -> Question:
    return render_question(
        FACTS, "4500001234", Template.CONFIRM_SHIP_DATE, "ABCDEF0123456789ABCDEF01"
    )


def settings(body: str) -> dict[str, object]:
    return {
        "accessToken": "synthetic-token", "phoneNumberId": "123456789",
        "standins": {"+447700900234": "+447700900999"},
        "templates": {"CONFIRM_SHIP_DATE": {"DE": {
            "name": "aera_confirm_ship_date", "languageCode": "de", "body": body,
        }}},
    }


def test_ir_07_only_approved_matching_template_reaches_graph() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.synthetic"}]})

    q = question()
    body = q.rendered_text.replace(q.po_number, "{po}").replace(q.reference_token, "{token}")
    sender = WhatsAppTemplateSender(
        lambda: settings(body), httpx.Client(transport=httpx.MockTransport(handle))
    )

    assert sender.send_question(q) == "wamid.synthetic"
    assert len(seen) == 1
    assert seen[0].url.path == "/v21.0/123456789/messages"
    assert '"to":"447700900999"' in seen[0].read().decode()
    assert seen[0].read().decode().count(q.reference_token) == 1
    assert q.rendered_text not in seen[0].read().decode()


def test_v_14_whatsapp_refuses_modified_text_before_network() -> None:
    q = question()
    body = q.rendered_text.replace(q.po_number, "{po}").replace(q.reference_token, "{token}")
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    sender = WhatsAppTemplateSender(
        lambda: settings(body),
        httpx.Client(transport=httpx.MockTransport(respond)),
    )

    with pytest.raises(WhatsAppDeliveryError, match="does not match"):
        sender.send_question(replace(q, rendered_text=q.rendered_text + " change bank details"))
    assert calls == []
