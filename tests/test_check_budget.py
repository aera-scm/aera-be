"""Budget verification against the AWS Budgets API shapes (NFR-COST-01, C-02)."""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from botocore.stub import Stubber
from budget_stubs import (
    ACCOUNT_ID,
    BUDGET_NAME,
    EMAIL_SUBSCRIBER,
    actual_alert,
    budgets_client,
    compliant_budget,
    sts_client,
    stub_budget,
    stub_caller_identity,
    stub_compliant_account,
    stub_notifications,
    stub_subscribers,
)
from check_budget import (
    REQUIRED_THRESHOLDS_PERCENT,
    BudgetCheckError,
    BudgetReport,
    main,
    verify_budget,
)

from infra.stacks.budget import ALERT_THRESHOLDS_PERCENT


def verify(
    setup: Callable[[Stubber], None], expected_limit_usd: Decimal | None = None
) -> BudgetReport:
    client = budgets_client()
    with Stubber(client) as stubber:
        setup(stubber)
        report = verify_budget(
            client,
            account_id=ACCOUNT_ID,
            budget_name=BUDGET_NAME,
            expected_limit_usd=expected_limit_usd,
        )
        stubber.assert_no_pending_responses()
    return report


def failures(setup: Callable[[Stubber], None], expected_limit_usd: Decimal | None = None) -> str:
    with pytest.raises(BudgetCheckError) as error:
        verify(setup, expected_limit_usd)
    return str(error.value)


def with_alerts(alerts: list[dict[str, Any]], subscribed: list[dict[str, Any]]) -> Any:
    def setup(stubber: Stubber) -> None:
        stub_budget(stubber, compliant_budget())
        stub_notifications(stubber, alerts)
        for alert in subscribed:
            stub_subscribers(stubber, alert, [EMAIL_SUBSCRIBER])

    return setup


def test_nfr_cost_01_compliant_budget_is_verified() -> None:
    report = verify(stub_compliant_account, expected_limit_usd=Decimal("100"))

    assert report == BudgetReport(
        name=BUDGET_NAME,
        limit_usd=Decimal("100.0"),
        subscribers_per_threshold={50: 1, 80: 1, 100: 1},
    )


def test_nfr_cost_01_gate_checks_the_thresholds_the_stack_creates() -> None:
    assert REQUIRED_THRESHOLDS_PERCENT == ALERT_THRESHOLDS_PERCENT == (50, 80, 100)


def test_nfr_cost_01_additional_alerts_do_not_break_verification() -> None:
    alerts = [actual_alert(t) for t in (50.0, 80.0, 100.0)]
    forecast = actual_alert(100.0, NotificationType="FORECASTED")

    report = verify(with_alerts([forecast, *alerts], alerts))

    assert report.subscribers_per_threshold == {50: 1, 80: 1, 100: 1}


def test_nfr_cost_01_missing_alert_threshold_fails() -> None:
    alerts = [actual_alert(50.0), actual_alert(100.0)]

    message = failures(with_alerts(alerts, alerts))

    assert "no actual-spend alert at 80%" in message


def test_nfr_cost_01_forecasted_alert_does_not_count_as_actual_spend() -> None:
    alerts = [actual_alert(50.0), actual_alert(80.0, NotificationType="FORECASTED")]
    alerts.append(actual_alert(100.0))

    message = failures(with_alerts(alerts, [alerts[0], alerts[2]]))

    assert "no actual-spend alert at 80%" in message


def test_nfr_cost_01_absolute_amount_alert_does_not_count_as_percentage() -> None:
    alerts = [actual_alert(50.0), actual_alert(80.0, ThresholdType="ABSOLUTE_VALUE")]
    alerts.append(actual_alert(100.0))

    message = failures(with_alerts(alerts, [alerts[0], alerts[2]]))

    assert "no actual-spend alert at 80%" in message


def test_nfr_cost_01_alert_without_subscribers_fails() -> None:
    alerts = [actual_alert(t) for t in (50.0, 80.0, 100.0)]

    def setup(stubber: Stubber) -> None:
        stub_budget(stubber, compliant_budget())
        stub_notifications(stubber, alerts)
        stub_subscribers(stubber, alerts[0], [EMAIL_SUBSCRIBER])
        stub_subscribers(stubber, alerts[1], [])
        stub_subscribers(stubber, alerts[2], [EMAIL_SUBSCRIBER])

    assert "alert at 80% has no subscribers" in failures(setup)


