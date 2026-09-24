from decimal import Decimal as D
from typing import Any

import pytest

from services.execution.journal import Journal, UncertainWrite, idempotency_key
from services.execution.ledger import Ledger, ReservationConflict


def reserve(ledger: Ledger, key: str, quantity: str = "600") -> None:
    ledger.reserve(
        material="MAT-48219",
        plant="1020",
        reservation_id=key,
        case_id=f"EXC-2026-{key}",
        quantity=D(quantity),
        available=D(600),
        source_ref="SAP:API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod('MAT-48219')",
    )


def test_BR_08_AT_09_first_reservation_wins_and_replay_is_free(dynamodb: Any) -> None:
    ledger = Ledger(dynamodb, "test")
    reserve(ledger, "0914")
    reserve(ledger, "0914")
    with pytest.raises(ReservationConflict, match="insufficient"):
        reserve(ledger, "0915")
    with pytest.raises(ReservationConflict, match="identity"):
        reserve(ledger, "0914", "1")
    ledger.release(material="MAT-48219", plant="1020", reservation_id="0914")
    ledger.release(material="MAT-48219", plant="1020", reservation_id="0914")
    reserve(ledger, "0915")
    balance = ledger._read("MAT#MAT-48219#PLANT#1020", "BAL")
    assert balance is not None and balance["allocated"] == 600 and balance["version"] == 3


@pytest.mark.parametrize("quantity", ["0", "-1", "0.5", "NaN", "Infinity"])
def test_BR_08_invalid_quantity(dynamodb: Any, quantity: str) -> None:
    with pytest.raises(ValueError):
        reserve(Ledger(dynamodb, "test"), "0914", quantity)


def test_BR_09_key_stable_and_action_index_separates_parts() -> None:
    key = idempotency_key("EXC-2026-0914", 1, 0, "CREATE_STO", "MAT-48219#1020")
    assert key == idempotency_key("EXC-2026-0914", 1, 0, "CREATE_STO", "MAT-48219#1020")
    assert key != idempotency_key("EXC-2026-0914", 1, 1, "CREATE_STO", "MAT-48219#1020")


def test_BR_09_BR_10_undo_first_and_replay(dynamodb: Any) -> None:
    journal = Journal(dynamodb, "test")
    with pytest.raises(ValueError, match="undo"):
        journal.claim("key")
    journal.prepare("key", {"qty": "600"}, {"type": "DELETE_STO_ITEM"})
    assert journal.claim("key") is None
    with pytest.raises(UncertainWrite):
        journal.claim("key")
    journal.complete("key", {"document": "4500000001"})
    assert journal.claim("key") == {"document": "4500000001"}
    with pytest.raises(ValueError, match="reused"):
        journal.prepare("key", {"qty": "601"}, {"type": "DELETE_STO_ITEM"})
