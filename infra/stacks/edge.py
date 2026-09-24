"""Edge stack (SRD 6.16): console REST API, channel webhooks, realtime WebSocket and SES
inbound (SRD 6.10, 6.17, IR-06, IR-07, IR-08, IR-11, NFR-SEC-04).

- REST API: console routes behind the Cognito authorizer; webhook routes are public and
  authenticated by signature in the handler.
- WebSocket: tickets from `POST /realtime/ticket`; pushes from the bus and the trace stream.
- SES: a receipt rule set that stores mail under `ses/` in the raw bucket; the bucket's
  EventBridge notification starts ses-inbound. Activating the rule set and verifying the
  receiving domain are owner steps (HANDOFF).
"""

from typing import Any

from aws_cdk import ArnFormat, CfnOutput, Duration, Stack
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_ses as ses
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.constructs.service_function import ServiceFunction
from infra.environments import require_deployable_environment
from infra.stacks.data import DataStack

REALTIME_EVENTS = [
    "CaseOpened",
    "CaseUpdated",
    "CaseClosed",
    "CaseReopened",
    "SignalQuarantined",
    "RunStarted",
    "RunEnded",
    "PlanProposed",
    "PlanVerified",
    "PlanRouted",
    "PlanApproved",
    "PlanRejected",
    "ExecutionStarted",
    "ExecutionCompleted",
    "ExecutionFailed",
    "PortfolioSolved",
]


class EdgeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        data: DataStack,
        pool_arn: str,
        code: lambda_.Code,
        console_origins: list[str] | None = None,
        inbound_recipients: list[str] | None = None,
        whatsapp_media: str = "graph",
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        require_deployable_environment(env_name)
        raw = data.buckets["raw"]
        tables = data.tables

        def function(component: str, **options: Any) -> lambda_.Function:
            service = ServiceFunction(
                self,
                component,
                env_name=env_name,
                component=component,
                code=code,
                **options,
            )
            data.key.grant_encrypt_decrypt(service.function)
            data.bus.grant_put_events_to(service.function)
            return service.function

        environment = {"AERA_RAW_BUCKET": raw.bucket_name}
        # Named, not referenced: the edge stack must not depend on the control stack (6.16).
        execution_arn = self.format_arn(
            service="states",
            resource="stateMachine",
            resource_name=f"aera-{env_name}-execution",
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )
        # The Mirror client serves the FR-ADM-03 reset.
        api_fn = function(
            "api",
            environment={**environment, "AERA_EXECUTION_STATE_MACHINE_ARN": execution_arn},
            secrets=("sap/mirror-oauth-client",),
        )
        api_fn.add_to_role_policy(
            iam.PolicyStatement(actions=["states:StartExecution"], resources=[execution_arn])
        )
        # FR-CHT-04 chat scan; the Guardrail lives in the gate stack, so match by account.
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[self.format_arn(service="bedrock", resource="guardrail/*")],
            )
        )
        webhooks = function(
            "webhooks",
            environment={**environment, "AERA_WHATSAPP_MEDIA": whatsapp_media},
            secrets=("channels/whatsapp", "channels/carrier-webhook"),
        )
        inbound = function("ses-inbound", environment=environment, timeout=Duration.seconds(60))
        realtime = function("realtime")

        for fn in (api_fn, webhooks, inbound):
            raw.grant_read_write(fn)
            tables["signals"].grant_read_write_data(fn)
            tables["idempotency"].grant_read_write_data(fn)
        # Admin (FR-ADM-01..03) edits config and resets the working tables; audit is kept.
        for name in ("cases", "audit", "trace", "connections", "config", "ledger", "dialogue"):
            tables[name].grant_read_write_data(api_fn)
        tables["connections"].grant_read_write_data(realtime)

        # REST API ---------------------------------------------------------------------------
        origins = console_origins or ["http://localhost:5173"]
        rest = apigw.RestApi(
            self,
            "Api",
            rest_api_name=f"aera-{env_name}-api",
            binary_media_types=["application/pdf"],
            deploy_options=apigw.StageOptions(
                stage_name=env_name,
                tracing_enabled=True,
                throttling_rate_limit=50,
                throttling_burst_limit=100,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=origins,
                allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
                allow_methods=["GET", "POST", "PUT", "OPTIONS"],
            ),
        )
        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self,
            "ConsoleAuthorizer",
            cognito_user_pools=[cognito.UserPool.from_user_pool_arn(self, "Pool", pool_arn)],
        )
        console = apigw.LambdaIntegration(api_fn)

        def signed(resource: apigw.IResource, method: str) -> None:
            resource.add_method(
                method,
                console,
                authorizer=authorizer,
                authorization_type=apigw.AuthorizationType.COGNITO,
            )

        cases = rest.root.add_resource("cases")
        signed(cases, "GET")
        case = cases.add_resource("{id}")
        signed(case, "GET")
        signed(case.add_resource("trace"), "GET")
        signed(case.add_resource("dialogue"), "GET")
        signed(case.add_resource("decision-record"), "GET")
        signed(case.add_resource("runs"), "POST")
        signed(case.add_resource("rollback"), "POST")
        signed(case.add_resource("chat"), "POST")
        signed(case.add_resource("approval"), "POST")
        signed(case.add_resource("projection"), "GET")
        signed(case.add_resource("whatif"), "POST")
        admin = rest.root.add_resource("admin")
        signed(admin.add_resource("config").add_resource("{key}"), "PUT")
        signed(admin.add_resource("killswitch"), "POST")
        signed(admin.add_resource("reset"), "POST")
        signed(
            case.add_resource("fields").add_resource("{fieldId}").add_resource("confirm"), "POST"
        )
        signals = rest.root.add_resource("signals")
        signed(signals, "GET")
        signed(signals, "POST")
        signed(rest.root.add_resource("metrics"), "GET")
        signed(rest.root.add_resource("realtime").add_resource("ticket"), "POST")
        hooks = rest.root.add_resource("webhooks")
        channel = apigw.LambdaIntegration(webhooks)
        whatsapp = hooks.add_resource("whatsapp")
        whatsapp.add_method("GET", channel)
        whatsapp.add_method("POST", channel)
        hooks.add_resource("carrier").add_method("POST", channel)

        # WebSocket --------------------------------------------------------------------------
        socket_integration = integrations.WebSocketLambdaIntegration("Realtime", realtime)
        socket = apigwv2.WebSocketApi(
            self,
            "Realtime",
            api_name=f"aera-{env_name}-realtime",
            connect_route_options=apigwv2.WebSocketRouteOptions(integration=socket_integration),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(integration=socket_integration),
        )
        socket.add_route("subscribe", integration=socket_integration)
        stage = apigwv2.WebSocketStage(
            self, "RealtimeStage", web_socket_api=socket, stage_name=env_name, auto_deploy=True
        )
        socket.grant_manage_connections(realtime)
        realtime.add_environment("AERA_WS_MANAGEMENT_URL", stage.callback_url)
        events.Rule(
            self,
            "PushDomainEvents",
            rule_name=f"aera-{env_name}-realtime",
            event_bus=data.bus,
            event_pattern=events.EventPattern(
                source=events.Match.prefix("aera."), detail_type=REALTIME_EVENTS
            ),
            targets=[targets.LambdaFunction(realtime, retry_attempts=2)],
        )
        realtime.add_event_source(
            sources.DynamoEventSource(
                tables["trace"],
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=50,
                retry_attempts=2,
            )
        )

        # SES inbound ------------------------------------------------------------------------
        rule_set = ses.CfnReceiptRuleSet(
            self, "InboundRules", rule_set_name=f"aera-{env_name}-inbound"
        )
        ses.CfnReceiptRule(
            self,
            "StoreSupplierMail",
            rule_set_name=rule_set.ref,
            rule=ses.CfnReceiptRule.RuleProperty(
                name=f"aera-{env_name}-store",
                enabled=True,
                scan_enabled=True,
                tls_policy="Require",
                recipients=inbound_recipients or None,
                actions=[
                    ses.CfnReceiptRule.ActionProperty(
                        s3_action=ses.CfnReceiptRule.S3ActionProperty(
                            bucket_name=raw.bucket_name, object_key_prefix="ses/"
                        )
                    )
                ],
            ),
        )
        events.Rule(
            self,
            "OnSupplierMail",
            rule_name=f"aera-{env_name}-ses-inbound",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [raw.bucket_name]},
                    "object": {"key": events.Match.prefix("ses/")},
                },
            ),
            targets=[targets.LambdaFunction(inbound, retry_attempts=8)],
        )

        for key, value in (("API_URL", rest.url), ("WS_URL", stage.url)):
            ssm.StringParameter(
                self,
                f"Param{key}",
                parameter_name=f"/aera/{env_name}/{key}",
                string_value=value,
                description=f"Console endpoint {key}",
            )
        CfnOutput(self, "ApiUrl", value=rest.url)
        CfnOutput(self, "WebSocketUrl", value=stage.url)
        self.functions = {
            "api": api_fn,
            "webhooks": webhooks,
            "ses-inbound": inbound,
            "realtime": realtime,
        }