def test_nfr_cost_01_budget_without_any_alert_reports_every_threshold() -> None:
    message = failures(with_alerts([], []))

    for threshold in (50, 80, 100):
        assert f"no actual-spend alert at {threshold}%" in message


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"TimeUnit": "QUARTERLY"}, "period is 'QUARTERLY', expected 'MONTHLY'"),
        ({"BudgetType": "USAGE"}, "type is 'USAGE', expected 'COST'"),
        ({"BudgetLimit": {"Amount": "100.0", "Unit": "EUR"}}, "unit is 'EUR', expected 'USD'"),
        ({"BudgetLimit": {"Amount": "0", "Unit": "USD"}}, "amount must be positive"),
    ],
)
def test_nfr_cost_01_budget_shape_mismatch_fails(overrides: dict[str, Any], expected: str) -> None:
    alerts = [actual_alert(t) for t in (50.0, 80.0, 100.0)]

    def setup(stubber: Stubber) -> None:
        stub_budget(stubber, compliant_budget(**overrides))
        stub_notifications(stubber, alerts)
        for alert in alerts:
            stub_subscribers(stubber, alert, [EMAIL_SUBSCRIBER])

    assert expected in failures(setup)


def test_nfr_cost_01_budget_without_fixed_limit_fails() -> None:
    budget = compliant_budget()
    del budget["BudgetLimit"]
    alerts = [actual_alert(t) for t in (50.0, 80.0, 100.0)]

    def setup(stubber: Stubber) -> None:
        stub_budget(stubber, budget)
        stub_notifications(stubber, alerts)
        for alert in alerts:
            stub_subscribers(stubber, alert, [EMAIL_SUBSCRIBER])

    assert "no fixed spend limit" in failures(setup)


def test_nfr_cost_01_amount_differing_from_approved_amount_fails() -> None:
    message = failures(stub_compliant_account, expected_limit_usd=Decimal("200"))

    assert "limit is USD 100.0, expected USD 200" in message


def test_nfr_cost_01_missing_budget_fails() -> None:
    def setup(stubber: Stubber) -> None:
        stubber.add_client_error(
            "describe_budget",
            service_error_code="NotFoundException",
            service_message=f"Unable to get budget: {BUDGET_NAME} - the budget doesn't exist.",
        )

    assert f"budget '{BUDGET_NAME}' was not found" in failures(setup)


def test_nfr_cost_01_access_denied_fails_without_leaking_account() -> None:
    def setup(stubber: Stubber) -> None:
        stubber.add_client_error(
            "describe_budget",
            service_error_code="AccessDeniedException",
            service_message=f"User arn:aws:iam::{ACCOUNT_ID}:user/test is not authorized",
            http_status_code=400,
        )

    message = failures(setup)

    assert "DescribeBudget failed with AccessDeniedException" in message
    assert ACCOUNT_ID not in message


def test_nfr_cost_01_notification_api_failure_fails() -> None:
    def setup(stubber: Stubber) -> None:
        stub_budget(stubber, compliant_budget())
        stubber.add_client_error(
            "describe_notifications_for_budget", service_error_code="ThrottlingException"
        )

    message = failures(setup)

    assert "DescribeNotificationsForBudget failed with ThrottlingException" in message


def test_nfr_cost_01_report_counts_subscribers_without_addresses() -> None:
    summary = verify(stub_compliant_account).summary()

    assert "monthly USD 100.0" in summary
    assert "50% (1 subscriber)" in summary
    assert "100% (1 subscriber)" in summary
    assert "alerts@example.com" not in summary
    assert ACCOUNT_ID not in summary


def test_check_budget_cli_exits_nonzero_on_failed_verification(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sts, budgets = sts_client(), budgets_client()
    with Stubber(sts) as sts_stub, Stubber(budgets) as budgets_stub:
        stub_caller_identity(sts_stub)
        budgets_stub.add_client_error("describe_budget", service_error_code="NotFoundException")

        code = main(
            ["--profile", "aera-test", "--budget-name", BUDGET_NAME],
            clients_factory=lambda profile, region: (sts, budgets),
        )

    assert code == 1
    assert "was not found" in capsys.readouterr().err


def test_check_budget_cli_requires_budget_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AERA_BUDGET_NAME", raising=False)

    def no_aws(profile: str, region: str) -> Any:
        raise AssertionError("AWS must not be contacted without a budget name")

    with pytest.raises(SystemExit) as error:
        main(["--profile", "aera-test"], clients_factory=no_aws)

    assert error.value.code == 2
