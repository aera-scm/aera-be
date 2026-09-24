"""External A2A and read-only MCP runtimes (FR-INT, BR-21, IR-10)."""

from typing import Any

from aws_cdk import CfnOutput, IgnoreMode, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from infra.constructs.service_function import ROOT
from infra.environments import require_deployable_environment


class InteropStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        api_function: lambda_.IFunction,
        discovery_url: str,
        client_id: str,
        oauth_scope: str,
        agent_card_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        require_deployable_environment(env_name)
        if agent_card_url and not agent_card_url.startswith("https://"):
            raise ValueError("AERA_AGENT_CARD_URL must be HTTPS")
        image = ecr_assets.DockerImageAsset(
            self,
            "InteropImage",
            directory=str(ROOT),
            file="services/interop/Dockerfile",
            platform=ecr_assets.Platform.LINUX_ARM64,
            ignore_mode=IgnoreMode.DOCKER,
            exclude=[
                "**",
                "!pyproject.toml",
                "!uv.lock",
                "!services/",
                "!services/**",
                "**/tests/",
                "**/__pycache__/",
                "**/*.pyc",
                "**/.env",
                "**/.env.*",
                "**/*.pem",
                "**/*.key",
                "**/secrets/",
            ],
        )
        for protocol in ("A2A", "MCP"):
            role = iam.Role(
                self,
                f"{protocol}Role",
                assumed_by=iam.ServicePrincipal(
                    "bedrock-agentcore.amazonaws.com",
                    conditions={"StringEquals": {"aws:SourceAccount": self.account}},
                ),
            )
            image.repository.grant_pull(role)
            api_function.grant_invoke(role)
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                        "logs:DescribeLogStreams",
                        "xray:PutTraceSegments",
                        "xray:PutTelemetryRecords",
                        "cloudwatch:PutMetricData",
                    ],
                    resources=["*"],
                )
            )
            runtime = agentcore.CfnRuntime(
                self,
                protocol,
                agent_runtime_name=f"aera_{env_name}_{protocol.lower()}",
                description=f"AERA external {protocol} interface (FR-INT)",
                role_arn=role.role_arn,
                agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                    container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                        container_uri=image.image_uri
                    )
                ),
                authorizer_configuration=agentcore.CfnRuntime.AuthorizerConfigurationProperty(
                    custom_jwt_authorizer=agentcore.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                        discovery_url=discovery_url,
                        allowed_clients=[client_id],
                        allowed_scopes=[oauth_scope],
                    )
                ),
                request_header_configuration=agentcore.CfnRuntime.RequestHeaderConfigurationProperty(
                    request_header_allowlist=["Authorization"]
                ),
                protocol_configuration=protocol,
                network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                    network_mode="PUBLIC"
                ),
                environment_variables={
                    "AERA_ENV": env_name,
                    "AERA_INTEROP_PROTOCOL": protocol,
                    "AERA_INTEROP_CLIENT_ID": client_id,
                    "AERA_INTEROP_API_FUNCTION": api_function.function_name,
                    **({"AERA_AGENT_CARD_URL": agent_card_url} if agent_card_url else {}),
                },
            )
            runtime.node.add_dependency(role)
            CfnOutput(self, f"{protocol}RuntimeArn", value=runtime.attr_agent_runtime_arn)
