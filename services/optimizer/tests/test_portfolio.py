"""FR-OPZ-01: competing cases form deterministic resource components."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from services.optimizer.portfolio import PortfolioCase, detect
from services.shared.models import Case, CaseStatus, Option

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)


def case(number: int, *, material: str = "MAT-A", plant: str = "1010") -> Case:
    return Case(
        case_id=f"EXC-2026-{number:04d}",
        type="MRP_EXCEPTION",
        material=material,
        plant=plant,
        status=CaseStatus.TRIAGED,
        created_at=NOW,
        updated_at=NOW,
    )


def option(action: dict[str, object], *, hours: int = 5) -> Option:
    return Option.model_validate(
        {
            "id": "A",
            "name": "Candidate",
            "actions": [action],
            "coverageUnits": 100,
            "arrival": NOW + timedelta(hours=hours),
            "costUsd": 1000,
            "costSourceRef": "ratecard:R1",
            "rationale": "Candidate from SAP and rate card",
        }
    )


def sto(donor: str, material: str = "MAT-A", recipient: str = "1010") -> Option:
    return option(
        {
            "type": "CREATE_STO",
            "fromPlant": donor,
            "toPlant": recipient,
            "material": material,
            "qty": Decimal(100),
            "deliveryDate": (NOW + timedelta(hours=5)).date(),
        }
    )


def test_fr_opz_01_two_cases_share_donor_across_recipient_plants() -> None:
    first = PortfolioCase(case(1), (sto("1020"),))
    second = PortfolioCase(case(2, plant="1030"), (sto("1020", recipient="1030"),))

    [portfolio] = detect([second, first], NOW)

    assert portfolio.case_ids == ("EXC-2026-0001", "EXC-2026-0002")
    assert portfolio.shared_resources == ("DONOR#MAT-A#1020",)


def test_fr_opz_01_same_plant_stock_and_transitive_overlap() -> None:
    entries = [
        PortfolioCase(case(1), (sto("1020"),)),
        PortfolioCase(case(2), ()),
        PortfolioCase(case(3, plant="1030"), (sto("1020", recipient="1030"),)),
    ]

    [portfolio] = detect(entries, NOW)

    assert portfolio.case_ids == tuple(f"EXC-2026-{n:04d}" for n in (1, 2, 3))
    assert set(portfolio.shared_resources) == {"STOCK#MAT-A#1010", "DONOR#MAT-A#1020"}


def test_fr_opz_01_freight_and_supplier_partial_compete() -> None:
    air = option(
        {
            "type": "BOOK_AIR_FREIGHT",
            "supplierId": "1000234",
            "poNumber": "4500001234",
            "poItem": "10",
            "qty": 100,
            "arrival": NOW + timedelta(hours=5),
        }
    )
    alternate = option(
        {
            "type": "CREATE_PO_ALTERNATE",
            "supplierId": "1000234",
            "material": "MAT-A",
            "plant": "1030",
            "qty": 100,
            "deliveryDate": (NOW + timedelta(hours=5)).date(),
        }
    )
    first = PortfolioCase(case(1), (air,))
    second = PortfolioCase(case(2, plant="1030"), (alternate,))

    [portfolio] = detect([first, second], NOW)

    assert "PARTIAL#1000234#MAT-A" in portfolio.shared_resources


def test_fr_opz_01_closed_and_out_of_horizon_cases_do_not_compete() -> None:
    closed = case(2).model_copy(update={"status": CaseStatus.CLOSED})
    late = sto("1020", recipient="1030").model_copy(update={"arrival": NOW + timedelta(hours=721)})
    entries = [
        PortfolioCase(case(1), (sto("1020"),)),
        PortfolioCase(closed, (sto("1020"),)),
        PortfolioCase(case(3, plant="1030"), (late,)),
    ]

    assert detect(entries, NOW) == []
