"""FR-LAB-01/02: selected parameters change both Mirror state and inbound evidence."""

from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image
from pydantic import ValidationError
from pypdf import PdfReader

from services.lab.scenario import Parameters, generate, mirror_changes

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)


def params(**changes: object) -> Parameters:
    return Parameters.model_validate(
        {
            "exceptionType": "SUPPLIER_DELAY",
            "material": "MAT-48219",
            "plant": "1010",
            "daysLate": 3,
            "quantityShort": 400,
            "channel": "EMAIL",
            "language": "EN",
            "hostile": False,
            **changes,
        }
    )


def test_fr_lab_01_bounded_material_plant_and_severity() -> None:
    for change in (
        {"plant": "9999"},
        {"material": "MAT-FAKE"},
        {"daysLate": 0},
        {"quantityShort": 1601},
    ):
        with pytest.raises(ValidationError):
            params(**change)


def test_fr_lab_02_mutation_tracks_selected_delay_and_shortage() -> None:
    changes = mirror_changes(params(), NOW)
    assert changes[0]["where"] == {
        "PurchasingDocument": "4500001234",
        "PurchasingDocumentItem": "10",
        "ScheduleLine": "1",
    }
    assert changes[0]["set"] == {"ScheduleLineDeliveryDate": "2026-10-08"}
    assert not any(change["entity"] == "A_MatlStkInAcctMod" for change in changes)
    shortage = mirror_changes(params(exceptionType="QUANTITY_SHORTFALL"), NOW)
    assert shortage[0]["set"] == {"ScheduleLineCommittedQuantity": "1200"}
    assert not any(change["entity"] == "A_MatlStkInAcctMod" for change in shortage)
    assert not any(change["entity"] == "MRPExceptionMessage" for change in shortage)
    carrier = mirror_changes(params(exceptionType="CARRIER_DELAY"), NOW)
    assert carrier[0]["set"] == {"ScheduleLineDeliveryDate": "2026-10-08"}
    assert all(
        change["entity"]
        in {"A_PurchaseOrderScheduleLine", "MRPExceptionMessage", "A_MatlStkInAcctMod"}
        for change in changes
    )


def test_fr_lab_02_artifacts_include_pdf_noisy_photo_carrier_and_hostile() -> None:
    chosen = params(language="ID", hostile=True)
    artifacts = generate(chosen, NOW, "01KTEST123456789ABCDEFGHJK")
    pdf_text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(artifacts.pdf)).pages)
    image = Image.open(BytesIO(artifacts.photo))
    assert "Pemasok terlambat" in pdf_text
    assert "4500001234" in pdf_text
    assert image.format == "PNG" and image.size == (1000, 500)
    assert len(set(image.get_flattened_data())) > 100
    assert b'"synthetic": true' in artifacts.carrier_event
    assert (
        artifacts.hostile_email and b"Ignore all previous instructions" in artifacts.hostile_email
    )
    assert b"confirmation-4500001234.pdf" in artifacts.email


def test_fr_lab_02_exception_artifacts_describe_distinct_disruptions() -> None:
    shortage = generate(params(exceptionType="QUANTITY_SHORTFALL"), NOW, "shortage")
    carrier = generate(params(exceptionType="CARRIER_DELAY"), NOW, "carrier")
    assert b"Quantity shortage" in shortage.email
    assert b"Carrier delay" in carrier.email
    assert "400 units short" in shortage.text
    assert "1600 units now arrive" in carrier.text
