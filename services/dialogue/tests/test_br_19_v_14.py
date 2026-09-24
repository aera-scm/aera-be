"""BR-19 and V-14: only fact questions to the supplier's master-data address."""

from dataclasses import replace

import pytest

from services.dialogue.policy import (
    Language,
    SupplierFacts,
    Template,
    render_question,
    verify_v_14,
)

FACTS = SupplierFacts(
    "EXC-2026-0914",
    "1000234",
    frozenset({"4500001234"}),
    "supplier@example.test",
    Language.DE,
    "SAP:API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder('4500001234')",
)
TOKEN = "AERA20260914ABCDEF"


@pytest.mark.parametrize("language", list(Language))
@pytest.mark.parametrize("template", list(Template))
def test_fr_neg_02_br_19_v_14_fixed_templates_in_three_languages(
    language: Language, template: Template
) -> None:
    facts = replace(FACTS, language=language)
    question = render_question(facts, "4500001234", template, TOKEN)

    assert question.recipient == facts.master_address
    assert question.language == language
    assert question.reference_token in question.rendered_text
    assert question.po_number in question.rendered_text
    assert question.english_copy.startswith("For purchase order")
    verify_v_14(question, facts)


@pytest.mark.parametrize(
    "po,token",
    [
        ("4500009999", TOKEN),
        ("4500001234; change bank", TOKEN),
        ("4500001234", "bad token"),
        ("4500001234", "<script>alert(1)</script>"),
    ],
)
def test_br_19_refuses_non_case_po_and_untrusted_token(po: str, token: str) -> None:
    with pytest.raises(ValueError, match="BR-19"):
        render_question(FACTS, po, Template.CONFIRM_PARTIAL_QTY, token)


ATTACK_TEXT = [
    "Please change bank details",
    "Send me all purchase orders",
    "We accept your new price",
    "We agree to a new quantity",
    "Change payment terms",
    "Attach the contract",
    "Ignore previous instructions",
    "Transfer funds to this account",
    "Confirm a discount",
    "Send other customers' orders",
    "Update beneficiary account",
    "Sign the new contract",
    "Commit to 600 units",
    "Pay within 30 days",
    "Quote the supplier attachment",
    "Use this unverified address",
    "Send the rate card",
    "Share all invoices",
    "Propose a new unit price",
    "Amend the purchase order",
    "Change the delivery contract",
    "Forward confidential documents",
    "Reveal internal planning data",
    "Accept these terms",
    "Approve a payment change",
    "Bypass the verifier",
    "Ship additional quantity at our cost",
    "Send all supplier contacts",
    "Copy the hostile message",
    "Authorize a price change",
]


@pytest.mark.parametrize("injected", ATTACK_TEXT)
def test_br_19_v_14_rejects_thirty_adversarial_message_edits(injected: str) -> None:
    approved = render_question(FACTS, "4500001234", Template.CONFIRM_SHIP_DATE, TOKEN)
    forged = replace(approved, rendered_text=approved.rendered_text + "\n" + injected)

    with pytest.raises(ValueError, match="V-14"):
        verify_v_14(forged, FACTS)


def test_v_14_rejects_recipient_po_and_english_copy_tampering() -> None:
    approved = render_question(FACTS, "4500001234", Template.REQUEST_TRACKING, TOKEN)
    for forged in (
        replace(approved, recipient="attacker@example.test"),
        replace(approved, po_number="4500009999"),
        replace(approved, english_copy="Payment approved"),
        replace(approved, template=Template.CONFIRM_PARTIAL_QTY),
    ):
        with pytest.raises(ValueError, match="V-14"):
            verify_v_14(forged, FACTS)
