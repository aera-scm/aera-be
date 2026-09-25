"""FR-INT-03: Runtime obtains its API credential through AgentCore Identity."""

from typing import Any

import httpx
import pytest

from services.interop import app


def test_fr_int_03_runtime_uses_workload_token_for_service_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "AWS_REGION": "us-east-1",
        "AERA_INTEROP_WORKLOAD": "aera_dev_a2a_service",
        "AERA_INTEROP_PROVIDER": "aera_dev_interop_service",
        "AERA_INTEROP_SCOPE": "aera-dev-interop/invoke",
        "AERA_INTEROP_API_URL": "https://api.example.invalid/dev/interop",
    }.items():
        monkeypatch.setenv(name, value)

    class Identity:
        def get_workload_access_token(self, name: str) -> dict[str, str]:
            assert name == "aera_dev_a2a_service"
            return {"workloadAccessToken": "synthetic-wat"}

        async def get_token(self, **kwargs: Any) -> str:
            assert kwargs == {
                "provider_name": "aera_dev_interop_service",
                "scopes": ["aera-dev-interop/invoke"],
                "agent_identity_token": "synthetic-wat",
                "auth_flow": "M2M",
            }
            return "synthetic-access-token"

    monkeypatch.setattr(app, "IdentityClient", lambda _: Identity())

    def post(url: str, **kwargs: Any) -> httpx.Response:
        assert url == "https://api.example.invalid/dev/interop"
        assert kwargs["headers"] == {"Authorization": "Bearer synthetic-access-token"}
        assert kwargs["json"] == {
            "externalClientId": "external-agent-1",
            "operation": "list_cases",
            "payload": {},
        }
        return httpx.Response(200, text="[]")

    monkeypatch.setattr(httpx, "post", post)
    assert app.invoke_api("external-agent-1", "list_cases", {}) == {
        "statusCode": 200,
        "body": "[]",
    }
