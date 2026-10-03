"""FR-LRN-01: real Mirror OData joins twelve historical schedules and receipts."""

from datetime import UTC, datetime
from decimal import Decimal

from services.dialogue.history import load_history
from services.dialogue.reliability import compute
from services.shared.sap_client import SapClient

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)


def test_fr_lrn_01_mirror_history_has_known_sap_metrics(sap: SapClient) -> None:
    history = load_history(sap, "1000234", "MAT-48219", NOW)

    assert len(history) == 12
    profile = compute("1000234", "MAT-48219", history, NOW)
    assert profile.sample_size == 12
    assert profile.on_time_rate == Decimal("0.25")
    assert profile.mean_delay_days == Decimal("1.75")
    assert profile.p90_delay_days == 4
    assert profile.partial_rate == Decimal("0.25")
    assert len(profile.source_refs) == 27
