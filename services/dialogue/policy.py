"""BR-19 / V-14: closed-template supplier questions from trusted case and SAP facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Template(StrEnum):
    CONFIRM_PARTIAL_QTY = "CONFIRM_PARTIAL_QTY"
    CONFIRM_SHIP_DATE = "CONFIRM_SHIP_DATE"
    REQUEST_TRACKING = "REQUEST_TRACKING"


class Language(StrEnum):
    EN = "EN"
    ID = "ID"
    DE = "DE"


_TEXT: dict[Template, dict[Language, str]] = {
    Template.CONFIRM_PARTIAL_QTY: {
        Language.EN: "For purchase order {po}, what quantity is available for shipment?",
        Language.ID: "Untuk pesanan pembelian {po}, berapa jumlah yang tersedia untuk dikirim?",
        Language.DE: "Welche Menge ist fuer Bestellung {po} zum Versand verfuegbar?",
    },
    Template.CONFIRM_SHIP_DATE: {
        Language.EN: "For purchase order {po}, what is the expected shipment date?",
        Language.ID: "Untuk pesanan pembelian {po}, kapan tanggal pengiriman yang diperkirakan?",
        Language.DE: "Wann ist der voraussichtliche Versandtermin fuer Bestellung {po}?",
    },
    Template.REQUEST_TRACKING: {
        Language.EN: "For purchase order {po}, what is the shipment tracking number?",
        Language.ID: "Untuk pesanan pembelian {po}, berapa nomor pelacakan pengiriman?",
        Language.DE: "Wie lautet die Sendungsverfolgungsnummer fuer Bestellung {po}?",
    },
}
_REFERENCE = {
    Language.EN: "Reference: {token}. Please include this reference in your reply.",
    Language.ID: "Referensi: {token}. Mohon sertakan referensi ini dalam balasan Anda.",
    Language.DE: "Referenz: {token}. Bitte geben Sie diese Referenz in Ihrer Antwort an.",
}


@dataclass(frozen=True)
class SupplierFacts:
    case_id: str
    supplier_id: str
    open_po_numbers: frozenset[str]
    master_address: str
    language: Language
    source_ref: str


@dataclass(frozen=True)
class Question:
    case_id: str
    supplier_id: str
    po_number: str
    template: Template
    language: Language
    recipient: str
    reference_token: str
    rendered_text: str
    english_copy: str
    source_ref: str


def _valid_po(value: str) -> bool:
    return value.isascii() and value.isdigit() and len(value) == 10


def _valid_token(value: str) -> bool:
    return value.isascii() and value.isalnum() and 12 <= len(value) <= 64


def _render(template: Template, language: Language, po: str, token: str) -> str:
    return (
        _TEXT[template][language].format(po=po)
        + "\n"
        + _REFERENCE[language].format(token=token)
    )


def render_question(
    facts: SupplierFacts, po_number: str, template: Template, reference_token: str
) -> Question:
    if (
        not facts.case_id
        or not facts.supplier_id
        or not facts.source_ref
        or not facts.master_address
        or facts.master_address != facts.master_address.strip()
        or not _valid_po(po_number)
        or po_number not in facts.open_po_numbers
        or not _valid_token(reference_token)
    ):
        raise ValueError("BR-19: question must use trusted case, open PO and master address")
    if "@" not in facts.master_address or any(c.isspace() for c in facts.master_address):
        raise ValueError("BR-19: invalid master address")
    question = Question(
        facts.case_id,
        facts.supplier_id,
        po_number,
        Template(template),
        Language(facts.language),
        facts.master_address,
        reference_token,
        _render(Template(template), Language(facts.language), po_number, reference_token),
        _render(Template(template), Language.EN, po_number, reference_token),
        facts.source_ref,
    )
    verify_v_14(question, facts)
    return question


def verify_v_14(question: Question, facts: SupplierFacts) -> None:
    """Re-render from trusted fields so free text cannot enter an outbound message."""
    if (
        question.case_id != facts.case_id
        or question.supplier_id != facts.supplier_id
        or question.po_number not in facts.open_po_numbers
        or not _valid_po(question.po_number)
        or question.recipient != facts.master_address
        or question.language != facts.language
        or question.source_ref != facts.source_ref
        or not _valid_token(question.reference_token)
        or question.rendered_text
        != _render(question.template, facts.language, question.po_number, question.reference_token)
        or question.english_copy
        != _render(question.template, Language.EN, question.po_number, question.reference_token)
    ):
        raise ValueError("V-14: outbound supplier question violates BR-19")
