"""FR-LRN-01/03: analytics persists SAP-derived samples and source references."""

from datetime import UTC, datetime
from typing import Any

from services.dialogue.reliability_job import ReliabilityJob, read_profile
from services.shared.sap_client import SapClient

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)


def test_fr_lrn_01_refreshes_profiles_from_mirror_only(dynamodb: Any, sap: SapClient) -> None:
    count = ReliabilityJob(sap, dynamodb, "test").refresh(NOW)

    assert count >= 1
    profile = read_profile(dynamodb, "1000234", "MAT-48219", "test")
    assert profile is not None
    assert profile["sampleSize"] == 12
    assert profile["p90DelayDays"] == 4
    assert profile["onTimeRate"] == 0.25
    assert len(profile["sourceRefs"]) == 27
    assert all(ref.startswith("SAP:") for ref in profile["sourceRefs"])


def test_fr_lrn_03_missing_profile_does_not_invent_statistics(dynamodb: Any) -> None:
    assert read_profile(dynamodb, "unknown", "MAT-A", "test") is None
