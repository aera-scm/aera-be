"""FR-LRN-01/03: sample, delay and partial rates use only sourced SAP history."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from services.dialogue.reliability import Receipt, Schedule, compute

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)
DUE = date(2026, 9, 20)


def line(n: int, days_late: int, *, parts: int = 1) -> Schedule:
    receipt_qty = Decimal(100) / parts
    return Schedule(
        "1000234",
        "MAT-A",
        DUE,
        Decimal(100),
        tuple(
            Receipt(DUE + timedelta(days=days_late), receipt_qty, f"SAP:RECEIPT/{n}-{p}")
            for p in range(parts)
        ),
        f"SAP:SCHEDULE/{n}",
    )


def test_fr_lrn_01_computes_rates_and_nearest_rank_p90() -> None:
    history = [line(i, i - 2, parts=2 if i == 9 else 1) for i in range(10)]

    profile = compute("1000234", "MAT-A", history, NOW)

    assert profile.sample_size == 10
    assert profile.on_time_rate == Decimal("0.3")
    assert profile.mean_delay_days == Decimal("2.8")
    assert profile.p90_delay_days == 6
    assert profile.partial_rate == Decimal("0.1")
    assert len(profile.source_refs) == 21


def test_fr_lrn_01_incomplete_receipt_is_censored_and_partial() -> None:
    incomplete = Schedule(
        "1000234",
        "MAT-A",
        DUE,
        Decimal(100),
        (Receipt(DUE, Decimal(50), "SAP:RECEIPT/1"),),
        "SAP:SCHEDULE/1",
    )

    profile = compute("1000234", "MAT-A", [incomplete], NOW)

    assert profile.on_time_rate == 0
    assert profile.partial_rate == 1
    assert profile.p90_delay_days == 15


def test_fr_lrn_03_excludes_other_supplier_material_and_old_history() -> None:
    old = Schedule("1000234", "MAT-A", date(2026, 1, 1), Decimal(100), (), "SAP:OLD")
    other = Schedule("other", "MAT-A", DUE, Decimal(100), (), "SAP:OTHER")
    profile = compute("1000234", "MAT-A", [old, other, line(1, 0)], NOW)

    assert profile.sample_size == 1
    assert profile.source_refs == ("SAP:RECEIPT/1-0", "SAP:SCHEDULE/1")


def test_fr_lrn_03_refuses_unsourced_history() -> None:
    bad = Schedule("1000234", "MAT-A", DUE, Decimal(100), (), "signal:email")
    with pytest.raises(ValueError, match="sourced positive SAP"):
        compute("1000234", "MAT-A", [bad], NOW)
