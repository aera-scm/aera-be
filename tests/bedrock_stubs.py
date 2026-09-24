"""Stubbed Bedrock, AgentCore, Textract and Comprehend clients for offline tests.

The model ids below are explicit test fixtures in the documented Bedrock format.
They are not approved model ids. A stubbed
response never counts as account or invocation evidence (ADR-003).
"""

from typing import Any

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_bedrock import BedrockClient
from mypy_boto3_bedrock_agentcore_control import BedrockAgentCoreControlClient
from mypy_boto3_bedrock_runtime import BedrockRuntimeClient
from mypy_boto3_comprehend import ComprehendClient
from mypy_boto3_textract import TextractClient

REGION = "us-east-1"
SUPERVISOR_MODEL_ID = "anthropic.claude-sonnet-4-5-20250929-v1:0"
SMALL_MODEL_ID = "amazon.nova-lite-v1:0"

_UNSIGNED = Config(signature_version=UNSIGNED)


def bedrock_client(region: str = REGION) -> BedrockClient:
    return boto3.client("bedrock", region_name=region, config=_UNSIGNED)


def runtime_client(region: str = REGION) -> BedrockRuntimeClient:
    return boto3.client("bedrock-runtime", region_name=region, config=_UNSIGNED)


def agentcore_client(region: str = REGION) -> BedrockAgentCoreControlClient:
    return boto3.client("bedrock-agentcore-control", region_name=region, config=_UNSIGNED)


def textract_client(region: str = REGION) -> TextractClient:
    return boto3.client("textract", region_name=region, config=_UNSIGNED)


def comprehend_client(region: str = REGION) -> ComprehendClient:
    return boto3.client("comprehend", region_name=region, config=_UNSIGNED)


def model_details(model_id: str, **overrides: Any) -> dict[str, Any]:
    details: dict[str, Any] = {
        "modelArn": f"arn:aws:bedrock:{REGION}::foundation-model/{model_id}",
        "modelId": model_id,
        "inferenceTypesSupported": ["ON_DEMAND"],
        "modelLifecycle": {"status": "ACTIVE"},
    }
    details.update(overrides)
    return details


def availability(model_id: str, **overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "modelId": model_id,
        "agreementAvailability": {"status": "AVAILABLE"},
        "authorizationStatus": "AUTHORIZED",
        "entitlementAvailability": "AVAILABLE",
        "regionAvailability": "AVAILABLE",
    }
    response.update(overrides)
    return response


def stub_model(
    stubber: Stubber,
    model_id: str,
    *,
    details: dict[str, Any] | None = None,
    access: dict[str, Any] | None = None,
) -> None:
    stubber.add_response(
        "get_foundation_model",
        {"modelDetails": details or model_details(model_id)},
        {"modelIdentifier": model_id},
    )
    stubber.add_response(
        "get_foundation_model_availability",
        access or availability(model_id),
        {"modelId": model_id},
    )


def converse_response(output_tokens: int = 2) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": "OK"}]}},
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": 12,
            "outputTokens": output_tokens,
            "totalTokens": 12 + output_tokens,
        },
        "metrics": {"latencyMs": 250},
    }
