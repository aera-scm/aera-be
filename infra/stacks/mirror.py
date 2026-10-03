"""SAP Mirror on ECS Fargate, the SRD 6.15 fallback to SAP BTP (IR-02, IR-03).

One small task runs the Mirror with its `aws` profile: in-memory SQLite and access tokens
of the Cognito user pool instead of HANA and XSUAA. An HTTP API with a VPC Link gives it an
HTTPS address without a domain or a load balancer; the task accepts nothing else. A custom
resource registers the URL and the OAuth client with AERA.
"""

from typing import Any

from aws_cdk import ArnFormat, CfnOutput, CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_servicediscovery as servicediscovery
from constructs import Construct

from infra.constructs.service_function import ROOT
from infra.environments import require_deployable_environment
from infra.stacks.data import DataStack

PORT = 4004
MIRROR_SECRET = "sap/mirror-oauth-client"  # pragma: allowlist secret (a name, not a value)
PARAMETERS = ("SAP_READ_BASE", "SAP_WRITE_BASE")
HEALTH = (
    "node -e \"fetch('http://127.0.0.1:4004/')"
    '.then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"'
)


class MirrorStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        data: DataStack,
        pool_id: str,
        pool_arn: str,
        issuer: str,
        token_url: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        require_deployable_environment(env_name)

        # OAuth client -------------------------------------------------------------------
        identifier = f"aera-{env_name}-mirror"
        access_scope, admin_scope = f"{identifier}/access", f"{identifier}/admin"
        scope_type = cognito.CfnUserPoolResourceServer.ResourceServerScopeTypeProperty
        resource_server = cognito.CfnUserPoolResourceServer(
            self,
            "MirrorScopes",
            user_pool_id=pool_id,
            identifier=identifier,
            name="AERA SAP Mirror",
            scopes=[
                scope_type(scope_name="access", scope_description="Read and write SAP data"),
                scope_type(scope_name="admin", scope_description="Reset the reference scenario"),
            ],
        )
        client = cognito.CfnUserPoolClient(
            self,
            "MirrorClient",
            user_pool_id=pool_id,
            client_name=identifier,
            generate_secret=True,
            allowed_o_auth_flows=["client_credentials"],
            allowed_o_auth_flows_user_pool_client=True,
            allowed_o_auth_scopes=[access_scope, admin_scope],
            prevent_user_existence_errors="ENABLED",
            enable_token_revocation=True,
        )
        client.add_dependency(resource_server)

        # Network: public subnets only, so no NAT gateway. The task needs a public address
        # for outbound calls and admits inbound traffic from the VPC Link alone.
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                )
            ],
        )
        public = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)
        link_group = ec2.SecurityGroup(
            self, "LinkGroup", vpc=vpc, description="API Gateway VPC Link to the SAP Mirror"
        )
        task_group = ec2.SecurityGroup(self, "TaskGroup", vpc=vpc, description="SAP Mirror task")
        task_group.add_ingress_rule(link_group, ec2.Port.tcp(PORT), "VPC Link only")

        # Task ---------------------------------------------------------------------------
        definition = ecs.FargateTaskDefinition(
            self,
            "Task",
            cpu=256,
            memory_limit_mib=512,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        log_group = logs.LogGroup(
            self,
            "Logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        container = definition.add_container(
            "mirror",
            image=ecs.ContainerImage.from_asset(
                str(ROOT / "sap-mirror"), platform=ecr_assets.Platform.LINUX_ARM64
            ),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="mirror", log_group=log_group),
            environment={
                "CDS_ENV": "aws",
                "MIRROR_OAUTH_ISSUER": issuer,
                "MIRROR_OAUTH_CLIENT_ID": client.ref,
                "MIRROR_OAUTH_SCOPE": access_scope,
                "MIRROR_OAUTH_ADMIN_SCOPE": admin_scope,
            },
            port_mappings=[ecs.PortMapping(container_port=PORT)],
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", HEALTH],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(30),
            ),
        )
        namespace = servicediscovery.PrivateDnsNamespace(
            self, "Namespace", name=f"{identifier}.internal", vpc=vpc
        )
        service = ecs.FargateService(
            self,
            "Service",
            cluster=ecs.Cluster(self, "Cluster", vpc=vpc, cluster_name=identifier),
            task_definition=definition,
            desired_count=1,
            assign_public_ip=True,
            vpc_subnets=public,
            security_groups=[task_group],
            # The data lives in the task's memory: never run two tasks at once.
            min_healthy_percent=0,
            max_healthy_percent=100,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            cloud_map_options=ecs.CloudMapOptions(
                cloud_map_namespace=namespace,
                name="mirror",
                dns_record_type=servicediscovery.DnsRecordType.SRV,
                dns_ttl=Duration.seconds(10),
                container=container,
                container_port=PORT,
            ),
        )
        if service.cloud_map_service is None:
            raise ValueError("the Mirror service has no Cloud Map registration")

        # HTTPS address ------------------------------------------------------------------
        link = apigwv2.VpcLink(self, "Link", vpc=vpc, subnets=public, security_groups=[link_group])
        api = apigwv2.HttpApi(
            self,
            "Api",
            api_name=identifier,
            create_default_stage=False,
            default_integration=integrations.HttpServiceDiscoveryIntegration(
                "Mirror", service.cloud_map_service, vpc_link=link
            ),
        )
        apigwv2.HttpStage(
            self,
            "Stage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigwv2.ThrottleSettings(rate_limit=50, burst_limit=100),
        )

        # Registration -------------------------------------------------------------------
        registrar = lambda_.Function(
            self,
            "Registrar",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_asset(str(ROOT / "infra" / "assets" / "mirror_registrar")),
            timeout=Duration.seconds(60),
            description="Registers the SAP Mirror URL and OAuth client with AERA",
        )
        secret_id = f"/aera/{env_name}/{MIRROR_SECRET}"
        parameters = [f"/aera/{env_name}/{name}" for name in PARAMETERS]
        registrar.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:DescribeUserPoolClient"], resources=[pool_arn]
            )
        )
        registrar.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:PutSecretValue"],
                resources=[
                    self.format_arn(
                        service="secretsmanager",
                        resource="secret",
                        resource_name=f"{secret_id}-*",
                        arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    )
                ],
            )
        )
        registrar.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:PutParameter"],
                resources=[
                    self.format_arn(
                        service="ssm", resource="parameter", resource_name=name.lstrip("/")
                    )
                    for name in parameters
                ],
            )
        )
        data.key.grant_encrypt_decrypt(registrar)
        registration = CustomResource(
            self,
            "Registration",
            service_token=registrar.function_arn,
            properties={
                "UserPoolId": pool_id,
                "ClientId": client.ref,
                "SecretId": secret_id,
                "TokenUrl": token_url,
                "MirrorUrl": api.api_endpoint,
                "Parameters": parameters,
            },
        )
        registration.node.add_dependency(registrar)
        registration.node.add_dependency(service)

        CfnOutput(self, "MirrorUrl", value=api.api_endpoint)
        CfnOutput(self, "MirrorClientId", value=client.ref)
