"""Keep operator environment variables out of offline tests."""

import pytest

OPERATOR_VARIABLES = (
    "AERA_ENV",
    "AERA_AWS_PROFILE",
    "AERA_REGION",
    "AERA_BUDGET_NAME",
    "AERA_BUDGET_LIMIT_USD",
    "AERA_BUDGET_RECIPIENTS",
    "MODEL_SUPERVISOR_ID",
    "MODEL_SMALL_ID",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)


@pytest.fixture(autouse=True)
def isolated_operator_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in OPERATOR_VARIABLES:
        monkeypatch.delenv(name, raising=False)
