"""Gate stack (SRD 6.16): gatekeeper, extraction, case-service, mrp-poller and the input
Guardrail (FR-ING-01..08, BR-02, BR-04, BR-15, IR-04, IR-05).

Components talk only through the `aera-{env}` bus (SRD 6.17). Failed deliveries after
retries land in a dead-letter queue instead of vanishing. The case service runs one at a
time (reserved concurrency 1) so two events can never open the same case twice (ADR-0013).
"""

from typing import Any

from aws_cdk import Duration, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.constructs.service_function import ServiceFunction
from infra.environments import require_deployable_environment
from infra.stacks.data import DataStack

MIRROR_SECRET = "sap/mirror-oauth-client"  # pragma: allowlist secret (a name, not a value)


class GateStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        data: DataStack,
        code: lambda_.Code,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        require_deployable_environment(env_name)

        guardrail = bedrock.CfnGuardrail(
            self,
            "InputGuardrail",
            name=f"aera-{env_name}-input",
            description="Prompt-attack filter for inbound signals (FR-ING-05)",
            blocked_input_messaging="Blocked by the AERA input guardrail.",
            blocked_outputs_messaging="Blocked by the AERA guardrail.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE"
                    )
                ]
            ),
            kms_key_arn=data.key.key_arn,
        )
        version = bedrock.CfnGuardrailVersion(
            self,
            "InputGuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description="Version used by gatekeeper and extraction",
        )
        for key, value in (
            ("GUARDRAIL_ID", guardrail.attr_guardrail_id),
            ("GUARDRAIL_VERSION", version.attr_version),
        ):
            ssm.StringParameter(
                self,
                f"Param{key}",
                parameter_name=f"/aera/{env_name}/{key}",
                string_value=value,
                description=f"SRD 6.23 {key}",
            )
        apply_guardrail = iam.PolicyStatement(
            actions=["bedrock:ApplyGuardrail"], resources=[guardrail.attr_guardrail_arn]
        )

        raw_bucket = data.buckets["raw"].bucket_name
        tables = data.tables

        def function(component: str, **options: Any) -> lambda_.Function:
            service = ServiceFunction(
                self,
                component,
                env_name=env_name,
                component=component,
                code=code,
                environment={"AERA_RAW_BUCKET": raw_bucket},
                secrets=(MIRROR_SECRET,),
                **options,
            )
            data.key.grant_encrypt_decrypt(service.function)
            data.bus.grant_put_events_to(service.function)
            return service.function

        gatekeeper = function("gatekeeper")
        extraction = function("extraction", timeout=Duration.seconds(60), memory_mb=1024)
        case_service = function(
            "case-service", timeout=Duration.seconds(60), reserved_concurrency=1
        )
        mrp_poller = function("mrp-poller", timeout=Duration.seconds(60))

        for fn in (gatekeeper, extraction, case_service):
            tables["signals"].grant_read_write_data(fn)
            # Audit events and their chain head (cases table) are written in one transaction.
            tables["audit"].grant_read_write_data(fn)
            tables["cases"].grant_read_write_data(fn)
        for fn in (gatekeeper, extraction):
            data.buckets["raw"].grant_read(fn)
            fn.add_to_role_policy(apply_guardrail)
        extraction.add_to_role_policy(
            iam.PolicyStatement(
                actions=["textract:AnalyzeDocument", "comprehend:DetectDominantLanguage"],
                resources=["*"],
            )
        )
        for fn in (extraction, mrp_poller):
            tables["config"].grant_read_data(fn)

        dead_letters = sqs.Queue(
            self,
            "DeadLetters",
            queue_name=f"aera-{env_name}-gate-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        for name, fn, detail_types in (
            ("Gatekeeper", gatekeeper, ["SignalReceived"]),
            ("Extraction", extraction, ["SignalAccepted"]),
            ("CaseService", case_service, ["SignalExtracted", "MrpExceptionsPolled"]),
        ):
            events.Rule(
                self,
                f"On{name}",
                rule_name=f"aera-{env_name}-{name.lower()}",
                event_bus=data.bus,
                event_pattern=events.EventPattern(
                    source=events.Match.prefix("aera."), detail_type=detail_types
                ),
                targets=[
                    targets.LambdaFunction(fn, retry_attempts=8, dead_letter_queue=dead_letters)
                ],
            )

        # FR-ING-01: every 15 minutes, via EventBridge Scheduler.
        invoker = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        mrp_poller.grant_invoke(invoker)
        scheduler.CfnSchedule(
            self,
            "MrpSchedule",
            name=f"aera-{env_name}-mrp-poller",
            schedule_expression="rate(15 minutes)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=mrp_poller.function_arn, role_arn=invoker.role_arn, input="{}"
            ),
        )
        self.functions = {
            "gatekeeper": gatekeeper,
            "extraction": extraction,
            "case-service": case_service,
            "mrp-poller": mrp_poller,
        }
