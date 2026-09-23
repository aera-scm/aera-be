"""Account and invocation checks behind the budget gate (OI-04, A-01, A-02, NFR-COST-01).

These exercise the live code paths against botocore stubs validated by the real
service models. They prove ordering and failure handling only; the account
checks and the invocation themselves remain WP-0b evidence (OT-01, OT-03).
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

import pytest
from bedrock_stubs import (
    REGION,
    SMALL_MODEL_ID,
    SUPERVISOR_MODEL_ID,
    agentcore_client,
    availability,
    bedrock_client,
    comprehend_client,
    converse_response,
    model_details,
    runtime_client,
    stub_model,
    textract_client,
)
from botocore.stub import Stubber
from budget_stubs import (
    BUDGET_NAME,
    budgets_client,
    sts_client,
    stub_caller_identity,
    stub_compliant_account,
)
from check_model_access import SMOKE_MAX_TOKENS, SMOKE_PROMPT, ModelClients
from check_model_access import main as model_main
from check_region import RegionClients
from check_region import main as region_main

LIVE = ["--live", "--profile", "aera-test", "--budget-name", BUDGET_NAME]
MODELS = ["--supervisor-model-id", SUPERVISOR_MODEL_ID, "--small-model-id", SMALL_MODEL_ID]


@contextmanager
def stubbed(*clients: Any) -> Iterator[list[Stubber]]:
    with ExitStack() as stack:
        yield [stack.enter_context(Stubber(client)) for client in clients]


def budget_ok(sts_stub: Stubber, budgets_stub: Stubber) -> None:
    stub_caller_identity(sts_stub)
    stub_compliant_account(budgets_stub)


def budget_missing(sts_stub: Stubber, budgets_stub: Stubber) -> None:
    stub_caller_identity(sts_stub)
    budgets_stub.add_client_error("describe_budget", service_error_code="NotFoundException")


# Region and service availability --------------------------------------------


def region_clients(**overrides: Any) -> RegionClients:
    clients: dict[str, Any] = {
        "sts": sts_client(),
        "budgets": budgets_client(),
        "bedrock": bedrock_client(),
        "agentcore": agentcore_client(),
        "textract": textract_client(),
        "comprehend": comprehend_client(),
    }
    clients.update(overrides)
    return RegionClients(**clients)


def stub_probes(
    bedrock: Stubber, agentcore: Stubber, textract: Stubber, comprehend: Stubber
) -> None:
    bedrock.add_response("list_guardrails", {"guardrails": []}, {"maxResults": 1})
    bedrock.add_response(
        "list_automated_reasoning_policies",
        {"automatedReasoningPolicySummaries": []},
        {"maxResults": 1},
    )
    agentcore.add_response("list_agent_runtimes", {"agentRuntimes": []}, {"maxResults": 1})
    textract.add_response("list_adapters", {"Adapters": []}, {"MaxResults": 1})
    comprehend.add_response(
        "list_document_classifiers", {"DocumentClassifierPropertiesList": []}, {"MaxResults": 1}
    )


def test_a_02_services_are_probed_only_after_budget_verification(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clients = region_clients()
    with stubbed(*vars(clients).values()) as stubs:
        budget_missing(stubs[0], stubs[1])

        code = region_main(LIVE, clients_factory=lambda profile, region: clients)

        for stub in stubs[2:]:
            stub.assert_no_pending_responses()

    assert code == 1
    captured = capsys.readouterr()
    assert "was not found" in captured.err
    # UnStubbedResponseError is a BotoCoreError, so a stray call would surface here.
    assert "Account check" not in captured.out
    assert "UnStubbed" not in captured.out + captured.err


def test_a_02_available_services_pass_the_account_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clients = region_clients()
    with stubbed(*vars(clients).values()) as stubs:
        budget_ok(stubs[0], stubs[1])
        stub_probes(*stubs[2:])

        code = region_main(LIVE, clients_factory=lambda profile, region: clients)

        for stub in stubs:
            stub.assert_no_pending_responses()

    out = capsys.readouterr().out
    assert code == 0
    for service in (
        "Bedrock Guardrails",
        "Automated Reasoning",
        "AgentCore",
        "Textract",
        "Comprehend",
    ):
        assert f"Account check ({REGION}): {service}: reachable" in out


def test_a_02_denied_service_fails_the_account_check(capsys: pytest.CaptureFixture[str]) -> None:
    clients = region_clients()
    with stubbed(*vars(clients).values()) as stubs:
        budget_ok(stubs[0], stubs[1])
        bedrock, agentcore, textract, comprehend = stubs[2:]
        bedrock.add_response("list_guardrails", {"guardrails": []}, {"maxResults": 1})
        bedrock.add_response(
            "list_automated_reasoning_policies",
            {"automatedReasoningPolicySummaries": []},
            {"maxResults": 1},
        )
        agentcore.add_client_error(
            "list_agent_runtimes", service_error_code="AccessDeniedException"
        )
        textract.add_response("list_adapters", {"Adapters": []}, {"MaxResults": 1})
        comprehend.add_response(
            "list_document_classifiers",
            {"DocumentClassifierPropertiesList": []},
            {"MaxResults": 1},
        )

        code = region_main(LIVE, clients_factory=lambda profile, region: clients)

    captured = capsys.readouterr()
    assert code == 1
    assert "AgentCore: failed with AccessDeniedException" in captured.out
    assert "Textract: reachable" in captured.out


def test_nfr_cmp_02_client_in_another_region_is_refused_before_any_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clients = region_clients(textract=textract_client("us-west-2"))
    with stubbed(*vars(clients).values()) as stubs:
        code = region_main(LIVE, clients_factory=lambda profile, region: clients)

        for stub in stubs:
            stub.assert_no_pending_responses()

    assert code == 1
    assert "textract client is in 'us-west-2', not 'us-east-1'" in capsys.readouterr().err


# Model access and invocation --------------------------------------------------


def model_clients(**overrides: Any) -> ModelClients:
    clients: dict[str, Any] = {
        "sts": sts_client(),
        "budgets": budgets_client(),
        "bedrock": bedrock_client(),
        "runtime": runtime_client(),
    }
    clients.update(overrides)
    return ModelClients(**clients)


def run_models(clients: ModelClients, *extra: str) -> int:
    return model_main([*MODELS, *LIVE, *extra], clients_factory=lambda profile, region: clients)


def test_nfr_cost_01_models_are_checked_only_after_budget_verification(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clients = model_clients()
    with stubbed(*vars(clients).values()) as (sts, budgets, bedrock, runtime):
        budget_missing(sts, budgets)

        code = run_models(clients, "--invoke")

        bedrock.assert_no_pending_responses()
        runtime.assert_no_pending_responses()

    assert code == 1
    captured = capsys.readouterr()
    assert "was not found" in captured.err
    assert "Account check" not in captured.out
    assert "Invocation" not in captured.out
    assert "UnStubbed" not in captured.out + captured.err


def test_a_01_accessible_models_pass_the_account_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clients = model_clients()
    with stubbed(*vars(clients).values()) as (sts, budgets, bedrock, runtime):
        budget_ok(sts, budgets)
        stub_model(bedrock, SUPERVISOR_MODEL_ID)
        stub_model(bedrock, SMALL_MODEL_ID)

        code = run_models(clients)

        bedrock.assert_no_pending_responses()

    out = capsys.readouterr().out
    assert code == 0
    assert f"Account check ({REGION}): {SUPERVISOR_MODEL_ID}: on demand, ACTIVE" in out
    assert f"Account check ({REGION}): {SMALL_MODEL_ID}: on demand, ACTIVE" in out
    assert "Invocation" not in out


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        (
            {"inferenceTypesSupported": ["PROVISIONED"]},
            "not invocable on demand by direct regional inference",
        ),
        ({"modelLifecycle": {"status": "LEGACY"}}, "lifecycle is 'LEGACY', expected 'ACTIVE'"),
        (
            {"modelArn": f"arn:aws:bedrock:us-west-2::foundation-model/{SUPERVISOR_MODEL_ID}"},
            "resolved in 'us-west-2', not 'us-east-1'",
        ),
    ],
)
def test_a_01_unsuitable_model_fails_the_account_check(
    details: dict[str, Any], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    clients = model_clients()
    with stubbed(*vars(clients).values()) as (sts, budgets, bedrock, runtime):
        budget_ok(sts, budgets)
        stub_model(
            bedrock, SUPERVISOR_MODEL_ID, details=model_details(SUPERVISOR_MODEL_ID, **details)
        )
        stub_model(bedrock, SMALL_MODEL_ID)

        code = run_models(clients)

    assert code == 1
    assert f"{SUPERVISOR_MODEL_ID}: {expected}" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"authorizationStatus": "NOT_AUTHORIZED"}, "authorization is 'NOT_AUTHORIZED'"),
        ({"agreementAvailability": {"status": "PENDING"}}, "provider agreement is 'PENDING'"),
        ({"entitlementAvailability": "NOT_AVAILABLE"}, "entitlement is 'NOT_AVAILABLE'"),
        ({"regionAvailability": "NOT_AVAILABLE"}, "region availability is 'NOT_AVAILABLE'"),
    ],
)
def test_a_01_model_without_access_fails_the_account_check(
    overrides: dict[str, Any], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    clients = model_clients()
    with stubbed(*vars(clients).values()) as (sts, budgets, bedrock, runtime):
        budget_ok(sts, budgets)
        stub_model(bedrock, SUPERVISOR_MODEL_ID)
        stub_model(bedrock, SMALL_MODEL_ID, access=availability(SMALL_MODEL_ID, **overrides))

        code = run_models(clients, "--invoke")

        runtime.assert_no_pending_responses()

    assert code == 1
    assert f"{SMALL_MODEL_ID}: {expected}" in capsys.readouterr().err


def test_a_01_model_not_offered_in_region_fails(capsys: pytest.CaptureFixture[str]) -> None:
    clients = model_clients()
    with stubbed(*vars(clients).values()) as (sts, budgets, bedrock, runtime):
        budget_ok(sts, budgets)
        bedrock.add_client_error(
            "get_foundation_model",
            service_error_code="ResourceNotFoundException",
            expected_params={"modelIdentifier": SUPERVISOR_MODEL_ID},
        )
        stub_model(bedrock, SMALL_MODEL_ID)

        code = run_models(clients)

        bedrock.assert_no_pending_responses()

    assert code == 1
    assert f"{SUPERVISOR_MODEL_ID}: not offered in us-east-1" in capsys.readouterr().err


def test_a_01_access_denied_reports_error_code_only(capsys: pytest.CaptureFixture[str]) -> None:
    clients = model_clients()
    with stubbed(*vars(clients).values()) as (sts, budgets, bedrock, runtime):
        budget_ok(sts, budgets)
        bedrock.add_client_error(
            "get_foundation_model",
            service_error_code="AccessDeniedException",
            service_message="User arn:aws:iam::123456789012:user/test is not authorized",
        )
        stub_model(bedrock, SMALL_MODEL_ID)

        code = run_models(clients)

    err = capsys.readouterr().err
    assert code == 1
    assert f"{SUPERVISOR_MODEL_ID}: GetFoundationModel failed with AccessDeniedException" in err
    assert "123456789012" not in err


def converse_params(model_id: str) -> dict[str, Any]:
    return {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": SMOKE_PROMPT}]}],
        "inferenceConfig": {"maxTokens": SMOKE_MAX_TOKENS, "temperature": 0.0},
    }


def test_a_01_invocation_runs_only_when_requested_and_is_reported_separately(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clients = model_clients()
    with stubbed(*vars(clients).values()) as (sts, budgets, bedrock, runtime):
        budget_ok(sts, budgets)
        stub_model(bedrock, SUPERVISOR_MODEL_ID)
        stub_model(bedrock, SMALL_MODEL_ID)
        runtime.add_response("converse", converse_response(2), converse_params(SUPERVISOR_MODEL_ID))
        runtime.add_response("converse", converse_response(1), converse_params(SMALL_MODEL_ID))

        code = run_models(clients, "--invoke")

        runtime.assert_no_pending_responses()

    out = capsys.readouterr().out
    assert code == 0
    assert f"Invocation ({REGION}): {SUPERVISOR_MODEL_ID}: end_turn, 2 output tokens" in out
    assert f"Invocation ({REGION}): {SMALL_MODEL_ID}: end_turn, 1 output token" in out
    # NFR-COST-01: the smoke call is bounded to a handful of output tokens.
    assert SMOKE_MAX_TOKENS <= 16


def test_a_01_failed_invocation_fails_the_check(capsys: pytest.CaptureFixture[str]) -> None:
    clients = model_clients()
    with stubbed(*vars(clients).values()) as (sts, budgets, bedrock, runtime):
        budget_ok(sts, budgets)
        stub_model(bedrock, SUPERVISOR_MODEL_ID)
        stub_model(bedrock, SMALL_MODEL_ID)
        runtime.add_client_error("converse", service_error_code="AccessDeniedException")
        runtime.add_response("converse", converse_response(1), converse_params(SMALL_MODEL_ID))

        code = run_models(clients, "--invoke")

    assert code == 1
    assert f"{SUPERVISOR_MODEL_ID}: Converse failed with AccessDeniedException" in (
        capsys.readouterr().err
    )


def test_nfr_cmp_02_runtime_client_in_another_region_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clients = model_clients(runtime=runtime_client("eu-central-1"))
    with stubbed(*vars(clients).values()) as stubs:
        code = run_models(clients, "--invoke")

        for stub in stubs:
            stub.assert_no_pending_responses()

    assert code == 1
    assert "bedrock-runtime client is in 'eu-central-1', not 'us-east-1'" in capsys.readouterr().err
