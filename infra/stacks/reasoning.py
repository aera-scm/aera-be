"""Reasoning stack (SRD 6.16, 6.18): tool Lambdas, AgentCore Gateway, AgentCore Runtime and
run-starter (FR-IMP-01, NFR-SEC-01, NFR-REL-04).

- Every tool of the catalogue is its own Lambda with its own role and is a Gateway target;
  the Gateway authenticates the runtime with IAM (SigV4).
- The supervisor runs in a Python 3.12 ARM64 container on AgentCore Runtime.
- NFR-SEC-01: neither the runtime nor any tool can start the execution state machine, send
  email or messages, or read the SAP write endpoint; none has AERA_SAP_WRITES.
"""

from typing import Any

from aws_cdk import ArnFormat, CfnOutput, Duration, IgnoreMode, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.constructs.service_function import ROOT, ServiceFunction
from infra.environments import APPROVED_REGION, require_deployable_environment
from infra.stacks.data import DataStack
from services.tools.registry import TOOLS, ToolSpec

MIRROR_SECRET = "sap/mirror-oauth-client"  # pragma: allowlist secret (a name, not a value)
READ_PARAMETERS = ("SAP_READ_BASE",)
AGENT_PARAMETERS = (
    "SAP_READ_BASE",
    "MODEL_SUPERVISOR_ID",
    "MODEL_SMALL_ID",
    "GUARDRAIL_ID",
    "GUARDRAIL_VERSION",
)
# What each tool may touch besides SAP reads.
CASE_WRITERS = {"ask_planner", "request_supplier_info", "propose_plan", "escalate"}
CALCULATORS = {"calc_impact", "calc_option"}


def _schema(spec: ToolSpec) -> agentcore.CfnGatewayTarget.SchemaDefinitionProperty:
    properties = {
        name: agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
            type=str(definition["type"]), description=definition.get("description")
        )
        for name, definition in spec.properties.items()
    }
    return agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
        type="object", properties=properties, required=list(spec.required)
    )


class ReasoningStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        data: DataStack,
        guardrail_arn: str,
        code: lambda_.Code,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        require_deployable_environment(env_name)
        tables = data.tables

        # Tools --------------------------------------------------------------------------
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        gateway = agentcore.CfnGateway(
            self,
            "Gateway",
            name=f"aera-{env_name}-tools",
            description="AERA read-only and proposal-only agent tools (SRD 6.3.2)",
            protocol_type="MCP",
            authorizer_type="AWS_IAM",
            role_arn=gateway_role.role_arn,
        )
        self.tool_functions: dict[str, lambda_.Function] = {}
        for spec in TOOLS:
            service = ServiceFunction(
                self,
                f"tool-{spec.name}",
                env_name=env_name,
                component=f"tool-{spec.name.replace('_', '-')}",
                module="tools",
                code=code,
                environment={"AERA_TOOL_NAME": spec.name},
                secrets=(MIRROR_SECRET,),
                parameters=READ_PARAMETERS,
                timeout=Duration.seconds(30),
            )
            fn = service.function
            data.key.grant_decrypt(fn)
            tables["cases"].grant_read_data(fn)
            tables["signals"].grant_read_data(fn)
            tables["config"].grant_read_data(fn)
            if spec.name in CALCULATORS | CASE_WRITERS:
                tables["cases"].grant_read_write_data(fn)
            if spec.name in CASE_WRITERS:
                tables["audit"].grant_read_write_data(fn)
                data.bus.grant_put_events_to(fn)
            if spec.name == "request_supplier_info":
                tables["dialogue"].grant_read_write_data(fn)
            fn.grant_invoke(gateway_role)
            agentcore.CfnGatewayTarget(
                self,
                f"Target-{spec.name}",
                gateway_identifier=gateway.attr_gateway_identifier,
                name=spec.name.replace("_", "-"),
                description=spec.description[:200],
                target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                    mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                        lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                            lambda_arn=fn.function_arn,
                            tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                                inline_payload=[
                                    agentcore.CfnGatewayTarget.ToolDefinitionProperty(
                                        name=spec.name,
                                        description=spec.description,
                                        input_schema=_schema(spec),
                                    )
                                ]
                            ),
                        )
                    )
                ),
                credential_provider_configurations=[
                    agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                        credential_provider_type="GATEWAY_IAM_ROLE"
                    )
                ],
            )
            self.tool_functions[spec.name] = fn

        # Runtime ------------------------------------------------------------------------
        runtime_role = iam.Role(
            self,
            "RuntimeRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:{self.partition}:bedrock:{self.region}::foundation-model/anthropic.claude-*",
                    f"arn:{self.partition}:bedrock:{self.region}::foundation-model/amazon.nova-*",
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"],
                conditions={"StringNotEquals": {"aws:RequestedRegion": APPROVED_REGION}},
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(actions=["bedrock:ApplyGuardrail"], resources=[guardrail_arn])
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeGateway"], resources=[gateway.attr_gateway_arn]
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    self.format_arn(
                        service="ssm", resource="parameter", resource_name=f"aera/{env_name}/{p}"
                    )
                    for p in AGENT_PARAMETERS
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    self.format_arn(
                        service="secretsmanager",
                        resource="secret",
                        resource_name=f"/aera/{env_name}/{MIRROR_SECRET}-*",
                        arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    )
                ],
            )
        )
        runtime_role.add_to_policy(
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
        for name in ("cases", "trace", "audit"):
            tables[name].grant_read_write_data(runtime_role)
        for name in ("signals", "config"):
            tables[name].grant_read_data(runtime_role)
        data.bus.grant_put_events_to(runtime_role)
        data.key.grant_encrypt_decrypt(runtime_role)
        image = ecr_assets.DockerImageAsset(
            self,
            "AgentImage",
            directory=str(ROOT),
            file="services/agent/Dockerfile",
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
        image.repository.grant_pull(runtime_role)
        runtime = agentcore.CfnRuntime(
            self,
            "Supervisor",
            agent_runtime_name=f"aera_{env_name}_supervisor",
            description="AERA supervisor agent (SRD 6.3, 6.18)",
            role_arn=runtime_role.role_arn,
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=image.image_uri,
                )
            ),
            lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                idle_runtime_session_timeout=900,
                max_lifetime=3600,
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC"
            ),
            environment_variables={
                "AERA_ENV": env_name,
                "AERA_GATEWAY_URL": gateway.attr_gateway_url,
                "POWERTOOLS_SERVICE_NAME": "aera-agent",
            },
        )
        runtime.node.add_dependency(runtime_role)

        # Run-starter --------------------------------------------------------------------
        starter = ServiceFunction(
            self,
            "run-starter",
            env_name=env_name,
            component="run-starter",
            code=code,
            environment={"AERA_AGENT_RUNTIME_ARN": runtime.attr_agent_runtime_arn},
            parameters=(),
        ).function
        starter.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[runtime.attr_agent_runtime_arn, f"{runtime.attr_agent_runtime_arn}/*"],
            )
        )
        tables["cases"].grant_read_write_data(starter)
        tables["audit"].grant_read_write_data(starter)
        data.bus.grant_put_events_to(starter)
        data.key.grant_encrypt_decrypt(starter)
        dead_letters = sqs.Queue(
            self,
            "DeadLetters",
            queue_name=f"aera-{env_name}-reasoning-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        events.Rule(
            self,
            "OnCaseReadyForRun",
            rule_name=f"aera-{env_name}-run-starter",
            event_bus=data.bus,
            event_pattern=events.EventPattern(
                source=events.Match.prefix("aera."), detail_type=["CaseReadyForRun"]
            ),
            targets=[
                targets.LambdaFunction(starter, retry_attempts=4, dead_letter_queue=dead_letters)
            ],
        )

        ssm.StringParameter(
            self,
            "ParamAgentRuntimeArn",
            parameter_name=f"/aera/{env_name}/AGENT_RUNTIME_ARN",
            string_value=runtime.attr_agent_runtime_arn,
            description="AgentCore Runtime of the supervisor",
        )
        CfnOutput(self, "GatewayUrl", value=gateway.attr_gateway_url)
        CfnOutput(self, "AgentRuntimeArn", value=runtime.attr_agent_runtime_arn)
        self.runtime_role = runtime_role
        self.run_starter = starter
