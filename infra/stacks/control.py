"""Control stack (SRD 6.16): the governed execution workflow and the routing outbox relay
(FR-EXE-01..08, BR-08..10, SRD 6.8, 6.17).

- `aera-{env}-execution` state machine (Step Functions Standard) runs every SAP write; its
  task Lambda is the only function with AERA_SAP_WRITES and the SAP write endpoint
  (FR-EXE-01).
- `PlanApproved` on the bus starts it; routing publishes that event through the outbox relay.
- Goods-receipt checks are one-shot EventBridge Scheduler schedules that put
  `GoodsReceiptDue` on the bus (FR-MON-01).
"""

import json
from typing import Any

from aws_cdk import Duration, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from infra.constructs.execution_workflow import definition
from infra.constructs.service_function import ServiceFunction
from infra.environments import require_deployable_environment
from infra.stacks.data import DataStack

MIRROR_SECRET = "sap/mirror-oauth-client"  # pragma: allowlist secret (a name, not a value)


def state_machine_name(env_name: str) -> str:
    return f"aera-{env_name}-execution"


class ControlStack(Stack):
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
        tables = data.tables

        scheduler_role = iam.Role(
            self,
            "GoodsReceiptSchedulerRole",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        data.bus.grant_put_events_to(scheduler_role)

        execution = ServiceFunction(
            self,
            "execution",
            env_name=env_name,
            component="execution",
            code=code,
            environment={
                "AERA_SAP_WRITES": "1",
                "AERA_BUS_ARN": data.bus.event_bus_arn,
                "AERA_SCHEDULER_ROLE_ARN": scheduler_role.role_arn,
            },
            secrets=(MIRROR_SECRET,),
            parameters=("SAP_READ_BASE", "SAP_WRITE_BASE"),
            timeout=Duration.minutes(5),
            memory_mb=1024,
        ).function
        for name in ("cases", "audit", "idempotency", "ledger", "signals"):
            tables[name].grant_read_write_data(execution)
        tables["config"].grant_read_data(execution)
        data.bus.grant_put_events_to(execution)
        data.key.grant_encrypt_decrypt(execution)
        execution.add_to_role_policy(
            iam.PolicyStatement(
                actions=["scheduler:CreateSchedule"],
                resources=[
                    self.format_arn(
                        service="scheduler",
                        resource="schedule",
                        resource_name=f"default/aera-{env_name}-gr-*",
                    )
                ],
            )
        )
        scheduler_role.grant_pass_role(execution.grant_principal)

        machine_role = iam.Role(
            self, "WorkflowRole", assumed_by=iam.ServicePrincipal("states.amazonaws.com")
        )
        execution.grant_invoke(machine_role)
        log_group = logs.LogGroup(
            self,
            "WorkflowLogs",
            log_group_name=f"/aws/vendedlogs/states/aera-{env_name}-execution",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        machine = sfn.StateMachine(
            self,
            "Workflow",
            state_machine_name=state_machine_name(env_name),
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_string(
                json.dumps(definition(execution.function_arn))
            ),
            role=machine_role,
            tracing_enabled=True,
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ERROR),
        )

        dead_letters = sqs.Queue(
            self,
            "DeadLetters",
            queue_name=f"aera-{env_name}-control-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        events.Rule(
            self,
            "OnPlanApproved",
            rule_name=f"aera-{env_name}-execution",
            event_bus=data.bus,
            event_pattern=events.EventPattern(
                source=events.Match.prefix("aera."), detail_type=["PlanApproved"]
            ),
            targets=[
                targets.SfnStateMachine(
                    machine,
                    input=events.RuleTargetInput.from_object(
                        {
                            "mode": "execute",
                            "caseId": events.EventField.from_path("$.detail.data.caseId"),
                            "planPartId": events.EventField.from_path("$.detail.data.planPartId"),
                        }
                    ),
                    retry_attempts=4,
                    dead_letter_queue=dead_letters,
                )
            ],
        )

        relay = ServiceFunction(
            self, "outbox", env_name=env_name, component="outbox", code=code
        ).function
        tables["cases"].grant_read_write_data(relay)
        data.bus.grant_put_events_to(relay)
        data.key.grant_encrypt_decrypt(relay)
        relay.add_event_source(
            sources.DynamoEventSource(
                tables["cases"],
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=25,
                retry_attempts=5,
                filters=[
                    lambda_.FilterCriteria.filter(
                        {
                            "eventName": lambda_.FilterRule.is_equal("INSERT"),
                            "dynamodb": {
                                "Keys": {"SK": {"S": lambda_.FilterRule.begins_with("OUTBOX#")}}
                            },
                        }
                    )
                ],
            )
        )
        # Notifier: the only function allowed to send (FR-COM-02, BR-03).
        notifier = ServiceFunction(
            self,
            "notifier",
            env_name=env_name,
            component="notifier",
            code=code,
            secrets=(MIRROR_SECRET, "channels/email-standins"),
            parameters=("SAP_READ_BASE",),
        ).function
        for name in ("cases", "audit"):
            tables[name].grant_read_write_data(notifier)
        tables["config"].grant_read_data(notifier)
        data.key.grant_encrypt_decrypt(notifier)
        notifier.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail"],
                resources=[self.format_arn(service="ses", resource="identity", resource_name="*")],
            )
        )
        monitor = ServiceFunction(
            self,
            "monitor",
            env_name=env_name,
            component="monitor",
            code=code,
            secrets=(MIRROR_SECRET,),
            parameters=("SAP_READ_BASE",),
        ).function
        for name in ("cases", "audit", "signals"):
            tables[name].grant_read_write_data(monitor)
        tables["idempotency"].grant_read_data(monitor)
        data.bus.grant_put_events_to(monitor)
        data.key.grant_encrypt_decrypt(monitor)
        for name, fn, detail_type in (
            ("Notifier", notifier, "NotificationRequested"),
            ("Monitor", monitor, "GoodsReceiptDue"),
        ):
            events.Rule(
                self,
                f"On{name}",
                rule_name=f"aera-{env_name}-{name.lower()}",
                event_bus=data.bus,
                event_pattern=events.EventPattern(
                    source=events.Match.prefix("aera."), detail_type=[detail_type]
                ),
                targets=[
                    targets.LambdaFunction(fn, retry_attempts=8, dead_letter_queue=dead_letters)
                ],
            )
        self.state_machine = machine
        self.execution_function = execution
