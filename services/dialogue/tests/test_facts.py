"""FR-NEG-02: recipient, open PO and language are read from SAP, not agent text."""

from datetime import UTC, datetime
from typing import Any

import pytest

from services.dialogue.facts import load_facts
from services.dialogue.policy import Language
from services.shared.cases import CaseStore
from services.shared.models import Case, CaseStatus
from services.shared.sap_client import SapClient

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)


def test_fr_neg_02_loads_master_recipient_and_german_language(
    dynamodb: Any, sap: SapClient
) -> None:
    cases = CaseStore(dynamodb, "test")
    cases.create(
        Case(
            case_id="EXC-2026-0914",
            type="SUPPLIER_DELAY",
            material="MAT-48219",
            plant="1010",
            po_number="4500001234",
            po_item="10",
            status=CaseStatus.INVESTIGATING,
            created_at=NOW,
            updated_at=NOW,
        ),
        actor="system",
    )

    facts = load_facts(cases, sap, "EXC-2026-0914")

    assert facts.supplier_id == "1000234"
    assert facts.language is Language.DE
    assert facts.master_address == "orders@krieger-guss.example"
    assert facts.open_po_numbers == frozenset({"4500001234"})
    assert "SAP:" in facts.source_ref


def test_fr_neg_02_refuses_non_investigating_case(dynamodb: Any, sap: SapClient) -> None:
    cases = CaseStore(dynamodb, "test")
    cases.create(
        Case(
            case_id="EXC-2026-0915",
            type="SUPPLIER_DELAY",
            material="MAT-48219",
            plant="1010",
            po_number="4500001234",
            status=CaseStatus.RECEIVED,
            created_at=NOW,
            updated_at=NOW,
        ),
        actor="system",
    )
    with pytest.raises(ValueError, match="INVESTIGATING state"):
        load_facts(cases, sap, "EXC-2026-0915")
